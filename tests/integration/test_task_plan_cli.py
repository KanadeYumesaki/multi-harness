"""CC-03 合成 Task → Context → 決定論的 Plan の統合試験。

実 SQLite と実 Filesystem を使い、**CLI と同じ Application 経路**を通す。
確かめるのは次の4種である。

1. 正常：Markdown／Text／JSON／Source から Plan が立つ
2. 不正入力：不正 JSON、不正 UTF-8、未知形式、Path escape、Symlink、Scope 外
3. 予算：ちょうど収まる／1 Token 足りない、と超過時に Provider を呼ばないこと
4. 決定性：凍結入力から2回、別 `PYTHONHASHSEED` の子 Process で3回目

**Approval も Effect も起こさない。** CC-03 の範囲は Plan までである。
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

from harness.application.task_plan_service import TaskPlanRequest
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.input_read import CapabilityScope
from harness.infrastructure.provider.mock_provider import DeterministicMockProvider
from harness.infrastructure.runtime_facade import HarnessRuntimeService, TaskPlanSetup

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-09-06T00:00:00Z"
ISSUED = "2026-09-06T00:00:00Z"
EXPIRES = "2026-09-06T01:00:00Z"
TASK_BODY = b"# Synthetic task\n\nRename the greeting.\n"

#: Capability ID は **安定値**である。`input_read_capability_set_hash` は
#: `ExecutionPlan` の必須 Field なので、ここを実行ごとの採番にすると
#: 同じ入力でも Plan Content Hash が動く。権限の名前であって採番ではない。
CAPABILITY_ID = "cap-cc03-docs"


def _sha256_of(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _declaration(*, total_tokens: int = 4000) -> dict[str, Any]:
    """人が宣言する側。**Harness に埋めさせない値をここへ書く。**"""
    executable = Path(sys.executable).resolve()
    return {
        "planner_algorithm_version": "harness-planner/1.0",
        "planner_identity": "harness-planner/mvp0a",
        "authority_scope": "ACTION_EXECUTION",
        "intent": {
            "action_type": "LOCAL_FILE_PROPOSAL",
            "data_classification": "SYNTHETIC",
            "trust_level": "UNTRUSTED_INPUT",
            "risk_level": "LOW",
            "maximum_attempts": 1,
            "timeout_seconds": 60,
        },
        "runtime_envelope": {
            "runtime_spec_version": "1.0.0",
            "runtime_type": "MOCK_ADAPTER",
            "launcher_version": "harness-launcher/1.0",
            "executable_path": str(executable),
            "executable_sha256": _sha256_of(executable),
            "argv": ["harness-mock-provider", "--offline"],
            "working_directory": "/workspace",
            "workspace_id": "cc03-workspace",
            "os_boundary": "LINUX",
            "provider": "mock",
            "adapter": "mock-adapter/1.0",
            "auth_route": "NONE",
            "model": "mock-model",
        },
        "invocation": {
            "invocation_mode": "MOCK",
            "provider": "mock",
            "billing_mode": "FREE",
            "expected_effect": "NONE",
            "model": "mock-model",
        },
        "token_profile": {
            "snapshot_id": "cc03-profile",
            "provider": "mock",
            "model": "mock-model",
            "tokenizer_name": "harness-utf8-byte-upper-bound",
            "tokenizer_version": "1.0.0",
            "counting_adapter_version": "harness-token-counter/2",
            "context_limit": 4096,
            "maximum_output_limit": 1024,
            "estimate_assurance": "CONSERVATIVE",
            "overheads": {
                "system_message_overhead": 3,
                "developer_message_overhead": 0,
                "tool_definition_overhead": 0,
                "per_message_overhead": 2,
                "structured_output_overhead": 0,
                "streaming_frame_overhead": 0,
                "retry_fallback_reservation": 0,
            },
            "retrieved_at": "2026-09-05T00:00:00Z",
            "expires_at": "2027-09-05T00:00:00Z",
        },
        "token_budget": {
            "policy_id": "cc03-budget",
            "total_tokens": total_tokens,
            "reserved_output_tokens": 16,
            "reserved_tool_tokens": 0,
        },
        "actions": [
            {
                "action_type": "LOCAL_FILE_WRITE",
                "normalized_inputs": {"source": "task"},
                "normalized_outputs": {"target_relative_path": "docs/out.md"},
            }
        ],
    }


def _workspace(root: Path, *, total_tokens: int = 4000, task_body: bytes = TASK_BODY) -> Path:
    workspace = root / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "task.md").write_bytes(task_body)
    (workspace / "docs" / "task.txt").write_bytes(b"plain synthetic task\n")
    (workspace / "docs" / "task.json").write_bytes(b'{"goal": "rename the greeting"}\n')
    (workspace / "docs" / "task.py").write_bytes(b"print('data, not a program')\n")
    (workspace / "docs" / "declaration.json").write_text(
        json.dumps(_declaration(total_tokens=total_tokens), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return workspace


def _setup(root: Path, workspace: Path) -> TaskPlanSetup:
    return TaskPlanSetup(
        repo_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=root / "artifacts",
        capability_id=CAPABILITY_ID,
        capability_scope=CapabilityScope(("docs",)),
        now=NOW,
        # tmp_path は tmpfs のことがある。**試験でだけ許す。**
        allow_test_filesystems=True,
    )


def _request(*, task_path: str, run_id: str = "run-1") -> TaskPlanRequest:
    return TaskPlanRequest(
        stream_id="CC03_STREAM",
        capability_id=CAPABILITY_ID,
        task_relative_path=task_path,
        declaration_relative_path="docs/declaration.json",
        task_read_decision_id=f"{run_id}:task",
        declaration_read_decision_id=f"{run_id}:declaration",
        bundle_id=f"bundle-{run_id}",
        receipt_id=f"receipt-{run_id}",
        run_id=run_id,
        execution_plan_id=f"plan-{run_id}",
        issued_at=ISSUED,
        expires_at=EXPIRES,
        now=NOW,
        workspace_snapshot_id="cc03-workspace-snapshot",
    )


def _plan(root: Path, workspace: Path, *, task_path: str, run_id: str = "run-1") -> Any:
    service = HarnessRuntimeService(root / "harness.db")
    service.migrate(recorded_at=NOW)
    return service.plan_task(_setup(root, workspace), _request(task_path=task_path, run_id=run_id))


# -- 1. 正常経路 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("task_path", "expected_format"),
    [
        ("docs/task.md", "TASK_MARKDOWN"),
        ("docs/task.txt", "TASK_TEXT"),
        ("docs/task.json", "TASK_JSON"),
        ("docs/task.py", "TASK_SOURCE"),
    ],
)
def test_each_supported_format_produces_a_plan(
    tmp_path: Path, task_path: str, expected_format: str
) -> None:
    """4形式それぞれから Plan が立ち、分類が Ledger の Event にも載ること。"""
    workspace = _workspace(tmp_path)
    outcome = _plan(tmp_path, workspace, task_path=task_path)

    assert outcome.task_format.value == expected_format
    assert outcome.task_read.classification == expected_format
    assert outcome.task_read.ledger_head_after > outcome.task_read.ledger_head_before
    assert str(outcome.plan.plan_content_hash).startswith("sha256:")
    outcome.plan.assert_integrity()


def test_source_input_stays_data_and_never_claims_a_control_role(tmp_path: Path) -> None:
    """Source を読んでも Control Role へ昇格しないこと。

    昇格すれば入力本文が Instruction として効きうる。Role は入力の拡張子で
    決まってはならない（§3.6 手順2）。
    """
    workspace = _workspace(tmp_path)
    outcome = _plan(tmp_path, workspace, task_path="docs/task.py")

    fragments = outcome.assembly.receipt.selected_fragment_ids
    assert len(fragments) == 1
    # Bundle は本文を持たない。Role の証跡は Manifest Hash 側にある。
    assert outcome.assembly.bundle.message_role_manifest_hash != outcome.plan.plan_content_hash
    # Control Role を名乗る候補は Service が作らない。作れば `require_admissible`
    # が `CONTROL_DATA_ROLE_ESCALATION` で落ちる。ここでは落ちずに通ることで
    # 「Untrusted のまま扱われた」ことを示す。
    assert outcome.task_format.value == "TASK_SOURCE"


# -- 2. 不正入力 -------------------------------------------------------------


def test_invalid_json_task_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "docs" / "task.json").write_bytes(b'{"goal": "unterminated\n')
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path="docs/task.json")
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_invalid_utf8_task_is_rejected(tmp_path: Path) -> None:
    """壊れた Byte 列を U+FFFD で埋めて「読めた」ことにしない。"""
    workspace = _workspace(tmp_path)
    (workspace / "docs" / "task.md").write_bytes(b"# broken \xff\xfe tail\n")
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path="docs/task.md")
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_unknown_format_suffix_is_rejected(tmp_path: Path) -> None:
    """未知の拡張子を Text へ倒さない。"""
    workspace = _workspace(tmp_path)
    (workspace / "docs" / "task.bin").write_bytes(b"binary-ish\n")
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path="docs/task.bin")
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


@pytest.mark.parametrize(
    ("task_path", "expected"),
    [
        ("../outside.md", ErrorCode.PATH_OUTSIDE_CAPABILITY),
        ("secret/keys.md", ErrorCode.PATH_OUTSIDE_CAPABILITY),
    ],
)
def test_paths_outside_the_capability_are_denied(
    tmp_path: Path, task_path: str, expected: ErrorCode
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "secret").mkdir()
    (workspace / "secret" / "keys.md").write_bytes(b"do not read\n")
    (tmp_path / "outside.md").write_bytes(b"outside\n")
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path=task_path)
    assert caught.value.code is expected


def test_symlink_task_is_denied(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "docs" / "link.md").symlink_to("/etc/hostname")
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path="docs/link.md")
    assert caught.value.code is ErrorCode.SYMLINK_DENIED


def test_declared_executable_digest_must_match_the_measured_one(tmp_path: Path) -> None:
    """宣言された実行体 SHA-256 を実測と突き合わせること。"""
    workspace = _workspace(tmp_path)
    document = _declaration()
    document["runtime_envelope"]["executable_sha256"] = "sha256:" + "0" * 64  # type: ignore[index]
    (workspace / "docs" / "declaration.json").write_text(
        json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path="docs/task.md")
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_unknown_declaration_key_is_rejected(tmp_path: Path) -> None:
    """綴り違いの宣言を「宣言しなかった」と同じ扱いにしない。"""
    workspace = _workspace(tmp_path)
    document = _declaration()
    document["invocation"]["endpoint"] = "https://example.invalid"  # type: ignore[index]
    (workspace / "docs" / "declaration.json").write_text(
        json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path="docs/task.md")
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_non_mock_invocation_mode_is_rejected(tmp_path: Path) -> None:
    """外部通信を持つ Mode を MVP0-A で組み立てない。"""
    workspace = _workspace(tmp_path)
    document = _declaration()
    document["invocation"]["invocation_mode"] = "EXTERNAL"  # type: ignore[index]
    (workspace / "docs" / "declaration.json").write_text(
        json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path="docs/task.md")
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


# -- 3. 予算 -----------------------------------------------------------------


def test_budget_boundary_just_fits_and_one_token_short_fails(tmp_path: Path) -> None:
    """境界のちょうど／1不足を両方確かめる。

    `available = total - reserved_output - fixed_overhead` が実効コスト以上なら
    通る。境界値は式から出しており、実装結果へ寄せていない。
    """
    fits_root = tmp_path / "fits"
    fits_root.mkdir()
    fits = _workspace(fits_root, total_tokens=60)
    outcome = _plan(fits_root, fits, task_path="docs/task.md")
    assert outcome.assembly.bundle.total_token_count == 41

    short_root = tmp_path / "short"
    short_root.mkdir()
    short = _workspace(short_root, total_tokens=59)
    with pytest.raises(HarnessError) as caught:
        _plan(short_root, short, task_path="docs/task.md")
    assert caught.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


def test_budget_overflow_does_not_invoke_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """予算超過で Provider を呼ばないこと。

    値を最大値へ丸めて成功にしない。Mock Provider の `propose` を落とし穴に
    差し替え、呼ばれたら試験が落ちるようにする。**そもそも Plan 経路は
    Provider を持たない**ので、これはその裏付けである。
    """

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("provider was invoked while planning")

    monkeypatch.setattr(DeterministicMockProvider, "propose", _boom)
    workspace = _workspace(tmp_path, total_tokens=20)
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path="docs/task.md")
    assert caught.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


def test_planning_never_invokes_the_provider_on_the_success_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功経路でも Provider を呼ばないこと。CC-03 は Plan までである。"""

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("provider was invoked while planning")

    monkeypatch.setattr(DeterministicMockProvider, "propose", _boom)
    workspace = _workspace(tmp_path)
    outcome = _plan(tmp_path, workspace, task_path="docs/task.md")
    assert outcome.plan.plan_content_hash is not None


