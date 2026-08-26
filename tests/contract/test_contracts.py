# SPDX-License-Identifier: Apache-2.0
"""Language-neutral contract, compatibility, and package-boundary tests."""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

import jsonschema
from meridian_storage.semantics import SchemaDocument

from meridian_storage.adapters.opensearch import MappingCompiler, adapter_descriptor
from meridian_storage.adapters.opensearch.descriptor import SUPPORTED_ENGINE_VERSIONS

ROOT = Path(__file__).resolve().parents[2]


def test_all_json_contracts_are_valid_draft_2020_12() -> None:
    files = sorted((ROOT / "contracts").glob("*.schema.json"))
    assert len(files) == 4
    for path in files:
        value = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(value)
        assert value["$id"].startswith("https://meridian.org/contracts/opensearch/")


def test_layout_conforms_to_packaged_contract(search_layout: object) -> None:
    schema = json.loads(
        (ROOT / "contracts/meridian.opensearch.layout.v1.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(search_layout.to_dict())


def test_compatibility_ledger_pins_exact_released_artifacts() -> None:
    value = json.loads((ROOT / "compatibility.json").read_text(encoding="utf-8"))
    assert value["catalogRegistry"] == [
        "structured",
        "object",
        "cache",
        "evidence",
        "streaming",
    ]
    assert value["operationContracts"] == ["meridian.structured.search@1.0.0"]
    dependencies = value["releasedDependencies"]
    assert set(dependencies) == {
        "meridian-storage-core",
        "meridian-storage-query",
        "meridian-storage-semantics",
    }
    assert all(item["version"] == "1.0.0" for item in dependencies.values())
    assert all(len(item["wheelSha256"]) == 64 for item in dependencies.values())
    engine = value["engineProfile"]
    assert engine["supportedPatchVersions"] == list(SUPPORTED_ENGINE_VERSIONS)
    assert engine["realConformanceVersion"] == "2.19.1"
    assert engine["requiredPlugins"] == ["analysis-icu"]
    assert "@sha256:" in engine["conformanceImage"]


def test_machine_readable_conformance_vectors_match_the_public_contract() -> None:
    schema = json.loads(
        (ROOT / "contracts/meridian.opensearch.conformance-vector.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    suite = json.loads((ROOT / "evidence/conformance-vectors.json").read_text(encoding="utf-8"))
    assert suite["lockedEngine"] == "OpenSearch 2.19.1"
    assert len(suite["vectors"]) == 7
    validator = jsonschema.Draft202012Validator(schema)
    for vector in suite["vectors"]:
        validator.validate(vector)
    assert {vector["category"] for vector in suite["vectors"]} == {
        "analysis",
        "failure",
        "mapping",
        "migration",
        "pagination",
        "projection",
        "query",
    }


def test_distribution_metadata_and_single_entry_point() -> None:
    distribution = metadata.distribution("meridian-storage-opensearch")
    assert distribution.version == "1.0.0"
    assert distribution.metadata["License-Expression"] == "Apache-2.0"
    assert distribution.requires is not None
    runtime = [item for item in distribution.requires if "extra ==" not in item]
    assert runtime == [
        "meridian-storage-core==1.0.0",
        "meridian-storage-query==1.0.0",
        "meridian-storage-semantics==1.0.0",
        "opensearch-py<4,>=3.2",
    ]
    points = [
        item for item in distribution.entry_points if item.group == "meridian_storage.adapters"
    ]
    assert [(item.name, item.value) for item in points] == [
        ("opensearch", "meridian_storage.adapters.opensearch:OpenSearchAdapterFactory")
    ]


def test_descriptor_has_no_catalog_or_native_query_escape_hatch() -> None:
    serialized = adapter_descriptor().to_dict()
    assert [item["operationContract"] for item in serialized["capabilities"]] == [
        "meridian.structured.search"
    ]
    text = json.dumps(serialized, sort_keys=True)
    assert "NativeQuery" not in text
    assert "query catalog" not in text.lower()
    assert "projection catalog" not in text.lower()


def test_document_schema_without_search_profile_is_rejected() -> None:
    schema = SchemaDocument.from_definition(
        catalog="structured",
        namespace="bad",
        name="schema",
        version="1.0.0",
        definition={
            "semanticKind": "relational",
            "fields": [{"name": "id", "logicalType": "string"}],
            "identity": ["id"],
        },
    )
    try:
        MappingCompiler().compile("structured:bad.items", schema)
    except ValueError as error:
        assert "profile" in str(error)
    else:
        raise AssertionError("mapping compiler accepted a non-search Schema")
