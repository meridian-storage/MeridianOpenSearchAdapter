<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

## 1.1.0

- Use public contract-compatible Core, Query and Semantics dependencies.
- Treat historical server versions as provenance; retain deployment lock and fingerprint checks.
- Verify plugins on each node, analyzer output, read/write alias agreement, bounded search/keyset
  and deployment-enabled PIT APIs. Report independently selected and observed release provenance.
- Run unchanged semantic regressions plus partial bulk failure on OpenSearch 2.19.1 and 2.19.3
  in disposable single-node and replicated cluster profiles.


All notable changes follow semantic versioning.

## 1.0.0 - 2026-08-26

- Add the Meridian V1 OpenSearch capability descriptor and Query V1 translator.
- Add closed search/document mappings with unrestricted ICU language metadata and EN/ZH initial
  conformance.
- Add externally versioned projections, tombstones, bounded bulk outcomes, scope isolation,
  stable signed pagination, optional PIT, facets, highlights, and logical result normalization.
- Add generation migration hooks, authenticated health/compatibility probes, and stable redacted
  error translation.
- Add unit, contract, packaging, real single-node, three-node replication, node-loss, and Meridian
  Core adapter-conformance suites.
