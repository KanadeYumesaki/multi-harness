#!/usr/bin/env python3
"""回答済み Package と比較対象 Canon の影響を機械判定する。**判定だけを返す。**

## この Tool がしないこと

Package を作らない。後継を発行しない。既存 Package の Bytes を 1 つも動かさない。
Owner 回答を書き換えない。設計版も Registry も Schema も触らない。返すのは
「影響があるか」「Owner 承認が要るか」「承認があれば後継の資格を満たすか」の 3 つ
だけである（CPB-6-A／CPB-7-A）。

## 3 値で答える

``impact`` は ``false`` / ``true`` / ``"unknown"`` のいずれかである。

* ``false`` … 回答時点 Canon と比較対象が同じ。後継は要らない
* ``true``  … 差分を全て特定できた。Owner が判断する
* ``unknown``… 完全に比べられなかった。**必ず Owner 承認待ちで止まる**

比べられなかったものを ``false`` にしない。判定不能は判定不能として返す
（不変条件#6／#9）。

## 承認は「この比較に対する承認」でなければならない

``--owner-approval`` は ``<package_id>:<compared_canon_digest>`` の形を要求する。
digest はこの Tool が比較対象から計算する。**別の Canon へ出した承認を使い回せ
ない。** 既存統治の選択肢 ID（``CPB-*`` など）は承認入力として受け付けない。

## 欠けた Field を埋めない

``schema_catalog_hash`` を持たない Package では、その Field を **比較対象から外す**
（CPB-5-A）。現行 Snapshot の値も ``null`` も入れない。何を比べたかは
``compared_fields`` に残す。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Final

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import chat_canon_binding  # noqa: E402
from design_identity import design_version  # noqa: E402

#: 判定に使う Canon の Field。``schema_catalog_hash`` は Package が持つときだけ。
CANON_FIELDS: Final[tuple[str, ...]] = (
    "design_version",
    "design_sha256",
    "registry_snapshot_hash",
    "schema_catalog_hash",
)

#: この Tool が絶対に書かない場所。**後継 Package の置き場である。**
FORBIDDEN_OUTPUT_DIRS: Final[tuple[str, ...]] = ("docs/decision",)

#: 承認入力として受け付けない語。既存統治の選択肢 ID を承認へ流用させない。
REJECTED_APPROVAL_PREFIXES: Final[tuple[str, ...]] = ("CPB-", "MTM-", "REF-")


class CompatibilityInputError(RuntimeError):
    """入力が判定に使えない。"""


def _sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompatibilityInputError(f"READ_FAILED: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CompatibilityInputError(f"NOT_AN_OBJECT: {path}")
    return value


def compared_canon(design: Path, snapshot: Path) -> dict[str, Any]:
    """比較対象 Canon を読む。実体でも隔離 Fixture でも同じ扱いにする。"""
    try:
        design_bytes = design.read_bytes()
    except OSError as exc:
        raise CompatibilityInputError(f"READ_FAILED: {design}: {exc}") from exc
    try:
        version = design_version(design)
    except ValueError as exc:
        raise CompatibilityInputError(f"DESIGN_TITLE_UNREADABLE: {design}: {exc}") from exc
    document = _read_json(snapshot)
    return {
        "design_path": str(design),
        "snapshot_path": str(snapshot),
        "design_version": version,
        "design_sha256": _sha_bytes(design_bytes),
        "registry_snapshot_hash": document.get("registry_snapshot_hash"),
        "schema_catalog_hash": document.get("schema_catalog_hash"),
    }


def canon_digest(canon: dict[str, Any]) -> str:
    """比較対象を 1 つの digest にする。承認をこの比較へ縛るために使う。"""
    projection = {name: canon.get(name) for name in CANON_FIELDS}
    body = json.dumps(projection, ensure_ascii=False, sort_keys=True)
    return _sha_bytes(body.encode("utf-8"))


def _choice_problems(package: dict[str, Any]) -> list[str]:
    """回答と設問の食い違い。**Hash の算式を推測しない。**

    回答 Hash の算式は Package ごとに違う（改行終端の有無が揃っていない）。
    共通の再計算を当てると、算式が違うだけの Package を改ざんと誤判定する。
    そこで比べるのは、選択 ID が設問の選択肢にあることと、写した文面が一致する
    ことだけにする。
    """
    problems: list[str] = []
    if package.get("status") != "ANSWERED":
        problems.append(f"PACKAGE_NOT_ANSWERED: status={package.get('status')!r}")
    if package.get("unanswered"):
        problems.append(f"PACKAGE_HAS_UNANSWERED: {package.get('unanswered')}")
    questions = package.get("questions")
    answers = package.get("answers")
    if not isinstance(questions, list) or not isinstance(answers, dict) or not answers:
        problems.append("PACKAGE_SHAPE_UNCOMPARABLE: questions/answers")
        return problems
    by_id = {q.get("id"): q for q in questions if isinstance(q, dict)}
    if set(answers) != set(by_id):
        problems.append("PACKAGE_ANSWER_SET_MISMATCH")
    for qid, answer in sorted(answers.items()):
        question = by_id.get(qid)
        if question is None or not isinstance(answer, dict):
            problems.append(f"PACKAGE_CHOICE_INCONSISTENT: {qid}: 設問が無い")
            continue
        options = {o.get("id"): o for o in question.get("options", []) if isinstance(o, dict)}
        chosen = options.get(answer.get("choice_id"))
        if chosen is None:
            problems.append(f"PACKAGE_CHOICE_INCONSISTENT: {qid}: 選択肢に無い ID")
            continue
        # 文面を写す Package と、`choice_id` だけを記録する Package がある。
        # **写していないことは違反ではない。** 写したのに食い違うことが違反である。
        for field in ("label", "detail"):
            if field in answer and answer.get(field) != chosen.get(field):
                problems.append(f"PACKAGE_CHOICE_INCONSISTENT: {qid}: {field} が写しでない")
    return problems


def _recorded_choices(package: dict[str, Any]) -> list[dict[str, str]]:
    answers = package.get("answers")
    if not isinstance(answers, dict):
        return []
    return [
        {"question_id": qid, "choice_id": str(answer.get("choice_id"))}
        for qid, answer in sorted(answers.items())
        if isinstance(answer, dict)
    ]


def judge(
    *,
    repo_root: Path,
    package_path: Path,
    design: Path,
    snapshot: Path,
    approval: str | None,
) -> dict[str, Any]:
    package = _read_json(package_path)
    package_id = str(package.get("package_id") or package_path.stem)
    binding = chat_canon_binding.answer_time_binding(package)
    schema_present = "schema_catalog_hash" in binding

    resolution = chat_canon_binding.resolve_answer_time_canon(
        repo_root, binding, package_id=package_id
    )
    target = compared_canon(design, snapshot)
    digest = canon_digest(target)

    # 比べる Field は Package が名乗ったものだけ。**欠けたものを補わない。**
    compared_fields = ["design_version", "design_sha256", "registry_snapshot_hash"]
    if schema_present:
        compared_fields.append("schema_catalog_hash")

    changed: list[dict[str, Any]] = []
    for field in compared_fields:
        bound_value = binding.get(field)
        target_value = target.get(field)
        if bound_value != target_value:
            changed.append({"field": field, "bound": bound_value, "compared": target_value})

    blockers: list[str] = list(_choice_problems(package))
    if not resolution["matches"]:
        failure = resolution["failure"] or {}
        blockers.append(f"ANSWER_TIME_CANON_UNRESOLVED: {failure.get('code')}")
    if any(target.get(field) is None for field in compared_fields):
        blockers.append("COMPARED_CANON_INCOMPLETE")

    impact: bool | str
    if blockers:
        impact = "unknown"
    elif changed:
        impact = True
    else:
        impact = False

    owner_approval_required = impact is not False
    expected_approval = f"{package_id}:{digest}"
    approval_present = approval is not None and approval == expected_approval
    approval_rejected_because: str | None = None
    if approval is not None and not approval_present:
        if approval.startswith(REJECTED_APPROVAL_PREFIXES):
            approval_rejected_because = "GOVERNANCE_CHOICE_ID_IS_NOT_AN_APPROVAL"
        else:
            approval_rejected_because = "APPROVAL_DOES_NOT_MATCH_COMPARISON"

    successor_eligible = impact is True and approval_present

    return {
        "report_id": "CODEX-CHAT-CANON-COMPATIBILITY",
        "package_id": package_id,
        "package_path": str(package_path),
        "bound_canon": {
            "design_version": binding.get("design_version"),
            "design_sha256": binding.get("design_sha256"),
            "registry_snapshot_hash": binding.get("registry_snapshot_hash"),
            "schema_catalog_hash_present": schema_present,
            "resolution": resolution,
        },
        "compared_canon": {**target, "digest": digest},
        "compared_fields": compared_fields,
        "impact": impact,
        "changed": changed,
        "blockers": sorted(blockers),
        "recorded_choices": _recorded_choices(package),
        "owner_approval_required": owner_approval_required,
        "approval_present": approval_present,
        "approval_rejected_because": approval_rejected_because,
        "expected_approval_form": "<package_id>:<compared_canon_digest>",
        "successor_eligible": successor_eligible,
        "what_this_tool_does_not_do": [
            "後継 Package を作らない。",
            "既存 Package の Bytes を変更しない。",
            "設計版・Registry・Schema・Snapshot を変更しない。",
            "Owner 回答を自動で埋めない。",
            "承認が無い判定を承認済みとして返さない。",
        ],
    }


def _guard_output(out: Path) -> None:
    resolved = out.resolve()
    for forbidden in FORBIDDEN_OUTPUT_DIRS:
        base = (ROOT / forbidden).resolve()
        if resolved == base or base in resolved.parents:
            raise CompatibilityInputError(
                f"OUTPUT_PATH_FORBIDDEN: {out}: この Tool は {forbidden} へ書かない"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--against-design", type=Path, required=True)
    parser.add_argument("--against-snapshot", type=Path, required=True)
    parser.add_argument("--owner-approval", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    try:
        if args.out is not None:
            _guard_output(args.out)
        report = judge(
            repo_root=args.repo_root,
            package_path=args.package,
            design=args.against_design,
            snapshot=args.against_snapshot,
            approval=args.owner_approval,
        )
    except (CompatibilityInputError, chat_canon_binding.CanonResolutionError) as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2

    body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(body, encoding="utf-8")
    print(body, end="")

    if report["impact"] is False:
        return 0
    if report["owner_approval_required"] and not report["approval_present"]:
        print(
            "OWNER_APPROVAL_REQUIRED: "
            f"--owner-approval {report['package_id']}:{report['compared_canon']['digest']}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
