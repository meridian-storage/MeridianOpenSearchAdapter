<!-- SPDX-License-Identifier: Apache-2.0 -->

# Conformance evidence

The test pyramid is executable and deterministic:

- unit tests cover mapping shapes, unrestricted analysis metadata, projections, every released
  logical query family, signed cursor modes, generation hooks, probe drift, runtime lifecycle,
  credential handling, redaction, and fail-closed malformed responses;
- contract tests validate the four Draft 2020-12 schemas, compatibility pins, Core descriptor,
  distribution metadata, and sole entry point;
- packaging tests enforce Apache material, SPDX headers, one repository/one distribution, and the
  sole adapter namespace;
- the single-node Docker vector uses digest-pinned OpenSearch 2.19.1 with `analysis-icu` and exercises
  real mapping creation, bulk indexing, stale versions, tombstones, EN/ZH/Arabic ICU relevance,
  scope isolation, facets, highlights, live/PIT pagination, strict mapping failure, migration,
  rollback, retirement, and physical health;
- the cluster vector verifies three eligible/data nodes, green health, one replica, then deliberately
  stops a node and proves the topology probe fails closed while replicated search returns with zero
  failed shards.

Run the real profiles:

```console
./scripts/run-single-conformance.sh
./scripts/run-cluster-conformance.sh
```

Both scripts use isolated Compose resources, wait for engine health, and remove their containers and
ephemeral volumes on exit. CI runs the same commands. Machine-readable vector descriptions live in
[`evidence/conformance-vectors.json`](../evidence/conformance-vectors.json); release artifact hashes
are emitted by `scripts/generate_evidence.py` after the reproducible build check.
