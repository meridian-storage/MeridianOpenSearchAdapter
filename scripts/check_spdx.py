# SPDX-License-Identifier: Apache-2.0
"""Verify license material and SPDX markers on repository-authored files."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PREFIXES = {
    ".py": "# SPDX-License-Identifier: Apache-2.0",
    ".sh": "# SPDX-License-Identifier: Apache-2.0",
    ".toml": "# SPDX-License-Identifier: Apache-2.0",
    ".yml": "# SPDX-License-Identifier: Apache-2.0",
    ".yaml": "# SPDX-License-Identifier: Apache-2.0",
    ".md": "<!-- SPDX-License-Identifier: Apache-2.0 -->",
}


def authored_files() -> list[Path]:
    roots = [
        ROOT / ".github",
        ROOT / "docs",
        ROOT / "scripts",
        ROOT / "src",
        ROOT / "tests",
    ]
    files = [
        ROOT / name
        for name in (
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "README.md",
            "RELEASING.md",
            "SECURITY.md",
            "pyproject.toml",
        )
    ]
    for root in roots:
        if root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(set(files))


def main() -> int:
    failures: list[str] = []
    if not (ROOT / "LICENSE").is_file() or not (ROOT / "NOTICE").is_file():
        failures.append("LICENSE and NOTICE are required")
    for path in authored_files():
        expected = PREFIXES.get(path.suffix)
        if path.name == "Dockerfile":
            expected = PREFIXES[".py"]
        if expected is None:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()[:3]
        if expected not in lines:
            failures.append(f"{path.relative_to(ROOT)}: missing {expected}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"SPDX verification passed for {len(authored_files())} authored files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
