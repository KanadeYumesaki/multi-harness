"""P2 4 Case の統合試験：Schema Complete / Migration / Backup Restore。

期待値は `design-source/registries/tests.yaml` から読み取る。
Schema数・Version数・Error Codeを試験側へ手入力しない（不変条件#18）。

Storage は `tmp_path` 内の決定論的Fakeで扱う。**本番DB・実Repository・
実Release Artifactを触らない。**
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from harness.domain.backup_restore import (
    LedgerChainSnapshot,
    evaluate_backup_restore,
    evaluate_fresh_install,
)
from harness.domain.cross_reference import verify_effect_receipt_reference
from harness.domain.hashing import ContentHash

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRIES = REPO_ROOT / "design-source" / "registries"
SCHEMAS = REPO_ROOT / "schemas" / "core"

import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
from ledger_probe import LedgerProbe, SideEffectProbe, record_case  # noqa: E402
from schema_fixtures import build_valid_instance  # noqa: E402


def _observe_p2(
    observation: Any,
    *,
    test_id: str,
    case_id: str,
    state: str,
    subject_id: str,
    error_code: str | None,
    payload: dict[str, Any],
) -> None:
    """P2 Case の観測。Ledgerを実際に読んでから記録する。

    この4 Case は Ledger へ何もAppendしない。`expected_event_sequence` も空である。
    **それでも Ledger を観測する。** 観測せずに空配列を書くのは
    「見ていない」を「見て0件」と偽ることになる（設計書§19.1.1）。

    Probe は Case ごとに新しく作る。共有すると他CaseのEventが混ざる。
    """
    ledger = LedgerProbe()
    effects = SideEffectProbe()
    head_before = ledger.head

    observation.record_input(payload)
    record_case(
        observation,
        state=state,
        subject_id=subject_id,
        ledger=ledger,
        head_before=head_before,
        effects=effects,
        error_code=error_code,
    )

    expected = _case(test_id, case_id)
    assert list(observation.observed_events) == (expected["expected_event_sequence"] or [])
    # Appendが無いのだから Head は動かない。動いていたら観測が壊れている。
    assert observation.ledger_head_after == head_before
    assert observation.ledger_head_before == head_before


def _case(test_id: str, case_id: str) -> dict[str, Any]:
    rows = yaml.safe_load((REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))["test_cases"]
    for row in rows:
        if row["test_id"] == test_id and row["case_id"] == case_id:
            return dict(row)
    raise AssertionError(f"Registryに {test_id}/{case_id} が無い")


def _catalog() -> list[dict[str, Any]]:
    """`(schema_name, schema_version)` 単位のCatalog。件数は正本から数える。"""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from schema_catalog import normalize_core_schemas

    rows = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text(encoding="utf-8"))["core_schemas"]
    return normalize_core_schemas(rows)


def _expected_valid_fixture_count() -> int:
    """`valid_fixture_count == N` の N を Case assertions から読む。

    試験側へ数を書かない。Registryが動けばここも動く。
    """
    expected = _case("AT-SCHEMA-COMPLETE-001", "ALL_VALID")
    for assertion in expected["assertions"]:
        if assertion.startswith("valid_fixture_count"):
            return int(assertion.split("==")[1].strip())
    raise AssertionError("valid_fixture_count の assertion が無い")


# ==========================================================================
# AT-SCHEMA-COMPLETE-001 / ALL_VALID
# ==========================================================================


@pytest.mark.case("AT-SCHEMA-COMPLETE-001/ALL_VALID")
def test_all_core_schema_valid_fixtures(case_observation: Any) -> None:
    """登録済みCore Schema全件で有効Fixtureが通ること。

    Fixtureは各SchemaのJSON Schemaから決定論的に導出する。
    手書きすると、Schemaが増えたのに足し忘れる／`required`が変わっても
    古いまま通り続ける、が起きる。
    """
    expected = _case("AT-SCHEMA-COMPLETE-001", "ALL_VALID")
    catalog = _catalog()

    validation_errors = 0
    validated: list[str] = []
    logical_names: set[str] = set()

    for entry in catalog:
        name = entry["schema_name"]
        version = entry["schema_version"]
        schema = json.loads((SCHEMAS / name / f"{version}.schema.json").read_text("utf-8"))
        instance = build_valid_instance(schema)
        errors = list(Draft202012Validator(schema).iter_errors(instance))
        if errors:
            validation_errors += len(errors)
            paths = [f"{name}@{version}:{list(e.absolute_path)}:{e.validator}" for e in errors]
            pytest.fail(f"有効Fixtureが自分のSchemaを通らない: {paths}")
        validated.append(f"{name}@{version}")
        logical_names.add(name)

    # assertions: valid_fixture_count == N / validation_errors == 0
    assert validation_errors == 0
    assert len(logical_names) == _expected_valid_fixture_count()

    # 実際に検証した件数を記録する。「通ったはず」の値を書かない。
    _observe_p2(
        case_observation,
        test_id="AT-SCHEMA-COMPLETE-001",
        case_id="ALL_VALID",
        state=expected["expected_state"],
        subject_id="schema-suite-1",
        error_code=None,
        payload={
            "validated_schema_versions": sorted(validated),
            "logical_schema_count": len(logical_names),
            "validation_errors": validation_errors,
        },
    )
    assert expected["expected_state"] == "ACCEPTED"
    # Version は論理Schema数より多い。混同していないこと。
    assert len(validated) > len(logical_names)


def test_one_and_two_point_zero_fixtures_are_not_interchangeable() -> None:
    """`1.0.0` と `2.0.0` のFixtureを取り違えないこと。

    Envelopeの `schema_version` は `const` である。片方のFixtureを
    もう片方のSchemaへ通すと必ず落ちる。**落ちることを確かめる。**
    """
    versioned = [e for e in _catalog() if e["schema_version"] == "2.0.0"]
    assert versioned, "2.0.0 が登録されていない"

    for entry in versioned:
        name = entry["schema_name"]
        v1 = json.loads((SCHEMAS / name / "1.0.0.schema.json").read_text("utf-8"))
        v2 = json.loads((SCHEMAS / name / "2.0.0.schema.json").read_text("utf-8"))
        fixture_v1 = build_valid_instance(v1)
        fixture_v2 = build_valid_instance(v2)

        assert Draft202012Validator(v1).is_valid(fixture_v1), name
        assert Draft202012Validator(v2).is_valid(fixture_v2), name
        # 取り違えは通らない。
        assert not Draft202012Validator(v2).is_valid(fixture_v1), f"{name}: v1 fixtureがv2を通った"
        assert not Draft202012Validator(v1).is_valid(fixture_v2), f"{name}: v2 fixtureがv1を通った"


def test_fixture_builder_is_deterministic() -> None:
    """同じSchemaから必ず同じFixtureが出ること（不変条件#4）。"""
    schema = json.loads((SCHEMAS / "ContextBundle" / "2.0.0.schema.json").read_text("utf-8"))
    first = build_valid_instance(schema)
    second = build_valid_instance(schema)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ==========================================================================