# -- 4. 決定性 ---------------------------------------------------------------


def test_same_frozen_input_gives_the_same_plan_content_hash(tmp_path: Path) -> None:
    """採番 ID と Authority を変えても Plan Content Hash が動かないこと。

    `run_id`／`execution_plan_id`／`bundle_id`／`receipt_id` はすべて変える。
    動けば、採番 ID が Plan Content へ漏れている（不変条件#4）。
    """
    workspace = _workspace(tmp_path)
    first = _plan(tmp_path, workspace, task_path="docs/task.md", run_id="run-a")
    second = _plan(tmp_path, workspace, task_path="docs/task.md", run_id="run-b")

    assert first.plan.plan_content_hash == second.plan.plan_content_hash
    # Authority は変わっているので Execution Plan Hash は変わる（§3.7.2）。
    assert first.plan.execution_plan_hash != second.plan.execution_plan_hash


def test_changing_one_byte_of_the_task_changes_the_plan(tmp_path: Path) -> None:
    """入力が1 Byte 変われば Plan Content Hash が変わること。"""
    base_root = tmp_path / "base"
    base_root.mkdir()
    base = _workspace(base_root)
    baseline = _plan(base_root, base, task_path="docs/task.md")

    changed_root = tmp_path / "changed"
    changed_root.mkdir()
    changed = _workspace(changed_root, task_body=TASK_BODY.replace(b".", b"!"))
    altered = _plan(changed_root, changed, task_path="docs/task.md")

    assert baseline.plan.plan_content_hash != altered.plan.plan_content_hash


