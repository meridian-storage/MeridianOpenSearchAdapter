# SPDX-License-Identifier: Apache-2.0
"""Query V1 expression, cursor, and fail-closed edge coverage."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import FakeClient
from meridian_storage.errors import InternalError, ValidationError
from meridian_storage.query import (
    BinaryExpression,
    BooleanExpression,
    CompiledQuery,
    CursorSigner,
    Field,
    FullTextMatch,
    ImplementationMode,
    Literal,
    MembershipExpression,
    NullTest,
    PageSpec,
    PlannedQuery,
    QueryOperation,
    QueryTarget,
    RequirementGraph,
    ResultSpec,
    SemanticRequirement,
    Sort,
    TimestampRange,
    TranslationContext,
    UnaryExpression,
)

from meridian_storage.adapters.opensearch.mapping import (
    HIDDEN_RECORD_ID,
    HIDDEN_RESOURCE,
    HIDDEN_SOURCE_SEQUENCE,
    HIDDEN_SOURCE_VERSION,
    ResourceLayout,
)
from meridian_storage.adapters.opensearch.query import (
    CompiledSearch,
    SearchCompiler,
    close_point_in_time,
    execute_search,
)

PLAN = "sha256:" + "a" * 64
REGISTRY = "sha256:" + "b" * 64
SCOPE = "sha256:" + "c" * 64


def _compiler(layout: ResourceLayout, *, pit: bool = False) -> SearchCompiler:
    return SearchCompiler(
        layout,
        "read-alias",
        CursorSigner(
            {"test": b"cursor-signing-key-that-has-32-bytes!"},
            active_key_id="test",
            clock=lambda: datetime(2026, 8, 26, tzinfo=UTC),
        ),
        pit_enabled=pit,
    )


def _operation(
    layout: ResourceLayout,
    expression: object = None,
    *,
    cursor: str | None = None,
    pit: bool = False,
    order: tuple[Sort, ...] = (),
) -> QueryOperation:
    return QueryOperation(
        catalog="structured",
        targets=(QueryTarget(layout.resource_ref),),
        operation="search",
        result=ResultSpec(shape="search"),
        filter=expression,  # type: ignore[arg-type]
        order=order,
        page=PageSpec(size=1, cursor=cursor, point_in_time=pit),
        consistency="eventual",
    )


def _compile(
    search: SearchCompiler, operation: QueryOperation, layout: ResourceLayout
) -> CompiledSearch:
    return search.compile_query_operation(
        operation,
        plan_fingerprint=PLAN,
        registry_fingerprint=REGISTRY,
        scope_fingerprint=SCOPE,
        schema_fingerprints={layout.resource_ref: layout.schema_fingerprint},
    )


def _hit(layout: ResourceLayout, record: str, sort: list[object]) -> dict[str, object]:
    return {
        "_score": 1.0,
        "sort": sort,
        "_source": {
            HIDDEN_RECORD_ID: f'"{record}"',
            HIDDEN_RESOURCE: layout.resource_ref,
            HIDDEN_SOURCE_VERSION: "v1",
            HIDDEN_SOURCE_SEQUENCE: 1,
        },
    }


def _response(layout: ResourceLayout, *hits: dict[str, object]) -> dict[str, object]:
    return {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {"total": len(hits), "hits": list(hits)},
    }


@pytest.mark.parametrize(
    "expression",
    [
        BinaryExpression("eq", Field("category"), Literal("News")),
        BinaryExpression("ne", Literal("News"), Field("category")),
        BinaryExpression("lt", Field("priority"), Literal(10)),
        BinaryExpression("lte", Field("priority"), Literal(10)),
        BinaryExpression("gt", Field("priority"), Literal(1)),
        BinaryExpression("gte", Field("priority"), Literal(1)),
        BinaryExpression("prefix", Field("category"), Literal("Ne")),
        MembershipExpression(Field("category"), (Literal("News"), Literal("Research"))),
        MembershipExpression(Field("category"), (Literal("Spam"),), negated=True),
        NullTest(Field("category")),
        TimestampRange(
            Field("published"),
            Literal("2026-01-01T00:00:00Z"),
            Literal("2027-01-01T00:00:00Z"),
        ),
        BooleanExpression(
            "and",
            (
                BinaryExpression("eq", Field("category"), Literal("News")),
                NullTest(Field("active"), is_null=False),
            ),
        ),
        BooleanExpression(
            "or",
            (
                FullTextMatch("meridian", (Field("title"),)),
                FullTextMatch("存储", (Field("body"),)),
            ),
        ),
        UnaryExpression("not", BinaryExpression("eq", Field("category"), Literal("Hidden"))),
    ],
)
def test_released_query_expression_allowlist(
    search_layout: ResourceLayout, expression: object
) -> None:
    compiled = _compile(
        _compiler(search_layout), _operation(search_layout, expression), search_layout
    )
    assert SCOPE in str(compiled.body)
    assert "query_string" not in str(compiled.body)


def test_query_translator_spi_round_trip(search_layout: ResourceLayout) -> None:
    search = _compiler(search_layout)
    operation = _operation(search_layout, FullTextMatch("meridian", (Field("title"),)))
    requirements = RequirementGraph(
        "meridian.structured.search@1.0.0",
        (SemanticRequirement("search", consistency_class="eventual"),),
    )
    plan = PlannedQuery(
        operation,
        "binding",
        requirements,
        {"search": ImplementationMode.NATIVE},
        "sha256:" + "d" * 64,
        REGISTRY,
        {search_layout.resource_ref: search_layout.schema_fingerprint},
    )
    context = TranslationContext(
        "binding",
        PLAN,
        REGISTRY,
        {search_layout.resource_ref: search_layout.schema_fingerprint},
        SCOPE,
        30_000,
    )
    assert search.capabilities.adapter_id == "org.meridian.storage.opensearch"
    compiled = search.compile(plan, context)
    normalized = search.normalize_result(compiled, _response(search_layout))
    assert normalized.data["consistency"] == "eventual"
    assert normalized.provenance["pagination"] == "live-keyset"
    with pytest.raises(TypeError):
        search.compile(object(), context)
    with pytest.raises(TypeError):
        search.normalize_result(
            CompiledQuery("org.meridian.storage.opensearch", PLAN, ["not", "an", "object"]),
            {},
        )


def test_point_in_time_cursor_reuses_snapshot(search_layout: ResourceLayout) -> None:
    search = _compiler(search_layout, pit=True)
    first = _compile(
        search,
        _operation(search_layout, FullTextMatch("query", (Field("body"),)), pit=True),
        search_layout,
    )
    raw = _response(
        search_layout,
        _hit(search_layout, "one", [1.0, "doc-1"]),
        _hit(search_layout, "two", [0.5, "doc-2"]),
    )
    raw["pit_id"] = "pit-1"
    page = search.normalize(first, raw)
    assert page.cursor and page.pit_id == "pit-1"
    second = _compile(
        search,
        _operation(
            search_layout,
            FullTextMatch("query", (Field("body"),)),
            cursor=page.cursor,
            pit=True,
        ),
        search_layout,
    )
    assert second.pit_id == "pit-1"
    assert second.body["search_after"] == [1.0, "doc-1"]

    without_pit = _compiler(search_layout)
    with pytest.raises(ValidationError):
        _compile(
            without_pit,
            _operation(
                search_layout,
                FullTextMatch("query", (Field("body"),)),
                cursor=page.cursor,
            ),
            search_layout,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"cursor": 1},
        {"where": {"category": {}}},
        {"where": {"category": {"$isNull": "yes"}}},
        {"where": {"published": {"$timestampRange": {}}}},
        {"where": {"published": {"$timestampRange": {"extra": 1}}}},
        {"where": {"category": {"$in": "News"}}},
        {"query": {"text": "x", "operator": "xor"}},
        {"query": {"text": "x", "fields": ["title", "title"]}},
        {"query": {"text": "x", "fields": ["category"]}},
        {"resource": "structured:other.items"},
        {"extra": True},
    ],
)
def test_mapping_inputs_fail_closed(
    search_layout: ResourceLayout, changes: dict[str, object]
) -> None:
    arguments: dict[str, object] = {
        "resource": search_layout.resource_ref,
        "query": "meridian",
        "where": {},
        "facets": [],
        "highlights": [],
        "limit": 10,
    }
    arguments.update(changes)
    with pytest.raises(ValidationError):
        _compiler(search_layout).compile_mapping(
            arguments,
            plan_fingerprint=PLAN,
            registry_fingerprint=REGISTRY,
            scope_fingerprint=SCOPE,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda hit, layout: hit["_source"].update({HIDDEN_RESOURCE: "structured:other.items"}),
        lambda hit, layout: hit["_source"].update({HIDDEN_RECORD_ID: 3}),
        lambda hit, layout: hit["_source"].update({HIDDEN_RECORD_ID: "{"}),
        lambda hit, layout: hit.update({"_score": "high"}),
        lambda hit, layout: hit["_source"].update({HIDDEN_SOURCE_VERSION: 1}),
        lambda hit, layout: hit["_source"].update({HIDDEN_SOURCE_SEQUENCE: True}),
        lambda hit, layout: hit.update({"highlight": {"unknown": ["x"]}}),
        lambda hit, layout: hit.update({"highlight": {layout.field("title").physical_name: [1]}}),
    ],
)
def test_search_hits_fail_closed(search_layout: ResourceLayout, mutation: object) -> None:
    hit = _hit(search_layout, "one", [1.0, "doc-1"])
    mutation(hit, search_layout)  # type: ignore[operator]
    with pytest.raises(ValidationError):
        _compiler(search_layout).normalize(
            _compile(_compiler(search_layout), _operation(search_layout), search_layout),
            _response(search_layout, hit),
        )


@pytest.mark.parametrize(
    "aggregations",
    [
        {"facet": {"meta": {}, "buckets": []}},
        {"facet": {"meta": {"logicalField": "category"}, "buckets": [{"key": "x"}]}},
        {
            "facet": {
                "meta": {"logicalField": "category"},
                "buckets": [{"key": "x", "doc_count": -1}],
            }
        },
    ],
)
def test_facets_fail_closed(search_layout: ResourceLayout, aggregations: dict[str, object]) -> None:
    raw = _response(search_layout)
    raw["aggregations"] = aggregations
    with pytest.raises(ValidationError):
        _compiler(search_layout).normalize(
            _compile(_compiler(search_layout), _operation(search_layout), search_layout), raw
        )


def test_compiled_search_and_execution_errors(search_layout: ResourceLayout) -> None:
    base = _compile(_compiler(search_layout), _operation(search_layout), search_layout)
    with pytest.raises(ValueError):
        CompiledSearch.from_command({})
    command = base.to_command()
    command["schemaFingerprints"] = {"resource": 1}
    with pytest.raises(TypeError):
        CompiledSearch.from_command(command)
    with pytest.raises(ValueError):
        CompiledSearch(
            base.read_target,
            base.body,
            PLAN,
            REGISTRY,
            SCOPE,
            base.schema_fingerprints,
            0,
        )

    class BrokenClient(FakeClient):
        def search(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            raise RuntimeError("private endpoint and credential")

    with pytest.raises(InternalError):
        execute_search(BrokenClient(), base)

    broken = BrokenClient()
    broken.transport.perform_request = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("private endpoint")
    )
    with pytest.raises(InternalError):
        close_point_in_time(broken, "pit")