# AT-SCHEMA-COMPLETE-001 / MISSING_CROSS_REFERENCE
# ==========================================================================


@pytest.mark.case("AT-SCHEMA-COMPLETE-001/MISSING_CROSS_REFERENCE")
def test_effect_receipt_missing_journal_rejected(case_observation: Any) -> None:
    """Journal参照の無い `EffectReceipt` を拒否すること。

    Schema検証は「文字列であること」までしか言えない。
    参照先の実在は別の層で確かめる。
    """
    expected = _case("AT-SCHEMA-COMPLETE-001", "MISSING_CROSS_REFERENCE")
    result = verify_effect_receipt_reference(
        {"operation_journal_id": "journal-does-not-exist"},
        known_journal_ids=frozenset({"journal-1"}),
    )
    assert result.state == expected["expected_state"]
    assert result.error_code is not None
    assert result.error_code.value == expected["expected_error_code"]
    assert result.rejected is True

    _observe_p2(
        case_observation,
        test_id="AT-SCHEMA-COMPLETE-001",
        case_id="MISSING_CROSS_REFERENCE",
        state=result.state,
        subject_id=result.schema_validation_id,
        error_code=result.error_code.value,
        payload={
            # 参照値そのものは載せない（不変条件#7）。欠落したField名だけを残す。
            "missing_reference": result.missing_reference,
            "known_journal_id_count": 1,
        },
    )


