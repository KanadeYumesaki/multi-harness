#!/usr/bin/env python3
"""`backup_restore` 領域の正式Evidenceを、**通常の本番入口の観測から**作る。

## 旧版（CC-RC-02）から変えたこと

| 旧版の不足 | 本版 |
|---|---|
| `--probe-effect`（部品Probe）を根拠にした | 同一DBのGateを持つ通常入口を復元区間で呼ぶ |
| Drain の `ACCEPTED` を再開状態へ写した | 判定と署名承認による再開操作を別欄で実測する |
| 子Processにtimeoutが無く、stderrを読んだ | timeoutと残存確認を持ち、stderrをJSONにしない |
| `measured: True` の固定Boolから PASS を導いた | 述語が観測値と生ログのBytesから導く |
| 最終Fileを書いてから Clean 確認した | 試行Directoryで作り、束縛確認後に公開し、失敗なら撤回する |

## 合成環境だけを使う

入力は新規に作ったDB/CAS/Worktreeだけで、**既存ユーザーDBを開かない。**
外部通信は無い（偽CLIと計数Port）。Evidence Root と合成Workspace は Repository の外を要求する。

## 束縛を手入力しない

Commit は Clean な作業木から、設計Hash・Registry Snapshot・Schema Set・Migration Head は
測定対象Repositoryの実物から導き、Manifest の値と照合する。一致しなければ観測しない。

## Verifier の受理と観測の合格は別

`verify_runtime_go.py` は `backup_restore` 固有の summary を検査しない。
ここで `status: PASS` を書けるのは述語が全て `PASS` のときだけであり、
Verifier が受理したことは別に報告する。
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _entry in (str(ROOT), str(ROOT / "tools"), str(ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import verify_runtime_go as verifier  # noqa: E402

from emit_case_evidence import EvidenceEmissionError, _require_clean_tree  # noqa: E402

AREA = "backup_restore"
COLLECTOR_CONTRACT = "cc-ops-evidence-01/backup-restore-area/1"
ATTACHED_MANIFEST_NAME = "manifest-with-backup-restore.json"
TRIALS = Path("trials") / "backup-restore"
CLOCK_DISCREPANCY_TOLERANCE_SECONDS = 2.0

LIMITS = (
    "同一UIDの非協調な書換えや、Gateを経由しない任意Native Code・基盤外の直接I/Oは観測範囲外",
    "偽CLIとStubBoundaryProbeは合成。実Provider送信・Landlock境界の実測はこの領域の主張に含めない",
    "Workbench入口は build_services() のGatewayを直接呼ぶ。HTTP層（LocalUiApi）は経由していない",
    "DRAINING中は kind=effect の継続入口を通す仕様のため、Drain後の拒否は新規受付だけを主張する",
    "受付再開は正常Drain後の署名承認経路を実測。残存予約整理と異常復元の回復はこの領域の観測に含めない",
    "BackupRestoreServiceのeffects_during_restoreは拒否した試行数であり、実行された作用数ではない",
)


class AreaCollectionError(EvidenceEmissionError):
    """領域Evidenceを作れない。**採用可能なPASS成果物を残さない。**"""


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)


def _encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_exclusive(path: Path, data: bytes) -> None:
    """試行Directoryの記録。既存Fileを上書きしない。"""
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def publish_new(
    out: Path, data: bytes, *, on_ready: Callable[[tuple[int, int]], None] | None = None
) -> None:
    """Temp write → fsync → link（既存なら失敗）→ Directory fsync。上書きしない。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".backup-restore-", dir=out.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            identity = os.fstat(handle.fileno())
        # link直後のfsync/cleanup失敗でも、自分のinodeだけを撤回できるよう先に登録する。
        if on_ready is not None:
            on_ready((identity.st_dev, identity.st_ino))
        os.link(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)
    _fsync_directory(out.parent)
    _fsync_directory(out.parent.parent)


def _without_symlinks(path: Path, label: str) -> Path:
    """既存の親を含めて検査し、書込み開始前に論理Pathの逸脱を拒否する。"""
    absolute = path.absolute()
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            raise AreaCollectionError(f"{label}_SYMLINK: {component}")
    return absolute.resolve(strict=False)


