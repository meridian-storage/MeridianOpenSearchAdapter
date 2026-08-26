# SPDX-License-Identifier: Apache-2.0
"""Bounded logical-search compilation, execution metadata, and normalization."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from meridian_storage.errors import ErrorCode, ValidationError
from meridian_storage.query import (
    CompiledQuery,
    CursorExpectations,
    CursorSigner,
    NormalizedQueryResult,
    PlannedQuery,
    QueryOperation,
    QueryTranslator,
    TranslationContext,
)
from meridian_storage.semantics import JsonValue, ResourceReference, canonical_json_bytes

from .._canonical import json_value, require_fingerprint, safe_token
from ..client import ClientProtocol
from ..configuration import AdapterLimits
from ..descriptor import ADAPTER_ID, query_capabilities
from ..errors import OpenSearchErrorCode, translate_engine_error
from ..mapping import (
    HIDDEN_DELETED,
    HIDDEN_DOCUMENT_ID,
    HIDDEN_RECORD_ID,
    HIDDEN_RESOURCE,
    HIDDEN_SCOPE,
    HIDDEN_SOURCE_SEQUENCE,
    HIDDEN_SOURCE_VERSION,
    FieldLayout,
    ResourceLayout,
)

_PIT_CURSOR_KEY = "org.meridian.opensearch/pit"


@dataclass(frozen=True, slots=True)
class CompiledSearch:
    read_target: str
    body: Mapping[str, JsonValue]
    plan_fingerprint: str
    registry_fingerprint: str
    scope_fingerprint: str
    schema_fingerprints: Mapping[str, str]
    page_size: int
    pit_requested: bool = False
    pit_id: str | None = None

    def __post_init__(self) -> None:
        safe_token(self.read_target, "read target", maximum=255)
        for value, name in (
            (self.plan_fingerprint, "plan fingerprint"),
            (self.registry_fingerprint, "registry fingerprint"),
            (self.scope_fingerprint, "scope fingerprint"),
        ):
            require_fingerprint(value, name)
        if isinstance(self.page_size, bool) or not 1 <= self.page_size <= 500:
            raise ValueError("compiled search page size must be between 1 and 500")
        canonical_json_bytes(cast(JsonValue, self.body))
        if self.pit_id is not None:
            safe_token(self.pit_id, "point-in-time id", maximum=4_096)

    def to_command(self) -> dict[str, JsonValue]:
        return {
            "formatVersion": "meridian.opensearch.compiled-search.v1",
            "readTarget": self.read_target,
            "body": dict(self.body),
            "planFingerprint": self.plan_fingerprint,
            "registryFingerprint": self.registry_fingerprint,
            "scopeFingerprint": self.scope_fingerprint,
            "schemaFingerprints": dict(self.schema_fingerprints),
            "pageSize": self.page_size,
            "pitRequested": self.pit_requested,
            "pitId": self.pit_id,
        }

    @classmethod
    def from_command(cls, value: Mapping[str, object]) -> CompiledSearch:
        required = {
            "formatVersion",
            "readTarget",
            "body",
            "planFingerprint",
            "registryFingerprint",
            "scopeFingerprint",
            "schemaFingerprints",
            "pageSize",
            "pitRequested",
            "pitId",
        }
        if set(value) != required or value.get("formatVersion") != (
            "meridian.opensearch.compiled-search.v1"
        ):
            raise ValueError("compiled search command contains unknown or missing fields")
        body = _object(value["body"], "compiled search body")
        schemas = _object(value["schemaFingerprints"], "compiled Schema fingerprints")
        if any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in schemas.items()
        ):
            raise TypeError("compiled Schema fingerprints must map strings to strings")
        return cls(
            read_target=cast(str, value["readTarget"]),
            body=cast(Mapping[str, JsonValue], body),
            plan_fingerprint=cast(str, value["planFingerprint"]),
            registry_fingerprint=cast(str, value["registryFingerprint"]),
            scope_fingerprint=cast(str, value["scopeFingerprint"]),
            schema_fingerprints=cast(Mapping[str, str], schemas),
            page_size=cast(int, value["pageSize"]),
            pit_requested=cast(bool, value["pitRequested"]),
            pit_id=cast(str | None, value["pitId"]),
        )


@dataclass(frozen=True, slots=True)
class SearchResponse:
    data: Mapping[str, JsonValue]
    cursor: str | None
    pit_id: str | None
    has_more: bool


class SearchCompiler(QueryTranslator):
    def __init__(
        self,
        layout: ResourceLayout,
        read_target: str,
        cursor_signer: CursorSigner,
        *,
        limits: AdapterLimits | None = None,
        pit_enabled: bool = False,
    ) -> None:
        self.layout = layout
        self.read_target = safe_token(read_target, "read target", maximum=255)
        self.cursor_signer = cursor_signer
        self.limits = limits or AdapterLimits()
        self.pit_enabled = pit_enabled
        self._capabilities = query_capabilities(limits=self.limits, pit_enabled=pit_enabled)

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        return self._capabilities

    def compile(self, plan: object, context: TranslationContext) -> CompiledQuery:
        if not isinstance(plan, PlannedQuery):
            raise TypeError("OpenSearch QueryTranslator requires a released PlannedQuery")
        operation = plan.operation
        compiled = self.compile_query_operation(
            operation,
            plan_fingerprint=context.plan_fingerprint,
            registry_fingerprint=context.registry_fingerprint,
            scope_fingerprint=context.scope_fingerprint,
            schema_fingerprints=context.schema_fingerprints,
        )
        return CompiledQuery(
            adapter_id=ADAPTER_ID,
            plan_fingerprint=context.plan_fingerprint,
            command=compiled.to_command(),
            expected_result_shape="search",
        )

    def normalize_result(
        self, compiled: CompiledQuery, raw_result: object
    ) -> NormalizedQueryResult:
        if not isinstance(compiled.command, Mapping):
            raise TypeError("compiled search command must be an object")
        search = CompiledSearch.from_command(compiled.command)
        result = self.normalize(search, raw_result)
        return NormalizedQueryResult(
            data=cast(JsonValue, result.data),
            cursor=result.cursor,
            provenance={
                "adapter": ADAPTER_ID,
                "consistency": "eventual",
                "pagination": "point-in-time" if result.pit_id else "live-keyset",
            },
        )

    def compile_mapping(
        self,
        arguments: Mapping[str, object],
        *,
        plan_fingerprint: str,
        registry_fingerprint: str,
        scope_fingerprint: str,
    ) -> CompiledSearch:
        required = {"resource", "query", "where", "facets", "highlights", "limit"}
        optional = {"cursor"}
        if required - set(arguments) or set(arguments) - required - optional:
            raise ValidationError(
                ErrorCode.OPERATION_INVALID,
                "structured.search input contains unknown or missing fields",
            )
        if arguments["resource"] != self.layout.resource_ref:
            raise ValidationError(
                ErrorCode.OPERATION_SCOPE,
                "structured.search input targets another bound Resource",
            )
        limit = _limit(arguments["limit"], self.limits.max_page_size)
        query_clause = self._mapping_query(arguments["query"])
        where = _object(arguments["where"], "search where")
        filters = self._mapping_filters(where)
        facets = _field_array(arguments["facets"], "facets", self.limits.max_facets)
        highlights = _field_array(arguments["highlights"], "highlights", self.limits.max_highlights)
        cursor = arguments.get("cursor")
        if cursor is not None and not isinstance(cursor, str):
            raise ValidationError(ErrorCode.OPERATION_INVALID, "search cursor must be a string")
        body = self._body(
            query_clause,
            filters,
            facets=facets,
            highlights=highlights,
            page_size=limit,
            sorts=(),
        )
        return self._cursor(
            body,
            cursor=cursor,
            plan_fingerprint=plan_fingerprint,
            registry_fingerprint=registry_fingerprint,
            scope_fingerprint=scope_fingerprint,
            schema_fingerprints={self.layout.resource_ref: self.layout.schema_fingerprint},
            page_size=limit,
            pit_requested=False,
        )

    def compile_query_operation(
        self,
        operation: QueryOperation,
        *,
        plan_fingerprint: str,
        registry_fingerprint: str,
        scope_fingerprint: str,
        schema_fingerprints: Mapping[str, str],
    ) -> CompiledSearch:
        if (
            operation.catalog != "structured"
            or operation.operation != "search"
            or operation.consistency != "eventual"
            or len(operation.targets) != 1
            or operation.joins
            or operation.grouping
            or operation.aggregates
            or operation.traversal is not None
            or operation.result.projection
        ):
            raise ValidationError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "OpenSearch supports one eventually consistent search target without joins",
            )
        target = operation.targets[0].resource
        if str(target) != self.layout.resource_ref:
            raise ValidationError(ErrorCode.OPERATION_SCOPE, "query targets another resource")
        if operation.page.point_in_time and not self.pit_enabled:
            raise ValidationError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "point-in-time search is not enabled by this deployment profile",
            )
        limit = _limit(operation.page.size, self.limits.max_page_size)
        clauses, facets, highlights = self._expression(operation.filter)
        query_clause: JsonValue = {"match_all": {}}
        filters: list[JsonValue] = []
        if clauses is not None:
            if _is_scoring(clauses):
                query_clause = clauses
            else:
                filters.append(clauses)
        sorts = tuple(item.to_dict() for item in operation.order)
        body = self._body(
            query_clause,
            filters,
            facets=facets,
            highlights=highlights,
            page_size=limit,
            sorts=sorts,
        )
        return self._cursor(
            body,
            cursor=operation.page.cursor,
            plan_fingerprint=plan_fingerprint,
            registry_fingerprint=registry_fingerprint,
            scope_fingerprint=scope_fingerprint,
            schema_fingerprints=schema_fingerprints,
            page_size=limit,
            pit_requested=operation.page.point_in_time,
        )

    def _mapping_query(self, value: object) -> JsonValue:
        if isinstance(value, str):
            text = value
            fields = self.layout.source_fields
            fuzziness = 0
            operator = "or"
            ranking = self.layout.ranking
        elif isinstance(value, Mapping):
            allowed = {"text", "fields", "fuzziness", "operator", "ranking"}
            if set(value) - allowed or "text" not in value:
                raise ValidationError(
                    ErrorCode.OPERATION_INVALID,
                    "logical search query accepts only text, fields, fuzziness, "
                    "operator, and ranking",
                )
            text = value["text"]
            fields = (
                self.layout.source_fields
                if "fields" not in value
                else _field_array(value["fields"], "query fields", len(self.layout.fields))
            )
            fuzziness = value.get("fuzziness", 0)
            operator = value.get("operator", "or")
            ranking = value.get("ranking", self.layout.ranking)
        else:
            raise ValidationError(
                ErrorCode.OPERATION_INVALID, "logical search query must be text or an object"
            )
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text.encode()) > self.limits.max_query_bytes
        ):
            raise ValidationError(
                ErrorCode.OPERATION_RESULT_LIMIT,
                f"search text must be non-empty and at most {self.limits.max_query_bytes} bytes",
            )
        if (
            isinstance(fuzziness, bool)
            or not isinstance(fuzziness, int)
            or fuzziness not in {0, 1, 2}
        ):
            raise ValidationError(ErrorCode.OPERATION_INVALID, "fuzziness must be 0, 1, or 2")
        if operator not in {"and", "or"}:
            raise ValidationError(ErrorCode.OPERATION_INVALID, "query operator must be and or or")
        if ranking != self.layout.ranking:
            raise ValidationError(
                ErrorCode.CAPABILITY_UNSUPPORTED, "ranking profile is absent from this projection"
            )
        physical = [self._layout_field(name, mode="full-text").physical_name for name in fields]
        if not physical:
            raise ValidationError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "projection has no declared full-text source fields",
            )
        return {
            "multi_match": {
                "query": text,
                "fields": physical,
                "operator": operator,
                **({"fuzziness": fuzziness} if fuzziness else {}),
            }
        }

    def _mapping_filters(self, where: Mapping[str, object]) -> list[JsonValue]:
        clauses: list[JsonValue] = []
        for field_name, condition in sorted(where.items()):
            if len(clauses) >= self.limits.max_filter_clauses:
                raise ValidationError(
                    ErrorCode.OPERATION_RESULT_LIMIT, "search filter clause limit exceeded"
                )
            if isinstance(condition, Mapping):
                if not condition:
                    raise ValidationError(ErrorCode.OPERATION_INVALID, "filter operator is empty")
                for operator, value in sorted(condition.items()):
                    clauses.append(self._mapping_filter(field_name, operator, value))
            else:
                clauses.append(self._mapping_filter(field_name, "$eq", condition))
        return clauses

    def _mapping_filter(self, name: str, operator: str, value: object) -> JsonValue:
        if operator in {"$eq", "$ne"}:
            field = _exact_field(self._layout_field(name, mode="exact"))
            clause: JsonValue = {"term": {field: json_value(value)}}
            return clause if operator == "$eq" else {"bool": {"must_not": [clause]}}
        if operator in {"$lt", "$lte", "$gt", "$gte"}:
            field = self._layout_field(name, mode="range").physical_name
            return {"range": {field: {operator[1:]: json_value(value)}}}
        if operator in {"$in", "$notIn"}:
            values = _values(value, "membership filter")
            if len(values) > 10_000:
                raise ValidationError(
                    ErrorCode.OPERATION_RESULT_LIMIT, "membership filter is too large"
                )
            field = _exact_field(self._layout_field(name, mode="exact"))
            clause = {"terms": {field: [json_value(item) for item in values]}}
            return clause if operator == "$in" else {"bool": {"must_not": [clause]}}
        if operator == "$prefix":
            if not isinstance(value, str):
                raise ValidationError(ErrorCode.OPERATION_INVALID, "prefix filter requires text")
            field = _exact_field(self._layout_field(name, mode="prefix"))
            return {"prefix": {field: {"value": value}}}
        if operator == "$isNull":
            if not isinstance(value, bool):
                raise ValidationError(ErrorCode.OPERATION_INVALID, "isNull requires a boolean")
            exists: JsonValue = {"exists": {"field": self._layout_field(name).physical_name}}
            return {"bool": {"must_not": [exists]}} if value else exists
        if operator == "$timestampRange":
            range_value = _object(value, "timestamp range")
            if set(range_value) - {"start", "end", "includeStart", "includeEnd"}:
                raise ValidationError(ErrorCode.OPERATION_INVALID, "timestamp range is not closed")
            bounds: dict[str, JsonValue] = {}
            if range_value.get("start") is not None:
                bounds["gte" if range_value.get("includeStart", True) else "gt"] = json_value(
                    range_value["start"]
                )
            if range_value.get("end") is not None:
                bounds["lte" if range_value.get("includeEnd", False) else "lt"] = json_value(
                    range_value["end"]
                )
            if not bounds:
                raise ValidationError(ErrorCode.OPERATION_INVALID, "timestamp range is empty")
            field = self._layout_field(name, mode="range").physical_name
            return {"range": {field: bounds}}
        raise ValidationError(
            ErrorCode.OPERATION_INVALID,
            "search filter operator is unsupported; DSL, regex, wildcard, and "
            "scripts are prohibited",
        )

    def _expression(
        self, expression: object
    ) -> tuple[JsonValue | None, tuple[str, ...], tuple[str, ...]]:
        if expression is None:
            return None, (), ()
        if not hasattr(expression, "to_dict"):
            raise TypeError("query filter must be a released ValueExpression")
        serialized = cast(Any, expression).to_dict()
        facets: set[str] = set()
        highlights: set[str] = set()

        def compile_node(node: Mapping[str, object]) -> JsonValue:
            kind = node.get("kind")
            if kind == "fullText":
                fields = _values(node.get("fields"), "full-text fields")
                names = tuple(
                    cast(str, _object(item, "full-text field")["name"]) for item in fields
                )
                query_value: dict[str, object] = {
                    "text": node.get("query"),
                    "fields": names,
                    "fuzziness": node.get("fuzzyTolerance", 0),
                    "operator": "or",
                    "ranking": node.get("ranking", self.layout.ranking),
                }
                facets.update(
                    _field_array(node.get("facets", ()), "facets", self.limits.max_facets)
                )
                highlights.update(
                    _field_array(
                        node.get("highlights", ()), "highlights", self.limits.max_highlights
                    )
                )
                return self._mapping_query(query_value)
            if kind in {"and", "or"}:
                operands = _values(node.get("operands"), f"{kind} operands")
                compiled = [compile_node(_object(item, f"{kind} operand")) for item in operands]
                return {
                    "bool": {
                        "filter" if kind == "and" else "should": compiled,
                        **({"minimum_should_match": 1} if kind == "or" else {}),
                    }
                }
            if kind == "not":
                negated_clause = compile_node(_object(node.get("operand"), "not operand"))
                return {"bool": {"must_not": [negated_clause]}}
            if kind in {"eq", "ne", "lt", "lte", "gt", "gte", "prefix"}:
                field, literal_value = _field_literal(node)
                return self._mapping_filter(field, f"${kind}", literal_value)
            if kind in {"in", "notIn"}:
                membership_operand = _object(node.get("operand"), "membership operand")
                if membership_operand.get("kind") != "field":
                    raise ValidationError(
                        ErrorCode.OPERATION_INVALID, "membership requires a field"
                    )
                values = [
                    _literal_value(_object(item, "membership value"))
                    for item in _values(node.get("values"), "membership values")
                ]
                return self._mapping_filter(
                    cast(str, membership_operand["name"]), f"${kind}", values
                )
            if kind == "isNull":
                null_operand = _object(node.get("operand"), "null-test operand")
                if null_operand.get("kind") != "field":
                    raise ValidationError(ErrorCode.OPERATION_INVALID, "null test requires a field")
                return self._mapping_filter(
                    cast(str, null_operand["name"]), "$isNull", node.get("expected")
                )
            if kind == "timestampRange":
                range_operand = _object(node.get("operand"), "timestamp operand")
                if range_operand.get("kind") != "field":
                    raise ValidationError(
                        ErrorCode.OPERATION_INVALID, "timestamp range requires a field"
                    )
                range_filter = {
                    "start": _optional_literal(node.get("start")),
                    "end": _optional_literal(node.get("end")),
                    "includeStart": node.get("includeStart", True),
                    "includeEnd": node.get("includeEnd", False),
                }
                return self._mapping_filter(
                    cast(str, range_operand["name"]), "$timestampRange", range_filter
                )
            raise ValidationError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                f"query expression {kind!r} is not natively supported by OpenSearch",
            )

        return (
            compile_node(_object(serialized, "query expression")),
            tuple(sorted(facets)),
            tuple(sorted(highlights)),
        )

    def _body(
        self,
        query_clause: JsonValue,
        filters: Sequence[JsonValue],
        *,
        facets: Sequence[str],
        highlights: Sequence[str],
        page_size: int,
        sorts: Sequence[Mapping[str, object]],
    ) -> dict[str, JsonValue]:
        mandatory: list[JsonValue] = [
            {"term": {HIDDEN_SCOPE: "__SCOPE_INJECTED_BELOW__"}},
            {"term": {HIDDEN_RESOURCE: self.layout.resource_ref}},
            {"term": {HIDDEN_DELETED: False}},
            *filters,
        ]
        query: JsonValue = {
            "bool": {
                "must": [query_clause] if _is_scoring(query_clause) else [],
                "filter": mandatory + ([] if _is_scoring(query_clause) else [query_clause]),
            }
        }
        body: dict[str, JsonValue] = {
            "query": query,
            "size": page_size + 1,
            "track_total_hits": True,
            "_source": [
                HIDDEN_RECORD_ID,
                HIDDEN_RESOURCE,
                HIDDEN_SOURCE_VERSION,
                HIDDEN_SOURCE_SEQUENCE,
            ],
            "sort": self._sort(sorts),
        }
        if facets:
            body["aggs"] = {
                f"facet_{index}": {
                    "terms": {
                        "field": _exact_field(self._layout_field(name, mode="facet")),
                        "size": self.limits.facet_bucket_limit,
                    },
                    "meta": {"logicalField": name},
                }
                for index, name in enumerate(facets)
            }
        if highlights:
            body["highlight"] = {
                "fields": {
                    self._layout_field(name, mode="highlight").physical_name: {
                        "number_of_fragments": self.limits.highlight_fragment_limit,
                        "fragment_size": 256,
                    }
                    for name in highlights
                },
                "require_field_match": True,
                "pre_tags": ["<em>"],
                "post_tags": ["</em>"],
            }
        return body

    def _sort(self, sorts: Sequence[Mapping[str, object]]) -> list[JsonValue]:
        result: list[JsonValue] = []
        for item in sorts:
            expression = _object(item.get("expression"), "sort expression")
            if expression.get("kind") != "field":
                raise ValidationError(ErrorCode.CAPABILITY_UNSUPPORTED, "sort requires a field")
            direction = item.get("direction")
            nulls = item.get("nulls")
            if direction not in {"asc", "desc"} or nulls not in {"first", "last"}:
                raise ValidationError(ErrorCode.OPERATION_INVALID, "sort is malformed")
            layout = self._layout_field(cast(str, expression["name"]), mode="exact")
            result.append(
                {
                    _exact_field(layout): {
                        "order": direction,
                        "missing": "_first" if nulls == "first" else "_last",
                    }
                }
            )
        if not result:
            result.append({"_score": {"order": "desc"}})
        result.append({HIDDEN_DOCUMENT_ID: {"order": "asc"}})
        return result

    def _layout_field(self, name: str, *, mode: str | None = None) -> FieldLayout:
        try:
            return self.layout.field(name, mode=mode)
        except ValueError as exc:
            raise ValidationError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "search references a field or field mode absent from the bound projection",
            ) from exc

    def _cursor(
        self,
        body: dict[str, JsonValue],
        *,
        cursor: str | None,
        plan_fingerprint: str,
        registry_fingerprint: str,
        scope_fingerprint: str,
        schema_fingerprints: Mapping[str, str],
        page_size: int,
        pit_requested: bool,
    ) -> CompiledSearch:
        body = _replace_scope(body, scope_fingerprint)
        pit_id: str | None = None
        if cursor is not None:
            payload = self.cursor_signer.verify(
                cursor,
                expected=CursorExpectations(
                    plan_fingerprint=plan_fingerprint,
                    schema_fingerprints=schema_fingerprints,
                    registry_fingerprint=registry_fingerprint,
                    scope_fingerprint=scope_fingerprint,
                    page_size=page_size,
                ),
            )
            sort_tuple = list(payload.sort_tuple)
            if (
                sort_tuple
                and isinstance(sort_tuple[-1], Mapping)
                and set(sort_tuple[-1]) == {_PIT_CURSOR_KEY}
            ):
                sentinel = cast(Mapping[str, JsonValue], sort_tuple.pop())
                pit_value = sentinel[_PIT_CURSOR_KEY]
                if not isinstance(pit_value, str):
                    raise ValidationError(ErrorCode.OPERATION_INVALID, "cursor PIT id is invalid")
                pit_id = pit_value
            if pit_requested != (pit_id is not None):
                raise ValidationError(
                    ErrorCode.OPERATION_INVALID, "cursor pagination mode differs from the query"
                )
            body["search_after"] = cast(JsonValue, sort_tuple)
        return CompiledSearch(
            read_target=self.read_target,
            body=body,
            plan_fingerprint=plan_fingerprint,
            registry_fingerprint=registry_fingerprint,
            scope_fingerprint=scope_fingerprint,
            schema_fingerprints=schema_fingerprints,
            page_size=page_size,
            pit_requested=pit_requested,
            pit_id=pit_id,
        )

    def normalize(self, compiled: CompiledSearch, raw_result: object) -> SearchResponse:
        response = _object(raw_result, "OpenSearch search response")
        if response.get("timed_out") is True:
            raise ValidationError(
                OpenSearchErrorCode.TIMEOUT, "OpenSearch returned a timed-out partial search"
            )
        shards = _object(response.get("_shards"), "search shard summary")
        failed = shards.get("failed")
        if not isinstance(failed, int) or isinstance(failed, bool) or failed != 0:
            raise ValidationError(
                OpenSearchErrorCode.PARTIAL_SHARD_FAILURE,
                "OpenSearch returned partial shard results",
            )
        hits_envelope = _object(response.get("hits"), "search hits")
        raw_hits = _values(hits_envelope.get("hits"), "search hits")
        has_more = len(raw_hits) > compiled.page_size
        selected_hits = raw_hits[: compiled.page_size]
        items = [self._normalize_hit(item) for item in selected_hits]
        total = _total(hits_envelope.get("total"))
        facets = self._normalize_facets(response.get("aggregations", {}))
        pit_id = response.get("pit_id", compiled.pit_id)
        if pit_id is not None and not isinstance(pit_id, str):
            raise ValidationError(
                ErrorCode.ADAPTER_FAILURE, "OpenSearch returned an invalid PIT id"
            )
        cursor: str | None = None
        if has_more:
            last = _object(selected_hits[-1], "last search hit")
            sort = _values(last.get("sort"), "search hit sort")
            sort_tuple: list[JsonValue] = [json_value(item) for item in sort]
            if compiled.pit_requested:
                if pit_id is None:
                    raise ValidationError(
                        ErrorCode.ADAPTER_FAILURE, "PIT search omitted its PIT id"
                    )
                sort_tuple.append({_PIT_CURSOR_KEY: pit_id})
            cursor = self.cursor_signer.issue(
                plan_fingerprint=compiled.plan_fingerprint,
                schema_fingerprints=compiled.schema_fingerprints,
                registry_fingerprint=compiled.registry_fingerprint,
                scope_fingerprint=compiled.scope_fingerprint,
                sort_tuple=sort_tuple,
                page_size=compiled.page_size,
            )
        data: dict[str, JsonValue] = {
            "items": cast(JsonValue, items),
            "facets": facets,
            "total": total,
            "consistency": "eventual",
        }
        return SearchResponse(data, cursor, pit_id, has_more)

    def _normalize_hit(self, value: object) -> dict[str, JsonValue]:
        hit = _object(value, "search hit")
        source = _object(hit.get("_source"), "search hit source")
        if source.get(HIDDEN_RESOURCE) != self.layout.resource_ref:
            raise ValidationError(ErrorCode.ADAPTER_FAILURE, "search hit crossed Resource scope")
        raw_record = source.get(HIDDEN_RECORD_ID)
        if not isinstance(raw_record, str):
            raise ValidationError(ErrorCode.ADAPTER_FAILURE, "search hit omitted logical identity")
        try:
            record_id = cast(JsonValue, json.loads(raw_record))
        except json.JSONDecodeError as exc:
            raise ValidationError(
                ErrorCode.ADAPTER_FAILURE, "search hit logical identity is corrupt"
            ) from exc
        collection = ResourceReference.parse(self.layout.resource_ref)
        score = hit.get("_score")
        if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
            raise ValidationError(ErrorCode.ADAPTER_FAILURE, "search hit score is invalid")
        source_version = source.get(HIDDEN_SOURCE_VERSION)
        source_sequence = source.get(HIDDEN_SOURCE_SEQUENCE)
        if (
            not isinstance(source_version, str)
            or isinstance(source_sequence, bool)
            or not isinstance(source_sequence, int)
        ):
            raise ValidationError(ErrorCode.ADAPTER_FAILURE, "search hit source version is invalid")
        result: dict[str, JsonValue] = {
            "recordRef": {
                "collectionRef": collection.to_dict(),
                "recordId": record_id,
            },
            "sourceVersion": source_version,
            "sourceSequence": source_sequence,
            "score": None if score is None else float(score),
            "sort": cast(
                JsonValue, [json_value(item) for item in _values(hit.get("sort"), "sort")]
            ),
            "highlights": self._normalize_highlights(hit.get("highlight", {})),
        }
        return result

    def _normalize_highlights(self, value: object) -> dict[str, JsonValue]:
        highlights = _object(value, "search highlights")
        reverse = {item.physical_name: name for name, item in self.layout.fields.items()}
        result: dict[str, JsonValue] = {}
        for physical, raw_fragments in sorted(highlights.items()):
            logical = reverse.get(physical)
            if logical is None:
                raise ValidationError(
                    ErrorCode.ADAPTER_FAILURE, "search returned an unknown highlight"
                )
            fragments = _values(raw_fragments, "highlight fragments")
            if len(fragments) > self.limits.highlight_fragment_limit or any(
                not isinstance(item, str) or len(item.encode()) > 4_096 for item in fragments
            ):
                raise ValidationError(
                    ErrorCode.OPERATION_RESULT_LIMIT, "highlight result exceeds bounds"
                )
            result[logical] = cast(JsonValue, list(fragments))
        return result

    def _normalize_facets(self, value: object) -> dict[str, JsonValue]:
        aggregations = _object(value, "search aggregations")
        result: dict[str, JsonValue] = {}
        for aggregation in aggregations.values():
            item = _object(aggregation, "facet aggregation")
            metadata = _object(item.get("meta"), "facet metadata")
            logical = metadata.get("logicalField")
            if not isinstance(logical, str):
                raise ValidationError(ErrorCode.ADAPTER_FAILURE, "facet metadata is absent")
            buckets = _values(item.get("buckets"), "facet buckets")
            if len(buckets) > self.limits.facet_bucket_limit:
                raise ValidationError(
                    ErrorCode.OPERATION_RESULT_LIMIT, "facet result exceeds bounds"
                )
            normalized: list[JsonValue] = []
            for raw_bucket in buckets:
                bucket = _object(raw_bucket, "facet bucket")
                count = bucket.get("doc_count")
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValidationError(ErrorCode.ADAPTER_FAILURE, "facet count is invalid")
                normalized.append({"value": json_value(bucket.get("key")), "count": count})
            result[logical] = normalized
        return result


def execute_search(
    client: ClientProtocol,
    compiled: CompiledSearch,
    *,
    pit_keep_alive: str = "2m",
) -> tuple[Mapping[str, object], str | None]:
    body = dict(compiled.body)
    pit_id = compiled.pit_id
    try:
        if compiled.pit_requested and pit_id is None:
            opened = client.transport.perform_request(
                "POST",
                f"/{compiled.read_target}/_search/point_in_time",
                params={"keep_alive": pit_keep_alive},
            )
            opened_mapping = _object(opened, "open point-in-time response")
            pit_id = cast(str | None, opened_mapping.get("pit_id"))
            if not isinstance(pit_id, str):
                raise ValidationError(ErrorCode.ADAPTER_FAILURE, "OpenSearch omitted its PIT id")
        if compiled.pit_requested:
            assert pit_id is not None
            body["pit"] = {"id": pit_id, "keep_alive": pit_keep_alive}
            raw = client.search(body=body)
        else:
            raw = client.search(index=compiled.read_target, body=body)
    except ValidationError:
        raise
    except Exception as exc:
        translate_engine_error(exc, operation_contract="meridian.structured.search")
    return raw, pit_id


def close_point_in_time(client: ClientProtocol, pit_id: str) -> None:
    try:
        client.transport.perform_request(
            "DELETE", "/_search/point_in_time", body={"pit_id": pit_id}
        )
    except Exception as exc:
        translate_engine_error(exc, operation_contract="meridian.structured.search")


def _replace_scope(value: JsonValue, scope: str) -> dict[str, JsonValue]:
    encoded = canonical_json_bytes(value).replace(
        b'"__SCOPE_INJECTED_BELOW__"', json.dumps(scope).encode()
    )
    result = json.loads(encoded)
    if not isinstance(result, dict):
        raise AssertionError("compiled search body must remain an object")
    return cast(dict[str, JsonValue], result)


def _is_scoring(value: JsonValue) -> bool:
    if not isinstance(value, Mapping):
        return False
    if "multi_match" in value:
        return True
    boolean = value.get("bool")
    return isinstance(boolean, Mapping) and any(
        _is_scoring(cast(JsonValue, item))
        for key in ("must", "should")
        for item in cast(Sequence[object], boolean.get(key, ()))
    )


def _exact_field(field: FieldLayout) -> str:
    return f"{field.physical_name}.exact" if "full-text" in field.modes else field.physical_name


def _field_literal(node: Mapping[str, object]) -> tuple[str, object]:
    left = _object(node.get("left"), "binary left operand")
    right = _object(node.get("right"), "binary right operand")
    if left.get("kind") == "field" and right.get("kind") == "literal":
        return cast(str, left["name"]), _literal_value(right)
    if right.get("kind") == "field" and left.get("kind") == "literal":
        return cast(str, right["name"]), _literal_value(left)
    raise ValidationError(ErrorCode.CAPABILITY_UNSUPPORTED, "comparison requires field and literal")


def _literal_value(value: Mapping[str, object]) -> object:
    if value.get("kind") != "literal" or "value" not in value:
        raise ValidationError(ErrorCode.CAPABILITY_UNSUPPORTED, "literal value is required")
    return value["value"]


def _optional_literal(value: object) -> object:
    if value is None:
        return None
    return _literal_value(_object(value, "range literal"))


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValidationError(ErrorCode.OPERATION_INVALID, f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _values(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(ErrorCode.OPERATION_INVALID, f"{name} must be an array")
    return cast(Sequence[object], value)


def _field_array(value: object, name: str, maximum: int) -> tuple[str, ...]:
    values = _values(value, name)
    if len(values) > maximum or any(not isinstance(item, str) or not item for item in values):
        raise ValidationError(
            ErrorCode.OPERATION_RESULT_LIMIT, f"{name} exceeds its field-name bound"
        )
    result = tuple(cast(str, item) for item in values)
    if len(result) != len(set(result)):
        raise ValidationError(ErrorCode.OPERATION_INVALID, f"{name} entries must be unique")
    return result


def _limit(value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValidationError(
            ErrorCode.OPERATION_RESULT_LIMIT, f"search limit must be between 1 and {maximum}"
        )
    return value


def _total(value: object) -> JsonValue:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return {"value": value, "relation": "eq"}
    item = _object(value, "total hits")
    count = item.get("value")
    relation = item.get("relation")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or relation not in {"eq", "gte"}
    ):
        raise ValidationError(ErrorCode.ADAPTER_FAILURE, "total-hit relation is invalid")
    return {"value": count, "relation": relation}


__all__ = [
    "CompiledSearch",
    "SearchCompiler",
    "SearchResponse",
    "close_point_in_time",
    "execute_search",
]