def test_absent_journal_field_is_also_rejected() -> None:
    """参照Field自体の欠落も拒否すること。

    「参照なし」として通すと、Journalの無いReceiptが受理される。
    """
    result = verify_effect_receipt_reference({}, known_journal_ids=frozenset({"journal-1"}))
    assert result.rejected is True
    assert result.missing_reference == "operation_journal_id"


def test_existing_journal_reference_is_accepted() -> None:
    """実在するJournalを指していれば通ること。"""
    result = verify_effect_receipt_reference(
        {"operation_journal_id": "journal-1"}, known_journal_ids=frozenset({"journal-1"})
    )
    assert result.rejected is False
    assert result.error_code is None


def test_schema_validation_and_cross_reference_are_separate_layers() -> None:
    """Schema検証を通っても Cross Reference で落ちうること。

    片方だけを見て通す経路が無いことを示す。
    """
    schema = json.loads((SCHEMAS / "EffectReceipt" / "1.0.0.schema.json").read_text("utf-8"))
    instance = build_valid_instance(schema)
    # Schema としては妥当である。
    assert Draft202012Validator(schema).is_valid(instance)
    # しかし参照先が無ければ Cross Reference は落ちる。
    result = verify_effect_receipt_reference(instance, known_journal_ids=frozenset())
    assert result.rejected is True


def test_cross_reference_result_does_not_leak_values() -> None:
    """参照先の実値をResultへ載せないこと（不変条件#7）。"""
    secretish = "journal-sk-live-000000000000"
    result = verify_effect_receipt_reference(
        {"operation_journal_id": secretish}, known_journal_ids=frozenset()
    )
    assert secretish not in json.dumps({"state": result.state, "missing": result.missing_reference})


# ==========================================================================
# AT-MIGRATION-001 / FRESH_INSTALL
# ==========================================================================


def _active_write_version() -> str:
    """`active_write_version` を Registry から読む。手入力しない。"""
    rows = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text(encoding="utf-8"))["core_schemas"]
    versions = {r.get("active_write_version") for r in rows if r.get("active_write_version")}
    assert versions, "active_write_version が無い"
    return sorted(versions)[-1]


@pytest.mark.case("AT-MIGRATION-001/FRESH_INSTALL")
def test_migration_fresh_database(tmp_path: Path, case_observation: Any) -> None:
    """新規DBのMigrationが完了すること。"""
    expected = _case("AT-MIGRATION-001", "FRESH_INSTALL")
    target = _active_write_version()

    outcome = evaluate_fresh_install(
        migration_result_id="mr-1",
        schema_version=target,
        expected_schema_version=target,
        integrity_check="ok",
        migration_head="0007_context_2_0_0",
    )
    # assertions: schema_version == expected_schema_version / integrity_check == ok
    assert outcome.state == expected["expected_state"]
    assert outcome.error_code is None
    assert outcome.schema_version == outcome.expected_schema_version
    assert outcome.integrity_check == "ok"
    assert outcome.migration_head
    assert outcome.accepted is True

    # 実際に生成されたRecordの値を読む。「成功したはずの値」を書かない。
    _observe_p2(
        case_observation,
        test_id="AT-MIGRATION-001",
        case_id="FRESH_INSTALL",
        state=outcome.state,
        subject_id=outcome.migration_result_id,
        error_code=outcome.error_code.value if outcome.error_code else None,
        payload={
            "schema_version": outcome.schema_version,
            "expected_schema_version": outcome.expected_schema_version,
            "integrity_check": outcome.integrity_check,
            "migration_head": outcome.migration_head,
            "problems": list(outcome.problems),
        },
    )


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"schema_version": "0.9.0"}, "schema_version != expected_schema_version"),
        ({"integrity_check": "malformed"}, "integrity_check != ok"),
        ({"migration_head": None}, "migration_head missing"),
        ({"schema_version": None}, "schema_version != expected_schema_version"),
    ],
)
def test_partial_migration_is_never_accepted(overrides: dict[str, Any], reason: str) -> None:
    """部分Migrationを成功扱いしないこと。

    途中まで適用されたDBは「どのVersionなのか」が確定しない。
    """
    target = _active_write_version()
    base: dict[str, Any] = {
        "migration_result_id": "mr-1",
        "schema_version": target,
        "expected_schema_version": target,
        "integrity_check": "ok",
        "migration_head": "0007",
    }
    base.update(overrides)
    outcome = evaluate_fresh_install(**base)
    assert outcome.accepted is False
    assert reason in outcome.problems
    assert outcome.error_code is not None
    assert outcome.error_code.value == "MIGRATION_FAILED"


