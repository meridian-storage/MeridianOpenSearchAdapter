<!-- SPDX-License-Identifier: Apache-2.0 -->

# Release selection and validation — 1.1.0

The deployment independently selects server images and installed Meridian distributions.
OpenSearch 1.1.0 consumes public Core `>=1.1,<2` (the released v1 provenance semantics),
Query `>=1.0.3,<2` and Semantics `>=2.0.1,<3` (the repaired dependency metadata and their
existing public API majors). These bounds do not assert behavior for untested combinations.
`requirements.lock` is the exact hash-verified CI recipe, not a runtime library membership test.
No sibling sources or dependency overrides participate in validation.

## Gate inventory

| Surface / variant | Classification | 1.1.0 behavior |
| --- | --- | --- |
| `descriptor.TESTED_ENGINE_VERSIONS`, legacy `SUPPORTED_ENGINE_VERSIONS`, wire `supportedEngineVersions`, ledger `supportedPatchVersions` | Historical release metadata | Preserved v1 content; never used as a release membership predicate. |
| `probe.snapshot` / both topology settings | API and feature requirements | Authenticated info, node/plugin and health responses; required plugins on every node; selected data/eligible node floor and health. Unlisted nonempty observed releases are accepted. |
| `probe.verify_layout` and `verify_physical` | Schema, API and placement requirements | Compiled Schema/mapping fingerprint, one read/write generation, ICU analyzer output, replica floor, bounded search/keyset API and enabled PIT API. Probe requests use `match_none`; temporary PIT is closed. |
| Factory / Core SPI and search session | Contract requirements | Adapter id/profile/SPI identity, closed settings, one bound Resource, supported search Operation, scopes, bounded queries, no authoritative transactions or unrestricted DSL. |
| Binding `engineVersion`, required capability/physical fingerprints; repeated probe | Deployment integrity / drift | Selected versus observed server equality remains required. A different declared release requires a new deployment manifest; it is not rejected for absence from a compiled table. |
| Endpoint, identity, TLS, credential resolver and engine errors | Authentication / authorization | Existing origin-only endpoint, trust and mutual TLS requirements and typed/redacted 401/403 failures remain. No fallback or credential bypass. |
| `compatibilityFingerprint` setting | Closed configuration syntax | Legacy optional syntactically validated fingerprint remains; it never represented an installed-package baseline or release gate. |
| Package metadata and artifact validator | API bounds and artifact integrity | Major bounds above replace historical exact recipes. Package name/version and distribution boundary still verified; exact published bytes remain immutable. |
| Single-node / three-node with replicas | Deployment topology | Same adapter checks, different explicit topology floors. Full semantic suite runs on both; node loss must fail the configured floor while replica search succeeds. |
| Managed / external endpoint ownership | Caller/provider boundary | The adapter receives the same Binding and never provisions/adopts resources. Metadata round trips cover both caller modes; no cloud managed-provider conformance is claimed. |
| Docker helpers and CI/release scripts | Test recipe / image integrity | Independently selected full image reference must carry a SHA256 digest. Same semantics run on each selected release; CI publication follows protected main. |

The additive `contracts/release-selection.v1.json` fixture records canonical descriptors,
Bindings, manifests and hashes against released Core. Existing SPI, Operation and serialized
manifest/config formats retain their versions and meanings. Release provenance is reported as
`selectedEngineVersion`, `observedEngineVersion` and `observedAdapterVersion` in probe evidence;
standalone probes explicitly report an unavailable selection. Missing observations fail rather
than copying configuration into evidence. A declared or observed version alone proves no feature.

## Validation recipes

All recipes select Core **1.1.0**, Query **1.0.3**, Semantics **2.0.1** and opensearch-py **3.2.0**.
Exact wheel/sdist hashes are in `compatibility.json`, and the full hash lock is `requirements.lock`.

| Engine / matching ICU plugin | Immutable upstream image index | Required profiles |
| --- | --- | --- |
| OpenSearch 2.19.1 / analysis-icu 2.19.1 | `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4` | Single node; three nodes; node loss |
| OpenSearch 2.19.3 / analysis-icu 2.19.3 | `sha256:e96cc6ae1500a073d973c0906f30f7cf4d9c461f32f855f9242a2da933660cdd` | Single node; three nodes; node loss |

2.19.3 is deliberately absent from the preserved historical descriptor table. It runs the same
real-engine EN/ZH/Arabic relevance, facets/highlights, scope isolation, live/PIT cursor, stale event,
tombstone, real partial bulk mapping rejection, rebuild, alias cutover and rollback acceptance.
The additional synthetic `99.0.0-deployment` metadata test only establishes removal of the
membership gate; it makes no engine compatibility claim.

```sh
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-build-isolation .
python -m pip check
python -m pytest -q --cov=meridian_storage.adapters.opensearch --cov-branch tests/unit tests/contract tests/packaging
MERIDIAN_PYTHON=python ./scripts/run-single-conformance.sh
MERIDIAN_PYTHON=python ./scripts/run-cluster-conformance.sh
export OPENSEARCH_VERSION=2.19.3
export OPENSEARCH_IMAGE=opensearchproject/opensearch:2.19.3@sha256:e96cc6ae1500a073d973c0906f30f7cf4d9c461f32f855f9242a2da933660cdd
MERIDIAN_PYTHON=python ./scripts/run-single-conformance.sh
MERIDIAN_PYTHON=python ./scripts/run-cluster-conformance.sh
```

CI's reproducible-package gate depends on both image recipes and all existing checks. The local
Docker fixtures disable engine security only within disposable test containers; TLS and credential
failure regressions use the existing client tests. No external provider or production deployment,
untested server release, or future Meridian release is certified by this result.

## Implementation validation

Local Python 3.13.3 validation passed 146 unit/contract/packaging tests with zero skips and
90.57% branch-inclusive coverage. Ruff, formatting, strict mypy, SPDX and distribution-boundary
checks passed; two builds with the same SOURCE_DATE_EPOCH produced identical wheel/sdist hashes.
Single-node semantics passed once per engine recipe. The 2.19.3 cluster passed full semantics,
three-node replication and deliberate node-loss vectors, with zero skips; the initial 2.19.1
cluster passed replication and node loss. CI additionally requires the full semantic vector on
both clusters and Python 3.12, 3.13 and 3.14 before the protected package gate can pass.
The public release must be installed and rechecked after CI publication before task completion.
