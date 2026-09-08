# SPDX-License-Identifier: Apache-2.0
"""Three-node replication and deliberate node-loss conformance vectors."""

from __future__ import annotations

import os

import pytest
from meridian_storage.errors import CompatibilityError
from meridian_storage.semantics import SchemaDocument
from test_real_opensearch import SCOPE_A, _client, _mutation

from meridian_storage.adapters.opensearch._canonical import generation_name, read_alias
from meridian_storage.adapters.opensearch.configuration import (
    OpenSearchSettings,
    TopologyExpectation,
)
from meridian_storage.adapters.opensearch.generation import GenerationManager
from meridian_storage.adapters.opensearch.mapping import MappingCompiler
from meridian_storage.adapters.opensearch.probe import OpenSearchProbe
from meridian_storage.adapters.opensearch.projection import ProjectionExecutor

PREFIX = "meridiancluster"


def _settings(layout: object) -> OpenSearchSettings:
    return OpenSearchSettings.from_mapping(
        {
            "indexPrefix": PREFIX,
            "layouts": {layout.resource_ref: layout.to_dict()},
            "requiredPlugins": ["analysis-icu"],
            "replicas": 1,
            "topology": TopologyExpectation.cluster_conformance().to_dict(),
        }
    )


@pytest.mark.cluster
@pytest.mark.conformance
def test_three_node_topology_replication_and_green_health(search_schema: SchemaDocument) -> None:
    if os.environ.get("OPENSEARCH_NODE_LOSS") == "1":
        pytest.skip("initial cluster vector runs before node loss")
    client = _client()
    layout = MappingCompiler(replicas=1).compile("structured:investigation.articles", search_schema)
    index = generation_name(PREFIX, layout.resource_ref, 1)
    if client.indices.exists(index=index):
        client.indices.delete(index=index)
    manager = GenerationManager(client, PREFIX)
    manager.create(layout, 1)
    manager.activate(layout, 1)
    result = ProjectionExecutor(client, layout, index).project(
        _mutation(
            layout,
            "cluster-record",
            1,
            title="Replicated Meridian",
            body="survives one node loss",
            scope=SCOPE_A,
        ),
        refresh_policy="wait_for",
    )
    assert result.outcome.value == "applied"
    # The probe validates global health, including asynchronously created system indices.
    # Await the same global condition, not only this test's already-green business index.
    health = client.cluster.health(
        wait_for_status="green",
        wait_for_no_relocating_shards=True,
        wait_for_no_initializing_shards=True,
        wait_for_nodes="3",
        timeout="30s",
        request_timeout=35,
    )
    assert health["timed_out"] is False
    assert health["status"] == "green"
    snapshot = OpenSearchProbe(client, _settings(layout)).snapshot()
    assert snapshot.data_nodes >= 3
    assert snapshot.eligible_nodes >= 3
    assert snapshot.cluster_status == "green"
    assert (
        client.indices.get_settings(index=index)[index]["settings"]["index"]["number_of_replicas"]
        == "1"
    )
    client.close()


@pytest.mark.cluster
@pytest.mark.conformance
def test_replica_search_survives_node_loss_and_probe_fails_topology(
    search_schema: SchemaDocument,
) -> None:
    if os.environ.get("OPENSEARCH_NODE_LOSS") != "1":
        pytest.skip("node-loss vector runs after one Compose node is stopped")
    client = _client()
    layout = MappingCompiler(replicas=1).compile("structured:investigation.articles", search_schema)
    with pytest.raises(CompatibilityError):
        OpenSearchProbe(client, _settings(layout)).snapshot()
    response = client.search(
        index=read_alias(PREFIX, layout.resource_ref),
        body={"query": {"match_all": {}}, "size": 10},
    )
    assert response["_shards"]["failed"] == 0
    assert response["hits"]["total"]["value"] == 1
    client.close()