def _outside(repo: Path, path: Path, label: str) -> None:
    if path == repo or path.is_relative_to(repo):
        raise AreaCollectionError(f"{label}_IN_REPOSITORY: {path}")
    if str(path).startswith("/mnt/"):
        raise AreaCollectionError(f"{label}_ON_DENIED_PATH: {path}")


def derive_binding(repo: Path, scope: str) -> dict[str, Any]:
    """測定対象Repositoryの実物から束縛値を導く。**Manifestの値を写さない。**"""
    from harness.infrastructure.schema.registry import CoreSchemaRegistry
    from harness.infrastructure.sqlite.migrations import SCHEMA_VERSION

    snapshot = json.loads((repo / "registry-snapshot.json").read_text(encoding="utf-8"))
    design = repo / f"design-v{snapshot['design_version']}-runtime-go.md"
    area = next((a for a in snapshot["evidence_areas"] if a["area"] == AREA), None)
    return {
        "design_file_sha256": verifier.sha256_file(design),
        "snapshot_design_sha256": snapshot["design_sha256"],
        "registry_snapshot_hash": snapshot["registry_snapshot_hash"],
        "schema_set_hash": CoreSchemaRegistry(repo).schema_set_hash(),
        "migration_head": str(SCHEMA_VERSION),
        "area_evidence_path": None if area is None else area["evidence_path"],
        "area_required": AREA
        in snapshot.get("scopes", {}).get(scope, {}).get("required_evidence_areas", []),
        "release_enabled": scope in snapshot.get("release_enabled_phases", []),
    }


def check_environment(manifest: dict[str, Any], evidence_root: Path) -> None:
    findings = verifier.Findings()
    verifier.verify_environment(manifest, evidence_root, findings)
    if not findings.ok:
        raise AreaCollectionError(
            f"ENVIRONMENT_BINDING_INVALID: {findings.errors + findings.missing}"
        )


def check_manifest(manifest: Any, binding: dict[str, Any], commit: str) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise AreaCollectionError("MANIFEST_NOT_OBJECT")
    if manifest.get("implementation_commit_sha") != commit:
        raise AreaCollectionError(
            f"COMMIT_MISMATCH: manifest={manifest.get('implementation_commit_sha')} tree={commit}"
        )
    if manifest.get("source_tree_clean") is not True:
        raise AreaCollectionError("MANIFEST_SOURCE_TREE_NOT_CLEAN")
    if not binding["release_enabled"] or not binding["area_required"]:
        raise AreaCollectionError(f"AREA_NOT_REQUIRED_FOR_SCOPE: {manifest.get('release_scope')}")
    if binding["design_file_sha256"] != binding["snapshot_design_sha256"]:
        raise AreaCollectionError("DESIGN_FILE_NOT_BOUND_TO_SNAPSHOT")
    for field, derived in (
        ("design_sha256", binding["design_file_sha256"]),
        ("registry_snapshot_hash", binding["registry_snapshot_hash"]),
        ("schema_set_hash", binding["schema_set_hash"]),
        ("migration_head", binding["migration_head"]),
    ):
        if manifest.get(field) != derived:
            raise AreaCollectionError(
                f"MANIFEST_{field.upper()}_MISMATCH: manifest={manifest.get(field)!r} "
                f"derived={derived!r}"
            )
    rows = [
        row
        for row in manifest.get("required_evidence_areas") or []
        if isinstance(row, dict) and row.get("area") == AREA
    ]
    if len(rows) != 1:
        raise AreaCollectionError(f"MANIFEST_AREA_ROW_COUNT: {len(rows)}")
    row = rows[0]
    if row.get("status") == verifier.PASS or row.get("evidence_manifest_hash"):
        raise AreaCollectionError("MANIFEST_AREA_ALREADY_BOUND")
    if not binding["area_evidence_path"]:
        raise AreaCollectionError("AREA_EVIDENCE_PATH_NOT_IN_REGISTRY")
    return row