def test_one_point_zero_reader_remains_available() -> None:
    """Migration後も `1.0.0` Readerが使えること。

    旧Recordを読む手段を消さない。
    """
    catalog = _catalog()
    read_only = [e for e in catalog if e["schema_version"] == "1.0.0" and e["read_only"]]
    assert read_only, "read_only の 1.0.0 が1件も無い"
    for entry in read_only:
        path = SCHEMAS / entry["schema_name"] / "1.0.0.schema.json"
        assert path.is_file()
        schema = json.loads(path.read_text("utf-8"))
        assert Draft202012Validator(schema).is_valid(build_valid_instance(schema))


def test_active_write_version_is_the_only_writable_one() -> None:
    """`active_write` がSchemaごとにちょうど1つであること。"""
    from collections import Counter

    counts = Counter(e["schema_name"] for e in _catalog() if e["active_write"])
    assert counts, "active_write が1件も無い"
    assert all(n == 1 for n in counts.values()), counts


def test_migration_vocabulary_is_not_in_the_registry() -> None:
    """Migration Tool専用語彙をRegistryへ足していないこと（D-Q6）。"""
    errors = yaml.safe_load((REGISTRIES / "errors.yaml").read_text(encoding="utf-8"))
    events = yaml.safe_load((REGISTRIES / "events.yaml").read_text(encoding="utf-8"))
    codes = {r["error_code"] for r in errors["error_codes"]}
    serialized = yaml.safe_dump(events["event_types"])
    for token in ("MIGRATED", "UNMIGRATABLE_LEGACY", "AUDIT_FIELD_NOT_TRANSFERABLE"):
        assert token not in codes, token
        assert token not in serialized, token


# ==========================================================================
# AT-MIGRATION-001 / BACKUP_RESTORE
# ==========================================================================


def _chain(count: int, *, seed: str = "a") -> LedgerChainSnapshot:
    """連続したHash Chainを決定論的に組む。"""
    entries = []
    previous: ContentHash | None = None
    for index in range(1, count + 1):
        digest = ContentHash.parse("sha256:" + f"{seed}{index}".ljust(64, "0"))
        entries.append((index, digest, previous))
        previous = digest
    return LedgerChainSnapshot(entries=tuple(entries))


@pytest.mark.case("AT-MIGRATION-001/BACKUP_RESTORE")
def test_backup_restore_and_chain_verify(tmp_path: Path, case_observation: Any) -> None:
    """Restore後にChain HeadとArtifact Manifestが一致すること。"""
    expected = _case("AT-MIGRATION-001", "BACKUP_RESTORE")
    original = _chain(5)
    restored = _chain(5)
    manifests = ["artifact-1", "artifact-2", "artifact-3"]

    # tmp_path内の決定論的Fake Storageへ書き出して読み戻す。
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"manifests": manifests}), encoding="utf-8")
    restored_manifests = json.loads(backup.read_text(encoding="utf-8"))["manifests"]

    result = evaluate_backup_restore(
        backup_restore_id="br-1",
        original=original,
        restored=restored,
        original_manifest_ids=manifests,
        restored_manifest_ids=restored_manifests,
        effects_during_restore=0,
    )
    # assertions: restored_chain_head == original_chain_head
    #             / artifact_manifest_count_equal == true
    assert result.state == expected["expected_state"]
    assert result.error_code is None
    assert result.restored_chain_head == result.original_chain_head
    assert result.artifact_manifest_count_equal is True
    assert result.effects_during_restore == 0
    assert result.accepted is True

    # 復元Manifestは実際に読み戻した値である。Head一致だけで完全性を判定しない。
    _observe_p2(
        case_observation,
        test_id="AT-MIGRATION-001",
        case_id="BACKUP_RESTORE",
        state=result.state,
        subject_id=result.backup_restore_id,
        error_code=result.error_code.value if result.error_code else None,
        payload={
            "original_chain_head": str(result.original_chain_head),
            "restored_chain_head": str(result.restored_chain_head),
            "original_manifest_ids": list(manifests),
            "restored_manifest_ids": list(restored_manifests),
            "artifact_manifest_count_equal": result.artifact_manifest_count_equal,
            "effects_during_restore": result.effects_during_restore,
            "problems": list(result.problems),
        },
    )


