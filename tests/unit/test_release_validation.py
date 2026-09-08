# SPDX-License-Identifier: Apache-2.0
"""Release provenance is independent of required features and deployment drift."""

from dataclasses import replace

import pytest
from conftest import FakeClient
from meridian_storage.errors import CompatibilityError, ValidationError
from meridian_storage.runtime.config import BindingConfig
from test_generation_probe_runtime import activate, create_context, raw_settings

from meridian_storage.adapters.opensearch.configuration import (
    OpenSearchSettings,
    TopologyExpectation,
)
from meridian_storage.adapters.opensearch.descriptor import (
    TESTED_ENGINE_VERSIONS,
    adapter_descriptor,
    capability_manifest,
)
from meridian_storage.adapters.opensearch.probe import OpenSearchProbe
from meridian_storage.adapters.opensearch.runtime import (
    OpenSearchAdapterFactory,
    expected_capability_fingerprint,
)


class SelectedReleaseClient(FakeClient):
    def __init__(self, version, **kwargs):
        super().__init__(**kwargs)
        self.version = version

    def info(self):
        return {"version": {"number": self.version}}


@pytest.mark.parametrize("version", ["2.19.3", "3.2.1", "99.0.0-deployment"])
@pytest.mark.parametrize("mode", ["managed", "external"])
@pytest.mark.parametrize("nodes", [1, 3])
def test_unlisted_release_round_trip_and_runtime_provenance(search_layout, version, mode, nodes):
    # The adapter does not provision or adopt engines in either deployment mode.
    assert version not in TESTED_ENGINE_VERSIONS
    fake = SelectedReleaseClient(version, nodes=nodes)
    base = create_context(
        search_layout,
        settings_changes=(
            {"replicas": 1, "topology": TopologyExpectation.cluster_conformance().to_dict()}
            if nodes == 3
            else {}
        ),
    )
    settings = OpenSearchSettings.from_mapping(base.binding.settings)
    binding = replace(
        base.binding,
        engine_version=version,
        required_capability_fingerprint=expected_capability_fingerprint(version, settings),
        extensions={"deploymentMode": mode},
    )
    assert BindingConfig.from_mapping(binding.to_dict(), "binding").to_dict() == binding.to_dict()
    manifest = capability_manifest(version, pit_enabled=True)
    assert manifest.to_dict()["engineVersion"] == version
    assert manifest.fingerprint == capability_manifest(version, pit_enabled=True).fingerprint
    assert manifest.descriptor.fingerprint == adapter_descriptor().fingerprint
    runtime = OpenSearchAdapterFactory(client_builder=lambda **kwargs: fake).create(
        replace(base, binding=binding)
    )
    runtime.open()
    probe = runtime.probe()
    assert probe.evidence["selectedEngineVersion"] == version
    assert probe.evidence["observedEngineVersion"] == version
    assert probe.evidence["observedAdapterVersion"] == "1.1.0"
    assert probe.manifest.fingerprint == binding.required_capability_fingerprint
    runtime.close()


def test_unlisted_releases_do_not_bypass_deployment_lock(search_layout):
    base = create_context(search_layout)
    runtime = OpenSearchAdapterFactory(
        client_builder=lambda **kwargs: SelectedReleaseClient("2.19.3")
    ).create(base)
    with pytest.raises(CompatibilityError, match="deployment-selected Binding lock"):
        runtime.open()
    runtime.close()


def test_unavailable_selection_is_not_fabricated(search_layout):
    probe = OpenSearchProbe(
        SelectedReleaseClient("2.19.3"),
        OpenSearchSettings.from_mapping(raw_settings(search_layout)),
    ).probe()
    assert probe.evidence["selectedEngineVersion"] == "unavailable"
    assert probe.evidence["observedEngineVersion"] == "2.19.3"


@pytest.mark.parametrize("value", [None, "", "  ", 21903])
def test_missing_observed_version_fails_without_copying_selection(search_layout, value):
    probe = OpenSearchProbe(
        SelectedReleaseClient(value),
        OpenSearchSettings.from_mapping(raw_settings(search_layout)),
        selected_engine_version="2.19.3",
    )
    with pytest.raises(ValidationError, match="version is absent"):
        probe.probe()


def test_required_plugin_is_checked_on_each_node(search_layout):
    client = SelectedReleaseClient("2.19.3", nodes=3)
    response = client.nodes.info()
    response["nodes"]["node-2"]["plugins"] = []
    client.nodes.info = lambda **kwargs: response
    with pytest.raises(CompatibilityError, match="required plugins are absent on a node"):
        OpenSearchProbe(
            client, OpenSearchSettings.from_mapping(raw_settings(search_layout))
        ).probe()


@pytest.mark.parametrize("pit_enabled", [False, True])
def test_required_search_api_and_optional_pit_are_exercised(search_layout, pit_enabled):
    client = SelectedReleaseClient("2.19.3")
    activate(client, search_layout)
    probe = OpenSearchProbe(
        client, OpenSearchSettings.from_mapping(raw_settings(search_layout, pitEnabled=pit_enabled))
    )
    probe.verify_layout(search_layout)
    assert client.search_calls[-1]["body"]["query"] == {"match_none": {}}
    assert client.search_calls[-1]["body"]["search_after"] == ["meridian-probe"]
    assert bool(client.transport.calls) is pit_enabled
    if pit_enabled:
        assert client.transport.calls[-1][0] == "DELETE"


@pytest.mark.parametrize("broken", ["analyzer", "pit", "shards", "timeout", "hits", "alias"])
def test_unlisted_release_still_fails_missing_required_feature(search_layout, broken):
    client = SelectedReleaseClient("2.19.3")
    activate(client, search_layout)
    if broken == "analyzer":
        client.indices.analyze = lambda **kwargs: {"tokens": []}
    elif broken == "pit":
        client.transport.perform_request = lambda *args, **kwargs: {}
    elif broken == "shards":
        client.search_response = {**client.search_response, "_shards": {"failed": 1}}
    elif broken == "timeout":
        client.search_response = {**client.search_response, "timed_out": True}
    elif broken == "hits":
        client.search_response = {**client.search_response, "hits": {"hits": None}}
    else:
        from meridian_storage.adapters.opensearch._canonical import write_alias

        client.indices.aliases[write_alias("meridian", search_layout.resource_ref)] = {"other"}
    probe = OpenSearchProbe(
        client, OpenSearchSettings.from_mapping(raw_settings(search_layout, pitEnabled=True))
    )
    with pytest.raises((CompatibilityError, ValidationError)):
        probe.verify_layout(search_layout)
