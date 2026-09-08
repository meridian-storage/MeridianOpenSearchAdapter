# SPDX-License-Identifier: Apache-2.0
"""Emit deterministic release evidence from verified distributions and locked inputs."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from verify_artifacts import verify

ROOT = Path(__file__).resolve().parents[1]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: generate_evidence.py DIST OUTPUT", file=sys.stderr)
        return 2
    artifacts = verify(Path(sys.argv[1]).resolve())
    output = Path(sys.argv[2]).resolve()
    contracts = {
        path.name: _digest(path) for path in sorted((ROOT / "contracts").glob("*.schema.json"))
    }
    evidence = {
        "formatVersion": "meridian.opensearch.release-evidence.v1",
        "adapterVersion": "1.1.0",
        "artifacts": artifacts,
        "compatibilitySha256": _digest(ROOT / "compatibility.json"),
        "contracts": contracts,
        "conformance": {
            "evidenceDocument": "docs/release-validation.md",
            "requiredProfiles": ["single-node", "three-node", "node-loss"],
            "selectedEngineRecipes": json.loads(
                (ROOT / "compatibility.json").read_text(encoding="utf-8")
            )["engineProfile"],
            "note": "Recipes are provenance; consult CI and release-validation for results.",
        },
        "sourceCommit": os.environ.get("GITHUB_SHA", "unavailable"),
        "releaseRunId": os.environ.get("GITHUB_RUN_ID", "unavailable"),
        "license": "Apache-2.0",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