def test_broken_chain_is_rejected_even_when_head_matches() -> None:
    """Headが一致していても途中が切れていれば拒否すること。

    **Headだけを比べるのは「最後の1件が同じ」しか言っていない。**
    間の改変を見逃す。
    """
    original = _chain(4)
    entries = list(_chain(4).entries)
    # 3件目のlinkを切る。Headは変わらない。
    sequence, digest, _ = entries[2]
    entries[2] = (sequence, digest, ContentHash.parse("sha256:" + "f" * 64))
    tampered = LedgerChainSnapshot(entries=tuple(entries))

    assert tampered.head == original.head  # Headは同じ
    result = evaluate_backup_restore(
        backup_restore_id="br-1",
        original=original,
        restored=tampered,
        original_manifest_ids=["a"],
        restored_manifest_ids=["a"],
        effects_during_restore=0,
    )
    assert result.accepted is False
    assert result.error_code is not None
    assert result.error_code.value == "LEDGER_CHAIN_TAMPERED"


def test_incomplete_backup_is_rejected() -> None:
    """Eventが欠けたBackupを拒否すること。"""
    result = evaluate_backup_restore(
        backup_restore_id="br-1",
        original=_chain(5),
        restored=_chain(3),
        original_manifest_ids=["a"],
        restored_manifest_ids=["a"],
        effects_during_restore=0,
    )
    assert result.accepted is False


def test_manifest_id_mismatch_is_rejected_even_when_count_matches() -> None:
    """件数が同じでも中身が違えば拒否すること。"""
    result = evaluate_backup_restore(
        backup_restore_id="br-1",
        original=_chain(3),
        restored=_chain(3),
        original_manifest_ids=["a", "b"],
        restored_manifest_ids=["a", "c"],
        effects_during_restore=0,
    )
    assert result.accepted is False
    assert result.artifact_manifest_count_equal is True
    assert "artifact_manifest ids differ" in result.problems
    assert result.error_code is not None
    assert result.error_code.value == "BACKUP_RESTORE_FAILED"


def test_effects_during_restore_are_rejected() -> None:
    """Restore中にEffectが走ったら拒否すること。

    Restoreは過去の再構成であり、新しい作用ではない。
    """
    result = evaluate_backup_restore(
        backup_restore_id="br-1",
        original=_chain(3),
        restored=_chain(3),
        original_manifest_ids=["a"],
        restored_manifest_ids=["a"],
        effects_during_restore=1,
    )
    assert result.accepted is False
    assert "effects executed during restore" in result.problems


def test_empty_chain_restore_is_consistent() -> None:
    """空Chain同士のRestoreが成立すること（境界）。"""
    result = evaluate_backup_restore(
        backup_restore_id="br-1",
        original=_chain(0),
        restored=_chain(0),
        original_manifest_ids=[],
        restored_manifest_ids=[],
        effects_during_restore=0,
    )
    assert result.accepted is True
    assert result.original_chain_head is None


def test_chain_snapshot_detects_sequence_gaps() -> None:
    """Sequenceの飛びを検出すること。"""
    entries = list(_chain(3).entries)
    sequence, digest, link = entries[1]
    entries[1] = (99, digest, link)
    assert LedgerChainSnapshot(entries=tuple(entries)).broken_links()


# ==========================================================================
# Phase 4：Migration Case でも v1.10 Loss Policy を維持する
# ==========================================================================