def build(
    *,
    repo: Path,
    evidence_root: Path,
    manifest_path: Path,
    workspace: Path,
    collector_root: Path = ROOT,
    observe: Callable[..., dict[str, Any]] | None = None,
    evaluate: Callable[..., dict[str, Any]] | None = None,
    derive: Callable[[Path, str], dict[str, Any]] = derive_binding,
    environment_check: Callable[[dict[str, Any], Path], None] = check_environment,
    trial_id: str | None = None,
    after_publish: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if observe is None or evaluate is None:
        from backup_restore_observation import observe_all
        from backup_restore_predicates import evaluate as evaluate_observations

        observe = observe or observe_all
        evaluate = evaluate or evaluate_observations

    repo = repo.resolve(strict=True)
    if repo != collector_root.resolve():
        raise AreaCollectionError(f"COLLECTOR_NOT_FROM_MEASURED_WORKTREE: {repo}")
    if not evidence_root.is_dir() or evidence_root.is_symlink():
        raise AreaCollectionError(f"EVIDENCE_ROOT_MISSING: {evidence_root}")
    evidence_root = _without_symlinks(evidence_root, "EVIDENCE")
    workspace = _without_symlinks(workspace, "WORKSPACE")
    _outside(repo, evidence_root, "EVIDENCE")
    _outside(repo, workspace, "WORKSPACE")
    if os.stat(evidence_root).st_dev != os.stat(repo).st_dev:
        raise AreaCollectionError("EVIDENCE_ON_DIFFERENT_FILESYSTEM")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AreaCollectionError(f"MANIFEST_NOT_REGULAR_FILE: {manifest_path}")
    manifest_path = manifest_path.resolve(strict=True)
    if not manifest_path.is_relative_to(evidence_root):
        raise AreaCollectionError("MANIFEST_OUTSIDE_EVIDENCE_ROOT")

    commit = _require_clean_tree(repo)
    manifest_bytes = manifest_path.read_bytes()
    manifest_hash = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise AreaCollectionError(f"MANIFEST_NOT_JSON: {exc}") from exc
    scope = manifest.get("release_scope") if isinstance(manifest, dict) else None
    if not isinstance(scope, str):
        raise AreaCollectionError("MANIFEST_RELEASE_SCOPE_MISSING")
    binding = derive(repo, scope)
    check_manifest(manifest, binding, commit)
    environment_check(manifest, evidence_root)

    out = _without_symlinks(evidence_root / binding["area_evidence_path"], "OUTPUT")
    attached = _without_symlinks(manifest_path.parent / ATTACHED_MANIFEST_NAME, "MANIFEST_OUT")
    if not out.is_relative_to(evidence_root) or out == evidence_root:
        raise AreaCollectionError("OUTPUT_OUTSIDE_EVIDENCE_ROOT")
    if out.exists() or out.is_symlink():
        raise AreaCollectionError(f"EVIDENCE_REUSE: {out}")
    if attached.exists() or attached.is_symlink():
        raise AreaCollectionError(f"MANIFEST_OUT_REUSE: {attached}")
    if workspace.exists() or workspace.is_symlink():
        raise AreaCollectionError(f"WORKSPACE_REUSE: {workspace}")

    trial_id = trial_id or (
        dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", trial_id) is None:
        raise AreaCollectionError("INVALID_TRIAL_ID")
    trial = _without_symlinks(evidence_root / TRIALS / trial_id, "TRIAL")
    trial.parent.mkdir(parents=True, exist_ok=True)
    try:
        trial.mkdir()
    except FileExistsError as exc:
        raise AreaCollectionError(f"TRIAL_REUSE: {trial}") from exc
    raw = trial / "raw"
    raw.mkdir()

    def relative(path: Path) -> str:
        return path.relative_to(evidence_root).as_posix()

    write_exclusive(
        trial / "trial-started.json",
        _encode(
            {
                "trial_id": trial_id,
                "commit": commit,
                "manifest_path": relative(manifest_path),
                "manifest_sha256": manifest_hash,
                "workspace": str(workspace),
                "started_at": utc_now(),
                "status": "IN_PROGRESS_NOT_ADOPTABLE",
            }
        ),
    )

    stage = "OBSERVE"
    published: list[tuple[Path, tuple[int, int], str]] = []

    def publish(path: Path, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        publish_new(
            path,
            data,
            on_ready=lambda identity: published.append((path, identity, digest)),
        )

    try:
        from backup_restore_observation import RawLogs

        started_at = utc_now()
        monotonic_start = time.monotonic()
        observations = observe(repo=repo, workspace=workspace, logs=RawLogs(raw, evidence_root))
        elapsed = round(time.monotonic() - monotonic_start, 3)
        recorded_at = utc_now()
        _fsync_directory(raw)
        observations_bytes = _encode(observations)
        write_exclusive(trial / "observations.json", observations_bytes)

        stage = "EVALUATE"
        verdict = evaluate(observations, raw_root=evidence_root)
        evaluation_bytes = _encode(verdict)
        write_exclusive(trial / "evaluation.json", evaluation_bytes)

        stage = "BIND_BEFORE_PUBLISH"
        _recheck(repo, commit, manifest_path, manifest_hash, derive, scope, binding)
        if verdict.get("status") != verifier.PASS:
            raise AreaCollectionError(f"AREA_NOT_PASS: {verdict.get('status')}")

        utc_elapsed = (_parse_utc(recorded_at) - _parse_utc(started_at)).total_seconds()
        discrepancy = round(abs(utc_elapsed - elapsed), 3)
        flow = observations.get("operator_flow") or {}
        body = {
            "evidence_schema_version": verifier.EVIDENCE_SCHEMA_VERSION,
            "release_scope": scope,
            "area": AREA,
            "status": verdict["status"],
            "summary": {
                "measured_from": "production_composition_and_cli_entrypoints",
                "collector_contract": COLLECTOR_CONTRACT,
                "observation_contract": observations.get("contract"),
                "predicate_contract": verdict.get("contract"),
                "status_derivation": "PASS only when every predicate check is PASS",
                "item_status": verdict["items"],
                "checks": verdict["checks"],
                "counts": verdict["counts"],
                "resume": {
                    "decision": (flow.get("resume") or {}).get("decision"),
                    "execution": (flow.get("resume") or {}).get("execution"),
                },
                "not_used_as_basis": [
                    "harness backup verify --probe-effect (RestoreGuard component probe)",
                    "tools/measure_backup_restore.py round-trip probe status",
                    "pytest results copied from other runs",
                ],
                "trial_id": trial_id,
                "trial_directory": relative(trial),
                "observations_path": relative(trial / "observations.json"),
                "observations_sha256": verifier.sha256_file(trial / "observations.json"),
                "evaluation_path": relative(trial / "evaluation.json"),
                "evaluation_sha256": verifier.sha256_file(trial / "evaluation.json"),
                "observations": observations,
                "binding": {
                    "manifest_path": relative(manifest_path),
                    "manifest_sha256": manifest_hash,
                    "design_sha256": binding["design_file_sha256"],
                    "registry_snapshot_hash": binding["registry_snapshot_hash"],
                },
                "timing": {
                    "elapsed_monotonic_seconds": elapsed,
                    "elapsed_utc_seconds": utc_elapsed,
                    "discrepancy_seconds": discrepancy,
                    "discrepancy_exceeds_tolerance": discrepancy
                    > CLOCK_DISCREPANCY_TOLERANCE_SECONDS,
                },
                "synthetic_only": True,
                "user_database_opened": False,
                "verifier_area_specific_check": False,
                "human_measured": False,
                "limits": list(LIMITS),
            },
            "producer": "tools/collect_backup_restore_area.py",
            "test_run_id": f"backup-restore-{scope}-{trial_id}",
            "started_at": started_at,
            "recorded_at": recorded_at,
            "implementation_commit_sha": commit,
            "schema_set_hash": manifest["schema_set_hash"],
            "migration_head": manifest["migration_head"],
            "runtime_environment_hash": manifest["runtime_environment"][
                "environment_manifest_hash"
            ],
        }
        evidence_bytes = _encode(body)
        write_exclusive(trial / "candidate-backup-restore-report.json", evidence_bytes)
        evidence_hash = verifier.sha256_file(trial / "candidate-backup-restore-report.json")

        attached_manifest = copy.deepcopy(manifest)
        row = next(r for r in attached_manifest["required_evidence_areas"] if r["area"] == AREA)
        row.update(
            status=body["status"],
            evidence_path=binding["area_evidence_path"],
            evidence_manifest_hash=evidence_hash,
        )
        attached_bytes = (json.dumps(attached_manifest, indent=2) + "\n").encode("utf-8")

        stage = "PUBLISH"
        publish(out, evidence_bytes)
        publish(attached, attached_bytes)
        if after_publish is not None:
            after_publish()

        stage = "BIND_AFTER_PUBLISH"
        _recheck(repo, commit, manifest_path, manifest_hash, derive, scope, binding)
        if verifier.sha256_file(out) != evidence_hash:
            raise AreaCollectionError("PUBLISHED_EVIDENCE_BYTES_CHANGED")
        if out.read_bytes() != evidence_bytes or attached.read_bytes() != attached_bytes:
            raise AreaCollectionError("PUBLISHED_BYTES_CHANGED")
        stage = "RECORD_PUBLICATION"
        result = {
            "status": body["status"],
            "path": str(out),
            "sha256": evidence_hash,
            "attached_manifest": str(attached),
            "attached_manifest_sha256": verifier.sha256_file(attached),
            "trial_directory": str(trial),
            "items": verdict["items"],
        }
        write_exclusive(
            trial / "trial-published.json", _encode({**result, "recorded_at": utc_now()})
        )
    except BaseException as exc:
        withdrawn = _withdraw(published, trial)
        unknown = [item for item in withdrawn if item.get("error")]
        try:
            write_exclusive(
                trial / "trial-failed.json",
                _encode(
                    {
                        "trial_id": trial_id,
                        "stage": stage,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:2000],
                        "published_then_withdrawn": withdrawn,
                        "published_state_unknown": bool(unknown),
                        "recorded_at": utc_now(),
                        "status": "FAILED_NOT_ADOPTABLE",
                    }
                ),
            )
        except OSError as record_error:
            raise AreaCollectionError(
                f"FAILURE_RECORD_UNAVAILABLE: original={type(exc).__name__}: {exc}; "
                f"record={record_error}; published_state_unknown={bool(unknown)}; "
                f"withdrawal={withdrawn}"
            ) from exc
        if unknown:
            raise AreaCollectionError(f"PUBLISHED_STATE_UNKNOWN: {unknown}") from exc
        raise

    return result


def _recheck(
    repo: Path,
    commit: str,
    manifest_path: Path,
    manifest_hash: str,
    derive: Callable[[Path, str], dict[str, Any]],
    scope: str,
    binding: dict[str, Any],
) -> None:
    try:
        current = _require_clean_tree(repo)
    except EvidenceEmissionError as exc:
        raise AreaCollectionError(f"TREE_CHANGED_DURING_COLLECTION: {exc}") from exc
    if current != commit:
        raise AreaCollectionError(f"COMMIT_CHANGED_DURING_COLLECTION: {commit} -> {current}")
    if not manifest_path.is_file() or verifier.sha256_file(manifest_path) != manifest_hash:
        raise AreaCollectionError("MANIFEST_CHANGED_DURING_COLLECTION")
    if derive(repo, scope) != binding:
        raise AreaCollectionError("BINDING_CHANGED_DURING_COLLECTION")


def _withdraw(
    published: list[tuple[Path, tuple[int, int], str]], trial: Path
) -> list[dict[str, str]]:
    """自分が公開したinode/Bytesだけを退避し、各撤回のI/O失敗を個別に記録する。

    あるDirectoryの同期失敗で残りの撤回を中断しない。置換された他者のFileは
    動かさず不明として報告する。同一UIDの非協調更新に対する完全な排他ではない。
    """
    moved: list[dict[str, str]] = []
    for path, identity, digest in reversed(published):
        target = trial / ("withdrawn-" + path.name)
        item = {"path": str(path), "withdrawn_to": ""}
        moved.append(item)
        try:
            info = path.lstat()
        except FileNotFoundError:
            item["state"] = "ABSENT"
            continue
        except OSError as exc:
            item["error"] = str(exc)
            continue
        try:
            if (
                path.is_symlink()
                or (info.st_dev, info.st_ino) != identity
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            ):
                raise OSError("PUBLISHED_IDENTITY_CHANGED: refusing to move another writer's file")
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"WITHDRAWAL_TARGET_EXISTS: {target}")
            os.rename(path, target)
            item["withdrawn_to"] = str(target)
        except OSError as exc:
            item["error"] = str(exc)
            continue
        errors = []
        for directory in (path.parent, trial):
            try:
                _fsync_directory(directory)
            except OSError as exc:
                errors.append(str(exc))
        if errors:
            item["error"] = "WITHDRAWAL_DURABILITY_UNCONFIRMED: " + "; ".join(errors)
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path, help="新規の合成環境Directory")
    args = parser.parse_args()
    try:
        result = build(
            repo=args.repo_root,
            evidence_root=args.evidence_root,
            manifest_path=args.manifest,
            workspace=args.workspace,
        )
    except EvidenceEmissionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
