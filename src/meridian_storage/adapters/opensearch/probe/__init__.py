# SPDX-License-Identifier: Apache-2.0
"""Authenticated engine, plugin, analyzer, topology, and physical probes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from meridian_storage.errors import ErrorCode, ValidationError
from meridian_storage.semantics import JsonValue, sha256_fingerprint
from meridian_storage.spi import AdapterProbe, PhysicalResource, PhysicalVerification

from .._canonical import read_alias, write_alias
from ..client import ClientProtocol
from ..configuration import OpenSearchSettings
from ..descriptor import ENGINE_PROFILE, SUPPORTED_ENGINE_VERSIONS, capability_manifest
from ..errors import incompatible, translate_engine_error
from ..mapping import ResourceLayout


@dataclass(frozen=True, slots=True)
class ProbeSnapshot:
    engine_version: str
    cluster_status: str
    data_nodes: int
    eligible_nodes: int
    plugins: tuple[str, ...]


class OpenSearchProbe:
    def __init__(self, client: ClientProtocol, settings: OpenSearchSettings) -> None:
        self._client = client
        self.settings = settings

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
        if version not in SUPPORTED_ENGINE_VERSIONS:
            incompatible(
                f"OpenSearch {version} is outside the adapter's released {ENGINE_PROFILE} profile"
            )
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
            for raw_plugin in raw_plugins:
                plugin = _object(raw_plugin, "node plugin")
                name = plugin.get("name") or plugin.get("component")
                if isinstance(name, str):
                    plugins.add(name)
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
            settings = self._client.indices.get_settings(index=read)
            self._client.indices.analyze(
                index=read,
                body={"analyzer": "meridian_icu", "text": "Meridian 中文 validation"},
            )
        except ValidationError:
            raise
        except Exception as exc:
            translate_engine_error(exc, resource_ref=layout.resource_ref)
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
        return layout.mapping_fingerprint

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
    if not isinstance(version, str):
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
