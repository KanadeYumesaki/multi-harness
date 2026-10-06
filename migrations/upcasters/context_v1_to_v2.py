#!/usr/bin/env python3
"""Context/Token 系 Core Schema の `1.0.0` → `2.0.0` Upcaster（§15.2）。

## 何を「移行できる」と言えるか

`2.0.0` は Required Field を増やした Major 変更である（Owner Decision D-1a）。
足りない Field の多くは **「そのRecordを作った時に実際に何を検証したか」** であり、
`1.0.0` の中身から後から導出できない。

* `input_read_capability_set_hash` … どのCapabilityで読んだか
* `input_read_evidence_hash` … 何をScanしたか
* `token_profile_snapshot_hash` / `token_budget_policy_hash` … どのProfile／Policyで測ったか
* `excluded_fragments[].reason` … なぜ落ちたか

**これらを推測・空値・ゼロ値で埋めない**（不変条件#9「判定不能は停止する」）。
埋めた瞬間、そのRecordは「検証していないのに検証したことになっている」偽の証跡になる。
読めない旧Recordが残るほうが、偽のHashより安全である。

## §15.2 が Upcaster へ要求する3項目

> Upcasterは`from_version`、`to_version`、Code Hash、変換理由、Lossless可否を持つ。

`upcaster_code_hash`（このFile自身のHash）、`conversion_reason`、`lossless` を
結果へ載せる。**`MIGRATED` は Lossless を意味しない。** 両者は別の Field であり、
成功したことと欠落なく運べたことを混同しない。

## 落とすFieldは3分類し、既定は拒否である

`1.0.0` にあって `2.0.0` に行き先が無いFieldがある。黙って捨てると `MIGRATED` が
「元Recordの内容を全て運んだ」という持っていない意味を主張する。

| 分類 | 扱い |
|---|---|
| 監査・証跡Field（`_AUDIT_EVIDENCE_FIELDS`） | **`UNMIGRATABLE_LEGACY`**。落とせば示せない |
| 無害Field（`_BENIGN_DROPPABLE`） | `MIGRATED` かつ `lossless=false` |
| どちらでもない | **`UNMIGRATABLE_LEGACY`**。分類できないものを捨てない |

現行の対象Schemaでは `_BENIGN_DROPPABLE` は空である。落ちうるFieldは
`retrieved_at`（いつ測ったか）、`masking_receipt_ids`（何をマスクしたか）、
`bundle_id`（どのBundleの受領証か）、`compressed_fragment_ids`（何を圧縮したか）で、
いずれも監査・証跡Fieldである。したがって現状 `MIGRATED` は常に `lossless=true` になる。
それでも両者を別Fieldに保つのは、Field集合が変わったときに黙って意味が
すり替わらないようにするためである。

**この方針は §15.2 より厳しい。** §15.2 は Lossless 可否の**記録**を求めるが、
監査Fieldの欠落を禁止してはいない。厳格化の根拠と設計反映の要否は
`docs/decision/MIGRATION-LOSS-POLICY.md` に記録した。設計書は無断で変更していない。

## Supplement は「不足Fieldの補完専用」である

`supplement` は `1.0.0` から導出できないFieldの外部提供値であり、
呼出側が別の権威ある出所から与える。**Upcasterはここに無い値を作らない。**

| 状況 | 扱い |
|---|---|
| 変換結果に無いFieldを埋める | 許可 |
| 変換結果と**同値**を渡す | 許可（冪等） |
| 変換結果と**異なる値**を渡す | `SUPPLEMENT_CONFLICT` |
| `schema_name`／`schema_version`／Envelopeを渡す | `SUPPLEMENT_FIELD_NOT_PERMITTED` |

同値を許すのは、呼出側が移行台帳から全Fieldをまとめて渡す運用を壊さないためである。
異なる値を許さないのは、それが「元Recordの書換え」だからである。Upcasterは
`1.0.0` の内容を translate する道具であって、内容を差し替える道具ではない。

## 元Recordは変更しない

§15.2「Upcasterは元Recordを保持し、Projection時だけ新形式へ変換」に従う。
入力`dict`を破壊的に変更せず、常に新しい`dict`を返す。

## この語彙はMigration Tool専用である

`status` / `reason_code` は `migration-result.schema.json` 内の `enum` で閉じている。
Ledger Event、`design-source/registries/errors.yaml`、Runtime GO Evidence へは
追加しない（Owner Decision D-Q6）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator, SchemaError

__all__ = [
    "SUPPORTED_SCHEMAS",
    "FieldLossClass",
    "MigrationResult",
    "classify_dropped_field",
    "upcast_record",
    "upcaster_code_hash",
]

FROM_VERSION: Final[str] = "1.0.0"
TO_VERSION: Final[str] = "2.0.0"

SCHEMA_ID_PREFIX: Final[str] = "https://fde-harness.local/schemas/core/"

CONVERSION_REASON: Final[str] = (
    "Owner Decision D-1a: Context/Token系5 SchemaへRequired Fieldを追加したMajor変更。"
    "1.0.0 Recordを 2.0.0 の形へ Projection する。"
)

# `1.0.0` から機械的に写せる対応。値の意味が同じものだけを並べる。
_RENAMES: Final[dict[str, dict[str, str]]] = {
    "ContextBundle": {
        "bundle_id": "context_bundle_id",
        "bundle_hash": "bundle_hash",
        "total_token_count": "total_estimated_tokens",
        "message_role_manifest_hash": "message_role_manifest_hash",
    },
    "ContextSelectionReceipt": {
        "receipt_id": "selection_receipt_id",
        # 同名・同意味。写せるものを `missing` に載せると移行不能の理由が嘘になる。
        "selected_fragment_ids": "selected_fragment_ids",
        "rejected_input_resources": "rejected_input_resources",
    },
    "ContextFragment": {
        "fragment_id": "fragment_id",
        "masking_receipt_id": "masking_receipt_id",
        "message_role": "message_role",
        "control_authority": "control_authority",
        "instruction_eligible": "instruction_eligible",
        "fragment_content_hash": "content_artifact_hash",
        "token_count": "estimated_tokens",
        "input_read_capability_id": "input_read_capability_id",
        "source_file_identity": "source_file_identity",
        "classification_scan_evidence_hash": "classification_scan_evidence_hash",
    },
    "TokenProfileSnapshot": {
        "snapshot_id": "token_profile_id",
        "snapshot_hash": "snapshot_hash",
        "provider": "provider_id",
        "model": "model_id",
        "tokenizer_name": "tokenizer_name",
        "tokenizer_version": "tokenizer_version",
        "vocabulary_hash": "vocabulary_hash",
        "context_limit": "context_limit",
        "maximum_output_limit": "maximum_output_limit",
        "estimate_assurance": "estimate_assurance",
        "expires_at": "expires_at",
    },
    "TokenBudgetPolicy": {
        "policy_id": "policy_id",
        "policy_hash": "policy_hash",
        "reserved_output_tokens": "reserved_output_tokens",
        "reserved_tool_tokens": "reserved_tool_tokens",
        "compression_max_depth": "compression_depth_limit",
    },
}

# `1.0.0` の `fragment_ids` を `2.0.0` の複数List Fieldへ写す。
_LIST_FANOUT: Final[dict[str, dict[str, str]]] = {
    "ContextBundle": {
        "fragment_ids": "selected_fragment_ids",
    },
}

SUPPORTED_SCHEMAS: Final[tuple[str, ...]] = tuple(sorted(_RENAMES))

# Envelope。`1.0.0`／`2.0.0` 共通であり、そのまま持ち越す。
_ENVELOPE: Final[tuple[str, ...]] = (
    "record_id",
    "created_at",
    "producer",
    "content_hash",
    "run_id",
    "correlation_id",
    "causation_id",
    "sequence_number",
)

# Upcasterが決める。Supplementで差し替えさせない。
_IDENTITY: Final[tuple[str, ...]] = ("schema_name", "schema_version")

# 落とせば後から何も示せなくなるField。落とすくらいなら移行しない。
_AUDIT_EVIDENCE_FIELDS: Final[dict[str, frozenset[str]]] = {
    "ContextBundle": frozenset(),
    "ContextSelectionReceipt": frozenset(
        {
            # どのBundleに対する受領証か。切れると受領証が宙に浮く。
            "bundle_id",
            # 何を除外したか。2.0.0 は理由付きの `excluded_fragments` を要求する。
            "excluded_fragment_ids",
            # 何を圧縮したか。
            "compressed_fragment_ids",
            # 何をマスクしたか（ADR-007）。落とすとマスクの事実を示せない。
            "masking_receipt_ids",
        }
    ),
    "ContextFragment": frozenset(),
    # いつ測ったか。`expires_at` だけでは鮮度判定の根拠が片方欠ける。
    "TokenProfileSnapshot": frozenset({"retrieved_at"}),
    "TokenBudgetPolicy": frozenset(),
}

# 落としてもよいField。現行は空である。空であること自体が現在の判断である。
_BENIGN_DROPPABLE: Final[dict[str, frozenset[str]]] = {name: frozenset() for name in _RENAMES}


class FieldLossClass:
    """落とすFieldの分類。"""

    AUDIT: Final[str] = "AUDIT"
    BENIGN: Final[str] = "BENIGN"
    UNKNOWN: Final[str] = "UNKNOWN"


class MigrationResult(dict[str, Any]):
    """`migration-result.schema.json` に適合する結果。

    `dict` を継承するのは、そのまま `json.dumps` してSchema検証へ渡せるようにするためである。
    """

    @property
    def migrated(self) -> bool:
        return bool(self["status"] == "MIGRATED")


def upcaster_code_hash() -> str:
    """このUpcaster自身のCode Hash（§15.2）。

    どのCodeで変換したかを結果へ束縛する。Code が変われば値が変わる。
    """
    return "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def classify_dropped_field(schema_name: str, field: str) -> str:
    """落とすFieldを分類する。**分類できないものは `UNKNOWN` で拒否側に倒す。**

    未知Fieldを黙って捨てる経路を作らない。旧Recordに想定外のFieldが
    入っていた場合、それが何であったかは本Moduleには判断できない（不変条件#9）。
    """
    if field in _AUDIT_EVIDENCE_FIELDS.get(schema_name, frozenset()):
        return FieldLossClass.AUDIT
    if field in _BENIGN_DROPPABLE.get(schema_name, frozenset()):
        return FieldLossClass.BENIGN
    return FieldLossClass.UNKNOWN


def _source_hash(record: dict[str, Any]) -> str:
    """元Recordの同一性。**元Recordを変更せず**Bytesから測る。"""
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _result(
    *,
    status: str,
    schema_name: str,
    source: dict[str, Any],
    missing: list[str] | None = None,
    dropped: list[str] | None = None,
    conflicts: list[str] | None = None,
    validation_errors: list[str] | None = None,
    reason_code: str | None,
    output: dict[str, Any] | None,
    lossless: bool | None = None,
) -> MigrationResult:
    return MigrationResult(
        status=status,
        from_version=FROM_VERSION,
        to_version=TO_VERSION,
        schema_name=schema_name,
        source_record_hash=_source_hash(source),
        upcaster_code_hash=upcaster_code_hash(),
        conversion_reason=CONVERSION_REASON,
        missing_fields=sorted(missing or []),
        dropped_fields=sorted(dropped or []),
        conflicting_fields=sorted(conflicts or []),
        validation_errors=sorted(validation_errors or []),
        reason_code=reason_code,
        lossless=lossless,
        output=output,
    )


def _unmigratable(
    schema_name: str, record: dict[str, Any], reason_code: str, **detail: Any
) -> MigrationResult:
    return _result(
        status="UNMIGRATABLE_LEGACY",
        schema_name=schema_name,
        source=record,
        reason_code=reason_code,
        output=None,
        lossless=None,
        **detail,
    )


def _target_schema_problem(schema_name: str, target_schema: object) -> str | None:
    """Target Schemaが「その名前の 2.0.0」であることを確かめる。

    別Schemaや別Versionの定義を渡されたまま変換すると、`required` も
    `properties` も違う相手に対して検証したことになる。**通った検証の対象が
    何だったのか分からない状態を作らない。**
    """
    if not isinstance(target_schema, dict):
        return "TARGET_SCHEMA_INVALID"
    properties = target_schema.get("properties")
    if not isinstance(properties, dict) or not isinstance(target_schema.get("required"), list):
        return "TARGET_SCHEMA_INVALID"
    expected_id = f"{SCHEMA_ID_PREFIX}{schema_name}/{TO_VERSION}.schema.json"
    if target_schema.get("$id") != expected_id:
        return "TARGET_SCHEMA_INVALID"
    name_const = properties.get("schema_name", {})
    version_const = properties.get("schema_version", {})
    if not isinstance(name_const, dict) or name_const.get("const") != schema_name:
        return "TARGET_SCHEMA_INVALID"
    if not isinstance(version_const, dict) or version_const.get("const") != TO_VERSION:
        return "TARGET_SCHEMA_INVALID"
    try:
        Draft202012Validator.check_schema(target_schema)
    except SchemaError:
        return "TARGET_SCHEMA_INVALID"
    return None


def _source_record_problem(schema_name: str, record: object) -> str | None:
    """元Recordが「その名前の 1.0.0」であることを確かめる。"""
    if not isinstance(record, dict):
        return "UNSUPPORTED_SOURCE_VERSION"
    if record.get("schema_name") != schema_name:
        # 引数のSchema名とRecord内のSchema名が食い違う。どちらが正か判定できない。
        return "SOURCE_SCHEMA_NAME_MISMATCH"
    if record.get("schema_version") != FROM_VERSION:
        # 未知Major（3.0.0 等）や 2.0.0 をここへ入れない。
        return "UNSUPPORTED_SOURCE_VERSION"
    return None


def _validation_errors(output: dict[str, Any], target_schema: dict[str, Any]) -> list[str]:
    """`2.0.0` Schema全体で検証し、**Pathと違反Keywordだけ**を返す。

    jsonschema の既定メッセージは違反した実値を埋め込む。Migration結果は
    Evidenceとして残るため、値をそのまま載せない（不変条件#7）。
    どのPathがどの規則で落ちたかは、値を出さなくても示せる。
    """
    validator = Draft202012Validator(target_schema)
    errors = []
    for error in validator.iter_errors(output):
        path = "$" + "".join(f"[{part!r}]" for part in error.absolute_path)
        errors.append(f"{path}: {error.validator}")
    return errors


def upcast_record(
    schema_name: str,
    record: dict[str, Any],
    target_schema: dict[str, Any],
    *,
    supplement: dict[str, Any] | None = None,
) -> MigrationResult:
    """`1.0.0` Recordを `2.0.0` へ変換する。変換できなければ移行不能を返す。

    `target_schema` は**生成済みの `2.0.0` Schema本体**を渡す。`required` の件数も
    許可Field名もそこから読む。ここへ手入力しない（不変条件#18）。

    判定順は「後から取り返せない拒否理由」を先に置く。Supplementをいくら足しても
    覆らない拒否（対象外Schema、Target不正、Source不正、監査Field欠落）を先に返す。
    """
    if schema_name not in _RENAMES:
        return _unmigratable(schema_name, record, "UNSUPPORTED_SOURCE_VERSION")

    problem = _target_schema_problem(schema_name, target_schema)
    if problem is not None:
        return _unmigratable(schema_name, record, problem)

    problem = _source_record_problem(schema_name, record)
    if problem is not None:
        return _unmigratable(schema_name, record, problem)

    # --- 落とすFieldの分類。Supplementでは覆らない ---------------------------
    carried = set(_RENAMES[schema_name]) | set(_LIST_FANOUT.get(schema_name, {}))
    carried |= set(_ENVELOPE) | set(_IDENTITY)
    dropped = sorted(field for field in record if field not in carried)
    audit_loss = [
        f for f in dropped if classify_dropped_field(schema_name, f) == FieldLossClass.AUDIT
    ]
    unknown_loss = [
        f for f in dropped if classify_dropped_field(schema_name, f) == FieldLossClass.UNKNOWN
    ]
    if audit_loss:
        return _unmigratable(schema_name, record, "AUDIT_FIELD_NOT_TRANSFERABLE", dropped=dropped)
    if unknown_loss:
        return _unmigratable(schema_name, record, "UNCLASSIFIED_FIELD_LOSS", dropped=dropped)

    # --- 機械的に写せるものを写す -------------------------------------------
    output: dict[str, Any] = {"schema_name": schema_name, "schema_version": TO_VERSION}
    for field in _ENVELOPE:
        if field in record:
            output[field] = record[field]
    for old, new in _RENAMES[schema_name].items():
        if old in record:
            output[new] = record[old]
    for old, new in _LIST_FANOUT.get(schema_name, {}).items():
        if old in record:
            output[new] = list(record[old])
    # `ContextBundle` の採用ID列は `1.0.0` でも順序付きList1本しか無い。
    # 「選択集合」と「並び」を同じ値で写すのは意味の推測ではなく同一性の保持である。
    if schema_name == "ContextBundle" and "selected_fragment_ids" in output:
        output.setdefault("ordered_fragment_ids", list(output["selected_fragment_ids"]))

    # --- Supplement は不足Fieldの補完専用 -----------------------------------
    supplement = dict(supplement or {})
    forbidden = sorted(set(supplement) & (set(_IDENTITY) | set(_ENVELOPE)))
    if forbidden:
        return _unmigratable(
            schema_name,
            record,
            "SUPPLEMENT_FIELD_NOT_PERMITTED",
            dropped=dropped,
            conflicts=forbidden,
        )
    conflicts = sorted(
        field for field, value in supplement.items() if field in output and output[field] != value
    )
    if conflicts:
        return _unmigratable(
            schema_name, record, "SUPPLEMENT_CONFLICT", dropped=dropped, conflicts=conflicts
        )
    for field, value in supplement.items():
        output[field] = value

    # --- Required の充足 -----------------------------------------------------
    missing = [field for field in target_schema["required"] if field not in output]
    if missing:
        return _unmigratable(
            schema_name,
            record,
            "REQUIRED_FIELD_NOT_RECONSTRUCTABLE",
            missing=missing,
            dropped=dropped,
        )

    # --- `2.0.0` Schema全体での検証 -----------------------------------------
    # Required だけでなく type／enum／const／pattern／入れ子Object／
    # additionalProperties まで通す。ここを抜くと、Schemaを満たさないRecordを
    # 「移行済み」と記録することになる。
    errors = _validation_errors(output, target_schema)
    if errors:
        return _unmigratable(
            schema_name,
            record,
            "SCHEMA_VALIDATION_FAILED",
            dropped=dropped,
            validation_errors=errors,
        )

    return _result(
        status="MIGRATED",
        schema_name=schema_name,
        source=record,
        dropped=dropped,
        reason_code=None,
        output=output,
        # 落としたFieldが1つも無いときだけ Lossless である。
        # `MIGRATED` であることと混同しない（§15.2）。
        lossless=not dropped,
    )
