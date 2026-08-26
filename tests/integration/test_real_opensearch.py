# SPDX-License-Identifier: Apache-2.0
"""Real-engine indexing, multilingual search, pagination, migration, and health vectors."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from meridian_storage.errors import ValidationError
from meridian_storage.query import (
    CursorSigner,
    Field,
    FullTextMatch,
    PageSpec,
    QueryOperation,
    QueryTarget,
    ResultSpec,
)
from opensearchpy import OpenSearch
from opensearchpy.exceptions import RequestError

from meridian_storage.adapters.opensearch._canonical import generation_name, read_alias, write_alias
from meridian_storage.adapters.opensearch.configuration import OpenSearchSettings
from meridian_storage.adapters.opensearch.generation import GenerationManager, GenerationState
from meridian_storage.adapters.opensearch.mapping import ResourceLayout
from meridian_storage.adapters.opensearch.probe import OpenSearchProbe
from meridian_storage.adapters.opensearch.projection import (
    MutationOutcome,
    ProjectionExecutor,
    ProjectionMutation,
)
from meridian_storage.adapters.opensearch.query import (
    SearchCompiler,
    close_point_in_time,
    execute_search,
)

PLAN = "sha256:" + "a" * 64
REGISTRY = "sha256:" + "b" * 64
SCOPE_A = "sha256:" + "c" * 64
SCOPE_B = "sha256:" + "d" * 64
PREFIX = "meridianit"


def _client() -> OpenSearch:
    raw = os.environ.get("OPENSEARCH_URL")
    if raw is None:
        pytest.skip("OPENSEARCH_URL is required for real-engine conformance")
    endpoint = urlsplit(raw)
    return OpenSearch(
        hosts=[
            {
                "host": endpoint.hostname or "localhost",
                "port": endpoint.port or 9200,
                "scheme": endpoint.scheme or "http",
            }
        ],
        use_ssl=endpoint.scheme == "https",
        verify_certs=False,
        max_retries=0,
        timeout=30,
    )


def _mutation(
    layout: ResourceLayout,
    record_id: str,
    sequence: int,
    *,
    title: str,
    body: str,
    category: str = "Research",
    scope: str = SCOPE_A,
    payload: bool = True,
) -> ProjectionMutation:
    return ProjectionMutation(
        layout.resource_ref,
        record_id,
        sequence,
        f"v{sequence}",
        "1.0.0",
        layout.schema_fingerprint,
        scope,
        f"event-{record_id}-{sequence}",
        (
            {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "title": title,
                "body": body,
                "category": category,
                "published": "2026-08-26T00:00:00Z",
                "priority": sequence,
                "active": True,
                "labels": ["multilingual", category],
            }
            if payload
            else None
        ),
        datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )


def _arguments(layout: ResourceLayout, text: str, **changes: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "resource": layout.resource_ref,
        "query": text,
        "where": {},
        "facets": [],
        "highlights": [],
        "limit": 10,
    }
    arguments.update(changes)
    return arguments


def _compiler(layout: ResourceLayout, *, pit: bool = False) -> SearchCompiler:
    return SearchCompiler(
        layout,
        read_alias(PREFIX, layout.resource_ref),
        CursorSigner(
            {"integration": b"real-opensearch-cursor-key-at-least-32-bytes"},
            active_key_id="integration",
            clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        ),
        pit_enabled=pit,
    )


def _compiled_mapping(search: SearchCompiler, layout: ResourceLayout, text: str, **changes: object):
    return search.compile_mapping(
        _arguments(layout, text, **changes),
        plan_fingerprint=PLAN,
        registry_fingerprint=REGISTRY,
        scope_fingerprint=SCOPE_A,
    )


def _search(client: OpenSearch, search: SearchCompiler, compiled):
    raw, opened_pit = execute_search(client, compiled)
    response = dict(raw)
    if opened_pit is not None and "pit_id" not in response:
        response["pit_id"] = opened_pit
    return search.normalize(compiled, response)


@pytest.mark.integration
@pytest.mark.relevance
@pytest.mark.conformance
def test_real_opensearch_v1_conformance(search_layout: ResourceLayout) -> None:
    client = _client()
    manager = GenerationManager(client, PREFIX)
    generation_one = generation_name(PREFIX, search_layout.resource_ref, 1)
    generation_two = generation_name(PREFIX, search_layout.resource_ref, 2)
    for index in (generation_one, generation_two):
        if client.indices.exists(index=index):
            client.indices.delete(index=index)
    try:
        assert manager.create(search_layout, 1).state is GenerationState.CREATED
        assert manager.activate(search_layout, 1).state is GenerationState.ACTIVE
        executor = ProjectionExecutor(
            client, search_layout, write_alias(PREFIX, search_layout.resource_ref)
        )
        mutations = (
            _mutation(
                search_layout,
                "en-1",
                10,
                title="Running foxes",
                body="Meridian multilingual search architecture",
                category="English",
            ),
            _mutation(
                search_layout,
                "zh-1",
                20,
                title="中文检索指南",
                body="Meridian 支持中文检索和多语言分词",
                category="中文",
            ),
            _mutation(
                search_layout,
                "ar-1",
                30,
                title="Future language",
                body="Meridian يدعم البحث متعدد اللغات",
                category="Arabic",
            ),
            _mutation(
                search_layout,
                "other-scope",
                40,
                title="Running outside scope",
                body="Meridian should remain isolated",
                scope=SCOPE_B,
            ),
        )
        bulk = executor.bulk(mutations, refresh_policy="wait_for")
        assert bulk.applied == 4 and not bulk.retryable and not bulk.permanent

        stale = executor.project(
            _mutation(
                search_layout,
                "en-1",
                9,
                title="Stale running fox",
                body="stale",
            ),
            refresh_policy="wait_for",
        )
        assert stale.outcome is MutationOutcome.STALE

        search = _compiler(search_layout)
        english = _search(
            client,
            search,
            _compiled_mapping(
                search,
                search_layout,
                "running",
                facets=["category"],
                highlights=["title"],
            ),
        )
        ids = {item["recordRef"]["recordId"] for item in english.data["items"]}
        assert ids == {"en-1"}
        assert english.data["facets"]["category"] == [{"value": "English", "count": 1}]

        chinese = _search(
            client,
            search,
            _compiled_mapping(
                search, search_layout, "中文检索", query={"text": "中文检索", "fields": ["body"]}
            ),
        )
        assert {item["recordRef"]["recordId"] for item in chinese.data["items"]} == {"zh-1"}
        arabic = _search(
            client,
            search,
            _compiled_mapping(
                search,
                search_layout,
                "متعدد اللغات",
                query={"text": "متعدد اللغات", "fields": ["body"]},
            ),
        )
        assert {item["recordRef"]["recordId"] for item in arabic.data["items"]} == {"ar-1"}

        first = _search(
            client,
            search,
            _compiled_mapping(
                search,
                search_layout,
                "meridian",
                query={"text": "meridian", "fields": ["body"]},
                limit=1,
            ),
        )
        assert first.cursor and first.has_more and len(first.data["items"]) == 1
        second = _search(
            client,
            search,
            _compiled_mapping(
                search,
                search_layout,
                "meridian",
                query={"text": "meridian", "fields": ["body"]},
                limit=1,
                cursor=first.cursor,
            ),
        )
        assert second.data["items"][0] != first.data["items"][0]

        pit_search = _compiler(search_layout, pit=True)
        operation = QueryOperation(
            catalog="structured",
            targets=(QueryTarget(search_layout.resource_ref),),
            operation="search",
            result=ResultSpec(shape="search"),
            filter=FullTextMatch("meridian", (Field("body"),)),
            page=PageSpec(size=1, point_in_time=True),
            consistency="eventual",
        )
        pit_compiled = pit_search.compile_query_operation(
            operation,
            plan_fingerprint=PLAN,
            registry_fingerprint=REGISTRY,
            scope_fingerprint=SCOPE_A,
            schema_fingerprints={search_layout.resource_ref: search_layout.schema_fingerprint},
        )
        pit_page = _search(client, pit_search, pit_compiled)
        assert pit_page.pit_id and pit_page.cursor
        close_point_in_time(client, pit_page.pit_id)

        tombstone = executor.project(
            _mutation(
                search_layout,
                "en-1",
                11,
                title="deleted",
                body="deleted",
                payload=False,
            ),
            refresh_policy="wait_for",
        )
        assert tombstone.outcome is MutationOutcome.APPLIED
        after_delete = _search(client, search, _compiled_mapping(search, search_layout, "running"))
        assert after_delete.data["items"] == []

        with pytest.raises(ValidationError):
            executor.encode(
                _mutation(
                    search_layout,
                    "invalid",
                    1,
                    title="invalid",
                    body="invalid",
                    category="invalid",
                ).__class__(
                    search_layout.resource_ref,
                    "invalid",
                    1,
                    "v1",
                    "1.0.0",
                    search_layout.schema_fingerprint,
                    SCOPE_A,
                    "event-invalid",
                    {"unknown": "strict"},
                    datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
                )
            )
        with pytest.raises(RequestError):
            client.index(
                index=write_alias(PREFIX, search_layout.resource_ref),
                id="invalid-direct",
                body={"unknown": "strict"},
                refresh="wait_for",
            )

        assert manager.create(search_layout, 2).state is GenerationState.CREATED
        generation_two_executor = ProjectionExecutor(client, search_layout, generation_two)
        migrated = generation_two_executor.bulk(mutations[:3], refresh_policy="wait_for")
        assert migrated.applied == 3
        activated = manager.activate(search_layout, 2)
        assert activated.previous_generation == 1
        assert manager.rollback(search_layout, 1).previous_generation == 2
        assert (
            manager.retire(
                search_layout,
                2,
                expected_mapping_fingerprint=search_layout.mapping_fingerprint,
            ).state
            is GenerationState.RETIRED
        )

        settings = OpenSearchSettings.from_mapping(
            {
                "indexPrefix": PREFIX,
                "layouts": {search_layout.resource_ref: search_layout.to_dict()},
                "requiredPlugins": ["analysis-icu"],
                "pitEnabled": True,
            }
        )
        probe = OpenSearchProbe(client, settings)
        assert probe.snapshot().engine_version == "2.19.1"
        assert probe.verify_layout(search_layout) == search_layout.mapping_fingerprint
    finally:
        for index in (generation_one, generation_two):
            if client.indices.exists(index=index):
                client.indices.delete(index=index)
        client.close()
