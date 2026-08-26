# SPDX-License-Identifier: Apache-2.0
"""Deterministic logical-schema to OpenSearch mapping compilation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from meridian_storage.semantics import (
    Cardinality,
    DocumentProfile,
    FieldDefinition,
    FullTextProfile,
    JsonValue,
    LogicalKind,
    ResourceReference,
    SchemaDocument,
    SemanticKind,
    sha256_fingerprint,
)

from .._canonical import field_name, path_name, require_fingerprint, safe_token
from ..analysis import analyzer_for_language, build_analysis, normalize_language

HIDDEN_SCOPE = "m_scope"
HIDDEN_RESOURCE = "m_resource"
HIDDEN_RECORD_ID = "m_record_id"
HIDDEN_SOURCE_VERSION = "m_source_version"
HIDDEN_SOURCE_SEQUENCE = "m_source_sequence"
HIDDEN_SCHEMA_VERSION = "m_schema_version"
HIDDEN_SCHEMA_FINGERPRINT = "m_schema_fingerprint"
HIDDEN_SOURCE_DIGEST = "m_source_digest"
HIDDEN_EVENT_ID = "m_event_id"
HIDDEN_PROJECTED_AT = "m_projected_at"
HIDDEN_DOCUMENT_ID = "m_document_id"
HIDDEN_DELETED = "m_deleted"

_LANGUAGE_ANNOTATIONS = (
    "org.meridian.search/language",
    "org.meridian.language/v1",
)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an array")
    result = tuple(cast(str, item) for item in value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return result


@dataclass(frozen=True, slots=True)
class FieldLayout:
    logical_name: str
    physical_name: str
    logical_kind: str
    cardinality: str
    modes: tuple[str, ...]
    language: str | None = None
    source_field: str | None = None
    json_pointer: str | None = None

    def __post_init__(self) -> None:
        safe_token(self.logical_name, "logical field", maximum=1_024)
        safe_token(self.physical_name, "physical field", maximum=128)
        LogicalKind(self.logical_kind)
        Cardinality(self.cardinality)
        modes = tuple(sorted(set(self.modes)))
        allowed = {"exact", "exists", "facet", "full-text", "highlight", "prefix", "range"}
        if not modes or set(modes) - allowed:
            raise ValueError("field layout contains unsupported or empty query modes")
        object.__setattr__(self, "modes", modes)
        if self.language is not None:
            object.__setattr__(self, "language", normalize_language(self.language))
        if self.json_pointer is not None and not self.json_pointer.startswith("/"):
            raise ValueError("indexed document path must be an RFC 6901 JSON pointer")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "logicalName": self.logical_name,
            "physicalName": self.physical_name,
            "logicalKind": self.logical_kind,
            "cardinality": self.cardinality,
            "modes": list(self.modes),
            "language": self.language,
            "sourceField": self.source_field,
            "jsonPointer": self.json_pointer,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FieldLayout:
        required = {
            "logicalName",
            "physicalName",
            "logicalKind",
            "cardinality",
            "modes",
            "language",
            "sourceField",
            "jsonPointer",
        }
        if set(value) != required:
            raise ValueError("field layout contains unknown or missing fields")
        return cls(
            logical_name=cast(str, value["logicalName"]),
            physical_name=cast(str, value["physicalName"]),
            logical_kind=cast(str, value["logicalKind"]),
            cardinality=cast(str, value["cardinality"]),
            modes=_strings(value["modes"], "field modes"),
            language=cast(str | None, value["language"]),
            source_field=cast(str | None, value["sourceField"]),
            json_pointer=cast(str | None, value["jsonPointer"]),
        )


@dataclass(frozen=True, slots=True)
class ResourceLayout:
    resource_ref: str
    schema_ref: str
    schema_fingerprint: str
    profile_kind: str
    profile_fingerprint: str
    analysis_fingerprint: str
    mapping_fingerprint: str
    fields: Mapping[str, FieldLayout]
    identity: tuple[str, ...]
    source_fields: tuple[str, ...]
    facet_fields: tuple[str, ...]
    highlight_fields: tuple[str, ...]
    ranking: str
    index_body: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        resource = ResourceReference.parse(self.resource_ref, catalog="structured")
        if resource.catalog.value != "structured":
            raise ValueError("OpenSearch search projections belong to the structured Catalog")
        object.__setattr__(self, "resource_ref", resource.canonical)
        for name in (
            "schema_fingerprint",
            "profile_fingerprint",
            "analysis_fingerprint",
            "mapping_fingerprint",
        ):
            require_fingerprint(getattr(self, name), name.replace("_", " "))
        fields = dict(sorted(self.fields.items()))
        if not fields or any(key != value.logical_name for key, value in fields.items()):
            raise ValueError("layout fields must be keyed by logical name")
        physical = [item.physical_name for item in fields.values()]
        if len(physical) != len(set(physical)):
            raise ValueError("layout physical field names must be unique")
        object.__setattr__(self, "fields", MappingProxyType(fields))
        for name in ("identity", "source_fields", "facet_fields", "highlight_fields"):
            values = tuple(getattr(self, name))
            if len(values) != len(set(values)) or not set(values) <= set(fields):
                raise ValueError(f"layout {name} must reference unique compiled fields")
            object.__setattr__(self, name, values)
        object.__setattr__(self, "index_body", MappingProxyType(dict(self.index_body)))

    @property
    def fingerprint(self) -> str:
        return self.mapping_fingerprint

    def field(self, logical_name: str, *, mode: str | None = None) -> FieldLayout:
        try:
            result = self.fields[logical_name]
        except KeyError as exc:
            raise ValueError(
                f"field {logical_name!r} is not in the bound search projection"
            ) from exc
        if mode is not None and mode not in result.modes:
            raise ValueError(f"field {logical_name!r} does not support {mode}")
        return result

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "formatVersion": "meridian.opensearch.layout.v1",
            "resourceRef": self.resource_ref,
            "schemaRef": self.schema_ref,
            "schemaFingerprint": self.schema_fingerprint,
            "profileKind": self.profile_kind,
            "profileFingerprint": self.profile_fingerprint,
            "analysisFingerprint": self.analysis_fingerprint,
            "mappingFingerprint": self.mapping_fingerprint,
            "fields": {key: value.to_dict() for key, value in self.fields.items()},
            "identity": list(self.identity),
            "sourceFields": list(self.source_fields),
            "facetFields": list(self.facet_fields),
            "highlightFields": list(self.highlight_fields),
            "ranking": self.ranking,
            "indexBody": dict(self.index_body),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ResourceLayout:
        required = {
            "formatVersion",
            "resourceRef",
            "schemaRef",
            "schemaFingerprint",
            "profileKind",
            "profileFingerprint",
            "analysisFingerprint",
            "mappingFingerprint",
            "fields",
            "identity",
            "sourceFields",
            "facetFields",
            "highlightFields",
            "ranking",
            "indexBody",
        }
        if set(value) != required or value.get("formatVersion") != "meridian.opensearch.layout.v1":
            raise ValueError("unsupported or malformed OpenSearch resource layout")
        raw_fields = _mapping(value["fields"], "layout fields")
        fields = {
            key: FieldLayout.from_mapping(_mapping(item, f"layout field {key}"))
            for key, item in raw_fields.items()
        }
        body = _mapping(value["indexBody"], "index body")
        return cls(
            resource_ref=cast(str, value["resourceRef"]),
            schema_ref=cast(str, value["schemaRef"]),
            schema_fingerprint=cast(str, value["schemaFingerprint"]),
            profile_kind=cast(str, value["profileKind"]),
            profile_fingerprint=cast(str, value["profileFingerprint"]),
            analysis_fingerprint=cast(str, value["analysisFingerprint"]),
            mapping_fingerprint=cast(str, value["mappingFingerprint"]),
            fields=fields,
            identity=_strings(value["identity"], "identity"),
            source_fields=_strings(value["sourceFields"], "source fields"),
            facet_fields=_strings(value["facetFields"], "facet fields"),
            highlight_fields=_strings(value["highlightFields"], "highlight fields"),
            ranking=cast(str, value["ranking"]),
            index_body=cast(Mapping[str, JsonValue], body),
        )


class MappingCompiler:
    """Compile portable document/search metadata into a closed physical layout."""

    def __init__(
        self,
        *,
        shards: int = 1,
        replicas: int = 0,
        refresh_interval: str = "1s",
    ) -> None:
        if isinstance(shards, bool) or not 1 <= shards <= 1_024:
            raise ValueError("shards must be between 1 and 1024")
        if isinstance(replicas, bool) or not 0 <= replicas <= 20:
            raise ValueError("replicas must be between 0 and 20")
        self._shards = shards
        self._replicas = replicas
        self._refresh_interval = safe_token(refresh_interval, "refresh interval", maximum=16)

    def compile(
        self,
        resource: ResourceReference | str,
        schema: SchemaDocument,
        profile: FullTextProfile | DocumentProfile | None = None,
    ) -> ResourceLayout:
        resource_ref = ResourceReference.parse(resource, catalog="structured")
        if resource_ref.catalog.value != "structured":
            raise ValueError("OpenSearch projection resources must be structured Collections")
        selected = profile or schema.profile
        if not isinstance(selected, (FullTextProfile, DocumentProfile)):
            raise ValueError("OpenSearch requires a full-text or document semantic profile")
        if schema.semantic_kind not in {SemanticKind.SEARCH, SemanticKind.DOCUMENT}:
            raise ValueError("OpenSearch projects only search or document semantic Schemas")
        names = set(schema.field_map)
        if not set(schema.identity) <= names or not schema.identity:
            raise ValueError("projected Schema requires a complete logical identity")

        source_fields: tuple[str, ...]
        facets: tuple[str, ...]
        highlights: tuple[str, ...]
        ranking: str
        languages: tuple[str, ...]
        document_paths: tuple[tuple[str, str], ...] = ()
        document_body: str | None = None
        if isinstance(selected, FullTextProfile):
            source_fields = selected.source_fields
            facets = selected.facets
            highlights = selected.highlights
            ranking = selected.ranking
            languages = selected.language_hints
            referenced = set(source_fields) | set(facets) | set(highlights)
            if selected.normalized_field is not None:
                referenced.add(selected.normalized_field)
            if not referenced <= names:
                raise ValueError("full-text profile references fields absent from its Schema")
            if any(
                schema.field_map[name].logical_type.kind
                not in {LogicalKind.STRING, LogicalKind.ENUM}
                for name in source_fields
            ):
                raise ValueError("full-text source fields must be string or enum fields")
        else:
            document_body = selected.body_field
            source_fields = ()
            facets = ()
            highlights = ()
            ranking = "bm25"
            languages = ("en", "zh")
            if selected.body_field not in names:
                raise ValueError("document body field is absent from its Schema")
            body = schema.field_map[selected.body_field]
            if body.logical_type.kind is not LogicalKind.JSON:
                raise ValueError("document body field must have logical type json")
            document_paths = tuple(
                (f"{selected.body_field}#{pointer}", pointer) for pointer in selected.indexed_paths
            )

        analysis = build_analysis(languages)
        properties: dict[str, JsonValue] = _hidden_properties()
        layouts: dict[str, FieldLayout] = {}
        for definition in schema.fields:
            language = _field_language(definition)
            is_source = definition.name in source_fields
            is_facet = definition.name in facets
            is_highlight = definition.name in highlights
            physical = field_name(definition.name)
            mapping, modes = _field_mapping(
                definition,
                full_text=is_source,
                facet=is_facet,
                highlight=is_highlight,
                language=language,
            )
            properties[physical] = mapping
            layouts[definition.name] = FieldLayout(
                logical_name=definition.name,
                physical_name=physical,
                logical_kind=definition.logical_type.kind.value,
                cardinality=definition.cardinality.value,
                modes=modes,
                language=language,
            )
        for logical, pointer in document_paths:
            assert document_body is not None
            physical = path_name(document_body, pointer)
            properties[physical] = {
                "type": "keyword",
                "ignore_above": 32_766,
            }
            layouts[logical] = FieldLayout(
                logical_name=logical,
                physical_name=physical,
                logical_kind=LogicalKind.STRING.value,
                cardinality=Cardinality.ONE.value,
                modes=("exact", "exists", "prefix"),
                source_field=document_body,
                json_pointer=pointer,
            )

        profile_value = cast(JsonValue, selected.to_dict())
        profile_fingerprint = sha256_fingerprint(profile_value)
        mapping_basis: JsonValue = {
            "resourceRef": resource_ref.canonical,
            "schemaFingerprint": schema.fingerprint,
            "profileFingerprint": profile_fingerprint,
            "analysisFingerprint": analysis.fingerprint,
            "fieldLayouts": {key: item.to_dict() for key, item in sorted(layouts.items())},
            "ranking": ranking,
        }
        mapping_fingerprint = sha256_fingerprint(mapping_basis)
        index_body: dict[str, JsonValue] = {
            "settings": {
                "number_of_shards": self._shards,
                "number_of_replicas": self._replicas,
                "refresh_interval": self._refresh_interval,
                "analysis": analysis.settings,
                "mapping": {"total_fields": {"limit": len(properties) + 32}},
            },
            "mappings": {
                "dynamic": "strict",
                "_meta": {
                    "formatVersion": "meridian.opensearch.mapping.v1",
                    "resourceRef": resource_ref.canonical,
                    "schemaFingerprint": schema.fingerprint,
                    "profileFingerprint": profile_fingerprint,
                    "analysisFingerprint": analysis.fingerprint,
                    "mappingFingerprint": mapping_fingerprint,
                },
                "properties": properties,
            },
        }
        return ResourceLayout(
            resource_ref=resource_ref.canonical,
            schema_ref=schema.ref.canonical,
            schema_fingerprint=schema.fingerprint,
            profile_kind=selected.kind.value,
            profile_fingerprint=profile_fingerprint,
            analysis_fingerprint=analysis.fingerprint,
            mapping_fingerprint=mapping_fingerprint,
            fields=layouts,
            identity=tuple(schema.identity),
            source_fields=tuple(source_fields),
            facet_fields=tuple(facets),
            highlight_fields=tuple(highlights),
            ranking=ranking,
            index_body=index_body,
        )


def _field_language(field: FieldDefinition) -> str | None:
    values = [field.annotations[key] for key in _LANGUAGE_ANNOTATIONS if key in field.annotations]
    if len(values) > 1 and len(set(values)) > 1:
        raise ValueError(f"field {field.name!r} has conflicting language annotations")
    if not values:
        return None
    language = values[0]
    if not isinstance(language, str):
        raise TypeError("field language annotation must be a BCP 47 string")
    return normalize_language(language)


def _field_mapping(
    field: FieldDefinition,
    *,
    full_text: bool,
    facet: bool,
    highlight: bool,
    language: str | None,
) -> tuple[JsonValue, tuple[str, ...]]:
    kind = field.logical_type.kind
    modes = {"exists"}
    mapping: dict[str, JsonValue]
    if kind is LogicalKind.BOOLEAN:
        mapping = {"type": "boolean"}
        modes.add("exact")
    elif kind in {LogicalKind.INT8, LogicalKind.INT16, LogicalKind.INT32, LogicalKind.INT64}:
        mapping = {"type": "long"}
        modes.update(("exact", "range"))
    elif kind is LogicalKind.DECIMAL:
        precision = cast(int, field.logical_type.precision)
        scale = cast(int, field.logical_type.scale)
        if precision > 18 or scale > 18:
            mapping = {"type": "keyword"}
            modes.add("exact")
        else:
            mapping = {"type": "scaled_float", "scaling_factor": 10**scale}
            modes.update(("exact", "range"))
    elif kind is LogicalKind.FLOAT64:
        mapping = {"type": "double"}
        modes.update(("exact", "range"))
    elif kind in {LogicalKind.STRING, LogicalKind.ENUM} and full_text:
        mapping = {
            "type": "text",
            "analyzer": analyzer_for_language(language),
            "search_analyzer": analyzer_for_language(language),
            "fields": {
                "exact": {
                    "type": "keyword",
                    "ignore_above": 32_766,
                }
            },
        }
        modes.update(("exact", "full-text", "prefix"))
        if highlight:
            modes.add("highlight")
        if facet:
            modes.add("facet")
    elif kind in {LogicalKind.STRING, LogicalKind.UUID, LogicalKind.ENUM}:
        mapping = {
            "type": "keyword",
            "ignore_above": 32_766,
        }
        modes.update(("exact", "prefix"))
        if facet:
            modes.add("facet")
    elif kind is LogicalKind.BYTES:
        mapping = {"type": "binary", "doc_values": False}
    elif kind is LogicalKind.UTC_TIMESTAMP:
        mapping = {"type": "date", "format": "strict_date_optional_time_nanos"}
        modes.update(("exact", "range"))
    elif kind is LogicalKind.DATE:
        mapping = {"type": "date", "format": "strict_date"}
        modes.update(("exact", "range"))
    elif kind is LogicalKind.DURATION:
        mapping = {"type": "long"}
        modes.update(("exact", "range"))
    elif kind is LogicalKind.WGS84_POINT:
        mapping = {"type": "geo_point"}
    elif kind is LogicalKind.JSON:
        mapping = {"type": "object", "enabled": False}
    elif kind in {LogicalKind.RECORD_REF, LogicalKind.OBJECT_REF}:
        mapping = {"type": "keyword", "index": False, "doc_values": False}
    else:
        raise ValueError(f"logical type {kind.value!r} is not projectable into OpenSearch")
    if facet and "facet" not in modes:
        raise ValueError(f"field {field.name!r} cannot be used as a facet")
    if highlight and "highlight" not in modes:
        raise ValueError(f"field {field.name!r} cannot be highlighted")
    return cast(JsonValue, mapping), tuple(sorted(modes))


def _hidden_properties() -> dict[str, JsonValue]:
    keyword: JsonValue = {"type": "keyword", "ignore_above": 32_766}
    return {
        HIDDEN_SCOPE: keyword,
        HIDDEN_RESOURCE: keyword,
        HIDDEN_RECORD_ID: {"type": "keyword", "index": False, "doc_values": False},
        HIDDEN_SOURCE_VERSION: keyword,
        HIDDEN_SOURCE_SEQUENCE: {"type": "long"},
        HIDDEN_SCHEMA_VERSION: keyword,
        HIDDEN_SCHEMA_FINGERPRINT: keyword,
        HIDDEN_SOURCE_DIGEST: keyword,
        HIDDEN_EVENT_ID: keyword,
        HIDDEN_PROJECTED_AT: {"type": "date", "format": "strict_date_optional_time_nanos"},
        HIDDEN_DOCUMENT_ID: keyword,
        HIDDEN_DELETED: {"type": "boolean"},
    }


__all__ = [
    "HIDDEN_DELETED",
    "HIDDEN_DOCUMENT_ID",
    "HIDDEN_EVENT_ID",
    "HIDDEN_PROJECTED_AT",
    "HIDDEN_RECORD_ID",
    "HIDDEN_RESOURCE",
    "HIDDEN_SCHEMA_FINGERPRINT",
    "HIDDEN_SCHEMA_VERSION",
    "HIDDEN_SCOPE",
    "HIDDEN_SOURCE_DIGEST",
    "HIDDEN_SOURCE_SEQUENCE",
    "HIDDEN_SOURCE_VERSION",
    "FieldLayout",
    "MappingCompiler",
    "ResourceLayout",
]
