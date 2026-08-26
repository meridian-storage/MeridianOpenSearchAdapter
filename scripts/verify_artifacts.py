# SPDX-License-Identifier: Apache-2.0
"""Fail closed unless built artifacts have the exact package boundary and metadata."""

from __future__ import annotations

import hashlib
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

EXPECTED_RUNTIME = {
    "meridian-storage-core==1.0.0",
    "meridian-storage-query==1.0.0",
    "meridian-storage-semantics==1.0.0",
    "opensearch-py<4,>=3.2",
}
REQUIRED_WHEEL = {
    "meridian_storage/adapters/opensearch/py.typed",
    "meridian_storage/adapters/opensearch/compatibility.json",
    "meridian_storage/adapters/opensearch/contracts/meridian.opensearch.layout.v1.schema.json",
}


def _safe(names: list[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe archive member: {name}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(directory: Path) -> dict[str, str]:
    wheels = sorted(directory.glob("meridian_storage_opensearch-1.0.0-*.whl"))
    sdists = sorted(directory.glob("meridian_storage_opensearch-1.0.0.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("dist must contain exactly one 1.0.0 wheel and one 1.0.0 sdist")
    wheel, sdist = wheels[0], sdists[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        _safe(names)
        if not set(names) >= REQUIRED_WHEEL:
            raise ValueError("wheel omits typed marker, compatibility ledger, or contracts")
        source_roots = {
            "/".join(PurePosixPath(name).parts[:3])
            for name in names
            if name.startswith("meridian_storage/") and name.endswith(".py")
        }
        if source_roots != {"meridian_storage/adapters/opensearch"}:
            raise ValueError(f"wheel contains an unexpected source package: {source_roots!r}")
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        if metadata["Name"] != "meridian-storage-opensearch" or metadata["Version"] != "1.0.0":
            raise ValueError("wheel name/version metadata differs from the release")
        if metadata["License-Expression"] != "Apache-2.0":
            raise ValueError("wheel License-Expression must be Apache-2.0")
        runtime = {
            item for item in metadata.get_all("Requires-Dist", []) if "; extra ==" not in item
        }
        if runtime != EXPECTED_RUNTIME:
            raise ValueError(f"runtime dependency set differs: {runtime!r}")
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        _safe(names)
        required_suffixes = ("/LICENSE", "/NOTICE", "/compatibility.json", "/pyproject.toml")
        if any(not any(name.endswith(suffix) for name in names) for suffix in required_suffixes):
            raise ValueError("sdist omits required license, compatibility, or package metadata")
    return {wheel.name: _sha256(wheel), sdist.name: _sha256(sdist)}


def main() -> int:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
    hashes = verify(directory)
    for name, digest in hashes.items():
        print(f"{digest}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