def test_plan_content_hash_matches_across_processes_with_different_hash_seeds(
    tmp_path: Path,
) -> None:
    """別 `PYTHONHASHSEED` の子 Process で3回目を Build して一致を確かめる。

    同一 Process 内の二重 Build では `set` 反復による非決定性を検出できない
    （不変条件#6）。ここだけが `PYTHONHASHSEED` 依存を捕まえられる。
    """
    workspace = _workspace(tmp_path)
    in_process = _plan(tmp_path, workspace, task_path="docs/task.md", run_id="baseline")

    seen = {str(in_process.plan.plan_content_hash)}
    for seed in ("0", "1", "4242"):
        seen.add(_run_cli_probe(tmp_path, workspace, seed=seed, run_id=f"seed-{seed}"))
    assert len(seen) == 1, f"PYTHONHASHSEED で Plan Content Hash が変わった: {seen}"


def _run_cli_probe(root: Path, workspace: Path, *, seed: str, run_id: str) -> str:
    """CLI を子 Process として走らせ、`plan_content_hash` を持ち帰る。

    起動は固定引数の `list[str]` だけで行う。`shell=True` も Shell 文字列も
    使わない（不変条件#8）。
    """
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = seed
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    completed = subprocess.run(  # noqa: S603 - 固定引数のlist[str]。shellを使わない
        [
            sys.executable,
            "-m",
            "harness.presentation.cli",
            "plan-task",
            "--database",
            str(root / "harness.db"),
            "--artifact-root",
            str(root / "artifacts"),
            "--workspace",
            str(workspace),
            "--task-path",
            "docs/task.md",
            "--declaration-path",
            "docs/declaration.json",
            "--scope",
            "docs",
            "--run-id",
            run_id,
            "--execution-plan-id",
            f"plan-{run_id}",
            "--bundle-id",
            f"bundle-{run_id}",
            "--receipt-id",
            f"receipt-{run_id}",
            "--stream-id",
            "CC03_STREAM",
            "--capability-id",
            CAPABILITY_ID,
            "--workspace-snapshot-id",
            "cc03-workspace-snapshot",
            "--issued-at",
            ISSUED,
            "--expires-at",
            EXPIRES,
            "--now",
            NOW,
            "--repo-root",
            str(REPO_ROOT),
        ],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout)
    assert report["planned"] is True
    # CLI が「承認も作用も起きていない」と明示していること。
    assert report["approval_requested"] is False
    assert report["effect_executed"] is False
    assert report["provider_invoked"] is False
    return str(report["plan"]["plan_content_hash"])


