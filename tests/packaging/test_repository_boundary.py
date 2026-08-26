# SPDX-License-Identifier: Apache-2.0
"""Repository license, namespace, and distribution boundary checks."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_required_apache_material_and_spdx_headers() -> None:
    assert "Apache License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Meridian OpenSearch Adapter" in (ROOT / "NOTICE").read_text(encoding="utf-8")
    files = [
        *ROOT.glob("src/**/*.py"),
        *ROOT.glob("tests/**/*.py"),
        *ROOT.glob("scripts/**/*.py"),
    ]
    assert files
    for path in files:
        assert path.read_text(encoding="utf-8").splitlines()[0] == (
            "# SPDX-License-Identifier: Apache-2.0"
        )


def test_repository_contains_exactly_one_python_distribution() -> None:
    pyprojects = [path for path in ROOT.rglob("pyproject.toml") if ".venv" not in path.parts]
    assert pyprojects == [ROOT / "pyproject.toml"]
    source_packages = [
        path
        for path in (ROOT / "src/meridian_storage").iterdir()
        if path.is_dir() and path.name != "adapters"
    ]
    assert source_packages == []
    adapters = [
        path.name for path in (ROOT / "src/meridian_storage/adapters").iterdir() if path.is_dir()
    ]
    assert adapters == ["opensearch"]
