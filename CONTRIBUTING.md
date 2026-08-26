<!-- SPDX-License-Identifier: Apache-2.0 -->

# Contributing

Contributions must preserve the package boundary and public contract:

- keep exactly one distribution, `meridian-storage-opensearch`, in this repository;
- preserve the PEP 420 namespace `meridian_storage.adapters.opensearch`;
- expose only released Meridian mapping-first Operations and logical results;
- keep OpenSearch DSL, mappings, endpoints, credentials, aliases, and lifecycle private;
- do not add Catalog types beyond the external Meridian registry; and
- include `SPDX-License-Identifier: Apache-2.0` in source and test files.

Run before opening a pull request:

```console
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m mypy src/meridian_storage/adapters/opensearch
python -m pytest -q --cov=meridian_storage.adapters.opensearch --cov-branch \
  tests/unit tests/contract tests/packaging
python -m build
python scripts/verify_artifacts.py dist
```

Changes to mappings, cursor formats, capability declarations, error codes, compatibility ranges,
or lifecycle hooks require design review and an update to deterministic conformance vectors.
Never place production endpoints, credentials, tenant data, or physical index names in tests,
issues, logs, or evidence.