# -- 5. 層と後方互換 ---------------------------------------------------------


#: 組立て（Composition Root）だけが具象を知ってよい。ここに挙げた File 以外の
#: `presentation/` が Repository を import していれば、Transaction 境界が
#: Application の外へ漏れている。**この一覧を安易に増やさない。**
_COMPOSITION_ROOTS = frozenset({"local_ui/composition.py"})


def test_cli_does_not_reach_into_repositories() -> None:
    """CLI が Repository へ直接触っていないこと（CLAUDE.md §2）。

    `cli.py` は Application Service と Facade だけを呼ぶ。Repository を
    import した時点で、Unit of Work の外で SQL が走りうる。
    """
    presentation = REPO_ROOT / "src" / "harness" / "presentation"
    offenders = []
    for path in sorted(presentation.rglob("*.py")):
        relative = path.relative_to(presentation).as_posix()
        if relative in _COMPOSITION_ROOTS:
            continue
        if "infrastructure.sqlite" in path.read_text(encoding="utf-8"):
            offenders.append(relative)
    assert offenders == [], f"Repository を直接見ている: {offenders}"


def test_the_plan_path_has_exactly_one_composition_root() -> None:
    """Plan 経路の組立てが1箇所であること。

    CLI が自分で具象を組み始めると、Migration や Connection の扱いが
    2通りになる。`runtime_facade` が唯一の組立て場所であることを固定する。
    """
    cli_source = (REPO_ROOT / "src" / "harness" / "presentation" / "cli.py").read_text(
        encoding="utf-8"
    )
    assert "CapabilityBroker" not in cli_source
    assert "ConnectionFactory" not in cli_source
    assert "ContextAssemblyService" not in cli_source
    assert "runtime_facade" in cli_source


