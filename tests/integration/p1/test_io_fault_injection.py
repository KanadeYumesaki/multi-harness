"""`AT-FAULT-IO-001` 3 Case の統合試験（§11）。

Fault は決定論的なFake Adapterで注入する。**実Filesystemを埋めたり
Deviceを壊したりしない。** 書込み先は `tmp_path` 配下に限る。

観測値は Case ごとに **exactly** で固定する。「0以上」や「変わっていない」
のような緩い言い方をしない。何回起きたかを数える。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain.errors import HarnessError
from harness.domain.hashing import ContentHash
from harness.domain.storage_fault import (
    StorageFaultKind,
    StorageWriteAttempt,
    evaluate_storage_fault,
)
from harness.infrastructure.fault_injection.deterministic_storage import (
    DeterministicStorageAdapter,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "support"))
from ledger_probe import LedgerProbe, SideEffectProbe, record_case

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_YAML = REPO_ROOT / "design-source" / "registries" / "tests.yaml"

BASE = ContentHash.parse("sha256:" + "1" * 64)
INTENDED = ContentHash.parse("sha256:" + "2" * 64)

CASE_FAULT: dict[str, StorageFaultKind] = {
    "ARTIFACT_ENOSPC": StorageFaultKind.ARTIFACT_WRITE_ENOSPC,
    "FSYNC_EIO": StorageFaultKind.FILE_FSYNC_EIO,
    "SQLITE_ENOSPC": StorageFaultKind.SQLITE_COMMIT_ENOSPC,
}


def _case(case_id: str) -> dict[str, Any]:
    rows = yaml.safe_load(TESTS_YAML.read_text(encoding="utf-8"))["test_cases"]
    for row in rows:
        if row["test_id"] == "AT-FAULT-IO-001" and row["case_id"] == case_id:
            return dict(row)
    raise AssertionError(f"Registryに AT-FAULT-IO-001/{case_id} が無い")


def _run(tmp_path: Path, kind: StorageFaultKind) -> DeterministicStorageAdapter:
    """Fault注入下で「Artifact書込み → Journal確定 → Receipt保存」を試みる。

    **順序が重要である。** Artifact を CAS へ収めるのは Effect の実行ではなく
    その準備である。Journal の `PREPARED_DURABLE` 確定（不変条件#2）は
    「これから外部作用を起こす」という宣言であり、**準備が終わってから**行う。

    逆順にすると、Artifact書込みが失敗しても Journal だけが残る。
    それは「作用を試みた」という記録が、試みてもいないのに残る状態である。
    Case が `journal_prepared == false` を要求しているのはこのためである。

    §13 の順序を実際に辿る。段階を飛ばして失敗させると、
    どこまで進んでいたかの違いを試験できない。
    """
    adapter = DeterministicStorageAdapter(root=tmp_path, fault_kind=kind)
    try:
        adapter.write_artifact("out/result.txt", b"payload")
        adapter.prepare_journal("effect-1")
        adapter.store_receipt("receipt-1")
    except HarnessError:
        pass
    return adapter


def _observe_io(observation: Any, outcome: Any, adapter: Any, kind: Any, case_id: str) -> None:
    """I/O FaultのCaseを観測する。

    これらのCaseはLedgerへ何もAppendしない。**それでもLedgerを観測する。**
    観測せずに空配列を書くのは「見ていない」を「見て0件」と偽ることになる。
    """
    counters = adapter.counters
    ledger = LedgerProbe()
    effects = SideEffectProbe()
    effects.workspace_commits = counters.workspace_commits
    head_before = ledger.head

    observation.record_input(
        {
            "storage_io_id": "io-1",
            "fault_kind": str(kind.value if hasattr(kind, "value") else kind),
            "base_hash": str(BASE),
            "intended_hash": str(INTENDED),
        }
    )
    observation.record_fault(
        fault_point="STORAGE_IO",
        fault_kind=str(kind.value if hasattr(kind, "value") else kind),
        deterministic=True,
    )
    record_case(
        observation,
        state=outcome.state,
        subject_id="io-1",
        ledger=ledger,
        head_before=head_before,
        effects=effects,
        error_code=outcome.error_code.value if outcome.error_code else None,
    )
    assert list(observation.observed_events) == (_case(case_id)["expected_event_sequence"] or [])


def _outcome(adapter: DeterministicStorageAdapter, kind: StorageFaultKind) -> Any:
    counters = adapter.counters
    return evaluate_storage_fault(
        StorageWriteAttempt(
            storage_io_id="io-1",
            fault_kind=kind,
            base_hash=BASE,
            intended_hash=INTENDED,
            # 失敗したなら対象は実行前のまま。成功時だけ intended へ届く。
            target_hash=INTENDED if counters.workspace_commits else BASE,
            manifest_created=counters.manifest_created,
            journal_prepared=counters.journal_prepared,
            atomic_replace_count=counters.atomic_replace_count,
            partial_transaction_rows=counters.partial_transaction_rows,
            effect_attempts=counters.effect_attempts,
        )
    )


# --------------------------------------------------------------------------
# 3 Case
# --------------------------------------------------------------------------


@pytest.mark.case("AT-FAULT-IO-001/ARTIFACT_ENOSPC")
def test_artifact_write_disk_full(tmp_path: Path, case_observation: Any) -> None:
    """Artifact書込みがENOSPCで失敗する。Manifestを作らない。"""
    expected = _case("ARTIFACT_ENOSPC")
    kind = CASE_FAULT["ARTIFACT_ENOSPC"]
    adapter = _run(tmp_path, kind)
    outcome = _outcome(adapter, kind)
    _observe_io(case_observation, outcome, adapter, kind, "ARTIFACT_ENOSPC")

    assert outcome.state == expected["expected_state"]
    assert outcome.error_code is not None
    assert outcome.error_code.value == expected["expected_error_code"]

    counters = adapter.counters
    # assertions: manifest_created == false / journal_prepared == false
    #             / target_hash == base_hash
    assert counters.manifest_created is False
    # Artifactが書けなかったので、Journalは確定していない。
    # 試みてもいない作用の「試みた」記録を残さない。
    assert counters.journal_prepared is False
    assert outcome.residue == ()
    assert outcome.clean is True

    # exactly 観測値
    assert counters.write_attempts == 1
    assert counters.workspace_commits == 0
    assert counters.atomic_replace_count == 0
    assert counters.receipt_writes == 0
    assert counters.effect_attempts == 0
    assert counters.external_effects == 0
    # 書きかけのTemp Fileが残っていない。
    assert adapter.surviving_temp_files() == []


@pytest.mark.case("AT-FAULT-IO-001/FSYNC_EIO")
def test_file_fsync_io_error(tmp_path: Path, case_observation: Any) -> None:
    """fsyncがEIOで失敗する。Atomic Renameへ進まない。"""
    expected = _case("FSYNC_EIO")
    kind = CASE_FAULT["FSYNC_EIO"]
    adapter = _run(tmp_path, kind)
    outcome = _outcome(adapter, kind)
    _observe_io(case_observation, outcome, adapter, kind, "FSYNC_EIO")

    assert outcome.state == expected["expected_state"]
    assert outcome.error_code is not None
    assert outcome.error_code.value == expected["expected_error_code"]

    counters = adapter.counters
    # assertions: atomic_replace_count == 0 / target_hash == base_hash
    assert counters.atomic_replace_count == 0
    assert counters.manifest_created is False
    assert counters.workspace_commits == 0
    assert counters.write_attempts == 1
    assert counters.receipt_writes == 0
    assert counters.external_effects == 0
    # fsync失敗後にTempを残さない。
    assert adapter.surviving_temp_files() == []


@pytest.mark.case("AT-FAULT-IO-001/SQLITE_ENOSPC")
def test_sqlite_commit_disk_full(tmp_path: Path, case_observation: Any) -> None:
    """SQLite commitがENOSPCで失敗する。部分行もEffect試行も残さない。"""
    expected = _case("SQLITE_ENOSPC")
    kind = CASE_FAULT["SQLITE_ENOSPC"]
    adapter = _run(tmp_path, kind)
    outcome = _outcome(adapter, kind)
    _observe_io(case_observation, outcome, adapter, kind, "SQLITE_ENOSPC")

    assert outcome.state == expected["expected_state"]
    assert outcome.error_code is not None
    assert outcome.error_code.value == expected["expected_error_code"]
    # assertions: partial_transaction_rows == 0 / effect_attempts == 0
    assert outcome.residue == ()
    assert outcome.clean is True

    counters = adapter.counters
    assert counters.partial_transaction_rows == 0
    assert counters.effect_attempts == 0
    assert counters.journal_prepared is False
    assert counters.ledger_commits == 0
    # Artifact は CAS へ収まっている。Journal が確定していないので
    # 誰からも参照されず、外部作用も起きていない。
    # **これは違反ではない。** 内容アドレスの未参照Objectであり、
    # 後段のGCが回収できる（`AT-GC-001/ORPHAN_CANDIDATE` の対象）。
    assert counters.write_attempts == 1
    assert counters.workspace_commits == 1
    # Receipt は書かれない。Journal が無いのだから記録すべき作用が無い。
    assert counters.receipt_writes == 0
    assert counters.external_effects == 0
    assert adapter.surviving_temp_files() == []


# --------------------------------------------------------------------------
# 正常経路（通る経路が本当に通ること）
# --------------------------------------------------------------------------


def test_no_fault_completes_the_whole_sequence(tmp_path: Path) -> None:
    """Faultが無ければ最後まで通ること。

    失敗経路だけを試験すると、**何をしても失敗するAdapter**でも全部PASSする。
    """
    adapter = _run(tmp_path, StorageFaultKind.NONE)
    counters = adapter.counters
    assert counters.journal_prepared is True
    assert counters.write_attempts == 1
    assert counters.atomic_replace_count == 1
    assert counters.manifest_created is True
    assert counters.workspace_commits == 1
    assert counters.receipt_writes == 1
    assert adapter.surviving_temp_files() == []

    outcome = _outcome(adapter, StorageFaultKind.NONE)
    assert outcome.state == "ACCEPTED"
    assert outcome.error_code is None


# --------------------------------------------------------------------------
# 横断的な性質
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(CASE_FAULT.values()))
def test_no_fault_path_produces_external_effects(kind: StorageFaultKind, tmp_path: Path) -> None:
    """どのFaultでも外部作用を起こさないこと。"""
    adapter = _run(tmp_path, kind)
    assert adapter.counters.external_effects == 0
    assert adapter.counters.effect_attempts == 0


@pytest.mark.parametrize("kind", list(CASE_FAULT.values()))
def test_fault_injection_is_deterministic(kind: StorageFaultKind, tmp_path: Path) -> None:
    """同じ入力から必ず同じ失敗が出ること。

    再現しない試験はEvidenceにならない。
    """
    first = _run(tmp_path / "a", kind).counters
    second = _run(tmp_path / "b", kind).counters
    assert first == second


@pytest.mark.parametrize("kind", list(CASE_FAULT.values()))
def test_no_temp_file_survives_any_fault(kind: StorageFaultKind, tmp_path: Path) -> None:
    """どのFaultでも書きかけのTemp Fileを残さないこと。"""
    adapter = _run(tmp_path, kind)
    assert adapter.surviving_temp_files() == []


def test_adapter_refuses_paths_outside_its_root(tmp_path: Path) -> None:
    """Root外への書込みを拒否すること。試験Adapterが実環境へ触れない。"""
    adapter = DeterministicStorageAdapter(root=tmp_path)
    with pytest.raises(HarnessError):
        adapter.write_artifact("../escape.txt", b"x")


def test_residue_is_reported_not_silently_accepted() -> None:
    """部分状態が残っていれば `residue` へ挙げること。

    残っているのに `ACCEPTED` にする経路を作らない。
    """
    outcome = evaluate_storage_fault(
        StorageWriteAttempt(
            storage_io_id="io-1",
            fault_kind=StorageFaultKind.ARTIFACT_WRITE_ENOSPC,
            base_hash=BASE,
            intended_hash=INTENDED,
            target_hash=INTENDED,  # 失敗したのに対象が変わっている
            manifest_created=True,
            journal_prepared=True,
            atomic_replace_count=1,
            partial_transaction_rows=3,
            effect_attempts=1,
        )
    )
    assert outcome.rejected is True
    assert outcome.clean is False
    assert "target_hash != base_hash" in outcome.residue
    assert "manifest_created" in outcome.residue
