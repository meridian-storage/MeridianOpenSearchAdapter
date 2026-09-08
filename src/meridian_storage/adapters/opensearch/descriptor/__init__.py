# SPDX-License-Identifier: Apache-2.0
"""Versioned Meridian Core and Query capability declarations."""

from __future__ import annotations

from meridian_storage.query import QueryCapabilities
from meridian_storage.spi import AdapterDescriptor, CapabilityManifest, OperationCapability

from ..configuration import AdapterLimits

ADAPTER_ID = "org.meridian.storage.opensearch"
ADAPTER_CONTRACT_VERSION = "1.0.0"
SEARCH_OPERATION_CONTRACT = "meridian.structured.search"
SEARCH_OPERATION_VERSION = "1.0.0"
ENGINE_PROFILE = "opensearch"
# Historical release metadata, never a preview or startup membership gate.
# Keep the exported legacy name and serialized v1 field for existing consumers.
TESTED_ENGINE_VERSIONS = (
    "2.17.0",
    "2.18.0",
    "2.19.0",
    "2.19.1",
    "2.19.2",
    "3.0.0",
    "3.1.0",
    "3.2.0",
)
SUPPORTED_ENGINE_VERSIONS = TESTED_ENGINE_VERSIONS

_OPERATORS = (
    "and",
    "eq",
    "field",
    "fullText",
    "gt",
    "gte",
    "in",
    "isNull",
    "literal",
    "lt",
    "lte",
    "ne",
    "not",
    "notIn",
    "or",
    "order",
    "prefix",
    "search",
    "timestampRange",
)
_NATIVE_SEMANTICS = (
    "query.operation.search",
    "query.operator.and",
    "query.operator.eq",
    "query.operator.field",
    "query.operator.fullText",
    "query.operator.gt",
    "query.operator.gte",
    "query.operator.in",
    "query.operator.isNull",
    "query.operator.literal",
    "query.operator.lt",
    "query.operator.lte",
    "query.operator.ne",
    "query.operator.not",
    "query.operator.notIn",
    "query.operator.or",
    "query.operator.prefix",
    "query.operator.timestampRange",
    "query.pagination.keyset",
)


def adapter_descriptor(limits: AdapterLimits | None = None) -> AdapterDescriptor:
    selected = limits or AdapterLimits()
    return AdapterDescriptor(
        adapter_id=ADAPTER_ID,
        adapter_contract_version=ADAPTER_CONTRACT_VERSION,
        driver="opensearch-py/3",
        supported_engine_versions={ENGINE_PROFILE: SUPPORTED_ENGINE_VERSIONS},
        capabilities=(
            OperationCapability(
                operation_contract=SEARCH_OPERATION_CONTRACT,
                operation_versions=(SEARCH_OPERATION_VERSION,),
                guarantees=(
                    "eventual-consistency",
                    "logical-record-references",
                    "scope-isolation",
                    "single-binding",
                    "stable-keyset",
                ),
                limits={
                    "bulkActions": selected.max_bulk_actions,
                    "bulkBytes": selected.max_bulk_bytes,
                    "facetBuckets": selected.facet_bucket_limit,
                    "facets": selected.max_facets,
                    "filterClauses": selected.max_filter_clauses,
                    "highlightFragments": selected.highlight_fragment_limit,
                    "highlights": selected.max_highlights,
                    "pageSize": selected.max_page_size,
                    "queryBytes": selected.max_query_bytes,
                },
                cursor_behavior="signed-live-search-after",
                migration_behavior="external-generation-hooks-with-atomic-alias-cutover",
                health_probes=(
                    "analysis-resources",
                    "authenticated",
                    "cluster-health",
                    "mapping-fingerprint",
                    "plugins",
                    "topology",
                ),
                extensions={
                    "analysisProfile": "multilingual-icu/v1",
                    "initialLanguages": ["en", "zh"],
                    "languageMetadata": "unrestricted-bcp47-with-icu-fallback",
                    "pointInTime": "deployment-conditional",
                    "projection": {
                        "consistency": "eventual",
                        "externalVersioning": "external_gte",
                        "writes": ["upsert", "tombstone-delete", "bounded-bulk"],
                    },
                    "rebuild": "generation-and-alias-hooks",
                    "denied": [
                        "authoritative-crud",
                        "authoritative-transactions",
                        "audit-atomicity",
                        "cas",
                        "consumer-dsl",
                        "consumer-regex",
                        "consumer-scripts",
                        "cross-binding-join",
                    ],
                },
            ),
        ),
    )


def capability_manifest(
    engine_version: str,
    *,
    pit_enabled: bool,
    limits: AdapterLimits | None = None,
) -> CapabilityManifest:
    descriptor = adapter_descriptor(limits)
    return CapabilityManifest(
        descriptor=descriptor,
        engine_profile=ENGINE_PROFILE,
        engine_version=engine_version,
        extensions={
            "analysisProfile": "multilingual-icu/v1",
            "pointInTime": "enabled" if pit_enabled else "disabled",
            "consistency": "eventual",
        },
    )


def query_capabilities(
    *, limits: AdapterLimits | None = None, pit_enabled: bool = False
) -> QueryCapabilities:
    selected = limits or AdapterLimits()
    features = [
        "analyzer:icu",
        "facets",
        "highlights",
        "live-keyset",
        "ranking:bm25",
    ]
    if pit_enabled:
        features.append("point-in-time")
    return QueryCapabilities(
        adapter_id=ADAPTER_ID,
        operations=("search",),
        native_semantics=_NATIVE_SEMANTICS,
        operators=_OPERATORS,
        logical_types=(
            "boolean",
            "date",
            "decimal",
            "duration",
            "enum",
            "float64",
            "int16",
            "int32",
            "int64",
            "int8",
            "string",
            "utcTimestamp",
            "uuid",
        ),
        consistency_classes=("eventual",),
        guarantees=("single-binding",),
        features=tuple(features),
        limits={
            "deadlineMs": 3_600_000,
            "facetBuckets": selected.facet_bucket_limit,
            "membershipNames": 10_000,
            "pageSize": selected.max_page_size,
            "resultBytes": 2_147_483_647,
            "resultValues": selected.max_page_size,
        },
    )


__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "ADAPTER_ID",
    "ENGINE_PROFILE",
    "SEARCH_OPERATION_CONTRACT",
    "SEARCH_OPERATION_VERSION",
    "SUPPORTED_ENGINE_VERSIONS",
    "TESTED_ENGINE_VERSIONS",
    "adapter_descriptor",
    "capability_manifest",
    "query_capabilities",
]
