# SPDX-License-Identifier: Apache-2.0
"""Generation, probe, failure normalization, client, and Core SPI tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest
from conftest import FakeClient
from meridian_storage.errors import (
    AuthenticationError,
    AuthorizationError,
    CompatibilityError,
    ConfigurationError,
    ConflictError,
    InternalError,
    LifecycleError,
    MeridianError,
    MeridianTimeoutError,
    NotFoundError,
    RateLimitError,
    TransientError,
    UnavailableError,
    ValidationError,
)
from meridian_storage.query import (
    Field,
    FullTextMatch,
    PageSpec,
    QueryOperation,
    QueryTarget,
    ResultSpec,
)
from meridian_storage.runtime.config import BindingConfig, TLSPolicy
from meridian_storage.semantics import StructuredCatalogProvider
from meridian_storage.spi import (
    AdapterCreateContext,
    ExecutionRequest,
    PhysicalResource,
    SecretValue,
)
from meridian_storage.testing.adapter_conformance import (
    AdapterConformanceTarget,
    run_adapter_conformance,
)

from meridian_storage import Operation, OperationContext, ResourceRef
from meridian_storage.adapters.opensearch._canonical import read_alias, write_alias
from meridian_storage.adapters.opensearch.client import create_client_handle
from meridian_storage.adapters.opensearch.configuration import (
    OpenSearchSettings,
    TopologyExpectation,
)
from meridian_storage.adapters.opensearch.errors import translate_engine_error
from meridian_storage.adapters.opensearch.generation import (
    GenerationManager,
    GenerationState,
)
from meridian_storage.adapters.opensearch.probe import OpenSearchProbe
from meridian_storage.adapters.opensearch.runtime import (
    OpenSearchAdapterFactory,
    expected_capability_fingerprint,
)


def raw_settings(layout: object, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "indexPrefix": "meridian",
        "layouts": {layout.resource_ref: layout.to_dict()},
        "requiredPlugins": ["analysis-icu"],
        "pitEnabled": False,
    }
    value.update(changes)
    return value


def create_context(
    layout: object,
    *,
    tls: bool = False,
    settings_changes: Mapping[str, object] | None = None,
) -> AdapterCreateContext:
    settings_raw = raw_settings(layout, **dict(settings_changes or {}))
    settings = OpenSearchSettings.from_mapping(settings_raw)
    tls_value: dict[str, object] = {
        "mode": "server" if tls else "disabled",
        "serverName": "opensearch.internal" if tls else None,
        "caRef": {"provider": "test", "reference": "ca"} if tls else None,
        "clientCertificateRef": None,
    }
    binding = BindingConfig.from_mapping(
        {
            "id": "search-binding",
            "adapterId": "org.meridian.storage.opensearch",
            "adapterContract": "1.0.0",
            "engineProfile": "opensearch",
            "engineVersion": "2.19.1",
            "endpoint": "https://localhost:9200" if tls else "http://localhost:9200",
            "serviceRef": None,
            "physicalNamespace": "meridian",
            "tls": tls_value,
            "identityRef": {"provider": "test", "reference": "identity"},
            "secretRef": {"provider": "test", "reference": "credential"},
            "client": {
                "minSize": 0,
                "maxSize": 4,
                "acquireTimeoutMs": 1000,
                "idleTimeoutMs": 1000,
                "operationTimeoutMs": 5000,
                "maxResultBytes": 1_000_000,
                "iteratorLifetimeMs": 60_000,
            },
            "requiredCapabilityFingerprint": expected_capability_fingerprint("2.19.1", settings),
            "requiredPhysicalFingerprint": None,
            "compatibilityPins": {},
            "settings": settings_raw,
            "extensions": {},
        },
        "bindings[0]",
    )
    return AdapterCreateContext(
        binding,
        SecretValue(b"admin"),
        SecretValue(b"correct-horse-battery-staple"),
        tls_ca=SecretValue(b"test-ca") if tls else None,
    )


def activate(fake: FakeClient, layout: object, generation: int = 1) -> GenerationManager:
    manager = GenerationManager(fake, "meridian")
    manager.create(layout, generation)
    manager.activate(layout, generation)
    return manager


def test_generation_plan_create_cutover_rollback_and_retire(
    fake_client: FakeClient, search_layout: object
) -> None:
    manager = GenerationManager(fake_client, "meridian")
    plan = manager.plan(search_layout, target_generation=1)
    assert plan.steps[-1] == "retain-previous-generation"
    assert plan.fingerprint.startswith("sha256:")
    assert manager.create(search_layout, 1).state is GenerationState.CREATED
    assert manager.verify(search_layout, 1).state is GenerationState.VERIFIED
    first = manager.activate(search_layout, 1)
    assert first.state is GenerationState.ACTIVE
    assert manager.create(search_layout, 2).generation == 2
    second = manager.activate(search_layout, 2)
    assert second.previous_generation == 1
    with pytest.raises(ConflictError):
        manager.retire(
            search_layout,
            2,
            expected_mapping_fingerprint=search_layout.mapping_fingerprint,
        )
    rolled_back = manager.rollback(search_layout, 1)
    assert rolled_back.generation == 1
    retired = manager.retire(
        search_layout,
        2,
        expected_mapping_fingerprint=search_layout.mapping_fingerprint,
    )
    assert retired.state is GenerationState.RETIRED
    with pytest.raises(ValueError):
        manager.plan(search_layout, current_generation=2, target_generation=2)
    with pytest.raises(ConflictError):
        manager.retire(
            search_layout,
            3,
            expected_mapping_fingerprint="sha256:" + "0" * 64,
        )


def test_probe_verifies_engine_plugin_topology_analysis_and_aliases(
    fake_client: FakeClient, search_layout: object
) -> None:
    activate(fake_client, search_layout)
    settings = OpenSearchSettings.from_mapping(raw_settings(search_layout))
    probe = OpenSearchProbe(fake_client, settings)
    snapshot = probe.snapshot()
    assert snapshot.engine_version == "2.19.1"
    assert snapshot.plugins == ("analysis-icu",)
    result = probe.probe()
    assert result.evidence["topology"] == "verified"
    physical = probe.verify_physical(
        (
            PhysicalResource(
                ResourceRef.parse(search_layout.resource_ref),
                "sha256:" + "2" * 64,
                search_layout.schema_fingerprint,
                "search",
            ),
        )
    )
    assert physical.mappings[search_layout.resource_ref] == search_layout.mapping_fingerprint
    assert physical.fingerprint.startswith("sha256:")


def test_probe_fails_closed_for_missing_resources(search_layout: object) -> None:
    no_plugin = FakeClient(plugin=False)
    settings = OpenSearchSettings.from_mapping(raw_settings(search_layout))
    with pytest.raises(CompatibilityError):
        OpenSearchProbe(no_plugin, settings).snapshot()
    red = FakeClient(status="red")
    with pytest.raises(CompatibilityError):
        OpenSearchProbe(red, settings).snapshot()
    small = FakeClient(nodes=1)
    cluster_settings = OpenSearchSettings.from_mapping(
        raw_settings(
            search_layout,
            topology=TopologyExpectation.cluster_conformance().to_dict(),
            replicas=1,
        )
    )
    with pytest.raises(CompatibilityError):
        OpenSearchProbe(small, cluster_settings).snapshot()
    with pytest.raises(ValidationError):
        OpenSearchProbe(FakeClient(), settings).verify_layout(search_layout)


class EngineFailureError(Exception):
    def __init__(self, status: int, error_type: str = "engine_error") -> None:
        self.status_code = status
        self.info = {"error": {"type": error_type, "reason": "credential should not leak"}}
        super().__init__("endpoint=https://secret password=hunter2")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthenticationError),
        (403, AuthorizationError),
        (409, ConflictError),
        (429, RateLimitError),
        (503, UnavailableError),
        (400, ValidationError),
    ],
)
def test_engine_failures_are_typed_and_redacted(status: int, expected: type[MeridianError]) -> None:
    with pytest.raises(expected) as raised:
        translate_engine_error(
            EngineFailureError(status, "mapper_parsing_exception"),
            operation_contract="meridian.structured.search",
            resource_ref="structured:ns.items",
        )
    payload = raised.value.to_dict()
    assert "secret" not in str(payload).lower()
    assert payload["cause"]["type"] == "EngineFailureError"
    assert payload["adapterProvenance"]["engineErrorType"] == "mapper_parsing_exception"


def test_client_construction_keeps_credentials_out_of_endpoint(search_layout: object) -> None:
    captured: dict[str, Any] = {}
    fake = FakeClient()

    def builder(**kwargs: Any) -> FakeClient:
        captured.update(kwargs)
        return fake

    handle = create_client_handle(create_context(search_layout), client_builder=builder)
    assert captured["hosts"][0] == {"host": "localhost", "port": 9200, "scheme": "http"}
    assert captured["http_auth"] == ("admin", "correct-horse-battery-staple")
    handle.close()
    assert fake.closed
    tls_handle = create_client_handle(
        create_context(search_layout, tls=True), client_builder=builder
    )
    assert captured["ca_certs"] and captured["verify_certs"] is True
    tls_handle.close()
    invalid = create_context(search_layout)
    object.__setattr__(invalid.binding, "endpoint", "http://user:password@localhost:9200/path")
    with pytest.raises(ConfigurationError):
        create_client_handle(invalid, client_builder=builder)


def test_core_adapter_conformance_runner(search_layout: object) -> None:
    fake = FakeClient()
    activate(fake, search_layout)
    title = search_layout.field("title").physical_name
    fake.search_response = {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {
            "total": {"value": 1, "relation": "eq"},
            "hits": [
                {
                    "_score": 1.0,
                    "sort": [1.0, "doc-1"],
                    "_source": {
                        "m_record_id": '"record-1"',
                        "m_resource": search_layout.resource_ref,
                        "m_source_version": "v1",
                        "m_source_sequence": 1,
                    },
                    "highlight": {title: ["<em>Meridian</em>"]},
                }
            ],
        },
    }
    context = create_context(search_layout)
    factory = OpenSearchAdapterFactory(client_builder=lambda **kwargs: fake)
    operation = StructuredCatalogProvider().normalize(
        StructuredCatalogProvider()
        .create_surface()
        .search(
            resource=search_layout.resource_ref,
            query="meridian",
            highlights=("title",),
            limit=10,
        )
    )
    target = AdapterConformanceTarget(
        factory=factory,
        create_context=context,
        resources=(
            PhysicalResource(
                ResourceRef.parse(search_layout.resource_ref),
                "sha256:" + "5" * 64,
                search_layout.schema_fingerprint,
                "search",
            ),
        ),
        operation=operation,
        context=OperationContext(
            principal_ref="identity:test",
            tenant="tenant-a",
            scope={"workspace": "cases"},
        ),
        assert_result=lambda result: (
            result.data["items"][0]["recordRef"]["recordId"] == "record-1"
            or (_ for _ in ()).throw(AssertionError("unexpected normalized result"))
        ),
    )
    report = run_adapter_conformance(target)
    assert report.adapter_id == "org.meridian.storage.opensearch"
    assert report.checks == (
        "authenticated-open",
        "deterministic-capability-manifest",
        "deterministic-physical-verification",
        "normalized-execution",
    )
    assert fake.closed


def test_alias_names_are_private_deterministic(search_layout: object) -> None:
    assert read_alias("meridian", search_layout.resource_ref).startswith("meridian-r-")
    assert write_alias("meridian", search_layout.resource_ref).startswith("meridian-w-")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, NotFoundError),
        (408, MeridianTimeoutError),
        (504, MeridianTimeoutError),
        (500, TransientError),
        (418, InternalError),
    ],
)
def test_remaining_engine_failure_classes(status: int, expected: type[MeridianError]) -> None:
    with pytest.raises(expected):
        translate_engine_error(EngineFailureError(status))


def test_client_service_reference_tls_and_secret_validation(search_layout: object) -> None:
    fake = FakeClient()
    captured: dict[str, Any] = {}

    def builder(**kwargs: Any) -> FakeClient:
        captured.clear()
        captured.update(kwargs)
        return fake

    base = create_context(search_layout)
    service_binding = replace(base.binding, endpoint=None, service_ref="service:search")
    service_context = replace(base, binding=service_binding)
    with pytest.raises(ConfigurationError):
        create_client_handle(service_context, client_builder=builder)

    class Resolver:
        def resolve(self, service_ref: str) -> str:
            assert service_ref == "service:search"
            return "http://search.internal:9201"

    handle = create_client_handle(
        service_context, service_resolver=Resolver(), client_builder=builder
    )
    assert captured["hosts"][0]["host"] == "search.internal"
    assert captured["hosts"][0]["port"] == 9201
    handle.close()
    handle.close()

    with pytest.raises(ConfigurationError):
        create_client_handle(
            replace(base, binding=replace(base.binding, endpoint="https://localhost:9200")),
            client_builder=builder,
        )
    server = create_context(search_layout, tls=True)
    with pytest.raises(ConfigurationError):
        create_client_handle(
            replace(server, binding=replace(server.binding, endpoint="http://localhost:9200")),
            client_builder=builder,
        )
    with pytest.raises(ConfigurationError):
        create_client_handle(replace(server, tls_ca=None), client_builder=builder)

    mutual_policy = TLSPolicy(
        "mutual",
        "opensearch.internal",
        server.binding.tls.ca_ref,
        server.binding.identity_ref,
    )
    mutual = replace(server, binding=replace(server.binding, tls=mutual_policy))
    with pytest.raises(ConfigurationError):
        create_client_handle(mutual, client_builder=builder)
    mutual = replace(mutual, tls_client_certificate=SecretValue(b"client-certificate"))
    handle = create_client_handle(mutual, client_builder=builder)
    assert captured["client_cert"]
    handle.close()

    with pytest.raises(ConfigurationError):
        create_client_handle(replace(base, identity=SecretValue(b"\xff")), client_builder=builder)
    with pytest.raises(ConfigurationError):
        create_client_handle(
            replace(base, credential=SecretValue(b"bad\x00secret")), client_builder=builder
        )

    def failing_builder(**kwargs: Any) -> FakeClient:
        del kwargs
        raise RuntimeError("builder failed")

    with pytest.raises(RuntimeError):
        create_client_handle(server, client_builder=failing_builder)


def _search_operation(layout: object) -> Operation:
    return StructuredCatalogProvider().normalize(
        StructuredCatalogProvider()
        .create_surface()
        .search(resource=layout.resource_ref, query="meridian", limit=10)
    )


def _execution_request(
    operation: Operation, *, binding_id: str = "search-binding"
) -> ExecutionRequest:
    return ExecutionRequest(
        operation,
        OperationContext(
            principal_ref="identity:test", tenant="tenant-a", scope={"workspace": "cases"}
        ),
        "request-1",
        "execution-1",
        binding_id,
        1,
        "sha256:" + "9" * 64,
        1,
    )


def test_runtime_lifecycle_scope_and_transaction_fail_closed(search_layout: object) -> None:
    fake = FakeClient()
    context = create_context(search_layout)
    runtime = OpenSearchAdapterFactory(client_builder=lambda **kwargs: fake).create(context)
    with pytest.raises(LifecycleError):
        runtime.probe()
    runtime.open()
    runtime.open()
    with pytest.raises(ValidationError):
        runtime.open_session(transactional=True)
    session = runtime.open_session(transactional=False)
    for action in (session.begin, session.commit, session.rollback):
        with pytest.raises(ValidationError):
            action()
    with pytest.raises(ValidationError):
        session.execute(_execution_request(_search_operation(search_layout), binding_id="other"))
    unknown = replace(
        _search_operation(search_layout), resources=(ResourceRef.parse("structured:other.items"),)
    )
    with pytest.raises(ValidationError):
        session.execute(_execution_request(unknown))
    invalid = replace(_search_operation(search_layout), read_only=False)
    with pytest.raises(ValidationError):
        session.execute(_execution_request(invalid))
    session.close()
    with pytest.raises(LifecycleError):
        session.execute(_execution_request(_search_operation(search_layout)))
    runtime.close()
    runtime.close()
    with pytest.raises(LifecycleError):
        runtime.open()


def test_factory_and_runtime_capability_pins(search_layout: object) -> None:
    base = create_context(search_layout)
    factory = OpenSearchAdapterFactory(client_builder=lambda **kwargs: FakeClient())
    with pytest.raises(ConfigurationError):
        factory.create(replace(base, binding=replace(base.binding, adapter_id="other")))
    with pytest.raises(ConfigurationError):
        factory.create(replace(base, binding=replace(base.binding, engine_profile="other")))
    with pytest.raises(ConfigurationError, match="unsupported OpenSearch Adapter SPI"):
        factory.create(replace(base, binding=replace(base.binding, adapter_contract="2.0.0")))
    with pytest.raises(ConfigurationError):
        factory.create(replace(base, binding=replace(base.binding, physical_namespace="different")))

    class VersionClient(FakeClient):
        def __init__(self, versions: list[str]) -> None:
            super().__init__()
            self.versions = versions

        def info(self) -> Mapping[str, object]:
            return {
                "version": {
                    "number": self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]
                }
            }

    runtime = OpenSearchAdapterFactory(
        client_builder=lambda **kwargs: VersionClient(["2.18.0"])
    ).create(base)
    with pytest.raises(CompatibilityError):
        runtime.open()
    runtime.close()

    bad_pin = replace(
        base,
        binding=replace(base.binding, required_capability_fingerprint="sha256:" + "0" * 64),
    )
    runtime = OpenSearchAdapterFactory(client_builder=lambda **kwargs: FakeClient()).create(bad_pin)
    with pytest.raises(CompatibilityError):
        runtime.open()
    runtime.close()

    changing = VersionClient(["2.19.1", "2.18.0"])
    runtime = OpenSearchAdapterFactory(client_builder=lambda **kwargs: changing).create(base)
    runtime.open()
    with pytest.raises(CompatibilityError):
        runtime.probe()
    runtime.close()


def test_runtime_executes_serialized_query_plan_and_closes_pit(search_layout: object) -> None:
    fake = FakeClient()
    context = create_context(search_layout, settings_changes={"pitEnabled": True})
    runtime = OpenSearchAdapterFactory(client_builder=lambda **kwargs: fake).create(context)
    runtime.open()
    query = QueryOperation(
        catalog="structured",
        targets=(QueryTarget(search_layout.resource_ref),),
        operation="search",
        result=ResultSpec(shape="search"),
        filter=FullTextMatch("meridian", (Field("title"),)),
        page=PageSpec(size=10, point_in_time=True),
        consistency="eventual",
    )
    operation = replace(_search_operation(search_layout), input={"queryPlan": query.to_dict()})
    session = runtime.open_session(transactional=False)
    result = session.execute(_execution_request(operation))
    assert result.data["cursor"] is None
    assert result.provenance["pagination"] == "point-in-time"
    assert fake.transport.calls[-1][0] == "DELETE"
    session.close()
    runtime.close()


def test_runtime_enforces_normalized_result_bytes(search_layout: object) -> None:
    fake = FakeClient()
    context = create_context(search_layout)
    tiny_client = replace(context.binding.client, max_result_bytes=1)
    context = replace(context, binding=replace(context.binding, client=tiny_client))
    runtime = OpenSearchAdapterFactory(client_builder=lambda **kwargs: fake).create(context)
    runtime.open()
    session = runtime.open_session(transactional=False)
    with pytest.raises(ValidationError):
        session.execute(_execution_request(_search_operation(search_layout)))
    session.close()
    runtime.close()
