#!/usr/bin/env python3
"""Block Record を実測 Evidence で閉じる（`RESOLVED_BY_FIX`）。

## JSON を手で書き換えない

閉じる操作は「不変Fieldを触らずに status と resolution だけを足す」ことである。
手編集だと、隣の行をうっかり変えても気付かない。本Toolは変更してよいKeyを
明示し、それ以外が動いたら停止する。

## Evidence を根拠に閉じる

`--evidence` へ Case Evidence の Path を渡す。Tool が読み直して次を確かめる。

* `status` が `PASS` であること
* `case_id` が Record の塞がりに対応していること
* `evidence_hash` が Body から測り直した値と一致すること

**通っていない Evidence では閉じない。** 実行していない Test を PASS と
書かないのと同じ理由である（不変条件#16）。

## 旧方式と正式方式を混ぜない

上の `--evidence` は **旧 Evidence 方式**である。Body 全体の自己Hash
（`evidence_hash`）を持つ Evidence だけを受ける。

`tools/collect_unit_cases.py` が出す**正式 Envelope** は自己Hashを持たず、
`test_id` と `case_id` を分け、Manifest の `evidence_manifest_hash` が
**File Bytes の SHA-256** である。別の束縛なので、旧検証を流用しない。
正式方式は `--formal-manifest` 以下で**明示的に選ぶ**。

**方式の入力が欠けたときに暗黙で片方へ倒さない。** どちらも指定しなければ
停止する。倒した先が間違っていれば、根拠の無い解消が静かに通ってしまう。

## 証跡の適用条件は Task で決まる

正式方式が閉じられるのは `blocked_resolution_formal.APPLICABLE_TASK_ID` の Task だけ
である。**Release Scope と正本Hashが合っていることは適用条件ではない。**
F-2 の Unit Case Evidence は F-2 の塞がりしか解いていないので、同じ Scope の別Task
（Backup 復旧未確認など）へ渡しても閉じない。

## 書き換えは最後にまとめて行う

検証 → 候補 resolution の組立て → Directory への排他Lock → 元Bytes照合 →
Temp write・fsync → **公開直前の元Bytes再照合** → Atomic Rename →
Directory fsync → 再読取り照合、の順にする。

`os.replace` が返る前の失敗は `ResolveError` で、Record は 1 Byte も動いていない。
返った後の失敗は `DurabilityUnconfirmed` で、**Record は更新済み**である。
両者を混ぜない。混ぜると「失敗したので元のまま」と読まれる。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "blocked" / "records"
#: 正式方式の対象集合を導く保存済み Owner 決定の置き場（`--repo-root` からの相対）。
DECISION_RELATIVE = "docs/decision"

sys.path.insert(0, str(ROOT / "tools"))
from blocked_resolution_formal import (  # noqa: E402
    FormalResolutionError,
    derive_target_cases,
    record_path,
    require_applicable_task,
    validate_chain,
    verify_formal_evidence,
    walk_chain,
)
from emit_case_evidence import EvidenceEmissionError, verify_case_evidence  # noqa: E402

#: 閉じるときに足してよいKey。これ以外が動いたら停止する。
MUTABLE_KEYS = {"status", "resolved_at", "resolution"}

#: 正式方式の `--decided-at`。Evidence 側と同じ UTC 表記だけを受ける。
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ResolveError(RuntimeError):
    """閉じられない。**Record は1 Byteも動いていない。**"""


class DurabilityUnconfirmed(RuntimeError):
    """**置換は成功した**が、その後の永続化・再読取りを確認できていない。

    `ResolveError` と混ぜない。混ぜると「失敗したので元のまま」と読まれ、
    実際には公開された Record が確認されないまま残る。

    **「いまも RESOLVED である」とは断定しない。** 確かなのは `os.replace` が
    返ったことだけで、その後に読めなかった以上、現在の状態は未確認である。
    成功した置換と、現在の確認不能を分けて扱う。
    """

    #: Operator への復帰手順。推測させない。
    RECOVERY = (
        "置換（公開）は成功している。現在の内容は確認できていないので、"
        "RESOLVED のままとは限らない。resolve をやり直しても、"
        "更新が残っていれば NOT_OPEN で止まる。\n"
        "  1. `git diff -- <record>` で現在の内容を確認する\n"
        "  2. `python tools/validate_blocked_record.py <record> --design <design> "
        "--registry registry-snapshot.json` を実行する\n"
        "  3. `sync` して再読取りし、何が残っているかを確かめる\n"
        "  4. 壊れている／消えている場合だけ、`git checkout -- <record>` で戻してから"
        "検証をやり直す。**Toolは自動で元Bytesへ復元しない。**"
        "他者の更新を巻き戻す恐れがあるためである"
    )


def _load(blocker_id: str) -> tuple[Path, bytes, dict[str, Any]]:
    """Record を**1回だけ**読み、その Bytes と、そこからparseしたJSONを返す。

    **2回読まない。** JSONを `read_text()` で読み、比較用Bytesを別の `read_bytes()`
    で読むと、その間に入った更新で両者が別スナップショットになる。すると旧JSONを
    検証し、新Bytesを「変化なし」の基準にしてしまい、割り込んだ更新を黙って
    上書きできてしまう。不変Fieldの検査も旧JSON同士の比較なので気付けない。

    同じ Bytes から作った JSON と、その Bytes を比較にも使えば、この窓は無くなる。
    """
    path = RECORDS / f"{blocker_id}.json"
    if not path.is_file():
        raise ResolveError(f"UNKNOWN_BLOCKER: {path}")
    raw = path.read_bytes()
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ResolveError(f"RECORD_NOT_VALID_JSON: {path}: {exc}") from exc
    if not isinstance(record, dict):
        raise ResolveError(f"RECORD_NOT_AN_OBJECT: {path}")
    return path, raw, record


def _check_evidence(paths: list[Path], record: dict[str, Any]) -> list[dict[str, str]]:
    """Evidence を読み直し、Record の塞がりに対応する PASS であることを確かめる。"""
    blob = json.dumps(record, ensure_ascii=False)
    checked: list[dict[str, str]] = []
    for path in paths:
        if not path.is_file():
            raise ResolveError(f"MISSING_EVIDENCE: {path}")
        try:
            verify_case_evidence(path)
        except EvidenceEmissionError as exc:
            raise ResolveError(f"TAMPERED_EVIDENCE: {exc}") from exc
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("status") != "PASS":
            raise ResolveError(f"EVIDENCE_NOT_PASS: {body.get('case_id')} = {body.get('status')}")
        case_id = str(body["case_id"])
        if case_id not in blob:
            # 別 Case の Evidence で閉じない。塞がりと根拠が繋がっていない。
            raise ResolveError(
                f"EVIDENCE_CASE_NOT_NAMED_BY_RECORD: {case_id} は "
                f"{record['blocker_id']} が名指ししていない"
            )
        checked.append(
            {
                "case_id": case_id,
                "evidence_hash": str(body["evidence_hash"]),
                "evidence_kind": str(body["evidence_kind"]),
                "event_observation": str(body["event_observation"]),
                "path_sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not checked:
        raise ResolveError("NO_EVIDENCE: 根拠なしに閉じない")
    return checked


def _check_formal(args: argparse.Namespace, record: dict[str, Any]) -> dict[str, Any]:
    """正式Manifest・正式Case Evidence・継承Chainを根拠として検証する。

    対象Caseは Record の自由文から拾わず、保存済みOwner決定と現行Registryの
    突合から導く。Verifier・Registry・期待値は1つも変更しない。
    """
    # 最初に「この証跡はこの塞がりへ適用できるのか」を決める。**Release Scope と
    # 正本Hashの一致は適用条件ではない。** これが無いと、F-2 の Unit Evidence で
    # 無関係な塞がり（たとえば Backup 復旧未確認）まで閉じられる。
    applicable_task = require_applicable_task(record)

    if not TIMESTAMP_RE.fullmatch(args.decided_at or ""):
        raise ResolveError(f"INVALID_DECIDED_AT: {args.decided_at!r} は UTC ISO-8601 ではない")
    repo_root = Path(args.repo_root).resolve()
    snapshot_path = repo_root / "registry-snapshot.json"
    if not snapshot_path.is_file():
        raise ResolveError(f"MISSING_REGISTRY_SNAPSHOT: {snapshot_path}")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    scope = args.release_scope
    if record.get("release_scope") != scope:
        raise ResolveError(
            f"RECORD_SCOPE_MISMATCH: record={record.get('release_scope')!r} 指定={scope!r}"
        )
    # 現行Recordの不変Fieldが「いまの正本」へ束縛されていること。
    # 旧正本へ束縛された歴史Recordを現HEADの証跡で閉じない。
    if record.get("registry_snapshot_hash") != snapshot.get("registry_snapshot_hash"):
        raise ResolveError(
            f"RECORD_REGISTRY_BINDING_STALE: record={record.get('registry_snapshot_hash')} "
            f"canon={snapshot.get('registry_snapshot_hash')}"
        )
    if record.get("design_sha256") != snapshot.get("design_sha256"):
        raise ResolveError(
            f"RECORD_DESIGN_BINDING_STALE: record={record.get('design_sha256')} "
            f"canon={snapshot.get('design_sha256')}"
        )

    chain = validate_chain(walk_chain(RECORDS, str(record["blocker_id"])))
    if chain["task_id"] != applicable_task:
        raise ResolveError(
            f"CHAIN_TASK_NOT_APPLICABLE: chain task_id={chain['task_id']!r} != {applicable_task!r}"
        )
    targets = derive_target_cases(repo_root / DECISION_RELATIVE, snapshot, scope)
    if targets["applicable_task_id"] != applicable_task:
        raise ResolveError(
            f"TARGET_SET_NOT_APPLICABLE: 対象集合は "
            f"{targets['applicable_task_id']!r} のものであり {applicable_task!r} ではない"
        )
    evidence = verify_formal_evidence(
        repo_root=repo_root,
        evidence_root=Path(args.evidence_root),
        manifest_path=Path(args.formal_manifest),
        report_path=Path(args.verification_report),
        scope=scope,
        expected_commit=args.implementation_commit,
        target_case_ids=targets["target_case_ids"],
        unit_case_ids=targets["unit_case_ids"],
    )
    return {"supersedes_chain": chain, "target_derivation": targets, "formal_evidence": evidence}


def _durable_replace(path: Path, original: bytes, payload: bytes) -> None:
    """排他を取り、公開直前まで元Bytesを見張りながら耐久的に置き換える。

    ## 公開の前と後で失敗の意味が違う

    `os.replace` が返る前に落ちたら Record は1 Byteも動いていない（`ResolveError`）。
    返った後に落ちたら **Record は already 更新済み**で、確かなのは「永続化を確認
    できていない」ことだけである（`DurabilityUnconfirmed`）。両者を同じ例外にすると、
    「失敗したので元のまま」と読んだ人が、実際には更新された Record を放置する。

    ## 初回のBytes比較だけでは競合を防げない

    比較してから `os.replace` するまでの間に別の更新が入れば、その更新は消える。
    そこで Record を収める **Directory** へ排他Lockを取り、**公開直前にもう一度**
    元Bytesを照合する。`original` は `_load()` が読んだのと同じスナップショットで
    なければならない。別々に読むと、検証した状態と上書きの基準がずれる。

    Lock対象をDirectoryにしたのは、Lock Fileを置くと作業木が汚れて実測ができなくなり、
    Record 自身をLockすると `os.replace` でinodeが入れ替わって後続Processが別のinodeを
    Lockしてしまうためである。

    **このLockは勧告Lockであり、効くのは本Toolを通る協調する更新者だけである。**
    `flock` を取らない直接編集（手でJSONを書き換える、別Toolが書く）までは防げない。
    その場合に効くのは公開直前のBytes照合だけで、照合と `os.replace` の間に入った
    非協調更新は防止できない。**完全な排他を主張しない。**
    """
    lock_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if path.read_bytes() != original:
            raise ResolveError(
                "RECORD_CHANGED_DURING_VERIFICATION: 検証中に Record が書き換わった。"
                "読み直して検証からやり直すこと"
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        handle, name = tempfile.mkstemp(dir=str(path.parent), prefix=".blk-resolve-", suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            # 公開直前の再検証。Temp の作成・fsync 中に入った更新をここで捕まえる。
            if path.read_bytes() != original:
                raise ResolveError(
                    "RECORD_CHANGED_BEFORE_PUBLISH: 書込み直前に Record が書き換わった。"
                    "その更新を上書きせず中止した。読み直して検証からやり直すこと"
                )
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

        # ここから先は**公開済み**である。以降の失敗で元へは戻らない。
        try:
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise DurabilityUnconfirmed(f"DIRECTORY_FSYNC_FAILED_AFTER_PUBLISH: {exc}") from exc
        # 再読取りそのものが落ちる場合（権限剥奪、消失、I/O Error）も**公開後**の
        # 失敗である。ここを try の外に置くと素の OSError が main() から漏れ、
        # 終了値2も「更新済み」の説明も復旧案内も出ない。
        try:
            readback = path.read_bytes()
        except OSError as exc:
            raise DurabilityUnconfirmed(
                f"READBACK_FAILED_AFTER_PUBLISH: 置換は成功したが読み直せない: {exc}"
            ) from exc
        if readback != payload:
            raise DurabilityUnconfirmed(
                "RECORD_READBACK_MISMATCH: 書き戻した内容を読み直して確認できない"
            )
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocker-id", required=True)
    parser.add_argument(
        "--evidence", nargs="+", type=Path, help="旧方式。Case Evidence の Path（自己Hash付き）"
    )
    parser.add_argument("--formal-manifest", type=Path, help="正式方式。Unit入りManifestのPath")
    parser.add_argument("--evidence-root", type=Path, help="正式方式。Evidence Root")
    parser.add_argument("--verification-report", type=Path, help="正式方式。Verifier出力Report")
    parser.add_argument("--release-scope", help="正式方式。Release Scope")
    parser.add_argument("--implementation-commit", help="正式方式。実装候補Commit SHA")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--verified-by", required=True, nargs="+")
    parser.add_argument("--decided-by", required=True)
    parser.add_argument("--decided-at", required=True)
    args = parser.parse_args(argv)

    formal_inputs = {
        "--formal-manifest": args.formal_manifest,
        "--evidence-root": args.evidence_root,
        "--verification-report": args.verification_report,
        "--release-scope": args.release_scope,
        "--implementation-commit": args.implementation_commit,
    }
    supplied = {name for name, value in formal_inputs.items() if value is not None}
    checked: list[dict[str, str]] = []
    formal: dict[str, Any] | None = None
    try:
        # 方式は明示で選ぶ。欠けたら止める。暗黙のfallbackを作らない。
        if args.evidence and supplied:
            raise ResolveError(
                "EVIDENCE_MODE_AMBIGUOUS: 旧方式(--evidence)と正式方式を同時に指定している"
            )
        if not args.evidence and not supplied:
            raise ResolveError(
                "EVIDENCE_MODE_NOT_SELECTED: --evidence（旧方式）か "
                f"{sorted(formal_inputs)}（正式方式）のどちらかを指定すること"
            )
        if supplied:
            absent = sorted(set(formal_inputs) - supplied)
            if absent:
                raise ResolveError(f"FORMAL_INPUT_INCOMPLETE: {absent} が無い")

        # 1回の読取りから JSON と比較用 Bytes の両方を得る。検証した状態と、
        # 更新の前提にする状態を同じスナップショットにそろえる。
        path, original, record = _load(args.blocker_id)
        before = json.loads(json.dumps(record, ensure_ascii=False))

        if record.get("status") != "OPEN":
            raise ResolveError(f"NOT_OPEN: {args.blocker_id} は {record.get('status')} である")
        # Block ID を Path へ結合する前に検証済みであることを確かめる。
        if record_path(RECORDS, args.blocker_id) != path:
            raise ResolveError(f"BLOCKER_PATH_UNSAFE: {args.blocker_id}")

        resolution: dict[str, Any] = {
            "outcome": "RESOLVED_BY_FIX",
            "decided_at": args.decided_at,
            "decided_by": args.decided_by,
            "summary": args.summary,
            "verified_by": list(args.verified_by),
        }
        if args.evidence:
            checked = _check_evidence(list(args.evidence), record)
            resolution["evidence_mode"] = "legacy"
            resolution["evidence"] = checked
        else:
            formal = _check_formal(args, record)
            resolution["evidence_mode"] = "formal"
            resolution["evidence"] = [
                {
                    "case_id": entry["case_id"],
                    "evidence_path": entry["evidence_path"],
                    "evidence_manifest_hash": entry["evidence_manifest_hash"],
                    "evidence_file_sha256": entry["evidence_file_sha256"],
                    "evidence_kind": entry["evidence_kind"],
                    "event_observation": entry["event_observation"],
                    "status": entry["status"],
                }
                for entry in formal["formal_evidence"]["target_cases"]
            ]
            resolution["formal_verification"] = formal

        record["status"] = "RESOLVED"
        record["resolved_at"] = args.decided_at
        record["resolution"] = resolution

        changed = {k for k in set(before) | set(record) if before.get(k) != record.get(k)}
        extra = changed - MUTABLE_KEYS
        if extra:
            raise ResolveError(f"IMMUTABLE_FIELD_CHANGED: {sorted(extra)}")

        payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _durable_replace(path, original, payload)
    except (ResolveError, FormalResolutionError) as exc:
        # ここへ来たら Record は動いていない。
        print(f"not resolved: {exc}", file=sys.stderr)
        return 1
    except DurabilityUnconfirmed as exc:
        # **ここへ来たら Record は更新済みである。** 「失敗＝元のまま」と読ませない。
        print(
            f"RECORD UPDATED BUT DURABILITY UNCONFIRMED: {exc}\n{DurabilityUnconfirmed.RECOVERY}",
            file=sys.stderr,
        )
        return 2

    print(f"resolved {args.blocker_id}")
    for entry in checked:
        print(f"  {entry['case_id']}  {entry['evidence_kind']}/{entry['event_observation']}")
        print(f"    {entry['evidence_hash']}")
    if formal is not None:
        summary = formal["formal_evidence"]
        print(f"  mode=formal commit={summary['measured_source']['implementation_commit_sha']}")
        print(f"  manifest={summary['evidence_inputs']['manifest_sha256']}")
        for entry in summary["target_cases"]:
            print(
                f"  {entry['case_id']}  {entry['evidence_kind']}/{entry['event_observation']}"
                f"  {entry['evidence_file_sha256']}"
            )
        print(
            f"  unit_case_suite cases={len(summary['unit_area']['case_ids'])} "
            f"out_of_scope_missing={summary['out_of_scope']['global_missing_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
