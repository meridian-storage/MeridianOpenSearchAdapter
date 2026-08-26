# SPDX-License-Identifier: Apache-2.0
"""Bounded search translation, scope isolation, pagination, and normalization tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import FakeClient
from meridian_storage.errors import ValidationError
from meridian_storage.query import (
    CursorSigner,
    Field,
    FullTextMatch,
    InvalidCursor,
    PageSpec,
    QueryOperation,
    QueryTarget,
    ResultSpec,
    Sort,
)
from meridian_storage.semantics import JsonValue

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


def compiler(layout: ResourceLayout, *, pit: bool = False) -> SearchCompiler:
    signer = CursorSigner(
        {"test": b"cursor-signing-key-that-has-32-bytes!"},
        active_key_id="test",
        clock=lambda: datetime(2026, 8, 26, tzinfo=UTC),
    )
    return SearchCompiler(layout, "read-alias", signer, pit_enabled=pit)


def mapping_arguments(layout: ResourceLayout, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "resource": layout.resource_ref,
        "query": "running fox",
        "where": {"category": "News", "priority": {"$gte": 2}},
        "facets": ["category"],
        "highlights": ["title"],
        "limit": 2,
    }
    value.update(changes)
    return value


def compile_mapping(layout: ResourceLayout, **changes: object) -> CompiledSearch:
    return compiler(layout).compile_mapping(
        mapping_arguments(layout, **changes),
        plan_fingerprint=PLAN,
        registry_fingerprint=REGISTRY,
        scope_fingerprint=SCOPE,
    )


def test_mapping_search_compiles_only_declared_logical_features(
    search_layout: ResourceLayout,
) -> None:
    compiled = compile_mapping(search_layout)
    body = compiled.body
    encoded = str(body)
    assert SCOPE in encoded
    assert search_layout.resource_ref in encoded
    assert "multi_match" in encoded
    assert "script" not in encoded and "query_string" not in encoded
    assert body["size"] == 3
    assert body["sort"][-1] == {"m_document_id": {"order": "asc"}}
    facet = body["aggs"]["facet_0"]
    assert facet["meta"]["logicalField"] == "category"
    title = search_layout.field("title")
    assert title.physical_name in body["highlight"]["fields"]
    restored = CompiledSearch.from_command(compiled.to_command())
    assert restored.to_command() == compiled.to_command()


@pytest.mark.parametrize(
    "where",
    [
        {"category": {"$ne": "Hidden"}},
        {"category": {"$in": ["News", "Research"]}},
        {"category": {"$notIn": ["Spam"]}},
        {"category": {"$prefix": "Res"}},
        {"category": {"$isNull": False}},
        {"published": {"$lt": "2027-01-01T00:00:00Z"}},
        {
            "published": {
                "$timestampRange": {
                    "start": "2026-01-01T00:00:00Z",
                    "end": "2027-01-01T00:00:00Z",
                    "includeStart": True,
                    "includeEnd": False,
                }
            }
        },
    ],
)
def test_logical_filter_allowlist(search_layout: ResourceLayout, where: object) -> None:
    result = compile_mapping(search_layout, where=where)
    assert "bool" in result.body["query"]


@pytest.mark.parametrize(
    "changes",
    [
        {"query": {"match_all": {}}},
        {"query": {"text": "x", "script": "return true"}},
        {"query": {"text": "x", "fuzziness": 3}},
        {"query": {"text": "x", "ranking": "consumer-boost"}},
        {"query": ""},
        {"where": {"category": {"$regex": ".*"}}},
        {"where": {"active": {"$prefix": "not-a-string"}}},
        {"facets": ["title"]},
        {"highlights": ["category"]},
        {"limit": 501},
    ],
)
def test_dsl_and_unadvertised_features_are_rejected(
    search_layout: ResourceLayout, changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        compile_mapping(search_layout, **changes)


def test_normalization_returns_only_logical_refs_and_signed_cursor(
    search_layout: ResourceLayout,
) -> None:
    search = compiler(search_layout)
    compiled = search.compile_mapping(
        mapping_arguments(search_layout),
        plan_fingerprint=PLAN,
        registry_fingerprint=REGISTRY,
        scope_fingerprint=SCOPE,
    )
    title = search_layout.field("title").physical_name
    raw = {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {
            "total": {"value": 3, "relation": "eq"},
            "hits": [
                _hit(search_layout, "id-1", 1, [3.0, "doc-1"], title),
                _hit(search_layout, "id-2", 2, [2.0, "doc-2"], title),
                _hit(search_layout, "id-3", 3, [1.0, "doc-3"], title),
            ],
        },
        "aggregations": {
            "facet_0": {
                "meta": {"logicalField": "category"},
                "buckets": [{"key": "News", "doc_count": 2}],
            }
        },
    }
    result = search.normalize(compiled, raw)
    assert len(result.data["items"]) == 2
    assert result.data["items"][0]["recordRef"]["recordId"] == "id-1"
    assert "_index" not in str(result.data)
    assert result.data["facets"]["category"] == [{"value": "News", "count": 2}]
    assert result.cursor and result.cursor.startswith("mqc1.")
    second = search.compile_mapping(
        mapping_arguments(search_layout, cursor=result.cursor),
        plan_fingerprint=PLAN,
        registry_fingerprint=REGISTRY,
        scope_fingerprint=SCOPE,
    )
    assert second.body["search_after"] == [2.0, "doc-2"]
    with pytest.raises(InvalidCursor):
        search.compile_mapping(
            mapping_arguments(search_layout, cursor=result.cursor),
            plan_fingerprint=PLAN,
            registry_fingerprint=REGISTRY,
            scope_fingerprint="sha256:" + "d" * 64,
        )


def test_query_v1_plan_compiles_full_text_filters_and_sort(search_layout: ResourceLayout) -> None:
    search = compiler(search_layout, pit=True)
    full_text = FullTextMatch(
        "running",
        (Field("title"), Field("body")),
        highlights=("title",),
        facets=("category",),
    )
    operation = QueryOperation(
        catalog="structured",
        targets=(QueryTarget(search_layout.resource_ref),),
        operation="search",
        result=ResultSpec(shape="search"),
        filter=full_text,
        order=(Sort(Field("priority"), "desc"),),
        page=PageSpec(size=10, point_in_time=True),
        consistency="eventual",
    )
    compiled = search.compile_query_operation(
        operation,
        plan_fingerprint=PLAN,
        registry_fingerprint=REGISTRY,
        scope_fingerprint=SCOPE,
        schema_fingerprints={search_layout.resource_ref: search_layout.schema_fingerprint},
    )
    assert compiled.pit_requested is True
    assert search_layout.field("priority").physical_name in str(compiled.body["sort"])
    unsupported = QueryOperation(
        catalog="structured",
        targets=(QueryTarget(search_layout.resource_ref),),
        operation="search",
        result=ResultSpec(shape="search"),
        filter=full_text,
        page=PageSpec(size=10),
        consistency="strong",
    )
    with pytest.raises(ValidationError):
        search.compile_query_operation(
            unsupported,
            plan_fingerprint=PLAN,
            registry_fingerprint=REGISTRY,
            scope_fingerprint=SCOPE,
            schema_fingerprints={search_layout.resource_ref: search_layout.schema_fingerprint},
        )


def test_pit_execution_and_explicit_close(search_layout: ResourceLayout) -> None:
    search = compiler(search_layout, pit=True)
    operation = QueryOperation(
        catalog="structured",
        targets=(QueryTarget(search_layout.resource_ref),),
        operation="search",
        result=ResultSpec(shape="search"),
        filter=FullTextMatch("query", (Field("body"),)),
        page=PageSpec(size=1, point_in_time=True),
        consistency="eventual",
    )
    compiled = search.compile_query_operation(
        operation,
        plan_fingerprint=PLAN,
        registry_fingerprint=REGISTRY,
        scope_fingerprint=SCOPE,
        schema_fingerprints={search_layout.resource_ref: search_layout.schema_fingerprint},
    )
    client = FakeClient()
    raw, pit_id = execute_search(client, compiled)
    assert raw == client.search_response and pit_id == "pit-1"
    assert "index" not in client.search_calls[-1]
    assert client.search_calls[-1]["body"]["pit"]["id"] == "pit-1"
    close_point_in_time(client, "pit-1")
    assert client.transport.calls[-1][0:2] == ("DELETE", "/_search/point_in_time")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"timed_out": True}),
        lambda value: value["_shards"].update({"failed": 1}),
        lambda value: value["hits"].update({"total": {"value": -1, "relation": "eq"}}),
    ],
)
def test_partial_or_malformed_results_fail_closed(
    search_layout: ResourceLayout, mutation: object
) -> None:
    search = compiler(search_layout)
    compiled = compile_mapping(search_layout)
    raw: dict[str, object] = {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
    }
    mutation(raw)  # type: ignore[operator]
    with pytest.raises(ValidationError):
        search.normalize(compiled, raw)


def _hit(
    layout: ResourceLayout,
    record_id: str,
    sequence: int,
    sort: list[JsonValue],
    highlight_field: str,
) -> dict[str, object]:
    return {
        "_index": "physical-generation-redacted",
        "_id": f"doc-{sequence}",
        "_score": float(sequence),
        "sort": sort,
        "_source": {
            HIDDEN_RECORD_ID: f'"{record_id}"',
            HIDDEN_RESOURCE: layout.resource_ref,
            HIDDEN_SOURCE_VERSION: f"v{sequence}",
            HIDDEN_SOURCE_SEQUENCE: sequence,
        },
        "highlight": {highlight_field: ["<em>Running</em> fox"]},
    }
