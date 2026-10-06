"""Build the Codex-side audit and unanswered DCR for Chat canon binding.

This tool deliberately does not modify existing decision packages, design files,
registry files, schemas, evidence, or blocked records.  It measures the current
tree and writes only the two new audit files and the two new DCR presentation
files.  A later Owner-answer tool may consume the DCR, but this module never
records an answer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import chat_canon_binding  # type: ignore[import-not-found]  # noqa: E402
from design_identity import design_version  # type: ignore[import-not-found]  # noqa: E402

DECISION_DIR = ROOT / "docs" / "decision"
AUDIT_DIR = ROOT / "docs" / "audit"
AUDIT_JSON = AUDIT_DIR / "codex-chat-canon-binding-review.json"
AUDIT_MD = AUDIT_DIR / "codex-chat-canon-binding-review.md"
DCR_JSON = DECISION_DIR / "DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE.json"
DCR_MD = DECISION_DIR / "DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE.md"

DESIGN = ROOT / "design-v1.24-runtime-go.md"
SNAPSHOT = ROOT / "registry-snapshot.json"

#: 凍結 Audit を **最初に作ったときの** 測定データ。
#:
#: 回答済み Package の検証はここを権威にしない。検証は Audit が記録した
#: ``commit`` から解決する（CPB-2-A／CPB-3-A）。この表を並べ替えても消しても、
#: 回答済みの検証結果は変わらない。
HISTORY_COMMITS = {
    "1.19": "5aaa6c63eb5237e468ccb616bc3ba6de44307fc1",
    "1.20": "069847b6c3fda5755bbc59d3e6017a50332cf906",
    "1.21": "8b0406b785ddef1aa4d8350e685c38cb2320879d",
    "1.22": "89839a8688e9fe56ef6d9c33f701b57e5b008e25",
    "1.23": "9a0e8e1eaad7d259373e95e18f558c786bbeb1af",
}

CHAT_PACKAGE_PREFIXES = ("DCR-CHAT-", "OWNER-DECISION-CHAT-")
REPRO_FILES = (
    "tests/spec_lint/test_chat_execution_mode_dcr.py",
    "tests/spec_lint/test_chat_provider_readiness.py",
    "tests/spec_lint/test_chat_provider_value_input.py",
    "tests/spec_lint/test_chat_provider_values_dcr.py",
    "tests/spec_lint/test_ci_post_reset_verification.py",
    "tests/spec_lint/test_provider_readiness_v1_cleanup.py",
    "tests/spec_lint/test_route_profile_inventory_v2.py",
    "tests/spec_lint/test_route_profile_retry_dcr.py",
    "tests/spec_lint/test_route_profile_value_inventory.py",
    "tests/spec_lint/test_route_rule_contract_derivation.py",
    "tests/spec_lint/test_route_rule_contract_values.py",
    "tests/spec_lint/test_route_rule_dcr.py",
)
REPRO_SYMBOLS = (
    "reproduc",
    "bound_to_the_current_canon",
    "current_canon",
)


def digest_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def git(*args: str) -> str:
    return subprocess.check_output(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        text=True,
    ).strip()


def current_canon() -> dict[str, Any]:
    design_bytes = DESIGN.read_bytes()
    snapshot = read_json(SNAPSHOT)
    return {
        "design_version": design_version(DESIGN),
        "design_sha256": digest_bytes(design_bytes),
        "registry_snapshot_hash": snapshot["registry_snapshot_hash"],
        "schema_catalog_hash": snapshot["schema_catalog_hash"],
        "design_path": str(DESIGN.relative_to(ROOT)),
    }


def is_chat_package(path: Path) -> bool:
    return path.name.startswith(CHAT_PACKAGE_PREFIXES)


def package_inventory() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(DECISION_DIR.glob("*.json")):
        if path.name == DCR_JSON.name:
            continue
        try:
            package = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not package.get("design_sha256"):
            continue
        answers = package.get("answers", {})
        unanswered = package.get("unanswered", [])
        rows.append(
            {
                "path": str(path.relative_to(ROOT)),
                "package_id": package.get("package_id", path.stem),
                "task_id": package.get("task_id"),
                "status": package.get("status"),
                "design_version": package.get("design_version"),
                "design_sha256": package.get("design_sha256"),
                "registry_snapshot_hash": package.get("registry_snapshot_hash"),
                "schema_catalog_hash_present": "schema_catalog_hash" in package,
                "answers_count": len(answers) if isinstance(answers, dict) else 0,
                "unanswered_count": len(unanswered) if isinstance(unanswered, list) else 0,
                "is_answered": bool(answers) and not unanswered,
                "is_chat": is_chat_package(path),
            }
        )
    return rows


def history_match(version: str, target_hash: str, *, commit: str) -> dict[str, Any]:
    """その版の設計正本を、**渡された commit** から解決する。

    ``commit`` は呼び手が渡す。初回測定は :data:`HISTORY_COMMITS` から、回答済み
    の検証は凍結 Audit の記録から渡る。**この関数の中で固定表を引かない。**

    解決できないときは ``match: False`` と ``failure_code`` を返す。成功時の戻り
    値は失敗の Key を持たないので、凍結 Audit の項目とそのまま比べられる。

    設計 File 名を戻り値へ写さない。``check_design_reference_currency`` が旧版名
    を運用上の指し先として数えるためである。版・commit・件数・真偽で足りる。
    """
    try:
        chat_canon_binding.resolve_historical_design(
            ROOT,
            {
                "commit": commit,
                "design_version": version,
                "package_design_sha256": target_hash,
            },
        )
    except chat_canon_binding.CanonResolutionError as exc:
        return {
            "design_version": version,
            "commit": commit,
            "package_design_sha256": target_hash,
            "matching_path_count": 0,
            "match": False,
            "failure_code": exc.code,
        }
    return {
        "design_version": version,
        "commit": commit,
        "package_design_sha256": target_hash,
        "matching_path_count": 1,
        "match": True,
    }


def supersedes_inventory() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}/{key}"
                if "supersed" in key.lower():
                    found.append({"path": child_path, "value": child})
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}/{index}")

    for path in sorted(DECISION_DIR.glob("*.json")):
        if path.name == DCR_JSON.name:
            continue
        try:
            walk(read_json(path), str(path.relative_to(ROOT)))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return found


def reproducibility_inventory() -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    total = 0
    for relative in REPRO_FILES:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        refs = [
            line.strip()
            for line in text.splitlines()
            if any(symbol in line.lower() for symbol in REPRO_SYMBOLS)
            and line.lstrip().startswith("def test_")
        ]
        files.append({"path": relative, "references": refs, "reference_count": len(refs)})
        total += len(refs)
    return {"distinct_files": len(files), "total_references": total, "files": files}


def build_audit() -> dict[str, Any]:
    canon = current_canon()
    rows = package_inventory()
    chat = [row for row in rows if row["is_chat"]]
    answered_chat = [row for row in chat if row["is_answered"]]
    with_schema = [row for row in answered_chat if row["schema_catalog_hash_present"]]
    without_schema = [row for row in answered_chat if not row["schema_catalog_hash_present"]]
    all_design_bound = [row for row in rows if row["design_sha256"]]
    all_answered = [row for row in rows if row["is_answered"]]
    historical = []
    by_version = {row["design_version"]: row for row in answered_chat}
    for version in sorted(HISTORY_COMMITS):
        row = by_version.get(version)
        if row is None:
            raise ValueError(f"missing answered Chat package for v{version}")
        historical.append(
            history_match(version, row["design_sha256"], commit=HISTORY_COMMITS[version])
        )
    return {
        "report_version": "1.0",
        "report_id": "CODEX-CHAT-CANON-BINDING-REVIEW",
        "measurement_basis": {
            "repository_root": str(ROOT),
            "head": git("rev-parse", "HEAD"),
            "design_version": canon["design_version"],
            "design_sha256": canon["design_sha256"],
            "registry_snapshot_hash": canon["registry_snapshot_hash"],
            "schema_catalog_hash": canon["schema_catalog_hash"],
        },
        "package_counts": {
            "all_design_bound_json": len(all_design_bound),
            "all_answered_json": len(all_answered),
            "chat_design_bound_json": len(chat),
            "chat_answered_json": len(answered_chat),
            "chat_answered_with_schema_catalog_hash": len(with_schema),
            "chat_answered_without_schema_catalog_hash": len(without_schema),
            "chat_unanswered_design_bound_json": len(chat) - len(answered_chat),
        },
        "chat_packages": chat,
        "schema_hash_distribution": {
            "with_hash": [row["path"] for row in with_schema],
            "without_hash": [row["path"] for row in without_schema],
        },
        "current_canon_binding_tests": {
            "function_count": 3,
            "answered_dcr_package_count": 2,
            "unanswered_input_or_inventory_count": 1,
            "references": [
                {
                    "path": "tests/spec_lint/test_chat_provider_values_dcr.py",
                    "symbol": "test_bound_to_the_current_canon",
                    "package": "DCR-CHAT-PROVIDER-VALUES.json",
                },
                {
                    "path": "tests/spec_lint/test_chat_provider_readiness.py",
                    "symbol": "test_dcr_is_bound_to_the_current_canon",
                    "package": "DCR-CHAT-PROVIDER-CONFIG.json",
                },
                {
                    "path": "tests/spec_lint/test_chat_provider_value_input.py",
                    "symbol": "test_bound_to_the_current_canon",
                    "package": (
                        "DCR-CHAT-PROVIDER-VALUE-INPUT.json + ROUTE-PROFILE-VALUE-INVENTORY-V2.json"
                    ),
                },
            ],
        },
        "reproducibility_tests": reproducibility_inventory(),
        "historical_design_hash_matches": historical,
        "supersedes_key_scan": {
            "count": len(supersedes_inventory()),
            "entries": supersedes_inventory(),
            "interpretation": (
                "存在する仕組みの実測であり、回答済みChat Packageへ適用する承認ではない"
            ),
        },
        "external_claims": {
            "v1_25_attempt": {
                "status": "UNVERIFIED_EXTERNAL_CLAIM",
                "reported_failure_count": 26,
                "basis": (
                    "ユーザー提供のClaude報告。実行Artifactが現行Repositoryに無いため、"
                    "監査事実として採用しない"
                ),
            }
        },
        "conclusions": [
            (
                "12件というChat回答済み件数は、対象をChatかつANSWEREDへ限定した場合に成立する。"
                "全design-bound JSONの件数とは別である。"
            ),
            (
                "回答済みChat 12件のschema_catalog_hashは7件あり、"
                "旧5件が全て欠くという説明は成立しない。"
            ),
            (
                "現行Canon一致を要求するテストは3関数で、"
                "回答済みDCR 2件と未回答入力/Inventory 1組を含む。"
            ),
            "回答時点のCanonを保持するか、版上げ時に全Packageを再発行するかは、実装ではなくOwner判断が必要である。",
        ],
    }


QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "CPB-1",
        "title": "回答済みPackageのCanon束縛時点",
        "question": "回答済みDecision PackageはどのCanonへ束縛するか",
        "recommended": "CPB-1-A",
        "options": [
            {
                "id": "CPB-1-A",
                "label": "回答時点のCanonを保持",
                "detail": (
                    "Packageが記録したdesign/registry/schema Hashと回答時点のGit正本を固定し、"
                    "後の版上げでBytesを動かさない。"
                ),
                "grounded_in": "現行PackageのHash記録と履歴一致の実測。",
            },
            {
                "id": "CPB-1-B",
                "label": "常に現行Canonへ追随",
                "detail": (
                    "既存のcurrent-canon試験を維持し、設計版上げのたびに"
                    "全ての回答済みPackageを再発行する。"
                ),
                "grounded_in": "現行のtest_bound_to_the_current_canon実測。",
            },
            {
                "id": "CPB-1-C",
                "label": "別の束縛記録へ分離",
                "detail": (
                    "Package本文の束縛を外し、回答とCanonの対応を別の正本記録で管理する。"
                    "新しい記録形式の設計が必要。"
                ),
                "grounded_in": "既存Packageのbound_toフィールドとsupersedesキーの存在。",
            },
        ],
    },
    {
        "id": "CPB-2",
        "title": "過去Canonの信頼根",
        "question": "回答時点の設計正本を後から検証する根拠をどこに置くか",
        "recommended": "CPB-2-A",
        "options": [
            {
                "id": "CPB-2-A",
                "label": "Git履歴の正本を使う",
                "detail": (
                    "回答時点Hashと一致する設計FileをGit commit履歴から解決する。"
                    "履歴に一致が無ければ検証を停止する。"
                ),
                "grounded_in": "v1.19〜v1.23の5件でcommitと設計Bytesが一致した実測。",
            },
            {
                "id": "CPB-2-B",
                "label": "Repository内の履歴台帳を追加",
                "detail": (
                    "版ごとの設計PathとHashを別Fileへ記録する。台帳自体の初期化と更新規則が必要。"
                ),
                "grounded_in": "現行Repositoryに版別台帳が無いことの実測。",
            },
            {
                "id": "CPB-2-C",
                "label": "外部の不変記録へ委ねる",
                "detail": "Repository外の署名済み記録を参照する。参照方式と可用性を別途定義する。",
                "grounded_in": "現行Repositoryに外部Trust Anchorの参照が無いことの実測。",
            },
        ],
    },
    {
        "id": "CPB-3",
        "title": "現行Canon一致試験の扱い",
        "question": "回答済みPackageを回答時点束縛へ変更する場合、現行一致試験をどう扱うか",
        "recommended": "CPB-3-A",
        "options": [
            {
                "id": "CPB-3-A",
                "label": "版対応Resolverへ変更",
                "detail": (
                    "Packageの記録Version/Hashを履歴から解決し、"
                    "回答時点のCanonと一致することを検証する。"
                ),
                "grounded_in": "現行3関数が現在のSnapshotとの一致を直接要求している実測。",
            },
            {
                "id": "CPB-3-B",
                "label": "回答時点Hashだけを検証",
                "detail": (
                    "current-canon要求を外し、Package内の記録Hashと"
                    "保存済み履歴の一致だけを確認する。"
                ),
                "grounded_in": "現行試験が束縛Hashを比較している実測。",
            },
            {
                "id": "CPB-3-C",
                "label": "現行一致を維持",
                "detail": "設計版を上げるたびに回答済みPackageを再生成して現行Hashへ追随させる。",
                "grounded_in": "現行試験の要求と、版上げ試行で束縛が外れたという報告。",
            },
        ],
    },
    {
        "id": "CPB-4",
        "title": "回答済みPackageの再現性",
        "question": "回答済みPackageの再現性をどの形で保証するか",
        "recommended": "CPB-4-A",
        "options": [
            {
                "id": "CPB-4-A",
                "label": "回答時点入力で再生成",
                "detail": (
                    "回答時点のCanonを解決する入力を与え、"
                    "回答ID・選択Hash・非回答部分を再生成して比較する。"
                ),
                "grounded_in": "現行ソースの12ファイル19参照の再現性試験実測。",
            },
            {
                "id": "CPB-4-B",
                "label": "保存Bytesを凍結比較",
                "detail": (
                    "回答済みPackageの保存Bytesを固定し、Builderの上書きを拒否したうえで"
                    "凍結Bytesを比較する。"
                ),
                "grounded_in": "回答済みPackageを変更しない既存運用とBuilder guardの実測。",
            },
            {
                "id": "CPB-4-C",
                "label": "回答済みPackageを再生成しない",
                "detail": (
                    "回答済みPackageの生成経路を検証対象から外し、"
                    "監査は記録済みBytesだけを確認する。"
                ),
                "grounded_in": "現行のreproducibility試験が存在する実測。",
            },
        ],
    },
    {
        "id": "CPB-5",
        "title": "schema_catalog_hash欠落の扱い",
        "question": "schema_catalog_hashを持たない回答済みChat Packageをどう扱うか",
        "recommended": "CPB-5-A",
        "options": [
            {
                "id": "CPB-5-A",
                "label": "欠落を回答時点の形として保持",
                "detail": "Fieldが無いこと自体を旧契約の記録として扱い、後から必須化しない。",
                "grounded_in": "回答済みChat 12件中5件にFieldが無い実測。",
            },
            {
                "id": "CPB-5-B",
                "label": "不足Packageを再発行",
                "detail": (
                    "schema_catalog_hashを追加した後継Packageを発行し、"
                    "旧Bytesはread-onlyで保持する。"
                ),
                "grounded_in": "旧Schemaをread-only保持した既存の版管理パターン。",
            },
            {
                "id": "CPB-5-C",
                "label": "Schema使用Packageだけ要求",
                "detail": (
                    "schema_catalog_hashが必要な契約を持つPackageだけへ要求し、他は欠落を許容する。"
                ),
                "grounded_in": "Packageごとにschema_catalog_hashの有無が異なる実測。",
            },
        ],
    },
    {
        "id": "CPB-6",
        "title": "supersedesの適用範囲",
        "question": "設計版上げ時に回答済みChat Packageへ後継Packageを発行してよいか",
        "recommended": "CPB-6-A",
        "options": [
            {
                "id": "CPB-6-A",
                "label": "明示条件つきで後継を許可",
                "detail": (
                    "supersedesを使い、回答ID・選択Hash・未変更Fieldを"
                    "機械比較できる場合だけ後継を発行する。"
                ),
                "grounded_in": (
                    "Repositoryにsupersedesキーが3箇所存在する実測。"
                    "ただしChatへの適用実績は未確認。"
                ),
            },
            {
                "id": "CPB-6-B",
                "label": "後継を発行しない",
                "detail": (
                    "回答済みPackageは回答時点Canonへ固定し、"
                    "版上げ後も旧Packageを正本として保持する。"
                ),
                "grounded_in": "既存回答済みPackageのBytes不変要求。",
            },
            {
                "id": "CPB-6-C",
                "label": "再回答を必須にする",
                "detail": "設計版上げごとにOwnerへ再提示し、新しい回答として記録する。",
                "grounded_in": "既存Recorderが回答IDを記録する運用。",
            },
        ],
    },
    {
        "id": "CPB-7",
        "title": "設計版上げの統治",
        "question": "既存回答を維持したまま設計版を上げる際の適用判定を誰が行うか",
        "recommended": "CPB-7-A",
        "options": [
            {
                "id": "CPB-7-A",
                "label": "互換性を機械判定しOwner承認",
                "detail": (
                    "変更されたField・Hash・選択肢の影響を検査し、"
                    "影響がある場合だけOwnerが後継または再回答を選ぶ。"
                ),
                "grounded_in": "既存のOwner回答ガードと不変Field検査。",
            },
            {
                "id": "CPB-7-B",
                "label": "版上げごとに全件再確認",
                "detail": "影響の大小に関係なく全ての回答済みPackageをOwnerが確認する。",
                "grounded_in": "current-canon束縛を維持する場合の運用上の帰結。",
            },
            {
                "id": "CPB-7-C",
                "label": "自動規則だけで決める",
                "detail": (
                    "既存選択IDとFieldの機械比較だけで継続可否を決め、Ownerの再承認を不要にする。"
                ),
                "grounded_in": "既存の機械的整合性検査。",
            },
        ],
    },
]


def build_dcr(audit: dict[str, Any]) -> dict[str, Any]:
    audit_text = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    audit_hash = digest_bytes(audit_text.encode("utf-8"))
    return {
        "document_version": "1.0",
        "package_id": "DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE",
        "task_id": "TASK-CODEX-CHAT-CANON-BINDING-LIFECYCLE-001",
        "status": "DECISION_REQUIRED",
        "design_version": audit["measurement_basis"]["design_version"],
        "design_sha256": audit["measurement_basis"]["design_sha256"],
        "registry_snapshot_hash": audit["measurement_basis"]["registry_snapshot_hash"],
        "schema_catalog_hash": audit["measurement_basis"]["schema_catalog_hash"],
        "raised_because": (
            "設計版上げ時に回答済みPackageが現行Canon一致試験と衝突するため、"
            "束縛時点・履歴根拠・再現性・後継発行の統治をOwnerが決める必要がある。"
        ),
        "bound_to": {
            "audit_path": str(AUDIT_JSON.relative_to(ROOT)),
            "audit_sha256": audit_hash,
            "measurement_head": audit["measurement_basis"]["head"],
        },
        "questions": QUESTIONS,
        "answers": {},
        "unanswered": [question["id"] for question in QUESTIONS],
        "counts": {
            "questions": len(QUESTIONS),
            "options": sum(len(question["options"]) for question in QUESTIONS),
            "answers": 0,
            "unanswered": len(QUESTIONS),
        },
        "measured_facts": {
            "chat_answered_packages": audit["package_counts"]["chat_answered_json"],
            "chat_design_bound_packages": audit["package_counts"]["chat_design_bound_json"],
            "current_canon_binding_function_count": audit["current_canon_binding_tests"][
                "function_count"
            ],
            "reproducibility_test_distinct_files": audit["reproducibility_tests"]["distinct_files"],
            "reproducibility_test_reference_count": audit["reproducibility_tests"][
                "total_references"
            ],
            "historical_hash_matches": all(
                item["match"] for item in audit["historical_design_hash_matches"]
            ),
            "supersedes_key_count": audit["supersedes_key_scan"]["count"],
        },
        "what_this_package_does_not_do": [
            "設計書の版上げ・Registry変更・Schema変更を行わない。",
            "回答済みDecision Package、Evidence、Block Recordを変更しない。",
            "v1.25反映や26件失敗という外部報告を実測事実として採用しない。",
            "ClaudeCode向けの実装指示やProduction変更を行わない。",
        ],
        "answer_source": "OWNER_ONLY",
    }


def audit_markdown(audit: dict[str, Any]) -> str:
    c = audit["package_counts"]
    lines = [
        "# Codex Chat Canon Binding Review",
        "",
        (
            "この監査は現行Repositoryから再生成した事実だけを記録する。"
            "回答済みPackage、設計正本、Registry、Schema、Evidence、Block Recordは変更していない。"
        ),
        "",
        "## 実測",
        "",
        f"- HEAD: `{audit['measurement_basis']['head']}`",
        (
            f"- Design: v{audit['measurement_basis']['design_version']} / "
            f"`{audit['measurement_basis']['design_sha256']}`"
        ),
        f"- Registry Snapshot: `{audit['measurement_basis']['registry_snapshot_hash']}`",
        f"- Schema Catalog: `{audit['measurement_basis']['schema_catalog_hash']}`",
        (
            f"- 全 design-bound JSON: {c['all_design_bound_json']} 件、"
            f"ANSWERED: {c['all_answered_json']} 件"
        ),
        (
            f"- Chat design-bound: {c['chat_design_bound_json']} 件、"
            f"ANSWERED: {c['chat_answered_json']} 件"
        ),
        (
            f"- Chat answeredのschema_catalog_hash有り: "
            f"{c['chat_answered_with_schema_catalog_hash']} 件 / "
            f"無し: {c['chat_answered_without_schema_catalog_hash']} 件"
        ),
        "",
        "## 監査結果",
        "",
        (
            "1. 「12件」はChatかつANSWEREDへ限定した数としてのみ成立する。"
            "全design-bound JSONの数ではない。"
        ),
        (
            "2. 回答済みChat 12件のschema_catalog_hashは7件に存在し、"
            "旧5件がすべて欠落という説明は成立しない。"
        ),
        (
            "3. current Canon束縛試験は3関数で、回答済みDCR 2件と"
            "未回答入力/Inventory 1組を対象にする。"
        ),
        "4. v1.19〜v1.23の5つの回答済みChat Packageは、Git履歴の対応設計Bytesと一致した。",
        "5. supersedesキーは3箇所で実測したが、回答済みChatへ適用する承認や前例は別途必要である。",
        "",
        "## 未確認として隔離した主張",
        "",
        "Claude報告のv1.25反映時26件失敗は、現行Repositoryに実行Artifactが無いため外部未検証主張として扱った。数値を正本の事実へ昇格させていない。",
        "",
        "## 次のOwner判断",
        "",
        "DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE（CPB-1〜CPB-7、選択肢21件、未回答7件）へ回答する。回答が記録されるまで設計版上げ・Package再生成・ClaudeCode実装指示は行わない。",
        "",
    ]
    return "\n".join(lines)


def dcr_markdown(dcr: dict[str, Any]) -> str:
    lines = [
        "# DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE",
        "",
        "## 目的",
        "",
        (
            "設計版上げで回答済みDecision Packageの束縛が外れる問題について、"
            "束縛時点・履歴根拠・再現性・後継発行の統治をOwnerが決める。"
        ),
        "",
        f"- status: `{dcr['status']}`",
        (
            f"- 設問: {dcr['counts']['questions']} 件 / "
            f"選択肢: {dcr['counts']['options']} 件 / "
            f"未回答: {dcr['counts']['unanswered']} 件"
        ),
        f"- Design: v{dcr['design_version']} / `{dcr['design_sha256']}`",
        "- 回答は記録していない。推奨は表示だけで、自動選択しない。",
        "",
        "## Ownerへの質問",
        "",
    ]
    for question in dcr["questions"]:
        lines.append(f"### {question['id']} — {question['title']}")
        lines.append("")
        lines.append(question["question"])
        lines.append("")
        for option in question["options"]:
            suffix = "（推奨・自動選択なし）" if option["id"] == question["recommended"] else ""
            lines.append(f"- `{option['id']}` {option['label']}{suffix}: {option['detail']}")
        lines.append("")
    lines.extend(
        [
            "## 制約",
            "",
            (
                "回答済みPackage、設計正本、Registry、Schema、Evidence、"
                "Block Record、ProductionコードはこのPackageでは変更しない。"
                "Provider固有値や新しい正本語彙も記録しない。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def write_or_check(path: Path, content: str, check: bool) -> None:
    if check:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            raise SystemExit(f"OUT_OF_DATE: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _validate_answered_package() -> None:
    """Validate the frozen answer-time audit and answered DCR in place.

    ``build_audit`` deliberately measures the *current* tree, whose commit
    changes on every implementation commit.  That is not the canon selected
    by an already answered package.  For an answered package we therefore
    validate the bytes frozen by its recorded audit hash and resolve the
    historical design commits again, without comparing them to current HEAD.
    """

    try:
        existing = read_json(DCR_JSON)
        frozen_audit = read_json(AUDIT_JSON)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ANSWERED_PACKAGE_INVALID: frozen record unreadable: {exc}") from exc
    bound_to = existing.get("bound_to")
    if not isinstance(bound_to, dict):
        raise SystemExit("ANSWERED_PACKAGE_INVALID: bound_to shape")
    measured_audit_hash = digest(AUDIT_JSON)
    if measured_audit_hash != bound_to.get("audit_sha256"):
        raise SystemExit("FROZEN_ANSWERED_AUDIT_CHANGED: recorded audit bytes changed")
    if bound_to.get("audit_path") != str(AUDIT_JSON.relative_to(ROOT)):
        raise SystemExit("ANSWERED_PACKAGE_INVALID: audit path binding")
    basis = frozen_audit.get("measurement_basis")
    if not isinstance(basis, dict):
        raise SystemExit("ANSWERED_PACKAGE_INVALID: audit measurement basis")
    if bound_to.get("measurement_head") != basis.get("head"):
        raise SystemExit("ANSWERED_PACKAGE_INVALID: measurement head binding")
    for field in (
        "design_version",
        "design_sha256",
        "registry_snapshot_hash",
        "schema_catalog_hash",
    ):
        if existing.get(field) != basis.get(field):
            raise SystemExit(f"ANSWERED_PACKAGE_INVALID: audit binding {field}")

    historical = frozen_audit.get("historical_design_hash_matches")
    if not isinstance(historical, list) or not historical:
        raise SystemExit("ANSWERED_PACKAGE_INVALID: historical design evidence")
    for item in historical:
        if not isinstance(item, dict) or not item.get("match"):
            raise SystemExit("GIT_HISTORY_MATCH_MISSING: frozen audit unresolved")
        if not item.get("commit"):
            raise SystemExit("ANSWERED_PACKAGE_INVALID: historical commit missing")
        checked = history_match(
            str(item["design_version"]),
            str(item["package_design_sha256"]),
            commit=str(item["commit"]),
        )
        if checked != {
            "design_version": item.get("design_version"),
            "commit": item.get("commit"),
            "package_design_sha256": item.get("package_design_sha256"),
            "matching_path_count": item.get("matching_path_count"),
            "match": item.get("match"),
        }:
            reason = checked.get("failure_code", "RESOLVED_VALUES_DIFFER")
            raise SystemExit(
                f"GIT_HISTORY_RESOLUTION_CHANGED: v{item.get('design_version')}: {reason}"
            )
    if not AUDIT_MD.exists() or AUDIT_MD.read_text(encoding="utf-8") != audit_markdown(
        frozen_audit
    ):
        raise SystemExit("FROZEN_ANSWERED_AUDIT_CHANGED: audit Markdown changed")

    # 回答済み Package 自身の Canon も Git 履歴から解決する。**作業ツリーの設計
    # File と現行 Snapshot は見ない。** 設計版を上げても、この検証は回答時点の
    # 版を指し続ける（CPB-1-A／CPB-2-A／CPB-3-A）。
    try:
        chat_canon_binding.verify_answer_time_canon(
            ROOT, existing, package_id=str(existing.get("package_id"))
        )
    except chat_canon_binding.CanonResolutionError as exc:
        raise SystemExit(f"ANSWER_TIME_CANON_UNRESOLVED: {exc.code}: {exc.detail}") from exc

    questions = existing.get("questions")
    answers = existing.get("answers")
    unanswered = existing.get("unanswered")
    counts = existing.get("counts")
    if existing.get("status") != "ANSWERED":
        raise SystemExit("ANSWERED_PACKAGE_INVALID: status")
    if not isinstance(questions, list) or not isinstance(answers, dict):
        raise SystemExit("ANSWERED_PACKAGE_INVALID: questions/answers shape")
    question_ids = [question.get("id") for question in questions]
    if any(not isinstance(question_id, str) for question_id in question_ids):
        raise SystemExit("ANSWERED_PACKAGE_INVALID: question id shape")
    if set(answers) != set(question_ids) or unanswered != []:
        raise SystemExit("ANSWERED_PACKAGE_INVALID: answered/unanswered mismatch")
    if not isinstance(counts, dict):
        raise SystemExit("ANSWERED_PACKAGE_INVALID: counts shape")
    if counts.get("answers") != len(answers) or counts.get("unanswered") != 0:
        raise SystemExit("ANSWERED_PACKAGE_INVALID: counts mismatch")
    if counts.get("questions") != len(questions):
        raise SystemExit("ANSWERED_PACKAGE_INVALID: question count")

    # 設問・選択肢・回答 ID・回答 Hash の改変を見つける。
    #
    # ここが無いと、選択 ID を別の選択肢へ差し替えても、選択肢を 1 つ削っても、
    # 回答 Hash を書き換えても検査を素通りした。**実測して分かった抜けである。**
    option_total = 0
    for question in questions:
        options = question.get("options")
        if not isinstance(options, list) or not options:
            raise SystemExit(f"ANSWERED_PACKAGE_INVALID: options: {question.get('id')}")
        option_total += len(options)
        by_id = {option.get("id"): option for option in options if isinstance(option, dict)}
        answer = answers.get(question.get("id"))
        if not isinstance(answer, dict):
            raise SystemExit(f"ANSWERED_PACKAGE_INVALID: answer shape: {question.get('id')}")
        chosen = by_id.get(answer.get("choice_id"))
        if chosen is None:
            raise SystemExit(f"ANSWER_CHOICE_NOT_IN_PACKAGE: {question.get('id')}")
        for field in ("label", "detail"):
            if answer.get(field) != chosen.get(field):
                raise SystemExit(f"ANSWER_CHOICE_TEXT_REWRITTEN: {question.get('id')}: {field}")
    if counts.get("options") != option_total:
        raise SystemExit("ANSWERED_PACKAGE_INVALID: option count")

    recorded_digest = chat_canon_binding.recorded_answer_digest(
        {qid: str(answer["choice_id"]) for qid, answer in answers.items()}
    )
    if existing.get("answer_sha256") != recorded_digest:
        raise SystemExit("ANSWER_HASH_MISMATCH: recorded answers do not produce the stored hash")

    if not DCR_MD.exists():
        raise SystemExit("ANSWERED_PACKAGE_INVALID: Markdown record missing")


def _guard_answered_package(*, check: bool) -> bool:
    """Protect an answered package from an unanswered rebuild."""

    if not DCR_JSON.exists():
        return False
    try:
        status = read_json(DCR_JSON).get("status")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ANSWERED_PACKAGE_INVALID: {DCR_JSON}: {exc}") from exc
    if status != "ANSWERED":
        return False
    _validate_answered_package()
    if check:
        print("answered package preserved; answer-time audit and Git history are valid")
        return True
    raise SystemExit(
        "ALREADY_ANSWERED: refusing to overwrite the Owner-answer package; "
        "use the answer-time resolver or an explicit successor workflow"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if _guard_answered_package(check=args.check):
        return
    audit = build_audit()
    audit_text = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    audit_md = audit_markdown(audit)
    dcr = build_dcr(audit)
    dcr_text = json.dumps(dcr, ensure_ascii=False, indent=2) + "\n"
    write_or_check(AUDIT_JSON, audit_text, args.check)
    write_or_check(AUDIT_MD, audit_md, args.check)
    write_or_check(DCR_JSON, dcr_text, args.check)
    write_or_check(DCR_MD, dcr_markdown(dcr), args.check)
    print(f"audit={AUDIT_JSON.relative_to(ROOT)}")
    print(f"dcr={DCR_JSON.relative_to(ROOT)}")
    print(f"questions={len(QUESTIONS)} unanswered={len(QUESTIONS)}")


if __name__ == "__main__":
    main()
