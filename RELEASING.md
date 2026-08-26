<!-- SPDX-License-Identifier: Apache-2.0 -->

# Releasing

Releases are created from a green, protected `main` branch.

1. Update `_version.py`, `CHANGELOG.md`, `compatibility.json`, contracts, and evidence together.
2. Run all static, unit, contract, packaging, single-node, cluster, and node-loss checks.
3. Build twice with the same `SOURCE_DATE_EPOCH`; verify identical wheel and sdist hashes.
4. Run `twine check` and `scripts/verify_artifacts.py` on the distributions.
5. Merge the reviewed pull request without bypassing required checks.
6. Push an annotated `vX.Y.Z` tag. The release workflow rebuilds, verifies, emits an SPDX SBOM,
   attests provenance, and creates the GitHub release.

PyPI publishing is disabled unless the repository variable
`PYPI_TRUSTED_PUBLISHING_ENABLED` is exactly `true`. The first PyPI publication requires the owner
to establish the namespace and trusted publisher; credentials or MFA must never be bypassed. After
that gate is established, GitHub Actions owns subsequent publication.

GitHub release artifacts are ordinary public package releases. This project performs no ASF
incubation, donation, trademark, or Apache publication workflow.
