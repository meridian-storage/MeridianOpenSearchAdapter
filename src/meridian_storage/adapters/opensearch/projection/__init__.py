# SPDX-License-Identifier: Apache-2.0
"""Idempotent externally versioned search-projection writes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from meridian_storage.errors import ErrorCode, ValidationError
from meridian_storage.semantics import JsonValue, canonical_json_bytes, sha256_fingerprint

from .._canonical import (
    canonical_record_id,
    document_id,
    json_value,
    require_fingerprint,
    safe_token,
)
from ..client import ClientProtocol
from ..configuration import AdapterLimits
from ..errors import OpenSearchErrorCode, translate_engine_error
from ..mapping import (
    HIDDEN_DELETED,
    HIDDEN_DOCUMENT_ID,
    HIDDEN_EVENT_ID,
    HIDDEN_PROJECTED_AT,
    HIDDEN_RECORD_ID,
    HIDDEN_RESOURCE,
    HIDDEN_SCHEMA_FINGERPRINT,
    HIDDEN_SCHEMA_VERSION,
    HIDDEN_SCOPE,
    HIDDEN_SOURCE_DIGEST,
    HIDDEN_SOURCE_SEQUENCE,
    HIDDEN_SOURCE_VERSION,
    FieldLayout,
    ResourceLayout,
)


class MutationOutcome(StrEnum):
    APPLIED = "applied"
    STALE = "stale"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


@dataclass(frozen=True, slots=True)
class ProjectionMutation:
    resource_ref: str
    record_id: JsonValue
    source_sequence: int
    source_version: str
    schema_version: str
    schema_fingerprint: str
    scope_fingerprint: str
    event_id: str
    payload: Mapping[str, object] | None
    projected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        safe_token(self.resource_ref, "projection resource", maximum=768)
        canonical_record_id(self.record_id)
        if (
            isinstance(self.source_sequence, bool)
            or not isinstance(self.source_sequence, int)
            or not 1 <= self.source_sequence <= 9_223_372_036_854_775_807
        ):
            raise ValueError("source sequence must be a positive signed 64-bit integer")
        safe_token(self.source_version, "source version")
        safe_token(self.schema_version, "Schema version")
        require_fingerprint(self.schema_fingerprint, "Schema fingerprint")
        require_fingerprint(self.scope_fingerprint, "scope fingerprint")
        safe_token(self.event_id, "event id")
        if self.payload is not None and any(not isinstance(key, str) for key in self.payload):
            raise TypeError("projection payload keys must be strings")
        if self.projected_at.tzinfo is None or self.projected_at.utcoffset() is None:
            raise ValueError("projection time must be timezone-aware")
        object.__setattr__(self, "projected_at", self.projected_at.astimezone(UTC))

    @property
    def deleted(self) -> bool:
        return self.payload is None


@dataclass(frozen=True, slots=True)
class MutationAcknowledgement:
    event_id: str
    record_id: str
    source_sequence: int
    outcome: MutationOutcome
    code: str | None = None

    @property
    def retryable(self) -> bool:
        return self.outcome is MutationOutcome.RETRYABLE


@dataclass(frozen=True, slots=True)
class BulkProjectionResult:
    acknowledgements: tuple[MutationAcknowledgement, ...]
    refresh_policy: str
    took_ms: int | None = None

    @property
    def applied(self) -> int:
        return sum(item.outcome is MutationOutcome.APPLIED for item in self.acknowledgements)

    @property
    def stale(self) -> int:
        return sum(item.outcome is MutationOutcome.STALE for item in self.acknowledgements)

    @property
    def retryable(self) -> tuple[MutationAcknowledgement, ...]:
        return tuple(item for item in self.acknowledgements if item.retryable)

    @property
    def permanent(self) -> tuple[MutationAcknowledgement, ...]:
        return tuple(
            item for item in self.acknowledgements if item.outcome is MutationOutcome.PERMANENT
        )


class ProjectionExecutor:
    def __init__(
        self,
        client: ClientProtocol,
        layout: ResourceLayout,
        write_target: str,
        *,
        limits: AdapterLimits | None = None,
    ) -> None:
        self._client = client
        self.layout = layout
        self.write_target = safe_token(write_target, "write target", maximum=255)
        self.limits = limits or AdapterLimits()

    def encode(self, mutation: ProjectionMutation) -> tuple[str, dict[str, JsonValue]]:
        if mutation.resource_ref != self.layout.resource_ref:
            raise ValidationError(
                ErrorCode.OPERATION_SCOPE, "projection mutation targets another bound resource"
            )
        if mutation.schema_fingerprint != self.layout.schema_fingerprint:
            raise ValidationError(
                ErrorCode.PHYSICAL_FINGERPRINT,
                "projection Schema fingerprint differs from the active mapping",
            )
        logical_record = canonical_record_id(mutation.record_id)
        physical_id = document_id(
            mutation.scope_fingerprint, mutation.resource_ref, mutation.record_id
        )
        normalized_payload: dict[str, JsonValue] = {}
        if mutation.payload is not None:
            unknown = set(mutation.payload) - {
                layout.source_field or layout.logical_name for layout in self.layout.fields.values()
            }
            if unknown:
                raise ValidationError(
                    ErrorCode.OPERATION_INVALID,
                    "projection payload contains fields absent from the bound Schema: "
                    f"{sorted(unknown)!r}",
                )
            for name, layout in self.layout.fields.items():
                if layout.json_pointer is not None:
                    source = mutation.payload.get(cast(str, layout.source_field))
                    value = _json_pointer(source, layout.json_pointer)
                else:
                    if name not in mutation.payload:
                        continue
                    value = mutation.payload[name]
                if value is not _MISSING:
                    normalized_payload[layout.physical_name] = _encode_field(value, layout)
        source_digest = sha256_fingerprint(
            cast(JsonValue, normalized_payload if mutation.payload is not None else None)
        )
        document: dict[str, JsonValue] = {
            **normalized_payload,
            HIDDEN_SCOPE: mutation.scope_fingerprint,
            HIDDEN_RESOURCE: mutation.resource_ref,
            HIDDEN_RECORD_ID: logical_record,
            HIDDEN_SOURCE_VERSION: mutation.source_version,
            HIDDEN_SOURCE_SEQUENCE: mutation.source_sequence,
            HIDDEN_SCHEMA_VERSION: mutation.schema_version,
            HIDDEN_SCHEMA_FINGERPRINT: mutation.schema_fingerprint,
            HIDDEN_SOURCE_DIGEST: source_digest,
            HIDDEN_EVENT_ID: mutation.event_id,
            HIDDEN_PROJECTED_AT: mutation.projected_at.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            HIDDEN_DOCUMENT_ID: physical_id,
            HIDDEN_DELETED: mutation.deleted,
        }
        return physical_id, document

    def project(
        self,
        mutation: ProjectionMutation,
        *,
        refresh_policy: str = "false",
    ) -> MutationAcknowledgement:
        refresh = _refresh(refresh_policy)
        physical_id, document = self.encode(mutation)
        try:
            self._client.index(
                index=self.write_target,
                id=physical_id,
                body=document,
                version=mutation.source_sequence,
                version_type="external_gte",
                refresh=refresh,
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status == 409:
                return _ack(mutation, MutationOutcome.STALE, OpenSearchErrorCode.VERSION_CONFLICT)
            translate_engine_error(
                exc,
                operation_contract="meridian.projection.opensearch.v1",
                resource_ref=mutation.resource_ref,
            )
        return _ack(mutation, MutationOutcome.APPLIED)

    def bulk(
        self,
        mutations: Sequence[ProjectionMutation],
        *,
        refresh_policy: str = "false",
    ) -> BulkProjectionResult:
        refresh = _refresh(refresh_policy)
        if not mutations or len(mutations) > self.limits.max_bulk_actions:
            raise ValidationError(
                ErrorCode.OPERATION_RESULT_LIMIT,
                f"bulk projection requires 1 to {self.limits.max_bulk_actions} mutations",
            )
        body: list[dict[str, JsonValue]] = []
        for mutation in mutations:
            physical_id, document = self.encode(mutation)
            body.append(
                {
                    "index": {
                        "_index": self.write_target,
                        "_id": physical_id,
                        "version": mutation.source_sequence,
                        "version_type": "external_gte",
                    }
                }
            )
            body.append(document)
        encoded_size = sum(len(canonical_json_bytes(cast(JsonValue, item))) + 1 for item in body)
        if encoded_size > self.limits.max_bulk_bytes:
            raise ValidationError(
                ErrorCode.OPERATION_RESULT_LIMIT,
                f"bulk projection exceeds {self.limits.max_bulk_bytes} encoded bytes",
            )
        try:
            response = self._client.bulk(body=body, refresh=refresh)
        except Exception as exc:
            translate_engine_error(exc, operation_contract="meridian.projection.opensearch.v1")
        raw_items = response.get("items")
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            raise ValidationError(
                ErrorCode.ADAPTER_FAILURE, "OpenSearch bulk response omitted per-item outcomes"
            )
        if len(raw_items) != len(mutations):
            raise ValidationError(
                ErrorCode.ADAPTER_FAILURE, "OpenSearch bulk response item count is inconsistent"
            )
        acknowledgements = tuple(
            _classify_bulk(mutation, item)
            for mutation, item in zip(mutations, raw_items, strict=True)
        )
        took = response.get("took")
        return BulkProjectionResult(
            acknowledgements,
            refresh,
            took if isinstance(took, int) and not isinstance(took, bool) and took >= 0 else None,
        )


_MISSING = object()


def _json_pointer(value: object, pointer: str) -> object:
    current = value
    for segment in pointer.split("/")[1:]:
        token = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            current = current.get(token, _MISSING)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def _encode_field(value: object, layout: FieldLayout) -> JsonValue:
    normalized = json_value(value)
    if layout.cardinality == "many":
        if not isinstance(normalized, list):
            raise ValidationError(
                ErrorCode.OPERATION_INVALID,
                f"field {layout.logical_name!r} requires an array",
            )
        values = normalized
    else:
        if isinstance(normalized, list) and layout.logical_kind not in {"json", "wgs84Point"}:
            raise ValidationError(
                ErrorCode.OPERATION_INVALID,
                f"field {layout.logical_name!r} does not accept an array",
            )
        values = None
    if layout.logical_kind == "wgs84Point" and (
        not isinstance(normalized, list)
        or len(normalized) != 2
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in normalized)
    ):
        raise ValidationError(
            ErrorCode.OPERATION_INVALID,
            f"field {layout.logical_name!r} requires [longitude, latitude]",
        )
    if layout.logical_kind in {"recordRef", "objectRef"}:
        if values is not None:
            return [canonical_json_bytes(item).decode("utf-8") for item in values]
        return canonical_json_bytes(normalized).decode("utf-8")
    return normalized


def _refresh(value: str) -> str:
    if value not in {"false", "wait_for"}:
        raise ValueError("refresh policy must be false or wait_for")
    return value


def _ack(
    mutation: ProjectionMutation,
    outcome: MutationOutcome,
    code: str | None = None,
) -> MutationAcknowledgement:
    return MutationAcknowledgement(
        event_id=mutation.event_id,
        record_id=canonical_record_id(mutation.record_id),
        source_sequence=mutation.source_sequence,
        outcome=outcome,
        code=None if code is None else str(code),
    )


def _classify_bulk(mutation: ProjectionMutation, raw_item: object) -> MutationAcknowledgement:
    if not isinstance(raw_item, Mapping) or len(raw_item) != 1:
        return _ack(mutation, MutationOutcome.PERMANENT, ErrorCode.ADAPTER_FAILURE)
    item = next(iter(raw_item.values()))
    if not isinstance(item, Mapping):
        return _ack(mutation, MutationOutcome.PERMANENT, ErrorCode.ADAPTER_FAILURE)
    status = item.get("status")
    if status in {200, 201}:
        return _ack(mutation, MutationOutcome.APPLIED)
    if status == 409:
        return _ack(mutation, MutationOutcome.STALE, OpenSearchErrorCode.VERSION_CONFLICT)
    if status == 429:
        return _ack(mutation, MutationOutcome.RETRYABLE, OpenSearchErrorCode.BULK_REJECTED)
    if status in {408, 502, 503, 504} or (
        isinstance(status, int) and not isinstance(status, bool) and status >= 500
    ):
        return _ack(mutation, MutationOutcome.RETRYABLE, ErrorCode.ADAPTER_FAILURE)
    return _ack(mutation, MutationOutcome.PERMANENT, OpenSearchErrorCode.MAPPING_CONFLICT)


__all__ = [
    "BulkProjectionResult",
    "MutationAcknowledgement",
    "MutationOutcome",
    "ProjectionExecutor",
    "ProjectionMutation",
]
