<!-- SPDX-License-Identifier: Apache-2.0 -->

# Meridian OpenSearch Adapter

`meridian-storage-opensearch` is the independently released Meridian V1 adapter for
eventually consistent `structured` Catalog search projections on OpenSearch.

The adapter owns OpenSearch translation, mappings, analyzers, indexing behavior, aliases,
health probes, and normalized failures. It does not provision clusters, expose OpenSearch DSL,
or provide authoritative document CRUD. Platform or Vangu IaC owns engine selection,
credentials, topology, migration execution, recovery, and lifecycle.

Implementation and release evidence are being completed under Feishu task `[02m-07]`.
