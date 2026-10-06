"""正式Case EvidenceでBlock Recordを閉じる契約（合成Fixture）。

## なぜ合成Fixtureか

実測Evidenceは `$HOME/runtime-evidence/` のような**このRepositoryの外**に
ある。CIの試験をその存在へ依存させると、Evidenceが無い環境で黙ってSkipされるか、
逆に落ち続ける。どちらも「契約を検査した」ことにならない。

ここでは正式Envelopeと同じ形をtmp上へ組み立て、実Toolを合成の失敗条件で動かす。
実Recordの解消根拠はReport側のReceiptが持ち、外部Evidenceがある環境で正式に
再検証する経路と分ける。

## 何を固定するか

1. 旧Evidence方式が壊れていないこと
2. 正式方式が**明示選択**であり、入力欠落で暗黙fallbackしないこと
3. 正常系が対象Case集合の完全な実測根拠で閉じられること
4. 改変・矛盾・欠落・重複・継承の壊れ・未観測counterを**拒否**すること
5. 失敗しても元Recordが1 Byteも動かないこと
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import verify_runtime_go as verifier  # noqa: E402

import blocked_resolution_formal as formal  # noqa: E402
import resolve_blocked_record as resolver  # noqa: E402
from emit_case_evidence import _evidence_hash  # noqa: E402

SCOPE = "MVP0-A"
STAMP = "2026-09-16T00:00:00Z"
#: 対象集合を導く Owner 決定の置き場。**下の Fixture が合成の決定へ向ける。**
DECISION_DIR = REPO_ROOT / "docs" / "decision"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, body: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return _digest(path)


def _head_commit() -> str:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def snapshot() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def commit() -> str:
    return _head_commit()


@pytest.fixture(scope="module")
def synthetic_decision_dir(
    tmp_path_factory: pytest.TempPathFactory, snapshot: dict[str, Any]
) -> Path:
    """正式方式の対象集合を与える合成の Owner 決定 2 文書。

    保存済みの決定（Owner の回答記録）は公開用の配布コピーに無い。ここでは現行
    Registry が Unit（`NOT_APPLICABLE`・期待Event列が空）と分類する必須 Case から
    先頭の数件を取り、2 文書へ同じ集合として書く。**Case を手で選ばない。**
    保存済みの決定そのものの突合は `tests/private_history/` の試験が確かめる。
    """
    required = {f"{t}/{c}" for t, c in snapshot["scopes"][SCOPE]["required_cases"]}
    candidates = [
        key
        for key in formal._unit_case_ids(snapshot, SCOPE)
        if key in required
        and snapshot["expectations"][key].get("event_observation_policy") == "NOT_APPLICABLE"
        and not list(snapshot["expectations"][key].get("expected_event_sequence") or [])
    ]
    cases = sorted(candidates)[:7]
    assert cases, "Unit Case が 1 件も無い（前提が崩れている）"
    directory = tmp_path_factory.mktemp("synthetic-owner-decision")
    _write(
        directory / "F1-F2-impact-matrix.json",
        {
            "synthetic_test_input": True,
            "owner_approval": {"answers": {"F2-APPROVAL": {"choice": "F2-ALL"}}, "unanswered": []},
            "candidates": {"F-2": cases},
        },
    )
    _write(
        directory / "OWNER-DECISION-UNIT-EVIDENCE-DCR.json",
        {
            "synthetic_test_input": True,
            "unanswered": [],
            "answers": {f"DCR-{n}": {"choice": f"DCR-{n}-SYNTHETIC"} for n in range(1, 6)},
            "gate": {"4_f2_cases": {"cases": cases}},
        },
    )
    return directory


@pytest.fixture(autouse=True)
def synthetic_owner_decision(
    synthetic_decision_dir: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setattr(request.module, "DECISION_DIR", synthetic_decision_dir)
    monkeypatch.setattr(resolver, "DECISION_RELATIVE", str(synthetic_decision_dir))


class World:
    """正式Envelopeと同じ形の合成Evidence一式。

    `write()` は Hash を依存順に測り直す。**Fixtureの中でHashを手入力しない。**
    改変系の試験は `write()` の後に `tamper()` でBytesだけを動かす。
    """

    def __init__(self, root: Path, snapshot: dict[str, Any], commit: str) -> None:
        self.root = root
        self.snapshot = snapshot
        self.commit = commit
        self.unit_ids = formal._unit_case_ids(snapshot, SCOPE)
        self.schema_set_hash = "sha256:" + "1" * 64
        self.migration_head = "10"
        self.environment = self._environment()
        self.cases = {key: self._case(key) for key in self.unit_ids}
        self.raws = {key: [self._observation(key)] for key in self.unit_ids}
        self.receipts = {key: self._receipt(key) for key in self.unit_ids}
        self.fixtures = {key: {"case_id": key, "synthetic": True} for key in self.unit_ids}
        self.area_case_ids = list(self.unit_ids)
        self.extra_case_rows: list[dict[str, Any]] = []
        self.manifest = self._manifest()
        self.report: dict[str, Any] = {}

    # ---- 構成要素 -------------------------------------------------------
    def _environment(self) -> dict[str, Any]:
        return {
            "evidence_schema_version": verifier.EVIDENCE_SCHEMA_VERSION,
            "release_scope": SCOPE,
            "area": "environment",
            "status": verifier.PASS,
            "summary": {"collector": "synthetic"},
            "producer": "tests/spec_lint/test_blocked_record_resolution_formal.py",
            "test_run_id": "synthetic-environment",
            "started_at": STAMP,
            "recorded_at": STAMP,
            "implementation_commit_sha": self.commit,
            "schema_set_hash": self.schema_set_hash,
            "migration_head": self.migration_head,
            "is_wsl2": True,
            "workspace_on_linux_native_fs": True,
            "python_version": "3.12.3",
            "workspace_root": "/home/synthetic/worktree",
            "mount_point": "/",
            "filesystem_type": "ext4",
            "mount_id": 1,
            "mountinfo_hash": "sha256:" + "2" * 64,
            "python_lock_hash": "sha256:" + "3" * 64,
        }

    def _case(self, key: str) -> dict[str, Any]:
        exp = self.snapshot["expectations"][key]
        test_id, _, case_name = key.partition("/")
        return {
            "evidence_schema_version": verifier.EVIDENCE_SCHEMA_VERSION,
            "release_scope": SCOPE,
            "test_id": test_id,
            "case_id": case_name,
            "status": verifier.PASS,
            "producer": "tools/collect_unit_cases.py",
            "test_run_id": key,
            "command": ["/usr/bin/python3", "-m", "pytest", "-q", f"tests/synthetic/{case_name}"],
            "exit_code": 0,
            "started_at": STAMP,
            "recorded_at": STAMP,
            "runner_source_hash": "sha256:" + "4" * 64,
            "implementation_commit_sha": self.commit,
            "schema_set_hash": self.schema_set_hash,
            "migration_head": self.migration_head,
            "runtime_environment_hash": None,
            "input_fixture_path": f"units/fixtures/{key}.json",
            "input_fixture_hash": None,
            "raw_result_path": f"units/executions/{key}/observations.jsonl",
            "raw_result_hash": None,
            "expectation_descriptor_hash": exp["expectation_descriptor_hash"],
            "observed_subject_type": exp["expected_subject_type"],
            "observed_state": exp["expected_state"],
            "observed_error_code": exp["expected_error_code"],
            "actual_subject_id": f"subject-{case_name.lower()}",
            "evidence_kind": "UNIT",
            "event_observation": "NOT_APPLICABLE",
            "event_observation_policy": "NOT_APPLICABLE",
            "observed_event_sequence": None,
            "durability_tier": exp.get("durability_tier"),
            "side_effects": dict.fromkeys(formal.UNIT_COUNTER_FIELDS, 0),
            "assertions": [{"expression": f"{key}: synthetic assertion", "result": True}],
        }

    def _observation(self, key: str) -> dict[str, Any]:
        exp = self.snapshot["expectations"][key]
        return {
            "unit_execution": {
                "contract": formal.MONITOR_CONTRACT,
                "scope": "pytest_python_test_function",
                "complete": True,
                "counts": dict.fromkeys(formal.UNIT_COUNTER_FIELDS, 0),
                "violations": [],
                "audit_probes": 2,
                "driver_process_launches": 0,
                "child_reports": [],
                "limits": ["synthetic monitor; not an OS sandbox"],
                "subject_type": exp["expected_subject_type"],
                "durability_observation": "NOT_APPLICABLE",
                "assertions": [{"expression": f"{key}: synthetic assertion", "result": True}],
            },
            "case_id": key,
            "node_id": f"tests/synthetic/{key}::test_case",
            "observed_state": exp["expected_state"],
            "observed_error_code": exp["expected_error_code"],
            "observed_event_sequence": [],
            "ledger_observed": False,
            "ledger_head_before": None,
            "ledger_head_after": None,
            "actual_subject_id": f"subject-{key.partition('/')[2].lower()}",
            "side_effects": dict.fromkeys(formal.UNIT_COUNTER_FIELDS, 0),
            "recorded": True,
        }

    def _receipt(self, key: str) -> dict[str, Any]:
        case = self.cases[key] if key in getattr(self, "cases", {}) else self._case(key)
        return {
            "contract": formal.RECEIPT_CONTRACT,
            "case_id": key,
            "command": list(case["command"]),
            "cwd": "/home/synthetic/worktree",
            "started_at": STAMP,
            "recorded_at": STAMP,
            "exit_code": 0,
            "runner_source_hash": case["runner_source_hash"],
            "raw_observations_hash": None,
        }

    def _manifest(self) -> dict[str, Any]:
        scope = self.snapshot["scopes"][SCOPE]
        return {
            "manifest_version": verifier.MANIFEST_VERSION,
            "design_version": "1.25",
            "design_sha256": self.snapshot["design_sha256"],
            "release_scope": SCOPE,
            "registry_snapshot_hash": self.snapshot["registry_snapshot_hash"],
            "implementation_repository": None,
            "implementation_commit_sha": self.commit,
            "source_tree_clean": True,
            "schema_set_hash": self.schema_set_hash,
            "migration_head": self.migration_head,
            "test_manifest_hash": scope["test_manifest_hash"],
            "expected_gate_count": len(scope["required_gate_ids"]),
            "expected_test_id_count": scope["required_test_id_count"],
            "expected_case_count": len(scope["required_cases"]),
            "skipped_count": 0,
            "xfail_count": 0,
            "release_decision": "BLOCKED_EVIDENCE_MISSING",
            "source_subtree_hashes": {},
            "runtime_environment": {
                "environment_manifest_path": "environment.json",
                "environment_manifest_hash": None,
                "is_wsl2": True,
                "workspace_on_linux_native_fs": True,
                "python_version": "3.12.3",
            },
            "test_cases": [],
            "gates": [],
            "required_evidence_areas": [],
        }

    # ---- 書き出し -------------------------------------------------------
    def write(self) -> None:
        environment_hash = _write(self.root / "environment.json", self.environment)
        self.manifest["runtime_environment"]["environment_manifest_hash"] = environment_hash

        rows: list[dict[str, Any]] = []
        case_hashes: dict[str, str] = {}
        for key in self.unit_ids:
            case = self.cases[key]
            fixture_hash = _write(self.root / f"units/fixtures/{key}.json", self.fixtures[key])
            raw_path = self.root / f"units/executions/{key}/observations.jsonl"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.raws[key]),
                encoding="utf-8",
            )
            raw_hash = _digest(raw_path)
            case["input_fixture_hash"] = fixture_hash
            case["raw_result_hash"] = raw_hash
            case["runtime_environment_hash"] = environment_hash
            receipt = self.receipts[key]
            receipt["raw_observations_hash"] = raw_hash
            for field in ("command", "started_at", "recorded_at", "runner_source_hash"):
                receipt[field] = case[field]
            receipt["exit_code"] = case["exit_code"]
            _write(raw_path.parent / "execution.json", receipt)
            case_hash = _write(self.root / f"units/cases/{key}.json", case)
            case_hashes[key] = case_hash
            rows.append(
                {
                    "test_id": case["test_id"],
                    "case_id": case["case_id"],
                    "status": case["status"],
                    "input_fixture_hash": fixture_hash,
                    "evidence_path": f"units/cases/{key}.json",
                    "evidence_manifest_hash": case_hash,
                }
            )
        self.manifest["test_cases"] = rows + self.extra_case_rows

        area = {
            "evidence_schema_version": verifier.EVIDENCE_SCHEMA_VERSION,
            "release_scope": SCOPE,
            "area": formal.UNIT_AREA,
            "status": verifier.PASS,
            "summary": {
                "case_ids": list(self.area_case_ids),
                "case_evidence_hashes": sorted(
                    case_hashes[key] for key in self.area_case_ids if key in case_hashes
                ),
                "unit_case_count": len(self.area_case_ids),
                "failing_case_ids": [],
                "design_sha256": self.snapshot["design_sha256"],
                "registry_snapshot_hash": self.snapshot["registry_snapshot_hash"],
                "human_measured": False,
            },
            "producer": "tools/emit_unit_area_evidence.py",
            "test_run_id": f"unit-case-suite-{SCOPE}",
            "started_at": STAMP,
            "recorded_at": STAMP,
            "implementation_commit_sha": self.commit,
            "schema_set_hash": self.schema_set_hash,
            "migration_head": self.migration_head,
            "runtime_environment_hash": environment_hash,
        }
        area_hash = _write(self.root / "units/unit-case-suite.json", area)
        self.manifest["required_evidence_areas"] = [
            {
                "area": "environment",
                "status": verifier.PASS,
                "evidence_path": "environment.json",
                "evidence_manifest_hash": environment_hash,
            },
            {
                "area": formal.UNIT_AREA,
                "status": verifier.PASS,
                "evidence_path": "units/unit-case-suite.json",
                "evidence_manifest_hash": area_hash,
            },
        ]
        manifest_hash = _write(self.manifest_path, self.manifest)
        self.report = {
            "decision": "BLOCKED_EVIDENCE_MISSING",
            "release_scope": SCOPE,
            "implementation_commit_sha": self.commit,
            "design_sha256": self.snapshot["design_sha256"],
            "registry_snapshot_hash": self.snapshot["registry_snapshot_hash"],
            "manifest_sha256": manifest_hash,
            "verifier_version": verifier.VERIFIER_VERSION,
            "verifier_source_sha256": verifier.sha256_file(REPO_ROOT / "verify_runtime_go.py"),
        }
        _write(self.report_path, self.report)

    # ---- 参照 -----------------------------------------------------------
    @property
    def manifest_path(self) -> Path:
        return self.root / "units/manifest-with-units.json"

    @property
    def report_path(self) -> Path:
        return self.root / "unit-verification-report.json"

    def targets(self) -> dict[str, Any]:
        return formal.derive_target_cases(DECISION_DIR, self.snapshot, SCOPE)

    def tamper(self, relative: str, mutate: Any) -> None:
        """書き出した後にBytesだけを動かす。Manifest側のHashは更新しない。"""
        path = self.root / relative
        body = json.loads(path.read_text(encoding="utf-8"))
        mutate(body)
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def verify(self) -> dict[str, Any]:
        targets = self.targets()
        return formal.verify_formal_evidence(
            repo_root=REPO_ROOT,
            evidence_root=self.root,
            manifest_path=self.manifest_path,
            report_path=self.report_path,
            scope=SCOPE,
            expected_commit=self.commit,
            target_case_ids=targets["target_case_ids"],
            unit_case_ids=targets["unit_case_ids"],
        )


@pytest.fixture
def world(tmp_path: Path, snapshot: dict[str, Any], commit: str) -> World:
    built = World(tmp_path / "evidence", snapshot, commit)
    built.write()
    return built


# --------------------------------------------------------------------------
# 正常系
# --------------------------------------------------------------------------


def test_formal_evidence_closes_the_target_cases(world: World) -> None:
    """対象Case集合の完全な実測根拠がそろえば正式方式で閉じられる。"""
    result = world.verify()
    targets = world.targets()
    assert [entry["case_id"] for entry in result["target_cases"]] == targets["target_case_ids"]
    assert result["coverage"]["target_case_count"] == targets["target_case_count"]
    for entry in result["target_cases"]:
        assert entry["status"] == verifier.PASS
        # Manifest が束縛するのは File Bytes である。自己Hashで代用していない。
        assert entry["evidence_file_sha256"] == entry["evidence_manifest_hash"]
        assert entry["evidence_kind"] == "UNIT"
        assert entry["observed_event_sequence_is_null"] is True
    assert result["unit_area"]["case_ids"] == targets["unit_case_ids"]
    assert result["measured_source"]["implementation_commit_sha"] == world.commit


def test_target_case_set_comes_from_owner_decisions_not_record_prose(
    snapshot: dict[str, Any],
) -> None:
    """対象集合は保存済みOwner決定と現行Registryの突合から導く。"""
    targets = formal.derive_target_cases(DECISION_DIR, snapshot, SCOPE)
    units = set(formal._unit_case_ids(snapshot, SCOPE))
    assert set(targets["target_case_ids"]) <= units
    for key in targets["target_case_ids"]:
        expectation = snapshot["expectations"][key]
        assert expectation["event_observation_policy"] == "NOT_APPLICABLE"
        assert not (expectation["expected_event_sequence"] or [])


def test_out_of_scope_shortfall_is_recorded_not_silently_dropped(world: World) -> None:
    """F-2と無関係な残件は閉じる条件にしないが、数えて残す。"""
    result = world.verify()
    assert result["out_of_scope"]["global_error_count"] == 0
    # 45 Gate と大半の Case は未提供のまま。ここを 0 と書いたら嘘になる。
    assert result["out_of_scope"]["global_missing_count"] > 0
    assert result["out_of_scope"]["excluded_missing_subjects"]
    assert result["out_of_scope"]["verification_report_decision"] == "BLOCKED_EVIDENCE_MISSING"


def test_legacy_evidence_mode_is_preserved(tmp_path: Path) -> None:
    """旧Evidence方式（自己Hash）の検証を壊していない。"""
    body: dict[str, Any] = {
        "evidence_schema_version": "3.0",
        "case_id": "AT-SYNTHETIC-001/LEGACY",
        "status": verifier.PASS,
        "evidence_kind": "UNIT",
        "event_observation": "NOT_APPLICABLE",
    }
    body["evidence_hash"] = _evidence_hash(body)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    record = {"blocker_id": "BLK-20260101-SYNTHETIC", "observed": {"summary": body["case_id"]}}
    checked = resolver._check_evidence([path], record)
    assert checked[0]["case_id"] == body["case_id"]
    assert checked[0]["evidence_hash"] == body["evidence_hash"]

    # 自己Hashが合わない旧Evidenceは従来どおり拒否する。
    body["status"] = "FAIL"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(resolver.ResolveError, match="TAMPERED_EVIDENCE"):
        resolver._check_evidence([path], record)


def test_formal_envelope_is_not_accepted_by_the_legacy_checker(world: World) -> None:
    """正式Envelopeを旧検証へ流し込まない。自己Hashを持たないので落ちる。"""
    key = world.targets()["target_case_ids"][0]
    path = world.root / f"units/cases/{key}.json"
    record = {"blocker_id": "BLK-20260101-SYNTHETIC", "observed": {"summary": key}}
    with pytest.raises(resolver.ResolveError, match="TAMPERED_EVIDENCE"):
        resolver._check_evidence([path], record)


# --------------------------------------------------------------------------
# 方式選択
# --------------------------------------------------------------------------


def _resolve_argv(**overrides: Any) -> list[str]:
    argv = [
        "--blocker-id",
        "BLK-20260907-F2-UNIT-EVIDENCE-SEPARATION-R11-V125",
        "--summary",
        "synthetic",
        "--verified-by",
        "synthetic",
        "--decided-by",
        "synthetic",
        "--decided-at",
        STAMP,
    ]
    for name, value in overrides.items():
        argv.extend([f"--{name.replace('_', '-')}", str(value)])
    return argv


def test_evidence_mode_must_be_selected_explicitly(
    capsys: pytest.CaptureFixture[str], world: World
) -> None:
    """方式の入力が欠けたら止まる。暗黙のfallbackを作らない。"""
    assert resolver.main(_resolve_argv()) == 1
    assert "EVIDENCE_MODE_NOT_SELECTED" in capsys.readouterr().err

    assert resolver.main(_resolve_argv(formal_manifest=world.manifest_path)) == 1
    assert "FORMAL_INPUT_INCOMPLETE" in capsys.readouterr().err

    assert (
        resolver.main(
            _resolve_argv(evidence=world.manifest_path, formal_manifest=world.manifest_path)
        )
        == 1
    )
    assert "EVIDENCE_MODE_AMBIGUOUS" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 束縛の否定系
# --------------------------------------------------------------------------


def test_a_different_commit_is_rejected(world: World) -> None:
    targets = world.targets()
    with pytest.raises(formal.FormalResolutionError, match="COMMIT_MISMATCH"):
        formal.verify_formal_evidence(
            repo_root=REPO_ROOT,
            evidence_root=world.root,
            manifest_path=world.manifest_path,
            report_path=world.report_path,
            scope=SCOPE,
            expected_commit="0" * 40,
            target_case_ids=targets["target_case_ids"],
            unit_case_ids=targets["unit_case_ids"],
        )


def test_dirty_tree_manifest_is_rejected(world: World) -> None:
    world.manifest["source_tree_clean"] = False
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="MANIFEST_BINDING_INVALID"):
        world.verify()


def test_foreign_design_binding_is_rejected(world: World) -> None:
    world.manifest["design_sha256"] = "sha256:" + "9" * 64
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="MANIFEST_BINDING_INVALID"):
        world.verify()


def test_foreign_registry_binding_is_rejected(world: World) -> None:
    world.manifest["registry_snapshot_hash"] = "sha256:" + "8" * 64
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="MANIFEST_BINDING_INVALID"):
        world.verify()


def test_scope_mismatch_is_rejected(world: World) -> None:
    world.manifest["release_scope"] = "MVP0-B"
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="MANIFEST_BINDING_INVALID"):
        world.verify()


def test_schema_or_migration_drift_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["migration_head"] = "11"
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="CASE_CONTRACT_INVALID"):
        world.verify()


def test_environment_must_be_native_linux(world: World) -> None:
    world.environment["workspace_on_linux_native_fs"] = False
    world.manifest["runtime_environment"]["workspace_on_linux_native_fs"] = False
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="MANIFEST_BINDING_INVALID"):
        world.verify()


def test_report_must_bind_the_manifest_bytes(world: World) -> None:
    world.report["manifest_sha256"] = "sha256:" + "7" * 64
    _write(world.report_path, world.report)
    with pytest.raises(formal.FormalResolutionError, match="REPORT_NOT_BOUND"):
        world.verify()


def test_report_from_an_unrelated_verifier_is_rejected(world: World) -> None:
    world.report["verifier_source_sha256"] = "sha256:" + "6" * 64
    _write(world.report_path, world.report)
    with pytest.raises(formal.FormalResolutionError, match="REPORT_NOT_BOUND"):
        world.verify()


def test_evidence_inside_the_repository_is_rejected(world: World) -> None:
    targets = world.targets()
    with pytest.raises(formal.FormalResolutionError, match="EVIDENCE_IN_REPOSITORY"):
        formal.verify_formal_evidence(
            repo_root=REPO_ROOT,
            # Repository 内の実在 Directory。配布コピーにも必ずある `tests/` を使う。
            evidence_root=REPO_ROOT / "tests",
            manifest_path=world.manifest_path,
            report_path=world.report_path,
            scope=SCOPE,
            expected_commit=world.commit,
            target_case_ids=targets["target_case_ids"],
            unit_case_ids=targets["unit_case_ids"],
        )


# --------------------------------------------------------------------------
# 改変・矛盾の否定系
# --------------------------------------------------------------------------


def test_tampered_case_evidence_bytes_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.tamper(f"units/cases/{key}.json", lambda body: body.update(actual_subject_id="other"))
    with pytest.raises(formal.FormalResolutionError, match="CASE_EVIDENCE_INVALID"):
        world.verify()


def test_tampered_raw_observations_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    path = world.root / f"units/executions/{key}/observations.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8").replace("subject-", "other-"), encoding="utf-8"
    )
    with pytest.raises(formal.FormalResolutionError, match="CASE_CONTRACT_INVALID"):
        world.verify()


def test_tampered_fixture_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.tamper(f"units/fixtures/{key}.json", lambda body: body.update(synthetic=False))
    with pytest.raises(formal.FormalResolutionError, match="CASE_CONTRACT_INVALID"):
        world.verify()


def test_tampered_area_evidence_is_rejected(world: World) -> None:
    world.tamper(
        "units/unit-case-suite.json", lambda body: body["summary"].update(human_measured=True)
    )
    with pytest.raises(formal.FormalResolutionError, match="UNIT_AREA_EVIDENCE_INVALID"):
        world.verify()


def test_resealed_but_contradictory_evidence_is_rejected(world: World) -> None:
    """Hashを測り直して再封印しても、意味が矛盾する文書は通さない。"""
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["observed_state"] = "ACCEPTED_BY_SYNTHETIC_REWRITE"
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="CASE_CONTRACT_INVALID"):
        world.verify()


def test_wrong_subject_type_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["observed_subject_type"] = "SOMETHING_ELSE"
    world.raws[key][0]["unit_execution"]["subject_type"] = "SOMETHING_ELSE"
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="CASE_CONTRACT_INVALID"):
        world.verify()


def test_wrong_error_code_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["observed_error_code"] = "SOME_OTHER_CODE"
    world.raws[key][0]["observed_error_code"] = "SOME_OTHER_CODE"
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="CASE_CONTRACT_INVALID"):
        world.verify()


def test_failed_assertion_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["assertions"] = [{"expression": "synthetic", "result": False}]
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="CASE_CONTRACT_INVALID"):
        world.verify()


def test_non_pass_case_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["status"] = "FAIL"
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="CASE_NOT_PASS"):
        world.verify()


def test_nonzero_effect_counter_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["side_effects"]["network_calls"] = 1
    world.raws[key][0]["unit_execution"]["counts"]["network_calls"] = 1
    world.raws[key][0]["side_effects"]["network_calls"] = 1
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="UNIT_COUNTER_NONZERO"):
        world.verify()


def test_unobserved_counter_is_not_read_as_zero(world: World) -> None:
    """counterが無い／`null` は「測っていない」。0として数えない。"""
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["side_effects"]["external_effects"] = None
    world.raws[key][0]["unit_execution"]["counts"]["external_effects"] = None
    world.raws[key][0]["side_effects"]["external_effects"] = None
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="UNIT_COUNTER_UNOBSERVED"):
        world.verify()


def test_missing_child_observation_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.raws[key][0]["unit_execution"]["driver_process_launches"] = 1
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="UNIT_CHILD_COVERAGE_MISSING"):
        world.verify()


def test_monitor_without_declared_limits_is_rejected(world: World) -> None:
    """観測限界の宣言が無い監視を「完全に見た」として扱わない。"""
    key = world.targets()["target_case_ids"][0]
    world.raws[key][0]["unit_execution"]["limits"] = []
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="UNIT_MONITOR_LIMITS_MISSING"):
        world.verify()


def test_incomplete_monitor_is_rejected(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.raws[key][0]["unit_execution"]["complete"] = False
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="UNIT_MONITOR_INCOMPLETE"):
        world.verify()


def test_not_applicable_must_keep_a_null_event_sequence(world: World) -> None:
    """未観測を空列へ変換しない。`[]` は「見て0件」の意味である。"""
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["observed_event_sequence"] = []
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="CASE_CONTRACT_INVALID"):
        world.verify()


def test_exemption_other_than_not_applicable_is_rejected(world: World) -> None:
    """NOT_APPLICABLE 以外の免除を認めない。"""
    key = world.targets()["target_case_ids"][0]
    world.cases[key]["event_observation_policy"] = "REQUIRED_EMPTY"
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="CASE_CONTRACT_INVALID"):
        world.verify()


def test_execution_receipt_must_match_the_evidence(world: World) -> None:
    key = world.targets()["target_case_ids"][0]
    world.tamper(f"units/executions/{key}/execution.json", lambda body: body.update(exit_code=1))
    with pytest.raises(formal.FormalResolutionError, match="UNIT_EXECUTION_NONZERO_EXIT"):
        world.verify()


# --------------------------------------------------------------------------
# 対象集合の否定系
# --------------------------------------------------------------------------


def test_missing_target_case_is_rejected(world: World) -> None:
    """対象Caseが1件でもManifestに無ければ閉じない。

    対象は1件ずつ独立に検証するので、全体Findingsの帰属判定（`TARGET_EVIDENCE_MISSING`）
    へ届く前にここで落ちる。全体側は取りこぼしの受け皿として残してある。
    """
    key = world.targets()["target_case_ids"][0]
    world.write()
    world.manifest["test_cases"] = [
        row for row in world.manifest["test_cases"] if f"{row['test_id']}/{row['case_id']}" != key
    ]
    _write(world.manifest_path, world.manifest)
    world.report["manifest_sha256"] = _digest(world.manifest_path)
    _write(world.report_path, world.report)
    with pytest.raises(formal.FormalResolutionError, match="TARGET_CASE_NOT_IN_MANIFEST"):
        world.verify()


def test_duplicate_manifest_row_is_rejected(world: World) -> None:
    world.write()
    world.manifest["test_cases"].append(dict(world.manifest["test_cases"][0]))
    _write(world.manifest_path, world.manifest)
    world.report["manifest_sha256"] = _digest(world.manifest_path)
    _write(world.report_path, world.report)
    with pytest.raises(formal.FormalResolutionError, match="DUPLICATE_MANIFEST_ROW"):
        world.verify()


def test_case_out_of_release_scope_is_rejected(world: World) -> None:
    """無関係なCaseの混入は errors である。未達として見逃さない。"""
    world.extra_case_rows = [
        {
            "test_id": "AT-NOT-IN-SCOPE-999",
            "case_id": "FOREIGN",
            "status": verifier.PASS,
            "input_fixture_hash": "sha256:" + "5" * 64,
            "evidence_path": "units/cases/foreign.json",
            "evidence_manifest_hash": "sha256:" + "5" * 64,
        }
    ]
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="MANIFEST_ERRORS_PRESENT"):
        world.verify()


def test_unit_area_case_set_must_match_the_snapshot(world: World) -> None:
    world.area_case_ids = world.unit_ids[:-1]
    world.write()
    with pytest.raises(formal.FormalResolutionError, match="UNIT_AREA_CASE_SET_MISMATCH"):
        world.verify()


def test_unit_area_case_hashes_must_match_the_manifest(world: World) -> None:
    world.write()
    world.tamper(
        "units/unit-case-suite.json",
        lambda body: body["summary"].update(case_evidence_hashes=["sha256:" + "0" * 64]),
    )
    with pytest.raises(formal.FormalResolutionError, match="UNIT_AREA_EVIDENCE_INVALID"):
        world.verify()


# --------------------------------------------------------------------------
# 継承Chain
# --------------------------------------------------------------------------


def _record(
    directory: Path,
    blocker_id: str,
    *,
    status: str = "RESOLVED",
    supersedes: str | None = None,
    successor: str | None = None,
    task_id: str = "SYNTHETIC-TASK",
    scope: str = SCOPE,
    acceptance: list[str] | None = None,
) -> Path:
    resolution: dict[str, Any] | None = None
    if successor is not None:
        resolution = {"outcome": "SUPERSEDED_BY_DESIGN_HASH", "successor_blocker_id": successor}
    body = {
        "record_version": "1.1",
        "blocker_id": blocker_id,
        "task_id": task_id,
        "release_scope": scope,
        "category": "DECISION_REQUIRED",
        "status": status,
        "observed": {"summary": "synthetic", "command": ["true"], "exit_code": 0},
        "next_action": {"action": "synthetic", "acceptance": acceptance or [f"{blocker_id} 受入"]},
        "owner": "synthetic",
        "created_at": STAMP,
        "resolved_at": STAMP if status == "RESOLVED" else None,
        "design_sha256": "sha256:" + "a" * 64,
        "registry_snapshot_hash": "sha256:" + "b" * 64,
        "supersedes": supersedes,
        "resolution": resolution,
    }
    path = directory / f"{blocker_id}.json"
    _write(path, body)
    return path


def test_chain_collects_every_inherited_acceptance(tmp_path: Path) -> None:
    _record(tmp_path, "BLK-20260101-ROOT", successor="BLK-20260102-MIDDLE", acceptance=["A"])
    _record(
        tmp_path,
        "BLK-20260102-MIDDLE",
        supersedes="BLK-20260101-ROOT",
        successor="BLK-20260103-HEAD",
        acceptance=["B"],
    )
    _record(
        tmp_path,
        "BLK-20260103-HEAD",
        status="OPEN",
        supersedes="BLK-20260102-MIDDLE",
        acceptance=["C"],
    )
    chain = formal.validate_chain(formal.walk_chain(tmp_path, "BLK-20260103-HEAD"))
    assert chain["chain_length"] == 3
    assert chain["inherited_acceptance"] == ["C", "B", "A"]


def test_supersedes_cycle_is_rejected(tmp_path: Path) -> None:
    _record(tmp_path, "BLK-20260101-ONE", status="OPEN", supersedes="BLK-20260102-TWO")
    _record(tmp_path, "BLK-20260102-TWO", supersedes="BLK-20260101-ONE")
    with pytest.raises(formal.FormalResolutionError, match="SUPERSEDES_CYCLE"):
        formal.walk_chain(tmp_path, "BLK-20260101-ONE")


def test_self_supersedes_is_rejected(tmp_path: Path) -> None:
    _record(tmp_path, "BLK-20260101-SELF", status="OPEN", supersedes="BLK-20260101-SELF")
    with pytest.raises(formal.FormalResolutionError, match="SELF_SUPERSEDES"):
        formal.walk_chain(tmp_path, "BLK-20260101-SELF")


def test_missing_predecessor_is_rejected(tmp_path: Path) -> None:
    _record(tmp_path, "BLK-20260101-HEAD", status="OPEN", supersedes="BLK-20259999-GONE")
    with pytest.raises(formal.FormalResolutionError, match="UNKNOWN_BLOCKER"):
        formal.walk_chain(tmp_path, "BLK-20260101-HEAD")


def test_two_open_records_in_one_chain_are_rejected(tmp_path: Path) -> None:
    _record(tmp_path, "BLK-20260101-OLD", status="OPEN", successor="BLK-20260102-NEW")
    _record(tmp_path, "BLK-20260102-NEW", status="OPEN", supersedes="BLK-20260101-OLD")
    with pytest.raises(formal.FormalResolutionError, match="MULTIPLE_OPEN_IN_CHAIN"):
        formal.validate_chain(formal.walk_chain(tmp_path, "BLK-20260102-NEW"))


def test_task_or_scope_change_inside_a_chain_is_rejected(tmp_path: Path) -> None:
    _record(tmp_path, "BLK-20260101-OTHER", successor="BLK-20260102-HEAD", task_id="OTHER-TASK")
    _record(tmp_path, "BLK-20260102-HEAD", status="OPEN", supersedes="BLK-20260101-OTHER")
    with pytest.raises(formal.FormalResolutionError, match="CHAIN_TASK_MISMATCH"):
        formal.validate_chain(formal.walk_chain(tmp_path, "BLK-20260102-HEAD"))

    other = tmp_path / "scope"
    other.mkdir()
    _record(other, "BLK-20260101-SCOPE", successor="BLK-20260102-HEAD", scope="MVP0-B")
    _record(other, "BLK-20260102-HEAD", status="OPEN", supersedes="BLK-20260101-SCOPE")
    with pytest.raises(formal.FormalResolutionError, match="CHAIN_SCOPE_MISMATCH"):
        formal.validate_chain(formal.walk_chain(other, "BLK-20260102-HEAD"))


def test_broken_successor_link_is_rejected(tmp_path: Path) -> None:
    _record(tmp_path, "BLK-20260101-ROOT", successor="BLK-20260199-ELSEWHERE")
    _record(tmp_path, "BLK-20260102-HEAD", status="OPEN", supersedes="BLK-20260101-ROOT")
    with pytest.raises(formal.FormalResolutionError, match="CHAIN_LINK_NOT_BIDIRECTIONAL"):
        formal.validate_chain(formal.walk_chain(tmp_path, "BLK-20260102-HEAD"))


def test_filename_and_body_must_agree(tmp_path: Path) -> None:
    path = _record(tmp_path, "BLK-20260101-HEAD", status="OPEN")
    body = json.loads(path.read_text(encoding="utf-8"))
    body["blocker_id"] = "BLK-20260101-OTHER"
    _write(path, body)
    with pytest.raises(formal.FormalResolutionError, match="BLOCKER_ID_FILENAME_MISMATCH"):
        formal.walk_chain(tmp_path, "BLK-20260101-HEAD")


@pytest.mark.parametrize(
    "blocker_id",
    ["../outside", "BLK-20260101-A/../../etc", "blk-lowercase", "", "BLK-2026-BAD"],
)
def test_path_traversal_in_a_blocker_id_is_rejected(tmp_path: Path, blocker_id: str) -> None:
    with pytest.raises(formal.FormalResolutionError, match="INVALID_BLOCKER_ID"):
        formal.record_path(tmp_path, blocker_id)


def test_symlinked_record_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    _write(outside, {"blocker_id": "BLK-20260101-LINK"})
    records = tmp_path / "records"
    records.mkdir()
    os.symlink(outside, records / "BLK-20260101-LINK.json")
    with pytest.raises(formal.FormalResolutionError, match="BLOCKER_RECORD_IS_SYMLINK"):
        formal.record_path(records, "BLK-20260101-LINK")


# --------------------------------------------------------------------------
# 証跡の適用条件（対象Taskとの結び付き）
# --------------------------------------------------------------------------


def test_formal_evidence_cannot_close_an_unrelated_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    snapshot: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """回帰: F-2 の証跡で無関係な塞がりを閉じないこと。

    再現は「Backup 復旧が未確認のまま」の Block へ F-2 の Unit Case Evidence を
    渡すというものである。Release Scope も正本Hashも一致しているので、Task を
    見ない限り検証はすべて通ってしまい、終了値0で RESOLVED になっていた。

    **証跡の適用条件は「その証跡がその塞がりを解いたこと」である。**
    Scope と Hash の一致はそれを意味しない。
    """
    records = tmp_path / "records"
    records.mkdir()
    path = _record(
        records,
        "BLK-20260105-BACKUP-RESTORE-UNVERIFIED",
        status="OPEN",
        task_id="MVP0A-BACKUP-RESTORE",
        acceptance=["Backup からの復旧が実測で確認されている"],
    )
    body = json.loads(path.read_text(encoding="utf-8"))
    # 正本束縛は現行と一致させる。ここが合っていても閉じないことを見る。
    body["design_sha256"] = snapshot["design_sha256"]
    body["registry_snapshot_hash"] = snapshot["registry_snapshot_hash"]
    _write(path, body)
    before = path.read_bytes()

    monkeypatch.setattr(resolver, "RECORDS", records)
    assert (
        resolver.main(
            [
                "--blocker-id",
                "BLK-20260105-BACKUP-RESTORE-UNVERIFIED",
                "--formal-manifest",
                str(world.manifest_path),
                "--evidence-root",
                str(world.root),
                "--verification-report",
                str(world.report_path),
                "--release-scope",
                SCOPE,
                "--implementation-commit",
                world.commit,
                "--summary",
                "F-2 の証跡で Backup の塞がりを閉じようとする",
                "--verified-by",
                "synthetic",
                "--decided-by",
                "synthetic",
                "--decided-at",
                STAMP,
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "FORMAL_MODE_NOT_APPLICABLE" in err
    assert formal.APPLICABLE_TASK_ID in err
    # Record は1 Byteも動いていない。
    assert path.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "OPEN"


def test_the_applicable_task_is_the_one_the_owner_decision_covers(
    snapshot: dict[str, Any],
) -> None:
    """適用先Taskと、対象集合の出どころが一致していること。

    保存 Block Record の論理 Task との一致は非公開側で確かめる
    （`tests/private_history/test_private_blocked_record_resolution_formal.py`）。
    """
    targets = formal.derive_target_cases(DECISION_DIR, snapshot, SCOPE)
    assert targets["applicable_task_id"] == formal.APPLICABLE_TASK_ID


@pytest.mark.parametrize(
    "task_id",
    ["MVP0A-BACKUP-RESTORE", "MVP0A-RUNTIME-GO-20260815", "TASK-LLM-001", "SYNTHETIC-TASK"],
)
def test_require_applicable_task_rejects_every_other_task(task_id: str) -> None:
    with pytest.raises(formal.FormalResolutionError, match="FORMAL_MODE_NOT_APPLICABLE"):
        formal.require_applicable_task({"blocker_id": "BLK-20260101-X", "task_id": task_id})


# --------------------------------------------------------------------------
# 書込みの安全性
# --------------------------------------------------------------------------


def test_durable_replace_detects_a_concurrent_change(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"original")
    with pytest.raises(resolver.ResolveError, match="RECORD_CHANGED_DURING_VERIFICATION"):
        resolver._durable_replace(path, b"stale-view", b"new")
    assert path.read_bytes() == b"original"


def test_a_concurrent_update_during_the_temp_write_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰: 初回のBytes比較のあとに入った更新を消さないこと。

    元Bytesを確認してから `os.replace` するまでの間に別の更新が入ると、
    比較が1回だけでは黙って上書きされる。**公開直前にもう一度照合する。**
    """
    path = tmp_path / "record.json"
    path.write_bytes(b"original")

    real_fsync = os.fsync
    injected: list[bool] = []

    def fsync_then_let_someone_else_write(fd: int) -> None:
        real_fsync(fd)
        if not injected:
            injected.append(True)
            # Temp の fsync 中に割り込んだ別の更新。
            path.write_bytes(b"someone-elses-update")

    monkeypatch.setattr(os, "fsync", fsync_then_let_someone_else_write)
    with pytest.raises(resolver.ResolveError, match="RECORD_CHANGED_BEFORE_PUBLISH"):
        resolver._durable_replace(path, b"original", b"mine")

    assert injected, "割り込み更新が起きていない。試験が何も見ていない"
    # 割り込んだ更新が残っていること。こちらの payload で塗り潰していない。
    assert path.read_bytes() == b"someone-elses-update"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["record.json"]


