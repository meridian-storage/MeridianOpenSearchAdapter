# SPDX-License-Identifier: Apache-2.0
"""Authenticated engine, plugin, analyzer, topology, and physical probes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from meridian_storage.errors import CompatibilityError, ErrorCode, ValidationError
from meridian_storage.semantics import JsonValue, sha256_fingerprint
from meridian_storage.spi import AdapterProbe, PhysicalResource, PhysicalVerification

from .._canonical import read_alias, write_alias
from .._version import __version__
from ..client import ClientProtocol
from ..configuration import OpenSearchSettings
from ..descriptor import capability_manifest
from ..errors import incompatible, translate_engine_error
from ..mapping import HIDDEN_DOCUMENT_ID, ResourceLayout


@dataclass(frozen=True, slots=True)
class ProbeSnapshot:
    engine_version: str
    cluster_status: str
    data_nodes: int
    eligible_nodes: int
    plugins: tuple[str, ...]


class OpenSearchProbe:
    def __init__(
        self,
        client: ClientProtocol,
        settings: OpenSearchSettings,
        *,
        selected_engine_version: str | None = None,
    ) -> None:
        self._client = client
        self.settings = settings
        self._selected_engine_version = selected_engine_version

    def probe(self) -> AdapterProbe:
        snapshot = self.snapshot()
        manifest = capability_manifest(
            snapshot.engine_version,
            pit_enabled=self.settings.pit_enabled,
            limits=self.settings.limits,
        )
        return AdapterProbe(
            manifest,
            evidence={
                "selectedEngineVersion": self._selected_engine_version or "unavailable",
                "observedEngineVersion": snapshot.engine_version,
                "observedAdapterVersion": __version__,
                "releaseObservation": "authenticated-engine-info",
                "analysis": "multilingual-icu/v1",
                "clusterStatus": snapshot.cluster_status,
                "dataNodes": str(snapshot.data_nodes),
                "eligibleNodes": str(snapshot.eligible_nodes),
                "pluginSetFingerprint": sha256_fingerprint(cast(JsonValue, list(snapshot.plugins))),
                "topology": "verified",
            },
        )

    def snapshot(self) -> ProbeSnapshot:
        try:
            info = self._client.info()
            nodes = self._client.nodes.info(metric="plugins")
            health = self._client.cluster.health()
        except Exception as exc:
            translate_engine_error(exc)
        version = _engine_version(info)
        node_values = _object(nodes, "node probe").get("nodes")
        node_map = _object(node_values, "node probe entries")
        data_nodes = 0
        eligible_nodes = 0
        plugins: set[str] = set()
        for raw_node in node_map.values():
            node = _object(raw_node, "node probe entry")
            roles = node.get("roles", ())
            if not isinstance(roles, Sequence) or isinstance(roles, (str, bytes)):
                raise ValidationError(ErrorCode.ADAPTER_FAILURE, "node roles are invalid")
            role_set = {str(role) for role in roles}
            if role_set & {"data", "data_content", "data_hot", "data_warm", "data_cold"}:
                data_nodes += 1
            if role_set & {"cluster_manager", "master"}:
                eligible_nodes += 1
            raw_plugins = node.get("plugins", ())
            if not isinstance(raw_plugins, Sequence) or isinstance(raw_plugins, (str, bytes)):
                raise ValidationError(ErrorCode.ADAPTER_FAILURE, "node plugins are invalid")
            node_plugins: set[str] = set()
            for raw_plugin in raw_plugins:
                plugin = _object(raw_plugin, "node plugin")
                name = plugin.get("name") or plugin.get("component")
                if isinstance(name, str):
                    node_plugins.add(name)
            missing_on_node = set(self.settings.required_plugins) - node_plugins
            if missing_on_node:
                incompatible(
                    f"OpenSearch required plugins are absent on a node: {sorted(missing_on_node)!r}"
                )
            plugins.update(node_plugins)
        missing = set(self.settings.required_plugins) - plugins
        if missing:
            incompatible(f"OpenSearch required plugins are absent: {sorted(missing)!r}")
        topology = self.settings.topology
        if (
            data_nodes < topology.minimum_data_nodes
            or eligible_nodes < topology.minimum_eligible_nodes
        ):
            incompatible("OpenSearch node topology is below the selected deployment profile")
        health_mapping = _object(health, "cluster health")
        status = health_mapping.get("status")
        if status not in {"green", "yellow"}:
            incompatible("OpenSearch cluster health is red or unknown")
        if topology.require_green and status != "green":
            incompatible("OpenSearch cluster-conformance profile requires green health")
        return ProbeSnapshot(
            version, cast(str, status), data_nodes, eligible_nodes, tuple(sorted(plugins))
        )

    def verify_layout(self, layout: ResourceLayout) -> str:
        read = read_alias(self.settings.index_prefix, layout.resource_ref)
        write = write_alias(self.settings.index_prefix, layout.resource_ref)
        try:
            if not self._client.indices.exists_alias(
                name=read
            ) or not self._client.indices.exists_alias(name=write):
                raise ValidationError(
                    ErrorCode.PHYSICAL_FINGERPRINT,
                    "required OpenSearch read/write aliases are absent",
                    resource_ref=layout.resource_ref,
                )
            mappings = self._client.indices.get_mapping(index=read)
            read_targets = set(_object(self._client.indices.get_alias(name=read), "read alias"))
            write_targets = set(_object(self._client.indices.get_alias(name=write), "write alias"))
            if len(read_targets) != 1 or read_targets != write_targets:
                raise ValidationError(
                    ErrorCode.PHYSICAL_FINGERPRINT,
                    "read/write aliases must resolve to the same single generation",
                    resource_ref=layout.resource_ref,
                )
            settings = self._client.indices.get_settings(index=read)
            analysis = self._client.indices.analyze(
                index=read,
                body={"analyzer": "meridian_icu", "text": "Meridian 中文 validation"},
            )
        except ValidationError:
            raise
        except Exception as exc:
            translate_engine_error(exc, resource_ref=layout.resource_ref)
        tokens = _object(analysis, "analyzer response").get("tokens")
        if (
            not isinstance(tokens, list)
            or not tokens
            or any(
                not isinstance(token, Mapping) or not isinstance(token.get("token"), str)
                for token in tokens
            )
        ):
            incompatible("OpenSearch required ICU analyzer returned no valid tokens")
        mapping_fingerprints = _mapping_fingerprints(mappings)
        if mapping_fingerprints != {layout.mapping_fingerprint}:
            raise ValidationError(
                ErrorCode.PHYSICAL_FINGERPRINT,
                "active aliases do not resolve to the compiled mapping fingerprint",
                resource_ref=layout.resource_ref,
            )
        replicas = _replica_counts(settings)
        if not replicas or min(replicas) < self.settings.topology.required_replicas:
            raise ValidationError(
                ErrorCode.PHYSICAL_FINGERPRINT,
                "active generation replica count is below the deployment profile",
                resource_ref=layout.resource_ref,
            )
        self._verify_search_api(read)
        return layout.mapping_fingerprint

    def _verify_search_api(self, read: str) -> None:
        """Exercise bounded search/keyset and optional PIT APIs without reading Records."""

        pit_id: str | None = None
        try:
            body: dict[str, object] = {
                "query": {"match_none": {}},
                "size": 1,
                "timeout": "5s",
                "sort": [{HIDDEN_DOCUMENT_ID: "asc"}],
                "search_after": ["meridian-probe"],
                "_source": False,
            }
            if self.settings.pit_enabled:
                opened = self._client.transport.perform_request(
                    "POST", f"/{read}/_search/point_in_time", params={"keep_alive": "1m"}
                )
                value = _object(opened, "PIT response").get("pit_id")
                if not isinstance(value, str) or not value:
                    incompatible("OpenSearch required point-in-time API returned no id")
                pit_id = value
                body["pit"] = {"id": pit_id, "keep_alive": "1m"}
                response = self._client.search(body=body)
            else:
                response = self._client.search(index=read, body=body)
            result = _object(response, "search API response")
            shards = _object(result.get("_shards"), "search API shards")
            if result.get("timed_out") is not False or shards.get("failed") != 0:
                incompatible("OpenSearch required search API timed out or returned shard failures")
            hits = _object(result.get("hits"), "search API hits").get("hits")
            if not isinstance(hits, list):
                incompatible("OpenSearch required search API returned invalid hits")
        except (ValidationError, CompatibilityError):
            raise
        except Exception as exc:
            translate_engine_error(exc)
        finally:
            if pit_id is not None:
                try:
                    self._client.transport.perform_request(
                        "DELETE", "/_search/point_in_time", body={"pit_id": pit_id}
                    )
                except Exception as exc:
                    translate_engine_error(exc)

    def verify_physical(self, resources: tuple[PhysicalResource, ...]) -> PhysicalVerification:
        mappings: dict[str, str] = {}
        for resource in resources:
            logical = str(resource.resource_ref)
            raw_layout = self.settings.layouts.get(logical)
            if raw_layout is None:
                raise ValidationError(
                    ErrorCode.PLACEMENT_UNRESOLVED,
                    "binding has no compiled OpenSearch layout for a required Resource",
                    resource_ref=logical,
                )
            layout = ResourceLayout.from_mapping(raw_layout)
            if resource.schema_fingerprint is not None and (
                resource.schema_fingerprint != layout.schema_fingerprint
            ):
                raise ValidationError(
                    ErrorCode.PHYSICAL_FINGERPRINT,
                    "required Schema fingerprint differs from the compiled layout",
                    resource_ref=logical,
                )
            mappings[logical] = self.verify_layout(layout)
        fingerprint = sha256_fingerprint(
            cast(
                JsonValue,
                {"formatVersion": "meridian.opensearch.physical.v1", "mappings": mappings},
            )
        )
        return PhysicalVerification(
            fingerprint,
            mappings=mappings,
            evidence={"resources": str(len(mappings)), "verification": "aliases-mappings-analysis"},
        )


def _engine_version(value: object) -> str:
    info = _object(value, "engine info")
    version = _object(info.get("version"), "engine version").get("number")
    if not isinstance(version, str) or not version.strip():
        raise ValidationError(ErrorCode.ADAPTER_FAILURE, "OpenSearch version is absent")
    return version


def _mapping_fingerprints(value: object) -> set[str]:
    response = _object(value, "mapping response")
    result: set[str] = set()
    for raw in response.values():
        item = _object(raw, "mapping index")
        mapping = _object(item.get("mappings"), "index mappings")
        metadata = _object(mapping.get("_meta"), "mapping metadata")
        fingerprint = metadata.get("mappingFingerprint")
        if not isinstance(fingerprint, str):
            raise ValidationError(ErrorCode.PHYSICAL_FINGERPRINT, "mapping metadata is incomplete")
        result.add(fingerprint)
    return result


def _replica_counts(value: object) -> list[int]:
    response = _object(value, "settings response")
    result: list[int] = []
    for raw in response.values():
        item = _object(raw, "index settings")
        settings = _object(item.get("settings"), "index settings body")
        index = _object(settings.get("index"), "index settings values")
        raw_count = index.get("number_of_replicas")
        try:
            count = int(cast(str | int, raw_count))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                ErrorCode.PHYSICAL_FINGERPRINT, "index replica setting is invalid"
            ) from exc
        result.append(count)
    return result


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(ErrorCode.ADAPTER_FAILURE, f"OpenSearch {name} must be an object")
    return cast(Mapping[str, Any], value)


__all__ = ["OpenSearchProbe", "ProbeSnapshot"]