# CC-03 review regressions: actual public entry and storage boundaries.
def _review_cli_args(root: Path, workspace: Path) -> list[str]:
    values = {
        "database": str(root / "review.db"),
        "artifact-root": str(root / "review-artifacts"),
        "workspace": str(workspace),
        "task-path": "docs/task.md",
        "declaration-path": "docs/declaration.json",
        "scope": "docs",
        "run-id": "review",
        "execution-plan-id": "review-plan",
        "bundle-id": "review-bundle",
        "receipt-id": "review-receipt",
        "stream-id": "REVIEW_STREAM",
        "capability-id": CAPABILITY_ID,
        "workspace-snapshot-id": "snapshot-A",
        "issued-at": ISSUED,
        "expires-at": EXPIRES,
        "now": NOW,
        "repo-root": str(REPO_ROOT),
    }
    return [
        "plan-task",
        *(value for name, value in values.items() for value in ("--" + name, value)),
    ]


@pytest.mark.parametrize(
    "target",
    ["root", "parent", "artifact", "database", "wal", "denied-artifact", "denied-database"],
)
def test_cli_refuses_invalid_paths_before_database_or_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    from harness.presentation.cli import main

    workspace = _workspace(tmp_path)
    args = _review_cli_args(tmp_path, workspace)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"preserve")
    if target == "root":
        link = tmp_path / "root-link"
        link.symlink_to(workspace, target_is_directory=True)
        args[args.index("--workspace") + 1] = str(link)
    elif target == "parent":
        link = tmp_path / "parent-link"
        link.symlink_to(tmp_path, target_is_directory=True)
        args[args.index("--workspace") + 1] = str(link / "workspace")
    elif target == "artifact":
        (tmp_path / "review-artifacts").symlink_to(workspace, target_is_directory=True)
    elif target == "database":
        (tmp_path / "review.db").symlink_to(sentinel)
    elif target == "wal":
        (tmp_path / "review.db-wal").symlink_to(sentinel)
    else:
        option = "--artifact-root" if target == "denied-artifact" else "--database"
        args[args.index(option) + 1] = "/mnt/c/cc03-must-not-be-created/output"

    def forbidden_migration(*args: object, **kwargs: object) -> None:
        raise AssertionError("migration ran before path rejection")

    monkeypatch.setattr(HarnessRuntimeService, "migrate", forbidden_migration)
    assert main(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned"] is False
    assert payload["error_code"] == (
        "WORKSPACE_ON_FOREIGN_FS_DENIED" if target.startswith("denied") else "SYMLINK_DENIED"
    )
    assert sentinel.read_bytes() == b"preserve"
    assert not (tmp_path / "review.db").exists() or (tmp_path / "review.db").is_symlink()
    assert (
        not (tmp_path / "review-artifacts").exists() or (tmp_path / "review-artifacts").is_symlink()
    )


def test_public_cli_cannot_enable_test_filesystems(tmp_path: Path) -> None:
    from harness.presentation.cli import main

    with pytest.raises(SystemExit) as caught:
        main([*_review_cli_args(tmp_path, tmp_path / "workspace"), "--allow-test-filesystems"])
    assert caught.value.code == 2


def test_snapshot_record_identifier_does_not_change_plan_content(tmp_path: Path) -> None:
    from dataclasses import replace

    workspace = _workspace(tmp_path)
    service = HarnessRuntimeService(tmp_path / "harness.db")
    service.migrate(recorded_at=NOW)
    first = service.plan_task(
        _setup(tmp_path, workspace),
        replace(
            _request(task_path="docs/task.md", run_id="one"), workspace_snapshot_id="snapshot-one"
        ),
    )
    second = service.plan_task(
        _setup(tmp_path, workspace),
        replace(
            _request(task_path="docs/task.md", run_id="two"), workspace_snapshot_id="snapshot-two"
        ),
    )
    assert first.plan.plan_content_hash == second.plan.plan_content_hash
    assert first.plan.execution_plan_hash != second.plan.execution_plan_hash


def test_runtime_checks_frozen_input_in_a_different_seed_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.domain.plan import build_execution_plan
    from harness.domain.plan_build_payload import read_plan_request

    workspace = _workspace(tmp_path)
    original = subprocess.run
    calls: list[dict[str, Any]] = []

    def measured(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        assert isinstance(args[0], list)
        assert kwargs["env"]["PYTHONHASHSEED"] != os.environ.get("PYTHONHASHSEED")
        assert isinstance(kwargs["input"], bytes)
        # Reading inputs again in the child would now produce a different plan.
        (workspace / "docs/task.md").write_bytes(b"changed after the input was frozen")
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", measured)
    outcome = _plan(tmp_path, workspace, task_path="docs/task.md")
    assert len(calls) == 1
    frozen_input, authority = read_plan_request(calls[0]["input"])
    assert (
        outcome.plan.plan_content_hash
        == build_execution_plan(frozen_input, authority).plan_content_hash
    )
    assert calls[0]["timeout"] > 0


@pytest.mark.parametrize(
    "failure", ["timeout", "unavailable", "exit", "empty", "malformed", "mismatch"]
)
def test_plan_is_rejected_if_runtime_rebuild_cannot_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    workspace = _workspace(tmp_path)

    def failed(*args: Any, **kwargs: Any) -> Any:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 1, output=b"synthetic-private-output")
        if failure == "unavailable":
            raise OSError("synthetic-private-output")
        output = {"empty": b"", "malformed": b"not a plan", "mismatch": b"sha256:" + b"0" * 64}.get(
            failure, b"error"
        )
        return subprocess.CompletedProcess(
            args[0], 1 if failure == "exit" else 0, output, b"synthetic-private-output"
        )

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(HarnessError) as caught:
        _plan(tmp_path, workspace, task_path="docs/task.md")
    assert caught.value.code is ErrorCode.PLAN_NONDETERMINISTIC
    assert "synthetic-private-output" not in str(caught.value)


def test_candidates_compress_and_persist_provenance(tmp_path):
    from dataclasses import replace

    from harness.domain.artifact import cas_object_segments
    from harness.domain.hashing import ContentHash, hash_bytes

    workspace = _workspace(tmp_path, total_tokens=150)
    (workspace / "docs" / "refs").mkdir()
    original = "The greeting is blue.\n" * 100
    (workspace / "docs" / "refs" / "facts.txt").write_text(original)
    service = HarnessRuntimeService(tmp_path / "harness.db")
    service.migrate(recorded_at=NOW)
    request = replace(
        _request(task_path="docs/task.md"),
        candidate_directories=("docs/refs",),
        compress_candidates=True,
    )
    outcome = service.plan_task(_setup(tmp_path, workspace), request)
    bundle = outcome.assembly.bundle
    assert "reference:docs/refs/facts.txt" in bundle.ordered_fragment_ids
    assert len(bundle.compression_artifact_ids) == 1
    artifact_hash = ContentHash.parse(bundle.compression_artifact_ids[0])
    artifact_path = tmp_path / "artifacts"
    artifact_path = artifact_path.joinpath(*cas_object_segments(artifact_hash))
    document = json.loads(artifact_path.read_bytes())
    assert document["source_content_hash"] == str(hash_bytes(original.encode()))
    assert document["compression_depth"] == 1
    text = "".join(original[span["start"] : span["end"]] for span in document["source_spans"])
    assert text == "The greeting is blue.\n"
    assert document["summary_content_hash"] == str(hash_bytes(text.encode()))
    bundle.assert_integrity()
    second = service.plan_task(_setup(tmp_path, workspace), replace(request, run_id="second"))
    assert outcome.plan.plan_content_hash == second.plan.plan_content_hash
    (workspace / "docs" / "refs" / "facts.txt").write_text(original.replace("blue", "green"))
    changed = service.plan_task(_setup(tmp_path, workspace), replace(request, run_id="changed"))
    assert outcome.plan.plan_content_hash != changed.plan.plan_content_hash


@pytest.mark.parametrize("body", ["Approval is required.\n", "秘密情報は禁止です。\n"])
def test_protected_candidates_are_excluded_without_compression(tmp_path, body):
    from dataclasses import replace

    workspace = _workspace(tmp_path, total_tokens=150)
    (workspace / "docs" / "refs").mkdir()
    (workspace / "docs" / "refs" / "policy.txt").write_text(body * 100)
    service = HarnessRuntimeService(tmp_path / "harness.db")
    service.migrate(recorded_at=NOW)
    request = replace(
        _request(task_path="docs/task.md"),
        candidate_directories=("docs/refs",),
        compress_candidates=True,
    )
    outcome = service.plan_task(_setup(tmp_path, workspace), request)
    assert outcome.assembly.bundle.compression_artifact_ids == ()
    assert "reference:docs/refs/policy.txt" in outcome.assembly.receipt.excluded_fragment_ids


@pytest.fixture
def local_workflow_cli(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()

    def git(args, cwd):
        return subprocess.run(  # noqa: S603 - fixed test programs and synthetic fixture argv
            [
                "/usr/bin/git",
                "-c",
                "user.name=Harness Synthetic Test",
                "-c",
                "user.email=harness@example.invalid",
                *args,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )

    git(["init"], seed)
    (seed / "docs").mkdir()
    (seed / "docs" / "task.md").write_bytes(TASK_BODY)
    (seed / "docs" / "out.md").write_text("before\n")
    (seed / "docs" / "declaration.json").write_text(json.dumps(_declaration()))
    git(["add", "docs"], seed)
    git(["commit", "-m", "test fixture"], seed)
    workspace = tmp_path / "worktree"
    git(["worktree", "add", "--detach", str(workspace)], seed)
    base = [
        "--database",
        str(tmp_path / "workflow.db"),
        "--artifact-root",
        str(tmp_path / "cas"),
        "--workspace",
        str(workspace),
        "--repo-root",
        str(REPO_ROOT),
        "--run-id",
        "workflow-test",
    ]

    def invoke(operation, *args, expected=0):
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
        result = subprocess.run(  # noqa: S603 - fixed test programs and synthetic fixture argv
            [
                sys.executable,
                "-B",
                "-m",
                "harness.presentation.cli",
                "workflow",
                operation,
                *base,
                *args,
            ],
            env=env,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        return json.loads(result.stdout)

    return workspace, invoke


def test_offline_workflow_across_cli_processes(local_workflow_cli):
    from harness.domain.hashing import hash_bytes

    workspace, invoke = local_workflow_cli
    created = invoke(
        "create",
        "--task-path",
        "docs/task.md",
        "--declaration-path",
        "docs/declaration.json",
        "--scope",
        "docs",
    )
    assert created["state"] == "PLANNED"
    assert (workspace / "docs" / "out.md").read_text() == "before\n"
    assert invoke("run", expected=2)["error_code"] == "APPROVAL_REQUIRED"
    assert (
        invoke(
            "approve",
            "--plan-hash",
            "sha256:" + "0" * 64,
            "--auth-session",
            "local-uid:" + str(os.getuid()),
            expected=2,
        )["error_code"]
        == "APPROVAL_INVALIDATED"
    )
    approved = invoke(
        "approve",
        "--plan-hash",
        created["execution_plan_hash"],
        "--auth-session",
        "local-uid:" + str(os.getuid()),
    )
    assert approved["state"] == "APPROVED"
    result = invoke("run")
    assert result["state"] == "AWAITING_RELEASE"
    payload = (workspace / "docs" / "out.md").read_bytes()
    assert str(hash_bytes(payload)) == result["proposed_artifact_hash"]
    assert json.loads(payload)["network"] == "NONE"
    assert invoke("run", expected=2)["error_code"] == "APPROVAL_REQUIRED"
    assert invoke("inspect")["state"] == "AWAITING_RELEASE"
    released = invoke(
        "release",
        "--evaluation-hash",
        result["evaluation_hash"],
        "--auth-session",
        "local-uid:" + str(os.getuid()),
    )
    assert released["state"] == "COMPLETED"
    assert released["release_decision_hash"]
    assert (
        invoke(
            "release",
            "--evaluation-hash",
            result["evaluation_hash"],
            "--auth-session",
            "local-uid:" + str(os.getuid()),
            expected=2,
        )["error_code"]
        == "APPROVAL_REQUIRED"
    )


@pytest.mark.parametrize("stage", ["before_run", "before_release"])
def test_workflow_refuses_workspace_drift(local_workflow_cli, stage):
    workspace, invoke = local_workflow_cli
    created = invoke(
        "create",
        "--task-path",
        "docs/task.md",
        "--declaration-path",
        "docs/declaration.json",
        "--scope",
        "docs",
    )
    invoke(
        "approve",
        "--plan-hash",
        created["execution_plan_hash"],
        "--auth-session",
        "local-uid:" + str(os.getuid()),
    )
    if stage == "before_run":
        (workspace / "docs" / "task.md").write_text("changed task")
        assert invoke("run", expected=2)["error_code"] == "APPROVAL_INVALIDATED"
        assert (workspace / "docs" / "out.md").read_text() == "before\n"
    else:
        result = invoke("run")
        (workspace / "docs" / "unexpected.txt").write_text("unplanned file")
        denied = invoke(
            "release",
            "--evaluation-hash",
            result["evaluation_hash"],
            "--auth-session",
            "local-uid:" + str(os.getuid()),
            expected=2,
        )
        assert denied["error_code"] == "UNRECONCILED_EFFECT_PRESENT"
        assert invoke("inspect")["run_state"] == "BLOCKED_REPAIR_REQUIRED"


def test_workflow_final_storage_refuses_stale_fence(local_workflow_cli, monkeypatch):
    from harness.infrastructure.local_workflow_runtime import local_workflow
    from harness.ports.lease import LeaseAcquireRequest

    workspace, invoke = local_workflow_cli
    created = invoke(
        "create",
        "--task-path",
        "docs/task.md",
        "--declaration-path",
        "docs/declaration.json",
        "--scope",
        "docs",
    )
    invoke(
        "approve",
        "--plan-hash",
        created["execution_plan_hash"],
        "--auth-session",
        "local-uid:" + str(os.getuid()),
    )
    with local_workflow(
        database=workspace.parent / "workflow.db",
        artifact_root=workspace.parent / "cas",
        workspace=workspace,
        repo_root=REPO_ROOT,
    ) as service:
        original = service.workspace.commit

        def stale(prepared, guard):
            state = service.inspect("workflow-test")
            lease = service.leases.get(state["lease_id"])
            with service.uow.begin_immediate():
                service.leases.release(lease, now=service.clock.now())
                service.leases.acquire(
                    LeaseAcquireRequest(
                        lease.resource_key,
                        "replacement",
                        "replacement-attempt",
                        service.clock.now(),
                        created["expires_at"],
                    )
                )
            return original(prepared, guard)

        monkeypatch.setattr(service.workspace, "commit", stale)
        with pytest.raises(HarnessError) as error:
            service.run("workflow-test")
        assert error.value.code is ErrorCode.STALE_FENCING_TOKEN
    assert (workspace / "docs" / "out.md").read_text() == "before\n"


def test_workflow_recovers_real_process_death_after_replace(local_workflow_cli):
    workspace, invoke = local_workflow_cli
    created = invoke(
        "create",
        "--task-path",
        "docs/task.md",
        "--declaration-path",
        "docs/declaration.json",
        "--scope",
        "docs",
    )
    invoke(
        "approve",
        "--plan-hash",
        created["execution_plan_hash"],
        "--auth-session",
        "local-uid:" + str(os.getuid()),
    )
    code = """
import os, sys
from pathlib import Path
from harness.infrastructure.local_workflow_runtime import local_workflow
with local_workflow(database=Path(sys.argv[1]), artifact_root=Path(sys.argv[2]),
                    workspace=Path(sys.argv[3]), repo_root=Path(sys.argv[4])) as service:
    original = service.workspace.commit
    def crash(prepared, guard):
        original(prepared, guard)
        os._exit(137)
    service.workspace.commit = crash
    service.run('workflow-test')
"""
    result = subprocess.run(  # noqa: S603 - fixed test programs and synthetic fixture argv
        [
            sys.executable,
            "-B",
            "-c",
            code,
            str(workspace.parent / "workflow.db"),
            str(workspace.parent / "cas"),
            str(workspace),
            str(REPO_ROOT),
        ],
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src")),
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 137, result.stderr
    after = (workspace / "docs" / "out.md").read_bytes()
    assert invoke("inspect")["state"] == "EXECUTION_ATTEMPTED"
    recovered = invoke("recover")
    assert recovered["state"] == "AWAITING_RELEASE"
    assert (workspace / "docs" / "out.md").read_bytes() == after
    assert recovered["recovery_decision"] == "EXPECTED_EFFECT_OBSERVED_NO_REPLAY"


def test_workflow_records_failed_evaluation_for_invalid_output(local_workflow_cli):
    workspace, invoke = local_workflow_cli
    created = invoke(
        "create",
        "--task-path",
        "docs/task.md",
        "--declaration-path",
        "docs/declaration.json",
        "--scope",
        "docs",
    )
    invoke(
        "approve",
        "--plan-hash",
        created["execution_plan_hash"],
        "--auth-session",
        "local-uid:" + str(os.getuid()),
    )
    result = invoke("run")
    (workspace / "docs/out.md").write_bytes(b"not valid JSON")
    failed = invoke("evaluate", expected=2)
    assert failed["state"] == "EVALUATION_FAILED"
    assert failed["run_state"] == "BLOCKED_REPAIR_REQUIRED"
    assert failed["evaluation_hash"] != result["evaluation_hash"]
    invoke(
        "release",
        "--evaluation-hash",
        failed["evaluation_hash"],
        "--auth-session",
        "local-uid:" + str(os.getuid()),
        expected=2,
    )


def test_workflow_plan_content_is_independent_of_run_ids(local_workflow_cli):
    _, invoke = local_workflow_cli
    args = (
        "--task-path",
        "docs/task.md",
        "--declaration-path",
        "docs/declaration.json",
        "--scope",
        "docs",
    )
    first = invoke("create", *args)
    second = invoke("create", *args, "--run-id", "a-different-run")
    assert first["plan_content_hash"] == second["plan_content_hash"]
    assert first["execution_plan_hash"] != second["execution_plan_hash"]