def _upcaster() -> Any:
    import importlib.util

    path = REPO_ROOT / "migrations" / "upcasters" / "context_v1_to_v2.py"
    spec = importlib.util.spec_from_file_location("_p2_upcaster", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v1_bundle() -> dict[str, Any]:
    return {
        "schema_name": "ContextBundle",
        "schema_version": "1.0.0",
        "record_id": "018f0000-0000-7000-8000-000000000001",
        "created_at": "2026-08-17T00:00:00Z",
        "producer": "harness/test",
        "content_hash": "sha256:" + "a" * 64,
        "bundle_id": "bundle-1",
        "bundle_hash": "sha256:" + "b" * 64,
        "fragment_ids": ["f1"],
        "total_token_count": 1,
        "message_role_manifest_hash": "sha256:" + "c" * 64,
    }


def test_migration_does_not_guess_missing_hashes() -> None:
    """欠落Hashを推測補完しないこと（v1.10 §15.2）。"""
    upcaster = _upcaster()
    target = json.loads((SCHEMAS / "ContextBundle" / "2.0.0.schema.json").read_text("utf-8"))
    result = upcaster.upcast_record("ContextBundle", _v1_bundle(), target)
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["output"] is None
    assert result["reason_code"] == "REQUIRED_FIELD_NOT_RECONSTRUCTABLE"


def test_migration_result_is_not_a_core_schema_record() -> None:
    """Migration結果を `2.0.0` Core Schema と混同しないこと。

    Migration結果はTool専用の形であり、Core Schema Recordではない。
    そのまま `2.0.0` として検証すると通らない。
    """
    upcaster = _upcaster()
    target = json.loads((SCHEMAS / "ContextBundle" / "2.0.0.schema.json").read_text("utf-8"))
    result = dict(upcaster.upcast_record("ContextBundle", _v1_bundle(), target))
    assert not Draft202012Validator(target).is_valid(result)
    # 逆に、専用Schemaには適合する。
    migration_schema = json.loads(
        (REPO_ROOT / "migrations" / "upcasters" / "migration-result.schema.json").read_text("utf-8")
    )
    Draft202012Validator(migration_schema).validate(result)


def test_migration_result_keeps_provenance_fields() -> None:
    """`source_record_hash` / `upcaster_code_hash` / `conversion_reason` を保つこと。"""
    upcaster = _upcaster()
    target = json.loads((SCHEMAS / "ContextBundle" / "2.0.0.schema.json").read_text("utf-8"))
    result = upcaster.upcast_record("ContextBundle", _v1_bundle(), target)
    assert result["source_record_hash"].startswith("sha256:")
    assert result["upcaster_code_hash"].startswith("sha256:")
    assert result["conversion_reason"]
    # 変換していないので Lossless 可否は述べない。
    assert result["lossless"] is None


def test_supplement_cannot_overwrite_existing_values_during_migration() -> None:
    """Supplementを既存値の上書きに使えないこと。"""
    upcaster = _upcaster()
    target = json.loads((SCHEMAS / "ContextBundle" / "2.0.0.schema.json").read_text("utf-8"))
    result = upcaster.upcast_record(
        "ContextBundle",
        _v1_bundle(),
        target,
        supplement={"bundle_hash": "sha256:" + "9" * 64},
    )
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "SUPPLEMENT_CONFLICT"


def test_audit_field_loss_still_blocks_migration() -> None:
    """監査Field欠落は依然として `UNMIGRATABLE` であること。"""
    upcaster = _upcaster()
    target = json.loads((SCHEMAS / "TokenProfileSnapshot" / "2.0.0.schema.json").read_text("utf-8"))
    record = {
        "schema_name": "TokenProfileSnapshot",
        "schema_version": "1.0.0",
        "record_id": "018f0000-0000-7000-8000-000000000001",
        "created_at": "2026-08-17T00:00:00Z",
        "producer": "harness/test",
        "content_hash": "sha256:" + "a" * 64,
        "snapshot_id": "tp-1",
        "snapshot_hash": "sha256:" + "2" * 64,
        "provider": "p",
        "model": "m",
        "tokenizer_name": "t",
        "tokenizer_version": "1",
        "context_limit": 1,
        "maximum_output_limit": 1,
        "estimate_assurance": "EXACT",
        "retrieved_at": "2026-08-17T00:00:00Z",
        "expires_at": "2026-08-18T00:00:00Z",
    }
    result = upcaster.upcast_record("TokenProfileSnapshot", record, target)
    assert result["reason_code"] == "AUDIT_FIELD_NOT_TRANSFERABLE"
    assert "retrieved_at" in result["dropped_fields"]


def test_unclassified_field_loss_still_blocks_migration() -> None:
    """未分類Field欠落も `UNMIGRATABLE` のままであること。"""
    upcaster = _upcaster()
    target = json.loads((SCHEMAS / "ContextBundle" / "2.0.0.schema.json").read_text("utf-8"))
    result = upcaster.upcast_record("ContextBundle", {**_v1_bundle(), "legacy_extra": 1}, target)
    assert result["reason_code"] == "UNCLASSIFIED_FIELD_LOSS"
