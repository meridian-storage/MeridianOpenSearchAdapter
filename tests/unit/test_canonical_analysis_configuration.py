# SPDX-License-Identifier: Apache-2.0
"""Canonicalization, analysis, settings, and descriptor unit tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from meridian_storage.errors import ConfigurationError

from meridian_storage.adapters.opensearch._canonical import (
    canonical_record_id,
    contract_token,
    document_id,
    field_name,
    generation_name,
    index_prefix,
    json_value,
    path_name,
    read_alias,
    require_fingerprint,
    scope_fingerprint,
    write_alias,
)
from meridian_storage.adapters.opensearch.analysis import (
    analyzer_for_language,
    build_analysis,
    normalize_language,
)
from meridian_storage.adapters.opensearch.configuration import (
    AdapterLimits,
    CursorSettings,
    OpenSearchSettings,
    TopologyExpectation,
)
from meridian_storage.adapters.opensearch.descriptor import (
    ADAPTER_ID,
    SEARCH_OPERATION_CONTRACT,
    adapter_descriptor,
    capability_manifest,
    query_capabilities,
)


def test_canonical_values_and_names_are_deterministic() -> None:
    timestamp = datetime(2026, 8, 26, 1, 2, 3, 4, tzinfo=UTC)
    assert json_value(timestamp) == "2026-08-26T01:02:03.000004Z"
    assert json_value(date(2026, 8, 26)) == "2026-08-26"
    assert json_value(timedelta(seconds=2)) == 2_000_000
    assert json_value(Decimal("12.340")) == "12.340"
    assert json_value(UUID("550e8400-e29b-41d4-a716-446655440000")) == (
        "550e8400-e29b-41d4-a716-446655440000"
    )
    assert json_value(b"abc") == "YWJj"
    assert json_value({"b": 1, "a": [True, None]}) == {"a": [True, None], "b": 1}
    assert canonical_record_id(("tenant", 42)) == '["tenant",42]'
    assert field_name("title") == field_name("title")
    assert field_name("title") != field_name("body")
    assert path_name("doc", "/title") != path_name("doc", "/body")
    assert index_prefix(" Meridian / Search ") == "meridian-search"
    assert generation_name("meridian", "structured:ns.items", 7).endswith("-g000007")
    assert read_alias("meridian", "structured:ns.items") != write_alias(
        "meridian", "structured:ns.items"
    )
    scope = scope_fingerprint("tenant-a", {"workspace": "cases"})
    assert document_id(scope, "structured:ns.items", 1) == document_id(
        scope, "structured:ns.items", 1
    )


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda: json_value(float("nan")), ValueError),
        (lambda: json_value(Decimal("Infinity")), ValueError),
        (lambda: json_value(datetime(2026, 1, 1)), ValueError),
        (lambda: json_value({1: "bad"}), TypeError),
        (lambda: json_value(object()), TypeError),
        (lambda: canonical_record_id(None), ValueError),
        (lambda: canonical_record_id([]), ValueError),
        (lambda: canonical_record_id(["a", None]), ValueError),
        (lambda: scope_fingerprint(None, {}), ValueError),
        (lambda: index_prefix("___"), ValueError),
        (lambda: generation_name("x", "structured:n.r", 0), ValueError),
        (lambda: require_fingerprint("sha256:no", "test"), ValueError),
        (lambda: contract_token("contains space", "token"), ValueError),
    ],
)
def test_canonical_rejections(call: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        call()  # type: ignore[operator]


def test_unrestricted_icu_profile_with_initial_languages() -> None:
    assert normalize_language("EN-us") == "en-us"
    assert analyzer_for_language("en-GB") == "meridian_en"
    assert analyzer_for_language("zh-Hans") == "meridian_zh"
    assert analyzer_for_language("ar") == "meridian_icu"
    assert analyzer_for_language(None) == "meridian_icu"
    analysis = build_analysis(["zh", "en", "ar", "en"])
    assert analysis.languages == ("ar", "en", "zh")
    assert analysis.fingerprint.startswith("sha256:")
    assert "icu_tokenizer" in str(analysis.settings)
    with pytest.raises(ValueError):
        normalize_language("bad language")
    with pytest.raises(ValueError):
        build_analysis([])


def test_closed_settings_and_topology() -> None:
    raw = {
        "indexPrefix": "meridian-search",
        "layouts": {"structured:ns.items": {"formatVersion": "placeholder"}},
        "limits": {"maxPageSize": 25, "maxBulkActions": 50},
        "topology": {"minimumDataNodes": 3, "minimumEligibleNodes": 3, "requiredReplicas": 1},
        "requiredPlugins": ["analysis-icu"],
        "shards": 2,
        "replicas": 1,
        "refreshInterval": "30s",
        "pitEnabled": False,
        "compatibilityFingerprint": "sha256:" + "a" * 64,
    }
    settings = OpenSearchSettings.from_mapping(raw)
    assert settings.index_prefix == "meridian-search"
    assert settings.limits.max_page_size == 25
    assert settings.topology.minimum_data_nodes == 3
    assert settings.shards == 2 and settings.replicas == 1
    assert TopologyExpectation.cluster_conformance().to_dict()["requiredReplicas"] == 1
    assert AdapterLimits().to_dict()["maxPageSize"] == 500
    with pytest.raises(ConfigurationError):
        OpenSearchSettings.from_mapping({**raw, "password": "forbidden"})
    with pytest.raises(ConfigurationError):
        OpenSearchSettings.from_mapping({"indexPrefix": "x", "layouts": []})
    with pytest.raises(ConfigurationError):
        AdapterLimits.from_mapping({"maxPageSize": 501})
    with pytest.raises(ConfigurationError):
        TopologyExpectation.from_mapping({"requireGreen": "yes"})


def test_cursor_settings_validate_rotation() -> None:
    settings = CursorSettings("active", {"old": b"o" * 32, "active": b"a" * 32})
    assert tuple(settings.keys) == ("active", "old")
    with pytest.raises(ConfigurationError):
        CursorSettings("missing", {"active": b"a" * 32})
    with pytest.raises(ConfigurationError):
        CursorSettings("active", {"active": b"short"})
    with pytest.raises(ConfigurationError):
        CursorSettings("active", {"active": b"a" * 32}, pit_keep_alive="forever")


def test_descriptor_is_exact_and_denies_out_of_scope_behavior() -> None:
    descriptor = adapter_descriptor()
    assert descriptor.adapter_id == ADAPTER_ID
    assert [item.operation_contract for item in descriptor.capabilities] == [
        SEARCH_OPERATION_CONTRACT
    ]
    capability = descriptor.capabilities[0]
    assert capability.guarantees == tuple(sorted(capability.guarantees))
    assert "consumer-dsl" in capability.extensions["denied"]
    manifest = capability_manifest("2.19.1", pit_enabled=False)
    assert manifest.extensions["pointInTime"] == "disabled"
    query = query_capabilities(pit_enabled=True)
    assert query.operations == ("search",)
    assert query.consistency_classes == ("eventual",)
    assert "point-in-time" in query.features
    assert query.fingerprint == query_capabilities(pit_enabled=True).fingerprint
