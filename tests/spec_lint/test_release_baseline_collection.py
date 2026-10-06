"""Release基礎計測は本物のSchemaとSQLiteを使用する。"""

from pathlib import Path

import pytest

from collect_release_baseline import measure_migration, measure_schemas
from emit_case_evidence import EvidenceEmissionError


def test_fresh_migration_is_measured_and_existing_db_is_rejected(tmp_path):
    path = tmp_path / "fresh.db"
    result = measure_migration(path)
    assert result["fresh_install_passed"] is True
    assert result["idempotent_second_install"] is True
    assert result["quick_check"] == "ok"
    assert int(result["migration_head"]) == max(result["applied_versions"])
    with pytest.raises(EvidenceEmissionError, match="ALREADY_EXISTS"):
        measure_migration(path)


def test_schema_set_is_bound_to_every_actually_validated_version():
    repo = Path(__file__).resolve().parents[2]
    result = measure_schemas(repo)
    assert result["validated_schema_count"] == len(
        {r["schema_name"] for r in result["validated_versions"]}
    )
    assert result["validated_versions"]
    assert all(r["schema_hash"].startswith("sha256:") for r in result["validated_versions"])