def test_a_post_publish_durability_failure_says_the_record_was_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回帰: Rename後の永続化失敗を「失敗＝元のまま」と読ませないこと。

    `os.replace` が返った後の Directory fsync 失敗では、**Record は更新済み**である。
    `ResolveError` と同じ例外にすると、読んだ人が更新済みRecordを放置する。
    """
    path = tmp_path / "record.json"
    path.write_bytes(b"original")

    real_fsync = os.fsync

    def fail_only_on_the_directory(fd: int) -> None:
        if os.fstat(fd).st_mode & 0o040000:  # S_IFDIR
            raise OSError(5, "injected directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_only_on_the_directory)
    with pytest.raises(
        resolver.DurabilityUnconfirmed, match="DIRECTORY_FSYNC_FAILED_AFTER_PUBLISH"
    ):
        resolver._durable_replace(path, b"original", b"mine")

    # 公開は済んでいる。ここを "original" だと説明したら嘘になる。
    assert path.read_bytes() == b"mine"
    # 更新済みであることと復帰手順が、例外の型と文面から読めること。
    assert not issubclass(resolver.DurabilityUnconfirmed, resolver.ResolveError)
    recovery = resolver.DurabilityUnconfirmed.RECOVERY
    # 公開が成功したことは言い切る。
    assert "公開" in recovery and "成功" in recovery
    # **現在も必ず RESOLVED だとは断定しない**（R5 の受入条件）。
    assert "とは限らない" in recovery
    # 復帰手順と、自動復元しない方針が読めること。
    assert "validate_blocked_record.py" in recovery
    assert "自動で元Bytesへ復元しない" in recovery


def test_main_separates_a_durability_failure_from_a_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    snapshot: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """終了値でも両者を分ける。検証失敗=1、公開後の永続化未確認=2。"""
    records = tmp_path / "records"
    records.mkdir()
    head = "BLK-20260104-SYNTHETIC-F2"
    path = _record(records, head, status="OPEN", task_id=formal.APPLICABLE_TASK_ID)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["design_sha256"] = snapshot["design_sha256"]
    body["registry_snapshot_hash"] = snapshot["registry_snapshot_hash"]
    _write(path, body)

    def boom(_: Path, __: bytes, ___: bytes) -> None:
        raise resolver.DurabilityUnconfirmed("DIRECTORY_FSYNC_FAILED_AFTER_PUBLISH: injected")

    monkeypatch.setattr(resolver, "RECORDS", records)
    monkeypatch.setattr(resolver, "_durable_replace", boom)
    assert (
        resolver.main(
            [
                "--blocker-id",
                head,
                "--formal-manifest",
                str(world.manifest_path),
                "--evidence-root",
                str(world.root),
                "--verification-report",
                str(world.report_path),
                "--release-scope",
                SCOPE,
                "--implementation-commit",
                world.commit,
                "--summary",
                "synthetic",
                "--verified-by",
                "synthetic",
                "--decided-by",
                "synthetic",
                "--decided-at",
                STAMP,
            ]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "RECORD UPDATED BUT DURABILITY UNCONFIRMED" in err
    assert "NOT_OPEN で止まる" in err


def test_durable_replace_writes_atomically_and_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(b"original")
    resolver._durable_replace(path, b"original", b"updated")
    assert path.read_bytes() == b"updated"
    # Temp Fileを残さない。
    assert sorted(p.name for p in tmp_path.iterdir()) == ["record.json"]


def test_a_failed_verification_leaves_the_record_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    records = tmp_path / "records"
    records.mkdir()
    path = _record(records, "BLK-20260101-HEAD", status="OPEN")
    before = path.read_bytes()
    monkeypatch.setattr(resolver, "RECORDS", records)
    assert (
        resolver.main(
            [
                "--blocker-id",
                "BLK-20260101-HEAD",
                "--formal-manifest",
                str(tmp_path / "absent.json"),
                "--evidence-root",
                str(tmp_path),
                "--verification-report",
                str(tmp_path / "absent-report.json"),
                "--release-scope",
                SCOPE,
                "--implementation-commit",
                "0" * 40,
                "--summary",
                "synthetic",
                "--verified-by",
                "synthetic",
                "--decided-by",
                "synthetic",
                "--decided-at",
                STAMP,
            ]
        )
        == 1
    )
    assert "not resolved" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_an_already_resolved_record_is_not_updated_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    records = tmp_path / "records"
    records.mkdir()
    path = _record(records, "BLK-20260101-DONE", status="RESOLVED")
    before = path.read_bytes()
    monkeypatch.setattr(resolver, "RECORDS", records)
    assert (
        resolver.main(
            [
                "--blocker-id",
                "BLK-20260101-DONE",
                "--evidence",
                str(tmp_path / "x.json"),
                "--summary",
                "synthetic",
                "--verified-by",
                "synthetic",
                "--decided-by",
                "synthetic",
                "--decided-at",
                STAMP,
            ]
        )
        == 1
    )
    assert "NOT_OPEN" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_formal_resolution_updates_only_the_mutable_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, world: World, snapshot: dict[str, Any]
) -> None:
    """解消は status / resolved_at / resolution だけを足す。不変Fieldは動かさない。"""
    records = tmp_path / "records"
    records.mkdir()
    head = "BLK-20260103-SYNTHETIC-F2"
    # 正式方式は F-2 の Task だけを閉じる。合成Recordもその Task を名乗る。
    _record(
        records,
        "BLK-20260102-SYNTHETIC-OLD",
        successor=head,
        task_id=formal.APPLICABLE_TASK_ID,
    )
    path = _record(
        records,
        head,
        status="OPEN",
        supersedes="BLK-20260102-SYNTHETIC-OLD",
        task_id=formal.APPLICABLE_TASK_ID,
    )
    body = json.loads(path.read_text(encoding="utf-8"))
    body["design_sha256"] = snapshot["design_sha256"]
    body["registry_snapshot_hash"] = snapshot["registry_snapshot_hash"]
    _write(path, body)
    before = json.loads(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(resolver, "RECORDS", records)
    assert (
        resolver.main(
            [
                "--blocker-id",
                head,
                "--formal-manifest",
                str(world.manifest_path),
                "--evidence-root",
                str(world.root),
                "--verification-report",
                str(world.report_path),
                "--release-scope",
                SCOPE,
                "--implementation-commit",
                world.commit,
                "--summary",
                "synthetic formal resolution",
                "--verified-by",
                "tests/spec_lint/test_blocked_record_resolution_formal.py",
                "--decided-by",
                "synthetic",
                "--decided-at",
                STAMP,
            ]
        )
        == 0
    )
    after = json.loads(path.read_text(encoding="utf-8"))
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == resolver.MUTABLE_KEYS
    assert after["status"] == "RESOLVED"
    assert after["resolution"]["outcome"] == "RESOLVED_BY_FIX"
    assert after["resolution"]["evidence_mode"] == "formal"
    assert "successor_blocker_id" not in after["resolution"]
    verification = after["resolution"]["formal_verification"]
    assert verification["supersedes_chain"]["chain_length"] == 2
    assert verification["formal_evidence"]["coverage"]["target_case_count"] == 7
    # 全体の未達は解消根拠から除外したことを残す。GOを主張しない。
    assert verification["formal_evidence"]["out_of_scope"]["global_missing_count"] > 0


def test_a_stale_record_binding_cannot_be_closed_by_current_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """旧正本へ束縛された歴史Recordを、現正本の証跡で閉じない。"""
    records = tmp_path / "records"
    records.mkdir()
    path = _record(records, "BLK-20260101-STALE", status="OPEN", task_id=formal.APPLICABLE_TASK_ID)
    before = path.read_bytes()
    monkeypatch.setattr(resolver, "RECORDS", records)
    assert (
        resolver.main(
            [
                "--blocker-id",
                "BLK-20260101-STALE",
                "--formal-manifest",
                str(world.manifest_path),
                "--evidence-root",
                str(world.root),
                "--verification-report",
                str(world.report_path),
                "--release-scope",
                SCOPE,
                "--implementation-commit",
                world.commit,
                "--summary",
                "synthetic",
                "--verified-by",
                "synthetic",
                "--decided-by",
                "synthetic",
                "--decided-at",
                STAMP,
            ]
        )
        == 1
    )
    assert "RECORD_REGISTRY_BINDING_STALE" in capsys.readouterr().err
    assert path.read_bytes() == before


# --------------------------------------------------------------------------
# R4 / R5 — 読取りスナップショットと公開後I/Oの分類
# --------------------------------------------------------------------------


def _applicable_open_record(records: Path, snapshot: dict[str, Any], blocker_id: str) -> Path:
    """正式方式の適用対象Taskを名乗る、現正本へ束縛したOPEN Record。"""
    path = _record(records, blocker_id, status="OPEN", task_id=formal.APPLICABLE_TASK_ID)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["design_sha256"] = snapshot["design_sha256"]
    body["registry_snapshot_hash"] = snapshot["registry_snapshot_hash"]
    _write(path, body)
    return path


def _resolve_argv_for(blocker_id: str, world: World) -> list[str]:
    return [
        "--blocker-id",
        blocker_id,
        "--formal-manifest",
        str(world.manifest_path),
        "--evidence-root",
        str(world.root),
        "--verification-report",
        str(world.report_path),
        "--release-scope",
        SCOPE,
        "--implementation-commit",
        world.commit,
        "--summary",
        "synthetic",
        "--verified-by",
        "synthetic",
        "--decided-by",
        "synthetic",
        "--decided-at",
        STAMP,
    ]


def test_r4_json_and_comparison_bytes_come_from_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    snapshot: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R4 回帰: 読んだJSONと比較用Bytesが別スナップショットにならないこと。

    JSONを読んでから比較用Bytesを読むまでの間に協調更新が入ると、旧JSONを基準に
    検証し、新Bytesを「変化なし」の基準にしてしまう。結果、**割り込んだ更新が
    黙って消える**。同じBytesからparseすれば、この窓が無くなる。
    """
    records = tmp_path / "records"
    records.mkdir()
    blocker = "BLK-20260106-SYNTHETIC-F2"
    path = _applicable_open_record(records, snapshot, blocker)

    injected: list[bool] = []
    real_load = resolver._load

    def load_then_let_someone_else_write(blocker: str) -> Any:
        loaded = real_load(blocker)
        if not injected:
            injected.append(True)
            # 初期読取りの**直後**に入った協調更新。受入条件の追記。
            # 修正前はこの後に比較用Bytesを読むため、原本は新内容・JSONは旧内容になり、
            # 旧JSONで上書きして成功していた。
            body = json.loads(path.read_text(encoding="utf-8"))
            body["next_action"]["acceptance"].append("並行して追記された受入条件")
            _write(path, body)
        return loaded

    monkeypatch.setattr(resolver, "_load", load_then_let_someone_else_write)
    monkeypatch.setattr(resolver, "RECORDS", records)
    returncode = resolver.main(_resolve_argv_for(blocker, world))
    monkeypatch.undo()

    assert injected, "割り込み更新が起きていない。試験が何も見ていない"
    after = json.loads(path.read_text(encoding="utf-8"))
    # **割り込んだ更新が消えていないこと。** ここが R4 の本体である。
    assert "並行して追記された受入条件" in after["next_action"]["acceptance"], (
        "並行して入った更新が失われた"
    )
    # 古い状態のまま成功させない。
    assert returncode != 0, "古いスナップショットで成功している"
    assert after["status"] == "OPEN"
    assert "RECORD_CHANGED" in capsys.readouterr().err


