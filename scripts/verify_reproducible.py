# SPDX-License-Identifier: Apache-2.0
"""Build twice with a fixed epoch and require byte-identical distributions."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _build(target: Path, epoch: str) -> dict[str, str]:
    environment = dict(os.environ)
    environment["SOURCE_DATE_EPOCH"] = epoch
    subprocess.run(  # noqa: S603 - fixed interpreter and module, no shell
        [sys.executable, "-m", "build", "--outdir", str(target)],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(target.iterdir())
        if path.is_file()
    }


def main() -> int:
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "1787702400")
    with tempfile.TemporaryDirectory(prefix="meridian-opensearch-repro-") as temporary:
        parent = Path(temporary)
        first = _build(parent / "first", epoch)
        second = _build(parent / "second", epoch)
    if first != second or len(first) != 2:
        print(f"non-reproducible artifacts: first={first!r}, second={second!r}", file=sys.stderr)
        return 1
    for name, digest in first.items():
        print(f"{digest}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
