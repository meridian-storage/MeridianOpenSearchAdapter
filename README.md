<!-- SPDX-License-Identifier: Apache-2.0 -->

# Meridian OpenSearch Adapter

`meridian-storage-opensearch` is the independently released Meridian V1 adapter for eventually
consistent `structured` Catalog search projections on OpenSearch. It is one Python distribution
in one public Apache-2.0 repository.

## Capabilities

The sole operation surface is `meridian.structured.search@1.0.0`. The adapter provides:

- closed, fingerprinted mappings for full-text and declared document-path projections;
- unrestricted BCP 47 language metadata with ICU fallback, with English and Chinese initial
  conformance;
- deterministic document identities, externally versioned upserts, tombstones, and bounded bulk
  partial outcomes;
- mapping-first Query V1 translation for the released logical operator allowlist;
- mandatory tenant/scope, Resource, and tombstone filters;
- signed `search_after` cursors, plus deployment-conditional point-in-time cursors;
- logical `RecordRef` results, bounded facets/highlights, and fail-closed partial-search handling;
- explicit generation create/verify/activate/rollback/retire hooks with atomic alias cutover; and
- authenticated engine, ICU plugin, topology, health, alias, analyzer, replica, and mapping probes.

The exact capability manifest, historical tested engine releases, and hard limits are available from
`adapter_descriptor()`, `capability_manifest()`, and `query_capabilities()`.

## Install

```console
python -m pip install meridian-storage-opensearch==1.1.0
```

The distribution consumes Core `>=1.1,<2`, Query `>=1.0.3,<2`, and Semantics `>=2.0.1,<3`.
These bounds retain the required public API majors and the released dependency repair.
The deployment selects exact versions; the CI recipe locks Core 1.1.0, Query 1.0.3 and
Semantics 2.0.1 with hashes. Historical tested engine versions never gate startup.
Untested combinations remain unverified. Artifact hashes and design revisions are recorded in
[`compatibility.json`](compatibility.json).

## Configure through a Meridian Binding

Platform or Vangu IaC supplies the endpoint or service reference, secret references, compiled
layouts, topology expectations, and exact capability fingerprint. Credentials never belong in an
endpoint URL or settings document.

```json
{
  "adapterId": "org.meridian.storage.opensearch",
  "engineProfile": "opensearch",
  "engineVersion": "2.19.1",
  "serviceRef": "platform://search/case-index",
  "physicalNamespace": "meridian",
  "requiredCapabilityFingerprint": "sha256:<64 hex characters>",
  "settings": {
    "indexPrefix": "meridian",
    "layouts": {
      "structured:investigation.articles": {
        "formatVersion": "meridian.opensearch.layout.v1"
      }
    },
    "requiredPlugins": ["analysis-icu"],
    "pitEnabled": true
  }
}
```

`layouts` must contain the complete value returned by `MappingCompiler.compile(...).to_dict()`;
the abbreviated object above only illustrates placement. Use
`expected_capability_fingerprint(engine_version, settings)` when producing the Binding pin.

## Projection and search APIs

Adapter-internal projection workers use `ProjectionExecutor` with an IaC-selected write alias.
Consumers do not receive this object and never see aliases, index names, mappings, analyzers, or
OpenSearch DSL. Consumers submit released mapping-first Expressions or serialized Operations
through Meridian Core.

The projection is always derived and eventually consistent. A successful authoritative write does
not imply immediate search visibility. `refresh=wait_for` is available to controlled projection
and conformance workflows; it is not an authoritative transaction guarantee.

## Ownership boundary

This package owns translation, mappings, analysis definitions, indexing requests, cursor behavior,
generation hooks, probes, and redacted engine-failure normalization. It does not create a Search or
Projection Catalog and does not expose native queries, regex, wildcard/query-string syntax, scripts,
credentials, endpoints, or physical lifecycle details.

Platform/Vangu IaC owns engine selection, provisioning/reference, state, identity and ACLs,
migration orchestration, recovery, and lifecycle. See [architecture](docs/architecture.md),
[migrations](docs/migrations.md), and the [failure model](docs/failure-model.md).

## Verify

```console
python -m pytest -q tests/unit tests/contract tests/packaging
./scripts/run-single-conformance.sh
./scripts/run-cluster-conformance.sh
python -m build
python scripts/verify_artifacts.py dist
```

The Docker harness locks OpenSearch 2.19.1 and 2.19.3 by digest and installs matching `analysis-icu`
plugin. Cluster conformance uses three eligible/data nodes, one replica, green-health verification,
and a deliberate node loss. See [conformance evidence](docs/conformance.md).

## License and security

Licensed under the Apache License 2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE), and
[SECURITY.md](SECURITY.md).

See [release validation](docs/release-validation.md) for the gate inventory and verified combinations.

## Build and release (Jumbo)

This repository is jumbo-managed (Jumbo Build & Versioning Standard,
section 3.5): resolution, builds, and releases run through jumbo, never
ad-hoc pip/uv installs.

```sh
jumbo lock   # resolve internal packages from the JumboIndex, third-party from PyPI
jumbo build  # build + tests at the resolved closure
```

The internal dependencies (`meridian-storage-core`, `meridian-storage-query`, `meridian-storage-semantics`) are resolved from the JumboIndex;
the lock records the exact promoted build of each. Consumers likewise
resolve this package (`meridian-storage-opensearch`) from the JumboIndex. Releases are dispatch-only through `.github/workflows/jumbo-publish.yml`;
as a public package, external publication is driven by the jumbo-computed
version, and every artifact's SHA-256 is recorded in the append-only
JumboIndex.