def test_r4_two_cooperating_resolvers_do_not_lose_an_update(
    tmp_path: Path, snapshot: dict[str, Any]
) -> None:
    """R4 回帰: 協調する2つのResolverが競合しても更新を落とさないこと。

    Lockは**協調する更新者**にだけ効く。非協調の直接編集まで防げるとは主張しない。
    """
    records = tmp_path / "records"
    records.mkdir()
    path = _applicable_open_record(records, snapshot, "BLK-20260107-SYNTHETIC-F2")
    first = path.read_bytes()

    # 先行Resolverが公開した後の内容。
    published = json.loads(first.decode("utf-8"))
    published["status"] = "RESOLVED"
    published["resolved_at"] = STAMP
    published["resolution"] = {"outcome": "RESOLVED_BY_FIX", "verified_by": ["first"]}
    winner = (json.dumps(published, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    resolver._durable_replace(path, first, winner)
    assert path.read_bytes() == winner

    # 後続Resolverが古い基準で公開しようとする。上書きさせない。
    loser = (json.dumps({"stale": True}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with pytest.raises(resolver.ResolveError, match="RECORD_CHANGED_DURING_VERIFICATION"):
        resolver._durable_replace(path, first, loser)
    assert path.read_bytes() == winner, "先行Resolverの更新が失われた"


def test_r5_post_publish_read_error_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5 回帰: 公開後の再読取りI/O失敗も専用例外へ分類すること。"""
    path = tmp_path / "record.json"
    path.write_bytes(b"original")

    # 回数を数えず、`os.replace` が返ったかどうかで公開の前後を判定する。
    # Fixture helper の読取りが混ざっても位置がずれない。
    published: list[bool] = []
    real_replace = os.replace
    real_read_bytes = Path.read_bytes

    def note_publish(src: Any, dst: Any, **kwargs: Any) -> None:
        real_replace(src, dst, **kwargs)
        published.append(True)

    def fail_only_after_publish(self: Path) -> bytes:
        if self == path and published:
            raise PermissionError(13, "injected post-publish read failure")
        return real_read_bytes(self)

    monkeypatch.setattr(os, "replace", note_publish)
    monkeypatch.setattr(Path, "read_bytes", fail_only_after_publish)
    with pytest.raises(resolver.DurabilityUnconfirmed, match="READBACK_FAILED_AFTER_PUBLISH"):
        resolver._durable_replace(path, b"original", b"mine")
    monkeypatch.undo()

    assert published, "公開まで到達していない"
    # 置換は成功している。読めなかったこと自体は置換の失敗ではない。
    assert path.read_bytes() == b"mine"


def test_r5_main_reports_a_post_publish_read_error_as_exit_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    snapshot: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R5 回帰: main() を通して終了値2・更新済みの説明・復旧案内が出ること。"""
    records = tmp_path / "records"
    records.mkdir()
    blocker = "BLK-20260108-SYNTHETIC-F2"
    path = _applicable_open_record(records, snapshot, blocker)

    real_replace = os.replace
    real_read_bytes = Path.read_bytes
    published: list[bool] = []

    def note_publish(src: Any, dst: Any, **kwargs: Any) -> None:
        real_replace(src, dst, **kwargs)
        published.append(True)

    def fail_only_after_publish(self: Path) -> bytes:
        if self == path and published:
            raise OSError(5, "injected post-publish read failure")
        return real_read_bytes(self)

    monkeypatch.setattr(resolver, "RECORDS", records)
    monkeypatch.setattr(os, "replace", note_publish)
    monkeypatch.setattr(Path, "read_bytes", fail_only_after_publish)
    returncode = resolver.main(_resolve_argv_for(blocker, world))
    monkeypatch.undo()

    assert published, "公開まで到達していない"
    assert returncode == 2, "公開後のI/O失敗が終了値2へ分類されていない"
    err = capsys.readouterr().err
    assert "RECORD UPDATED BUT DURABILITY UNCONFIRMED" in err
    assert "NOT_OPEN で止まる" in err
    # 置換後のBytesが実際に残っていること。例外の型だけを見て終わらせない。
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "RESOLVED"
