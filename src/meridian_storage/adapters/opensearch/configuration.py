# SPDX-License-Identifier: Apache-2.0
"""Closed adapter-owned settings for OpenSearch translation and verification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from meridian_storage.errors import ConfigurationError, ErrorCode
from meridian_storage.semantics import JsonValue

from ._canonical import index_prefix, require_fingerprint, safe_token


def _closed(
    value: object,
    name: str,
    *,
    allowed: frozenset[str],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ConfigurationError(ErrorCode.CONFIG_INVALID, f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigurationError(
            ErrorCode.CONFIG_UNKNOWN_FIELD,
            f"{name} contains unknown fields: {sorted(unknown)!r}",
        )
    return cast(Mapping[str, object], value)


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigurationError(
            ErrorCode.CONFIG_INVALID,
            f"{name} must be an integer between {minimum} and {maximum}",
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(ErrorCode.CONFIG_INVALID, f"{name} must be a boolean")
    return value


def _strings(value: object, name: str, *, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ConfigurationError(ErrorCode.CONFIG_INVALID, f"{name} must be an array")
    result = tuple(sorted(safe_token(item, name, maximum=maximum) for item in value))
    if len(result) != len(set(result)):
        raise ConfigurationError(ErrorCode.CONFIG_INVALID, f"{name} entries must be unique")
    return result


@dataclass(frozen=True, slots=True)
class AdapterLimits:
    max_page_size: int = 500
    max_query_bytes: int = 16_384
    max_facets: int = 32
    max_highlights: int = 32
    max_filter_clauses: int = 128
    max_bulk_actions: int = 1_000
    max_bulk_bytes: int = 8 * 1024 * 1024
    facet_bucket_limit: int = 100
    highlight_fragment_limit: int = 5

    @classmethod
    def from_mapping(cls, value: object) -> AdapterLimits:
        item = _closed(
            value,
            "settings.limits",
            allowed=frozenset(
                {
                    "maxPageSize",
                    "maxQueryBytes",
                    "maxFacets",
                    "maxHighlights",
                    "maxFilterClauses",
                    "maxBulkActions",
                    "maxBulkBytes",
                    "facetBucketLimit",
                    "highlightFragmentLimit",
                }
            ),
        )
        defaults = cls()
        return cls(
            max_page_size=_integer(
                item.get("maxPageSize", defaults.max_page_size), "maxPageSize", 1, 500
            ),
            max_query_bytes=_integer(
                item.get("maxQueryBytes", defaults.max_query_bytes),
                "maxQueryBytes",
                1,
                1_048_576,
            ),
            max_facets=_integer(item.get("maxFacets", defaults.max_facets), "maxFacets", 0, 100),
            max_highlights=_integer(
                item.get("maxHighlights", defaults.max_highlights), "maxHighlights", 0, 100
            ),
            max_filter_clauses=_integer(
                item.get("maxFilterClauses", defaults.max_filter_clauses),
                "maxFilterClauses",
                1,
                1_024,
            ),
            max_bulk_actions=_integer(
                item.get("maxBulkActions", defaults.max_bulk_actions),
                "maxBulkActions",
                1,
                10_000,
            ),
            max_bulk_bytes=_integer(
                item.get("maxBulkBytes", defaults.max_bulk_bytes),
                "maxBulkBytes",
                1_024,
                100 * 1024 * 1024,
            ),
            facet_bucket_limit=_integer(
                item.get("facetBucketLimit", defaults.facet_bucket_limit),
                "facetBucketLimit",
                1,
                10_000,
            ),
            highlight_fragment_limit=_integer(
                item.get("highlightFragmentLimit", defaults.highlight_fragment_limit),
                "highlightFragmentLimit",
                1,
                100,
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "facetBucketLimit": self.facet_bucket_limit,
            "highlightFragmentLimit": self.highlight_fragment_limit,
            "maxBulkActions": self.max_bulk_actions,
            "maxBulkBytes": self.max_bulk_bytes,
            "maxFacets": self.max_facets,
            "maxFilterClauses": self.max_filter_clauses,
            "maxHighlights": self.max_highlights,
            "maxPageSize": self.max_page_size,
            "maxQueryBytes": self.max_query_bytes,
        }


@dataclass(frozen=True, slots=True)
class TopologyExpectation:
    minimum_data_nodes: int = 1
    minimum_eligible_nodes: int = 1
    required_replicas: int = 0
    require_green: bool = False

    @classmethod
    def from_mapping(cls, value: object) -> TopologyExpectation:
        item = _closed(
            value,
            "settings.topology",
            allowed=frozenset(
                {"minimumDataNodes", "minimumEligibleNodes", "requiredReplicas", "requireGreen"}
            ),
        )
        defaults = cls()
        return cls(
            minimum_data_nodes=_integer(
                item.get("minimumDataNodes", defaults.minimum_data_nodes),
                "minimumDataNodes",
                1,
                1_000,
            ),
            minimum_eligible_nodes=_integer(
                item.get("minimumEligibleNodes", defaults.minimum_eligible_nodes),
                "minimumEligibleNodes",
                1,
                1_000,
            ),
            required_replicas=_integer(
                item.get("requiredReplicas", defaults.required_replicas),
                "requiredReplicas",
                0,
                20,
            ),
            require_green=_boolean(
                item.get("requireGreen", defaults.require_green), "requireGreen"
            ),
        )

    @classmethod
    def cluster_conformance(cls) -> TopologyExpectation:
        """Return the LLD cluster-conformance floor."""

        return cls(3, 3, 1, True)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "minimumDataNodes": self.minimum_data_nodes,
            "minimumEligibleNodes": self.minimum_eligible_nodes,
            "requiredReplicas": self.required_replicas,
            "requireGreen": self.require_green,
        }


@dataclass(frozen=True, slots=True)
class CursorSettings:
    key_id: str
    keys: Mapping[str, bytes] = field(repr=False)
    ttl_seconds: int = 900
    pit_keep_alive: str = "2m"

    def __post_init__(self) -> None:
        key_id = safe_token(self.key_id, "cursor key id", maximum=64)
        keys = dict(sorted(self.keys.items()))
        if key_id not in keys or not keys or len(keys) > 8:
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID, "cursor keys must contain the active key id"
            )
        if any(not key or not 32 <= len(value) <= 1_024 for key, value in keys.items()):
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID, "cursor signing keys must be 32 to 1024 bytes"
            )
        object.__setattr__(self, "key_id", key_id)
        object.__setattr__(self, "keys", MappingProxyType(keys))
        _integer(self.ttl_seconds, "cursor ttlSeconds", 30, 86_400)
        keep_alive = safe_token(self.pit_keep_alive, "PIT keep alive", maximum=16)
        if not keep_alive[:-1].isdigit() or keep_alive[-1] not in "smhd":
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID, "PIT keep alive must use an s, m, h, or d suffix"
            )


@dataclass(frozen=True, slots=True)
class OpenSearchSettings:
    index_prefix: str
    layouts: Mapping[str, Mapping[str, object]]
    limits: AdapterLimits = field(default_factory=AdapterLimits)
    topology: TopologyExpectation = field(default_factory=TopologyExpectation)
    required_plugins: tuple[str, ...] = ("analysis-icu",)
    shards: int = 1
    replicas: int = 0
    refresh_interval: str = "1s"
    pit_enabled: bool = True
    compatibility_fingerprint: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OpenSearchSettings:
        item = _closed(
            value,
            "binding.settings",
            allowed=frozenset(
                {
                    "indexPrefix",
                    "layouts",
                    "limits",
                    "topology",
                    "requiredPlugins",
                    "shards",
                    "replicas",
                    "refreshInterval",
                    "pitEnabled",
                    "compatibilityFingerprint",
                }
            ),
        )
        if "indexPrefix" not in item or "layouts" not in item:
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID, "binding.settings requires indexPrefix and layouts"
            )
        raw_layouts = item["layouts"]
        if not isinstance(raw_layouts, Mapping) or any(
            not isinstance(key, str) or not isinstance(layout, Mapping)
            for key, layout in raw_layouts.items()
        ):
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID, "layouts must map resources to objects"
            )
        layouts = {
            safe_token(key, "layout resource", maximum=768): MappingProxyType(dict(layout))
            for key, layout in sorted(raw_layouts.items())
        }
        refresh = safe_token(item.get("refreshInterval", "1s"), "refresh interval", maximum=16)
        if refresh != "-1" and (not refresh[:-1].isdigit() or refresh[-1] not in "smh"):
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID, "refreshInterval must be -1 or use an s, m, or h suffix"
            )
        compatibility = item.get("compatibilityFingerprint")
        return cls(
            index_prefix=index_prefix(item["indexPrefix"]),
            layouts=MappingProxyType(layouts),
            limits=AdapterLimits.from_mapping(item.get("limits", {})),
            topology=TopologyExpectation.from_mapping(item.get("topology", {})),
            required_plugins=_strings(
                item.get("requiredPlugins", ("analysis-icu",)), "requiredPlugins", maximum=128
            ),
            shards=_integer(item.get("shards", 1), "shards", 1, 1_024),
            replicas=_integer(item.get("replicas", 0), "replicas", 0, 20),
            refresh_interval=refresh,
            pit_enabled=_boolean(item.get("pitEnabled", True), "pitEnabled"),
            compatibility_fingerprint=(
                None
                if compatibility is None
                else require_fingerprint(compatibility, "compatibility fingerprint")
            ),
        )


__all__ = [
    "AdapterLimits",
    "CursorSettings",
    "OpenSearchSettings",
    "TopologyExpectation",
]
