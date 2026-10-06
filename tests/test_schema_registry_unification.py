"""単一CoreSchemaRegistryのBinding回帰試験。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from harness.domain.errors import ErrorCode, HarnessError
from harness.infrastructure.core_schema_registry import CoreSchemaRegistry as LegacyRegistry
from harness.infrastructure.schema.registry import CoreSchemaRegistry

ROOT = Path(__file__).resolve().parents[1]


def _copied_registry_root(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    shutil.copytree(ROOT / "design-source", root / "design-source")
    shutil.copytree(ROOT / "schemas", root / "schemas")
    return root


def test_legacy_import_is_the_single_catalog_registry() -> None:
    assert LegacyRegistry is CoreSchemaRegistry
    assert CoreSchemaRegistry.bundled().schema_names == CoreSchemaRegistry(ROOT).schema_names


def test_schema_set_hash_changes_for_schema_or_registry_change(tmp_path: Path) -> None:
    copied = _copied_registry_root(tmp_path)
    baseline = CoreSchemaRegistry(copied).schema_set_hash()

    schema_path = copied / "schemas/core/EventEnvelope/1.0.0.schema.json"
    schema_path.write_bytes(schema_path.read_bytes() + b"\n")
    after_schema_change = CoreSchemaRegistry(copied).schema_set_hash()
    assert after_schema_change != baseline

    # §15.9の複数Version形式。先頭Schemaの宣言Versionだけを変える（Pathは変えない）。
    schemas_yaml = copied / "design-source/registries/schemas.yaml"
    document = yaml.safe_load(schemas_yaml.read_text(encoding="utf-8"))
    first = document["core_schemas"][0]
    first["active_write_version"] = "1.0.1"
    first["versions"][0]["version"] = "1.0.1"
    schemas_yaml.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    after_registry_change = CoreSchemaRegistry(copied).schema_set_hash()
    assert after_registry_change != after_schema_change


def test_saving_boundary_api_rejects_version_mismatch() -> None:
    registry = CoreSchemaRegistry.bundled()
    with pytest.raises(HarnessError) as error:
        registry.validate_or_raise("MaskingReceipt", "9.9.9", {})
    assert error.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION
