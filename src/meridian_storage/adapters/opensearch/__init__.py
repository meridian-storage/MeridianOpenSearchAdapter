# SPDX-License-Identifier: Apache-2.0
"""Meridian V1 OpenSearch search-projection Adapter."""

from ._version import __version__
from .analysis import AnalysisDefinition, analyzer_for_language, build_analysis
from .configuration import AdapterLimits, OpenSearchSettings, TopologyExpectation
from .descriptor import (
    ADAPTER_CONTRACT_VERSION,
    ADAPTER_ID,
    SEARCH_OPERATION_CONTRACT,
    adapter_descriptor,
    capability_manifest,
    query_capabilities,
)
from .generation import GenerationManager, GenerationRecord, MigrationPlan
from .mapping import FieldLayout, MappingCompiler, ResourceLayout
from .projection import (
    BulkProjectionResult,
    MutationAcknowledgement,
    MutationOutcome,
    ProjectionExecutor,
    ProjectionMutation,
)
from .query import CompiledSearch, SearchCompiler, SearchResponse
from .runtime import (
    OpenSearchAdapterFactory,
    OpenSearchAdapterRuntime,
    OpenSearchAdapterSession,
    expected_capability_fingerprint,
)

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "ADAPTER_ID",
    "SEARCH_OPERATION_CONTRACT",
    "AdapterLimits",
    "AnalysisDefinition",
    "BulkProjectionResult",
    "CompiledSearch",
    "FieldLayout",
    "GenerationManager",
    "GenerationRecord",
    "MappingCompiler",
    "MigrationPlan",
    "MutationAcknowledgement",
    "MutationOutcome",
    "OpenSearchAdapterFactory",
    "OpenSearchAdapterRuntime",
    "OpenSearchAdapterSession",
    "OpenSearchSettings",
    "ProjectionExecutor",
    "ProjectionMutation",
    "ResourceLayout",
    "SearchCompiler",
    "SearchResponse",
    "TopologyExpectation",
    "__version__",
    "adapter_descriptor",
    "analyzer_for_language",
    "build_analysis",
    "capability_manifest",
    "expected_capability_fingerprint",
    "query_capabilities",
]
