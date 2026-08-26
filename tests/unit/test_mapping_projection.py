# SPDX-License-Identifier: Apache-2.0
"""Closed mappings and externally versioned projection tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from conftest import FakeClient
from meridian_storage.errors import ValidationError
from meridian_storage.semantics import (
    DocumentProfile,
    ResourceReference,
    SchemaDocument,
)

from meridian_storage.adapters.opensearch.mapping import (
    HIDDEN_DELETED,
    HIDDEN_DOCUMENT_ID,
    HIDDEN_RECORD_ID,
    HIDDEN_SCOPE,
    MappingCompiler,
    ResourceLayout,
)
from meridian_storage.adapters.opensearch.projection import (
    MutationOutcome,
    ProjectionExecutor,
    ProjectionMutation,
)


def mutation(layout: ResourceLayout, **changes: object) -> ProjectionMutation:
    values: dict[str, object] = {
        "resource_ref": layout.resource_ref,
        "record_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_sequence": 7,
        "source_version": "record-v7",
        "schema_version": "1.0.0",
        "schema_fingerprint": layout.schema_fingerprint,
        "scope_fingerprint": "sha256:" + "1" * 64,
        "event_id": "evt-7",
        "payload": {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "title": "Running Foxes",
            "body": "中文检索 works",
            "category": "News",
            "published": "2026-08-26T00:00:00Z",
            "priority": 3,
            "active": True,
            "labels": ["Golden", "中文"],
        },
        "projected_at": datetime(2026, 8, 26, tzinfo=UTC),
    }
    values.update(changes)
    return ProjectionMutation(**values)  # type: ignore[arg-type]


def test_search_mapping_is_closed_hashed_and_round_trips(search_layout: ResourceLayout) -> None:
    value = search_layout.to_dict()
    restored = ResourceLayout.from_mapping(value)
    assert restored.to_dict() == value
    assert restored.mapping_fingerprint == search_layout.mapping_fingerprint
    mappings = search_layout.index_body["mappings"]
    assert mappings["dynamic"] == "strict"
    properties = mappings["properties"]
    assert all(name.startswith(("f_", "m_")) for name in properties)
    title = search_layout.field("title")
    assert title.language == "en"
    assert {"full-text", "highlight", "exact"} <= set(title.modes)
    assert properties[title.physical_name]["fields"]["exact"]["type"] == "keyword"
    assert "normalizer" not in properties[title.physical_name]["fields"]["exact"]
    assert search_layout.field("category", mode="facet")
    with pytest.raises(ValueError):
        search_layout.field("not_declared")
    with pytest.raises(ValueError):
        search_layout.field("category", mode="full-text")


def test_document_profile_extracts_declared_paths_only() -> None:
    schema = SchemaDocument.from_definition(
        catalog="structured",
        namespace="docs",
        name="document",
        version="1.0.0",
        definition={
            "semanticKind": "document",
            "fields": [
                {"name": "id", "logicalType": "string"},
                {"name": "document", "logicalType": "json"},
            ],
            "identity": ["id"],
            "extensions": {
                "org.meridian.profile/v1": {
                    "kind": "document",
                    "bodyField": "document",
                    "unknownFields": "extensible",
                    "indexedPaths": ["/author/name", "/status"],
                }
            },
        },
    )
    layout = MappingCompiler().compile("structured:docs.documents", schema)
    assert layout.profile_kind == "document"
    assert layout.source_fields == ()
    assert layout.field("document#/author/name").json_pointer == "/author/name"
    client = FakeClient()
    executor = ProjectionExecutor(client, layout, "write")
    projected = ProjectionMutation(
        layout.resource_ref,
        "doc-1",
        1,
        "v1",
        "1.0.0",
        layout.schema_fingerprint,
        "sha256:" + "2" * 64,
        "event-1",
        {"id": "doc-1", "document": {"author": {"name": "Ada"}, "other": 4}},
        datetime(2026, 8, 26, tzinfo=UTC),
    )
    _, document = executor.encode(projected)
    assert document[layout.field("document#/author/name").physical_name] == "Ada"
    assert layout.field("document#/status").physical_name not in document
    with pytest.raises(ValueError):
        MappingCompiler().compile(
            "structured:docs.documents", schema, DocumentProfile("id", indexed_paths=("bad",))
        )


def test_projection_encodes_hidden_evidence_and_tombstone(search_layout: ResourceLayout) -> None:
    client = FakeClient()
    executor = ProjectionExecutor(client, search_layout, "write")
    physical_id, document = executor.encode(mutation(search_layout))
    assert document[HIDDEN_DOCUMENT_ID] == physical_id
    assert document[HIDDEN_RECORD_ID] == '"550e8400-e29b-41d4-a716-446655440000"'
    assert document[HIDDEN_SCOPE] == "sha256:" + "1" * 64
    assert document[HIDDEN_DELETED] is False
    assert document[search_layout.field("labels").physical_name] == ["Golden", "中文"]
    tombstone = mutation(search_layout, source_sequence=8, event_id="evt-8", payload=None)
    _, deleted = executor.encode(tombstone)
    assert deleted[HIDDEN_DELETED] is True
    assert search_layout.field("title").physical_name not in deleted
    assert executor.project(mutation(search_layout)).outcome is MutationOutcome.APPLIED
    call = client.index_calls[-1]
    assert call["version_type"] == "external_gte"
    assert call["version"] == 7


def test_projection_rejects_malformed_and_cross_scope(search_layout: ResourceLayout) -> None:
    executor = ProjectionExecutor(FakeClient(), search_layout, "write")
    with pytest.raises(ValidationError):
        executor.encode(mutation(search_layout, resource_ref="structured:other.items"))
    with pytest.raises(ValidationError):
        executor.encode(mutation(search_layout, schema_fingerprint="sha256:" + "3" * 64))
    with pytest.raises(ValidationError):
        executor.encode(
            mutation(
                search_layout,
                payload={
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "unknown": "not mapped",
                },
            )
        )
    payload = dict(mutation(search_layout).payload or {})
    payload["labels"] = "not-an-array"
    with pytest.raises(ValidationError):
        executor.encode(mutation(search_layout, payload=payload))
    with pytest.raises(ValueError):
        mutation(search_layout, source_sequence=0)
    with pytest.raises(ValueError):
        mutation(search_layout, projected_at=datetime(2026, 1, 1))


def test_bulk_classifies_every_item(search_layout: ResourceLayout) -> None:
    client = FakeClient()
    client.bulk_response = {
        "took": 12,
        "errors": True,
        "items": [
            {"index": {"status": 201}},
            {"index": {"status": 409}},
            {"index": {"status": 429}},
            {"index": {"status": 400}},
            {"index": {"status": 503}},
        ],
    }
    executor = ProjectionExecutor(client, search_layout, "write")
    mutations = [
        mutation(search_layout, source_sequence=index + 1, event_id=f"evt-{index + 1}")
        for index in range(5)
    ]
    result = executor.bulk(mutations, refresh_policy="wait_for")
    assert [item.outcome for item in result.acknowledgements] == [
        MutationOutcome.APPLIED,
        MutationOutcome.STALE,
        MutationOutcome.RETRYABLE,
        MutationOutcome.PERMANENT,
        MutationOutcome.RETRYABLE,
    ]
    assert result.applied == 1 and result.stale == 1
    assert len(result.retryable) == 2 and len(result.permanent) == 1
    assert result.took_ms == 12
    with pytest.raises(ValidationError):
        executor.bulk([])
    with pytest.raises(ValueError):
        executor.bulk([mutations[0]], refresh_policy="true")


def test_mapping_supports_portable_scalar_shapes() -> None:
    schema = SchemaDocument.from_definition(
        catalog="structured",
        namespace="types",
        name="search",
        version="1.0.0",
        definition={
            "semanticKind": "search",
            "fields": [
                {"name": "id", "logicalType": "string"},
                {"name": "text", "logicalType": "string"},
                {
                    "name": "small_decimal",
                    "logicalType": {"kind": "decimal", "precision": 8, "scale": 2},
                },
                {
                    "name": "large_decimal",
                    "logicalType": {"kind": "decimal", "precision": 40, "scale": 20},
                },
                {"name": "float", "logicalType": "float64"},
                {"name": "bytes", "logicalType": "bytes"},
                {"name": "date", "logicalType": "date"},
                {"name": "duration", "logicalType": "duration"},
                {"name": "point", "logicalType": "wgs84Point"},
                {"name": "ref", "logicalType": "recordRef"},
            ],
            "identity": ["id"],
            "extensions": {
                "org.meridian.profile/v1": {
                    "kind": "search",
                    "sourceFields": ["text"],
                    "languageHints": ["fr"],
                    "analyzerProfile": "icu",
                    "normalizedField": None,
                    "facets": [],
                    "highlights": [],
                    "ranking": "bm25",
                }
            },
        },
    )
    layout = MappingCompiler().compile(ResourceReference.parse("structured:types.items"), schema)
    properties = layout.index_body["mappings"]["properties"]
    assert properties[layout.field("small_decimal").physical_name]["type"] == "scaled_float"
    assert properties[layout.field("large_decimal").physical_name]["type"] == "keyword"
    assert properties[layout.field("point").physical_name]["type"] == "geo_point"
    assert "range" not in layout.field("large_decimal").modes
    encoded = ProjectionExecutor(FakeClient(), layout, "write").encode(
        ProjectionMutation(
            layout.resource_ref,
            "id-1",
            1,
            "v1",
            "1.0.0",
            layout.schema_fingerprint,
            "sha256:" + "4" * 64,
            "evt",
            {
                "id": "id-1",
                "text": "bonjour",
                "small_decimal": Decimal("1.20"),
                "large_decimal": Decimal("12345678901234567890.123"),
                "float": 1.5,
                "bytes": b"abc",
                "date": "2026-08-26",
                "duration": 10,
                "point": [-122.4, 37.8],
                "ref": {"collection": "x", "id": 1},
            },
            datetime(2026, 8, 26, tzinfo=UTC),
        )
    )[1]
    assert encoded[layout.field("bytes").physical_name] == "YWJj"
    assert isinstance(encoded[layout.field("ref").physical_name], str)
