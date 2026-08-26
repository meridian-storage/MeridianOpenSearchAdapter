# SPDX-License-Identifier: Apache-2.0
"""Shared released-contract fixtures for OpenSearch adapter tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from meridian_storage.semantics import ResourceReference, SchemaDocument

from meridian_storage.adapters.opensearch import MappingCompiler, ResourceLayout


@pytest.fixture
def search_schema() -> SchemaDocument:
    return SchemaDocument.from_definition(
        catalog="structured",
        namespace="investigation",
        name="article",
        version="1.0.0",
        definition={
            "semanticKind": "search",
            "fields": [
                {"name": "id", "logicalType": "uuid"},
                {
                    "name": "title",
                    "logicalType": "string",
                    "annotations": {"org.meridian.search/language": "en"},
                },
                {"name": "body", "logicalType": "string"},
                {"name": "category", "logicalType": "string"},
                {"name": "published", "logicalType": "utcTimestamp"},
                {"name": "priority", "logicalType": "int32"},
                {"name": "active", "logicalType": "boolean"},
                {"name": "labels", "logicalType": "string", "cardinality": "many"},
            ],
            "identity": ["id"],
            "consistency": "eventual",
            "extensions": {
                "org.meridian.profile/v1": {
                    "kind": "search",
                    "sourceFields": ["title", "body"],
                    "languageHints": ["en", "zh", "ar"],
                    "analyzerProfile": "icu",
                    "normalizedField": None,
                    "facets": ["category", "labels"],
                    "highlights": ["title", "body"],
                    "ranking": "bm25",
                }
            },
        },
    )


@pytest.fixture
def search_layout(search_schema: SchemaDocument) -> ResourceLayout:
    return MappingCompiler().compile(
        ResourceReference.parse("structured:investigation.articles"), search_schema
    )


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def perform_request(self, method: str, url: str, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append((method, url, kwargs))
        if method == "POST" and url.endswith("/_search/point_in_time"):
            return {"pit_id": "pit-1"}
        return {"succeeded": True}


class FakeIndices:
    def __init__(self) -> None:
        self.created: dict[str, Mapping[str, object]] = {}
        self.aliases: dict[str, set[str]] = {}
        self.mappings: dict[str, Mapping[str, object]] = {}
        self.settings: dict[str, Mapping[str, object]] = {}
        self.alias_actions: list[Mapping[str, object]] = []
        self.deleted: list[str] = []

    def create(self, *, index: str, body: Mapping[str, object]) -> Mapping[str, object]:
        self.created[index] = body
        self.mappings[index] = {"mappings": body["mappings"]}
        index_settings = body["settings"]
        assert isinstance(index_settings, Mapping)
        self.settings[index] = {
            "settings": {
                "index": {
                    "number_of_replicas": str(index_settings["number_of_replicas"]),
                }
            }
        }
        return {"acknowledged": True}

    def get_mapping(self, *, index: str) -> Mapping[str, object]:
        targets = self._targets(index)
        return {target: self.mappings[target] for target in targets}

    def get_settings(self, *, index: str) -> Mapping[str, object]:
        targets = self._targets(index)
        return {target: self.settings[target] for target in targets}

    def analyze(self, **kwargs: object) -> Mapping[str, object]:
        return {"tokens": [{"token": "meridian"}, {"token": "中文"}]}

    def update_aliases(self, *, body: Mapping[str, object]) -> Mapping[str, object]:
        actions = body["actions"]
        assert isinstance(actions, list)
        self.alias_actions.extend(actions)
        for action in actions:
            assert isinstance(action, Mapping)
            if "remove" in action:
                item = action["remove"]
                assert isinstance(item, Mapping)
                self.aliases.setdefault(str(item["alias"]), set()).discard(str(item["index"]))
            if "add" in action:
                item = action["add"]
                assert isinstance(item, Mapping)
                self.aliases.setdefault(str(item["alias"]), set()).add(str(item["index"]))
        return {"acknowledged": True}

    def exists_alias(self, *, name: str) -> bool:
        return bool(self.aliases.get(name))

    def get_alias(self, *, name: str) -> Mapping[str, object]:
        return {index: {"aliases": {name: {}}} for index in sorted(self.aliases.get(name, set()))}

    def delete(self, *, index: str) -> Mapping[str, object]:
        self.deleted.append(index)
        self.created.pop(index, None)
        return {"acknowledged": True}

    def _targets(self, value: str) -> tuple[str, ...]:
        if value in self.aliases:
            return tuple(sorted(self.aliases[value]))
        if value in self.mappings:
            return (value,)
        raise KeyError(value)


class FakeCluster:
    def __init__(self, status: str = "green") -> None:
        self.status = status

    def health(self) -> Mapping[str, object]:
        return {"status": self.status}


class FakeNodes:
    def __init__(self, count: int = 1, *, plugin: bool = True) -> None:
        self.count = count
        self.plugin = plugin

    def info(self, **kwargs: object) -> Mapping[str, object]:
        del kwargs
        return {
            "nodes": {
                f"node-{index}": {
                    "roles": ["data", "cluster_manager"],
                    "plugins": ([{"name": "analysis-icu"}] if self.plugin else []),
                }
                for index in range(self.count)
            }
        }


class FakeClient:
    def __init__(self, *, nodes: int = 1, plugin: bool = True, status: str = "green") -> None:
        self.indices = FakeIndices()
        self.cluster = FakeCluster(status)
        self.nodes = FakeNodes(nodes, plugin=plugin)
        self.transport = FakeTransport()
        self.index_calls: list[dict[str, object]] = []
        self.bulk_response: Mapping[str, object] = {"items": []}
        self.search_response: Mapping[str, object] = {
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
        }
        self.search_calls: list[dict[str, object]] = []
        self.closed = False

    def info(self) -> Mapping[str, object]:
        return {"version": {"number": "2.19.1"}}

    def ping(self, **kwargs: object) -> bool:
        del kwargs
        return True

    def index(self, **kwargs: object) -> Mapping[str, object]:
        self.index_calls.append(dict(kwargs))
        return {"result": "created"}

    def bulk(self, **kwargs: object) -> Mapping[str, object]:
        self.index_calls.append(dict(kwargs))
        return self.bulk_response

    def search(self, **kwargs: object) -> Mapping[str, object]:
        self.search_calls.append(dict(kwargs))
        return self.search_response

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
