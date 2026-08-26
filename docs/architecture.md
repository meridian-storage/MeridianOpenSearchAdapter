<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture and authority boundary

This adapter is a derived search-projection implementation behind Meridian V1 Core and Query
contracts. It is not a Catalog and is never an authoritative record store.

```text
Authoritative store/outbox
  -> Platform/Vangu migration or projection worker
  -> ProjectionMutation -> external_gte index/tombstone -> private write alias

Mapping-first Expression or serialized Operation
  -> Meridian Query plan + scope context
  -> SearchCompiler -> bounded private OpenSearch request -> private read alias
  -> fail-closed response validation -> logical RecordRef search result
```

## Adapter-owned components

- `descriptor`: released Core/Query capabilities, denials, engine profile, and limits.
- `analysis`: deterministic ICU analyzer settings; English stemming; Chinese and future-language
  ICU paths.
- `mapping`: hashed field names, strict dynamic mappings, declared document JSON paths, and embedded
  schema/profile/analysis/mapping fingerprints.
- `projection`: deterministic scoped document IDs, full evidence metadata, external sequencing,
  tombstones, bounded bulk classification, and no authoritative CRUD surface.
- `query`: the released logical operator allowlist, mandatory scope/Resource/deleted filters,
  signed cursor verification, optional PIT, and logical normalization.
- `generation`: physical generation hooks and atomic read/write alias changes.
- `probe`: engine patch, ICU plugin, topology, health, mapping, alias, analysis, and replica checks.
- `runtime`: the single Meridian adapter entry point, lifecycle, and credential-derived cursor key.

## IaC-owned components

Platform/Vangu IaC chooses and provisions/references the engine, compiles and pins Bindings, resolves
services and secrets, establishes identity/ACLs, orchestrates backfills and cutovers, retains or
retires generations, monitors projection lag, and owns recovery and cluster lifecycle. Adapter hooks
perform narrowly named engine operations; they do not make orchestration decisions.

## Public data boundary

Results contain logical collection/record references, source version/sequence, scores, sort values,
declared highlights/facets, total relation, consistency class, and a signed opaque cursor. They do
not contain endpoints, credentials, OpenSearch DSL, `_index`, physical `_id`, aliases, mappings, or
engine error reasons.

The external Meridian Catalog registry remains exactly `structured`, `object`, `cache`, `evidence`,
and `streaming`. This package implements only the `structured` search projection capability.
