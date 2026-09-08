# SPDX-License-Identifier: Apache-2.0
"""Meridian Core Adapter SPI factory, runtime, and search session."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from typing import Any, cast

from meridian_storage.errors import (
    CompatibilityError,
    ConfigurationError,
    ErrorCode,
    LifecycleError,
    ValidationError,
)
from meridian_storage.query import CursorSigner, QueryOperation
from meridian_storage.semantics import JsonValue, canonical_json_bytes, sha256_fingerprint
from meridian_storage.spi import (
    AdapterCreateContext,
    AdapterProbe,
    ExecutionRequest,
    ExecutionResult,
    PhysicalResource,
    PhysicalVerification,
)

from meridian_storage import Operation

from ._canonical import index_prefix, read_alias, scope_fingerprint
from .client import (
    ClientHandle,
    ClientProtocol,
    ServiceResolver,
    create_client_handle,
)
from .configuration import OpenSearchSettings
from .descriptor import (
    ADAPTER_CONTRACT_VERSION,
    ADAPTER_ID,
    ENGINE_PROFILE,
    SEARCH_OPERATION_CONTRACT,
    SEARCH_OPERATION_VERSION,
    capability_manifest,
)
from .mapping import ResourceLayout
from .probe import OpenSearchProbe
from .query import SearchCompiler, close_point_in_time, execute_search


class OpenSearchAdapterFactory:
    """Discoverable factory for the sole package-owned Adapter implementation."""

    def __init__(
        self,
        *,
        service_resolver: ServiceResolver | None = None,
        client_builder: Callable[..., ClientProtocol] | None = None,
    ) -> None:
        self._service_resolver = service_resolver
        self._client_builder = client_builder

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    def create(self, context: AdapterCreateContext) -> OpenSearchAdapterRuntime:
        if context.binding.adapter_id != ADAPTER_ID:
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID, "Binding Adapter identity does not select OpenSearch"
            )
        if context.binding.adapter_contract != ADAPTER_CONTRACT_VERSION:
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID, "Binding requires an unsupported OpenSearch Adapter SPI"
            )
        if context.binding.engine_profile != ENGINE_PROFILE:
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID, "Binding Engine profile does not select OpenSearch"
            )
        settings = OpenSearchSettings.from_mapping(context.binding.settings)
        if settings.index_prefix != index_prefix(context.binding.physical_namespace):
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID,
                "OpenSearch indexPrefix must equal the normalized Binding physical namespace",
            )
        options: dict[str, Any] = {"service_resolver": self._service_resolver}
        if self._client_builder is not None:
            options["client_builder"] = self._client_builder
        handle = create_client_handle(context, **options)
        cursor_key = hmac.new(
            context.credential.reveal(), b"meridian-storage-opensearch/cursor/v1", hashlib.sha256
        ).digest()
        key_id = hashlib.sha256(cursor_key).hexdigest()[:16]
        signer = CursorSigner({key_id: cursor_key}, active_key_id=key_id, ttl_seconds=900)
        return OpenSearchAdapterRuntime(context, handle, settings, signer)


class OpenSearchAdapterRuntime:
    def __init__(
        self,
        context: AdapterCreateContext,
        handle: ClientHandle,
        settings: OpenSearchSettings,
        cursor_signer: CursorSigner,
    ) -> None:
        self._context = context
        self._handle = handle
        self._client = handle.client
        self.settings = settings
        self.cursor_signer = cursor_signer
        self._probe = OpenSearchProbe(
            self._client, settings, selected_engine_version=context.binding.engine_version
        )
        self._opened = False
        self._closed = False
        self._manifest: AdapterProbe | None = None

    def open(self) -> None:
        self._ensure_not_closed()
        if self._opened:
            return
        probe = self._probe.probe()
        binding = self._context.binding
        manifest = probe.manifest
        if manifest.engine_version != binding.engine_version:
            raise CompatibilityError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "observed OpenSearch release differs from the deployment-selected Binding lock",
            )
        if manifest.fingerprint != binding.required_capability_fingerprint:
            raise CompatibilityError(
                ErrorCode.CAPABILITY_FINGERPRINT,
                "OpenSearch capability fingerprint differs from the Binding pin",
            )
        self._manifest = probe
        self._opened = True

    def probe(self) -> AdapterProbe:
        self._ensure_open()
        # Re-run the authenticated checks; Core conformance requires deterministic probes.
        probe = self._probe.probe()
        assert self._manifest is not None
        if probe.manifest.fingerprint != self._manifest.manifest.fingerprint:
            raise CompatibilityError(
                ErrorCode.CAPABILITY_FINGERPRINT, "OpenSearch capability changed after startup"
            )
        return probe

    def verify_physical(self, resources: tuple[PhysicalResource, ...]) -> PhysicalVerification:
        self._ensure_open()
        return self._probe.verify_physical(resources)

    def open_session(self, *, transactional: bool) -> OpenSearchAdapterSession:
        self._ensure_open()
        if transactional:
            raise ValidationError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "OpenSearch search projections do not provide authoritative transactions",
            )
        session = OpenSearchAdapterSession(
            self._client,
            self._context.binding.id,
            self.settings,
            self.cursor_signer,
            max_result_bytes=self._context.binding.client.max_result_bytes,
        )
        return session

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._opened = False
        self._handle.close()

    def _ensure_not_closed(self) -> None:
        if self._closed:
            raise LifecycleError(ErrorCode.RUNTIME_CLOSED, "OpenSearch runtime is closed")

    def _ensure_open(self) -> None:
        self._ensure_not_closed()
        if not self._opened:
            raise LifecycleError(ErrorCode.RUNTIME_STATE, "OpenSearch runtime is not open")


class OpenSearchAdapterSession:
    def __init__(
        self,
        client: ClientProtocol,
        binding_id: str,
        settings: OpenSearchSettings,
        cursor_signer: CursorSigner,
        *,
        max_result_bytes: int,
    ) -> None:
        self._client = client
        self._binding_id = binding_id
        self._settings = settings
        self._cursor_signer = cursor_signer
        self._max_result_bytes = max_result_bytes
        self._closed = False

    def begin(self) -> None:
        raise ValidationError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "OpenSearch search sessions are non-transactional",
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if self._closed:
            raise LifecycleError(ErrorCode.RUNTIME_CLOSED, "OpenSearch session is closed")
        if request.binding_id != self._binding_id:
            raise ValidationError(
                ErrorCode.OPERATION_SCOPE, "request Binding does not match session"
            )
        operation = request.operation
        _validate_operation(operation)
        logical = str(operation.resources[0])
        raw_layout = self._settings.layouts.get(logical)
        if raw_layout is None:
            raise ValidationError(
                ErrorCode.PLACEMENT_UNRESOLVED,
                "Binding has no OpenSearch layout for the requested Resource",
                resource_ref=logical,
            )
        layout = ResourceLayout.from_mapping(raw_layout)
        compiler = SearchCompiler(
            layout,
            read_alias(self._settings.index_prefix, logical),
            self._cursor_signer,
            limits=self._settings.limits,
            pit_enabled=self._settings.pit_enabled,
        )
        scope = scope_fingerprint(request.context.tenant, request.context.scope)
        query_plan = operation.input.get("queryPlan")
        if query_plan is not None:
            if not isinstance(query_plan, Mapping):
                raise ValidationError(ErrorCode.OPERATION_INVALID, "queryPlan must be an object")
            query_operation = QueryOperation.from_mapping(query_plan)
            plan_fingerprint = _query_plan_fingerprint(query_plan)
            compiled = compiler.compile_query_operation(
                query_operation,
                plan_fingerprint=plan_fingerprint,
                registry_fingerprint=request.registry_fingerprint,
                scope_fingerprint=scope,
                schema_fingerprints={layout.resource_ref: layout.schema_fingerprint},
            )
        else:
            plan_fingerprint = _mapping_plan_fingerprint(operation)
            compiled = compiler.compile_mapping(
                operation.input,
                plan_fingerprint=plan_fingerprint,
                registry_fingerprint=request.registry_fingerprint,
                scope_fingerprint=scope,
            )
        raw, opened_pit = execute_search(self._client, compiled)
        response_value = dict(raw)
        if opened_pit is not None and "pit_id" not in response_value:
            response_value["pit_id"] = opened_pit
        response = compiler.normalize(compiled, response_value)
        if response.pit_id is not None and not response.has_more:
            close_point_in_time(self._client, response.pit_id)
        data: dict[str, JsonValue] = {**response.data, "cursor": response.cursor}
        result_bytes = len(canonical_json_bytes(data))
        if result_bytes > self._context_max_result_bytes:
            raise ValidationError(
                ErrorCode.OPERATION_RESULT_LIMIT, "normalized search result exceeds Binding limit"
            )
        return ExecutionResult(
            data,
            result_bytes=result_bytes,
            provenance={
                "adapter": ADAPTER_ID,
                "consistency": "eventual",
                "pagination": "point-in-time" if response.pit_id else "live-keyset",
                "schemaFingerprint": layout.schema_fingerprint,
            },
        )

    @property
    def _context_max_result_bytes(self) -> int:
        return self._max_result_bytes

    def commit(self) -> None:
        raise ValidationError(
            ErrorCode.CAPABILITY_UNSUPPORTED, "OpenSearch search sessions cannot commit"
        )

    def rollback(self) -> None:
        raise ValidationError(
            ErrorCode.CAPABILITY_UNSUPPORTED, "OpenSearch search sessions cannot roll back"
        )

    def close(self) -> None:
        self._closed = True


def _validate_operation(operation: Operation) -> None:
    if (
        operation.catalog != "structured"
        or operation.operation_contract != SEARCH_OPERATION_CONTRACT
        or operation.operation_version != SEARCH_OPERATION_VERSION
        or not operation.read_only
        or len(operation.resources) != 1
    ):
        raise ValidationError(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            "OpenSearch accepts only one-resource structured.search@1.0.0 read Operations",
            operation_contract=operation.operation_contract,
        )


def _mapping_plan_fingerprint(operation: Operation) -> str:
    input_value = dict(operation.input)
    input_value.pop("cursor", None)
    return sha256_fingerprint(
        {
            "formatVersion": "meridian.opensearch.mapping-search-plan.v1",
            "resource": str(operation.resources[0]),
            "input": input_value,
        }
    )


def _query_plan_fingerprint(value: Mapping[str, object]) -> str:
    # Query V1's canonical plan includes its cursor. Pagination must bind to the
    # logical plan excluding the cursor so every page verifies against page one.
    mutable = _thaw(cast(JsonValue, value))
    assert isinstance(mutable, dict)
    page = mutable.get("page")
    if isinstance(page, dict):
        page["cursor"] = None
    return sha256_fingerprint(cast(JsonValue, mutable))


def _thaw(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def expected_capability_fingerprint(
    engine_version: str,
    settings: OpenSearchSettings,
) -> str:
    """Return the exact startup pin that Platform IaC places in a Binding."""

    return cast(
        str,
        capability_manifest(
            engine_version,
            pit_enabled=settings.pit_enabled,
            limits=settings.limits,
        ).fingerprint,
    )


__all__ = [
    "OpenSearchAdapterFactory",
    "OpenSearchAdapterRuntime",
    "OpenSearchAdapterSession",
    "expected_capability_fingerprint",
]
