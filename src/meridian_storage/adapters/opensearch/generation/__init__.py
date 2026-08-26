# SPDX-License-Identifier: Apache-2.0
"""Adapter-owned index generation and atomic alias migration hooks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from meridian_storage.errors import ConflictError, ErrorCode, ValidationError
from meridian_storage.semantics import JsonValue, sha256_fingerprint

from .._canonical import generation_name, read_alias, safe_token, write_alias
from ..client import ClientProtocol
from ..errors import translate_engine_error
from ..mapping import ResourceLayout


class GenerationState(StrEnum):
    CREATED = "created"
    VERIFIED = "verified"
    ACTIVE = "active"
    RETAINED = "retained"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class GenerationRecord:
    resource_ref: str
    generation: int
    mapping_fingerprint: str
    state: GenerationState
    previous_generation: int | None = None

    @property
    def evidence_fingerprint(self) -> str:
        return sha256_fingerprint(
            {
                "resourceRef": self.resource_ref,
                "generation": self.generation,
                "mappingFingerprint": self.mapping_fingerprint,
                "state": self.state.value,
                "previousGeneration": self.previous_generation,
            }
        )


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    resource_ref: str
    current_generation: int | None
    target_generation: int
    target_mapping_fingerprint: str
    steps: tuple[str, ...] = (
        "create-generation",
        "load-authoritative-scan",
        "tail-outbox",
        "verify-counts-and-digests",
        "atomic-alias-cutover",
        "retain-previous-generation",
    )

    @property
    def fingerprint(self) -> str:
        return sha256_fingerprint(
            {
                "formatVersion": "meridian.opensearch.migration-plan.v1",
                "resourceRef": self.resource_ref,
                "currentGeneration": self.current_generation,
                "targetGeneration": self.target_generation,
                "targetMappingFingerprint": self.target_mapping_fingerprint,
                "steps": list(self.steps),
            }
        )


class GenerationManager:
    """Execute only physical index hooks; orchestration remains with Platform/Vangu IaC."""

    def __init__(self, client: ClientProtocol, index_prefix: str) -> None:
        self._client = client
        self._prefix = safe_token(index_prefix, "index prefix", maximum=80)

    def plan(
        self,
        layout: ResourceLayout,
        *,
        target_generation: int,
        current_generation: int | None = None,
    ) -> MigrationPlan:
        if current_generation is not None and target_generation <= current_generation:
            raise ValueError("target generation must be newer than the current generation")
        generation_name(self._prefix, layout.resource_ref, target_generation)
        return MigrationPlan(
            resource_ref=layout.resource_ref,
            current_generation=current_generation,
            target_generation=target_generation,
            target_mapping_fingerprint=layout.mapping_fingerprint,
        )

    def create(self, layout: ResourceLayout, generation: int) -> GenerationRecord:
        index = generation_name(self._prefix, layout.resource_ref, generation)
        try:
            self._client.indices.create(index=index, body=dict(layout.index_body))
        except Exception as exc:
            translate_engine_error(exc, resource_ref=layout.resource_ref)
        return GenerationRecord(
            layout.resource_ref, generation, layout.mapping_fingerprint, GenerationState.CREATED
        )

    def verify(self, layout: ResourceLayout, generation: int) -> GenerationRecord:
        index = generation_name(self._prefix, layout.resource_ref, generation)
        try:
            response = self._client.indices.get_mapping(index=index)
        except Exception as exc:
            translate_engine_error(exc, resource_ref=layout.resource_ref)
        mapping = _index_payload(response, index).get("mappings")
        if not isinstance(mapping, Mapping):
            raise ValidationError(ErrorCode.PHYSICAL_FINGERPRINT, "index mapping is absent")
        metadata = mapping.get("_meta")
        actual = metadata.get("mappingFingerprint") if isinstance(metadata, Mapping) else None
        if actual != layout.mapping_fingerprint:
            raise ValidationError(
                ErrorCode.PHYSICAL_FINGERPRINT,
                "index mapping fingerprint differs from the compiled projection",
                resource_ref=layout.resource_ref,
            )
        return GenerationRecord(
            layout.resource_ref, generation, layout.mapping_fingerprint, GenerationState.VERIFIED
        )

    def activate(self, layout: ResourceLayout, generation: int) -> GenerationRecord:
        self.verify(layout, generation)
        index = generation_name(self._prefix, layout.resource_ref, generation)
        read = read_alias(self._prefix, layout.resource_ref)
        write = write_alias(self._prefix, layout.resource_ref)
        actions: list[dict[str, JsonValue]] = []
        previous: int | None = None
        for alias in (read, write):
            for current in self._alias_indices(alias):
                if current != index:
                    actions.append({"remove": {"index": current, "alias": alias}})
                    parsed = _generation_suffix(current)
                    previous = max(previous or 0, parsed or 0) or previous
            actions.append(
                {
                    "add": {
                        "index": index,
                        "alias": alias,
                        "is_write_index": alias == write,
                    }
                }
            )
        try:
            self._client.indices.update_aliases(body={"actions": actions})
        except Exception as exc:
            translate_engine_error(exc, resource_ref=layout.resource_ref)
        return GenerationRecord(
            layout.resource_ref,
            generation,
            layout.mapping_fingerprint,
            GenerationState.ACTIVE,
            previous_generation=previous,
        )

    def rollback(self, layout: ResourceLayout, retained_generation: int) -> GenerationRecord:
        """Atomically reactivate an explicitly retained, fingerprint-compatible generation."""

        result = self.activate(layout, retained_generation)
        return GenerationRecord(
            result.resource_ref,
            result.generation,
            result.mapping_fingerprint,
            GenerationState.ACTIVE,
            result.previous_generation,
        )

    def retire(
        self,
        layout: ResourceLayout,
        generation: int,
        *,
        expected_mapping_fingerprint: str,
    ) -> GenerationRecord:
        """Delete only an explicitly named inactive generation after fingerprint verification."""

        index = generation_name(self._prefix, layout.resource_ref, generation)
        if expected_mapping_fingerprint != layout.mapping_fingerprint:
            raise ConflictError(
                ErrorCode.PHYSICAL_FINGERPRINT,
                "retirement fingerprint does not match the bound layout",
            )
        active = set(self._alias_indices(read_alias(self._prefix, layout.resource_ref))) | set(
            self._alias_indices(write_alias(self._prefix, layout.resource_ref))
        )
        if index in active:
            raise ConflictError(ErrorCode.RUNTIME_STATE, "an active generation cannot be retired")
        self.verify(layout, generation)
        try:
            self._client.indices.delete(index=index)
        except Exception as exc:
            translate_engine_error(exc, resource_ref=layout.resource_ref)
        return GenerationRecord(
            layout.resource_ref, generation, layout.mapping_fingerprint, GenerationState.RETIRED
        )

    def _alias_indices(self, alias: str) -> tuple[str, ...]:
        try:
            if not self._client.indices.exists_alias(name=alias):
                return ()
            response = self._client.indices.get_alias(name=alias)
        except Exception as exc:
            translate_engine_error(exc)
        if not isinstance(response, Mapping):
            raise ValidationError(ErrorCode.ADAPTER_FAILURE, "alias response must be an object")
        return tuple(sorted(str(index) for index in response))


def _index_payload(response: object, index: str) -> Mapping[str, object]:
    if not isinstance(response, Mapping):
        raise ValidationError(ErrorCode.ADAPTER_FAILURE, "index response must be an object")
    value = response.get(index)
    if not isinstance(value, Mapping):
        raise ValidationError(ErrorCode.ADAPTER_FAILURE, "index response omitted its target")
    return cast(Mapping[str, object], value)


def _generation_suffix(index: str) -> int | None:
    marker = index.rsplit("-g", 1)
    if len(marker) != 2 or not marker[1].isdigit():
        return None
    return int(marker[1])


__all__ = [
    "GenerationManager",
    "GenerationRecord",
    "GenerationState",
    "MigrationPlan",
]
