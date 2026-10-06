"""Gate Corpus：Schema Version移行（§15.2、Owner Decision D-1a／D-Q6）。

§15.2 は「Gate Corpusに旧Version読込み、Upcast、未知Major拒否、Hash差異試験を含める」
と定める。本Fileがその実体である。

守っている核は1つ。**判定できないものを判定できたことにしない。**
`1.0.0` から `2.0.0` の Required Field を作れないとき、Upcaster は空値やゼロ値で
埋めずに `UNMIGRATABLE_LEGACY` を返す。埋めた瞬間、そのRecordは
「検証していないのに検証したことになっている」偽の証跡になる。

欠落方針（監査Field欠落は移行不能）の根拠は Owner Decision（M-Q1〜M-Q4）である。
決裁記録は非公開側に保持し、記録の在否は `tests/private_history/` が確かめる。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import types
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from build_core_schemas import build_schema
from harness.domain.hashing import ContentHash
from harness.domain.schema_set import SchemaRef, compute_schema_set_hash
from schema_catalog import (
    compute_schema_catalog_hash,
    normalize_core_schemas,
    schema_catalog_entries,
)

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPO_ROOT / "schemas" / "core"
UPCASTERS = REPO_ROOT / "migrations" / "upcasters"
REGISTRIES = REPO_ROOT / "design-source" / "registries"

CONTEXT_TOKEN_SCHEMAS = (
    "ContextBundle",
    "ContextSelectionReceipt",
    "ContextFragment",
    "TokenProfileSnapshot",
    "TokenBudgetPolicy",
)


def _load(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UPCASTER = _load(UPCASTERS / "context_v1_to_v2.py", "_context_upcaster")
RESULT_VALIDATOR = Draft202012Validator(
    json.loads((UPCASTERS / "migration-result.schema.json").read_text(encoding="utf-8"))
)


def _schema(name: str, version: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / name / f"{version}.schema.json").read_text(encoding="utf-8"))


def _envelope(schema_name: str, version: str) -> dict[str, Any]:
    return {
        "schema_name": schema_name,
        "schema_version": version,
        "record_id": "018f0000-0000-7000-8000-000000000001",
        "created_at": "2026-08-16T00:00:00Z",
        "producer": "harness/test",
        "content_hash": "sha256:" + "a" * 64,
    }


def _bundle_v1() -> dict[str, Any]:
    return {
        **_envelope("ContextBundle", "1.0.0"),
        "bundle_id": "bundle-1",
        "bundle_hash": "sha256:" + "b" * 64,
        "fragment_ids": ["f1", "f2"],
        "total_token_count": 42,
        "message_role_manifest_hash": "sha256:" + "c" * 64,
    }


# `1.0.0` からも機械変換からも導出できないField。呼出側が別の権威ある出所から与える。
BUNDLE_SUPPLEMENT: dict[str, Any] = {
    "selection_receipt_id": "receipt-1",
    "excluded_fragment_ids": [],
    "compression_artifact_ids": [],
    "token_profile_snapshot_hash": "sha256:" + "d" * 64,
    "token_budget_policy_hash": "sha256:" + "e" * 64,
    "input_read_capability_set_hash": "sha256:" + "f" * 64,
    "input_read_evidence_hash": "sha256:" + "1" * 64,
}


def _upcast(
    name: str,
    record: dict[str, Any],
    supplement: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upcastし、結果が必ず `migration-result.schema.json` に適合することを確かめる。

    どの試験でも結果Schemaを検査する。移行不能側だけ形が崩れる事故を防ぐ。
    """
    result = UPCASTER.upcast_record(
        name,
        record,
        target if target is not None else _schema(name, "2.0.0"),
        supplement=supplement,
    )
    RESULT_VALIDATOR.validate(result)
    return dict(result)


# --------------------------------------------------------------------------
# 旧Version読込み（§15.2 Readerは対応Major Versionを明示）
# --------------------------------------------------------------------------


def test_one_point_zero_records_still_validate() -> None:
    """`1.0.0` Recordは今も読める。旧Readerを壊していない。"""
    Draft202012Validator(_schema("ContextBundle", "1.0.0")).validate(_bundle_v1())


def test_two_point_zero_is_registered_and_distinct_from_one_point_zero() -> None:
    """`2.0.0` が生成され、`1.0.0` とは別の識別子・別のRequired集合を持つ。"""
    for name in CONTEXT_TOKEN_SCHEMAS:
        v1, v2 = _schema(name, "1.0.0"), _schema(name, "2.0.0")
        assert v1["$id"] != v2["$id"], name
        assert v2["properties"]["schema_version"]["const"] == "2.0.0"
        # Required Fieldの追加がMajor変更の理由である（§15.2）。
        assert set(v2["required"]) - set(v1["required"]), f"{name}: Required追加が無い"


def test_one_point_zero_schema_is_not_overwritten_by_two_point_zero() -> None:
    """`1.0.0` が `2.0.0` の定義で上書きされていないこと。

    改修前は定義Tableが `schema_name` だけをKeyにしており、`2.0.0` を足すと
    `1.0.0` も新定義で再生成された。旧Recordを読むReaderとUpcasterの土台が
    静かに消える。ここが最も踏みやすい事故である。
    """
    for name in CONTEXT_TOKEN_SCHEMAS:
        committed = _schema(name, "1.0.0")
        assert build_schema(name, "1.0.0") == committed, f"{name}@1.0.0 が上書きされている"


def test_undefined_version_fails_closed_instead_of_falling_back_by_name() -> None:
    """未定義Versionを名前だけで解決しないこと。"""
    with pytest.raises(KeyError):
        build_schema("ContextBundle", "3.0.0")


# --------------------------------------------------------------------------
# §15.2 が Upcaster へ要求する Field
# --------------------------------------------------------------------------


def test_result_carries_the_fields_section_15_2_requires() -> None:
    """`from_version`／`to_version`／Code Hash／変換理由／Lossless可否を持つこと。"""
    result = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
    assert result["from_version"] == "1.0.0"
    assert result["to_version"] == "2.0.0"
    assert result["upcaster_code_hash"].startswith("sha256:")
    assert result["conversion_reason"]
    assert isinstance(result["lossless"], bool)


def test_code_hash_matches_the_upcaster_source() -> None:
    """Code Hash は Upcaster 自身のBytesから測ること。"""
    import hashlib

    expected = (
        "sha256:" + hashlib.sha256((UPCASTERS / "context_v1_to_v2.py").read_bytes()).hexdigest()
    )
    assert UPCASTER.upcaster_code_hash() == expected
    assert _upcast("ContextBundle", _bundle_v1())["upcaster_code_hash"] == expected


def test_unmigratable_declares_lossless_as_null_not_false() -> None:
    """移行不能のとき Lossless 可否は「不明」であって「false」ではない。

    変換していないものについて欠落の有無を述べない。
    """
    result = _upcast("ContextBundle", _bundle_v1())
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["lossless"] is None


# --------------------------------------------------------------------------
# Target Schema の検証
# --------------------------------------------------------------------------


def test_target_schema_of_another_name_is_rejected() -> None:
    """別Schemaの定義を渡して変換しないこと。

    通ってしまうと、`required` も `properties` も違う相手に対して検証したことになり、
    「通った検証の対象が何だったのか」が分からなくなる。
    """
    result = _upcast(
        "ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT, target=_schema("ContextFragment", "2.0.0")
    )
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "TARGET_SCHEMA_INVALID"
    assert result["output"] is None


def test_target_schema_of_version_one_is_rejected() -> None:
    """Target に `1.0.0` を渡して「移行済み」と言わせないこと。"""
    result = _upcast(
        "ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT, target=_schema("ContextBundle", "1.0.0")
    )
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "TARGET_SCHEMA_INVALID"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: s.pop("$id"), id="no_id"),
        pytest.param(lambda s: s.update({"$id": "https://evil.example/x.json"}), id="wrong_id"),
        pytest.param(
            lambda s: s["properties"]["schema_version"].update({"const": "3.0.0"}),
            id="version_const_not_2",
        ),
        pytest.param(
            lambda s: s["properties"]["schema_name"].update({"const": "Other"}),
            id="name_const_mismatch",
        ),
        pytest.param(lambda s: s.pop("required"), id="no_required"),
        pytest.param(
            lambda s: s["properties"].update({"bundle_hash": {"type": "not-a-type"}}),
            id="not_a_valid_schema",
        ),
    ],
)
def test_malformed_target_schema_is_rejected(mutate: Any) -> None:
    """`$id`・`const`・`required` が期待どおりでないTargetを拒否すること。"""
    target = _schema("ContextBundle", "2.0.0")
    mutate(target)
    result = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT, target=target)
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "TARGET_SCHEMA_INVALID"


def test_non_object_target_is_rejected() -> None:
    result = UPCASTER.upcast_record("ContextBundle", _bundle_v1(), "not-a-schema")
    RESULT_VALIDATOR.validate(result)
    assert result["reason_code"] == "TARGET_SCHEMA_INVALID"


# --------------------------------------------------------------------------
# Source Record の検証
# --------------------------------------------------------------------------


def test_source_schema_name_mismatch_is_rejected() -> None:
    """引数のSchema名とRecord内の`schema_name`が食い違うRecordを拒否すること。

    どちらが正しいか判定できない。片方を黙って採ると、別Schemaの
    Recordを取り違えて変換する経路になる。
    """
    record = {**_bundle_v1(), "schema_name": "ContextFragment"}
    result = _upcast("ContextBundle", record)
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "SOURCE_SCHEMA_NAME_MISMATCH"
    assert result["output"] is None


def test_unknown_major_version_is_rejected() -> None:
    """未知Major（`3.0.0`）を受け付けない。"""
    result = _upcast("ContextBundle", {**_bundle_v1(), "schema_version": "3.0.0"})
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "UNSUPPORTED_SOURCE_VERSION"
    assert result["output"] is None


def test_already_migrated_record_is_not_upcast_twice() -> None:
    """`2.0.0` Recordを再度Upcastしない。"""
    result = _upcast("ContextBundle", {**_bundle_v1(), "schema_version": "2.0.0"})
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "UNSUPPORTED_SOURCE_VERSION"


def test_missing_schema_version_is_rejected() -> None:
    """`schema_version` の無いRecordを `1.0.0` とみなさないこと。"""
    record = _bundle_v1()
    del record["schema_version"]
    result = _upcast("ContextBundle", record)
    assert result["reason_code"] == "UNSUPPORTED_SOURCE_VERSION"


def test_unsupported_schema_is_rejected() -> None:
    """対象外Schema（D-01の範囲外）を黙って通さない。"""
    result = UPCASTER.upcast_record(
        "ExecutionPlan",
        _envelope("ExecutionPlan", "1.0.0"),
        _schema("ExecutionPlan", "1.0.0"),
    )
    RESULT_VALIDATOR.validate(result)
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "UNSUPPORTED_SOURCE_VERSION"


# --------------------------------------------------------------------------
# Supplement は不足Fieldの補完専用
# --------------------------------------------------------------------------


def test_supplement_may_repeat_an_identical_value() -> None:
    """変換結果と同値の補足は許す（冪等）。

    呼出側が移行台帳から全Fieldをまとめて渡す運用を壊さない。
    """
    result = _upcast(
        "ContextBundle",
        _bundle_v1(),
        {**BUNDLE_SUPPLEMENT, "bundle_hash": "sha256:" + "b" * 64},
    )
    assert result["status"] == "MIGRATED", result["reason_code"]
    assert result["conflicting_fields"] == []


def test_supplement_with_a_different_value_is_rejected() -> None:
    """変換結果と異なる値の補足を拒否すること。

    それは補完ではなく**元Recordの書換え**である。Upcasterは `1.0.0` の内容を
    translate する道具であって、内容を差し替える道具ではない。
    """
    result = _upcast(
        "ContextBundle",
        _bundle_v1(),
        {**BUNDLE_SUPPLEMENT, "bundle_hash": "sha256:" + "9" * 64},
    )
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "SUPPLEMENT_CONFLICT"
    assert result["conflicting_fields"] == ["bundle_hash"]
    assert result["output"] is None


def test_supplement_conflict_lists_every_conflicting_field() -> None:
    """競合Fieldを1件だけ挙げて残りを隠さないこと。"""
    result = _upcast(
        "ContextBundle",
        _bundle_v1(),
        {
            **BUNDLE_SUPPLEMENT,
            "bundle_hash": "sha256:" + "9" * 64,
            "total_estimated_tokens": 999,
        },
    )
    assert result["conflicting_fields"] == ["bundle_hash", "total_estimated_tokens"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_name", "Other"),
        ("schema_version", "1.0.0"),
        ("record_id", "018f0000-0000-7000-8000-00000000ffff"),
        ("created_at", "1999-01-01T00:00:00Z"),
        ("producer", "spoofed"),
        ("content_hash", "sha256:" + "0" * 64),
    ],
)
def test_supplement_cannot_overwrite_identity_or_envelope(field: str, value: Any) -> None:
    """`schema_name`／`schema_version`／Envelopeの上書きを禁じること。

    ここを通すと、別Schema・別Versionを名乗るRecordや、作成者・作成時刻を
    差し替えたRecordを「移行済み」として作れてしまう。
    """
    result = _upcast("ContextBundle", _bundle_v1(), {**BUNDLE_SUPPLEMENT, field: value})
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "SUPPLEMENT_FIELD_NOT_PERMITTED"
    assert result["conflicting_fields"] == [field]
    assert result["output"] is None


def test_supplement_identical_envelope_value_is_still_rejected() -> None:
    """Envelopeは同値でも受け取らないこと。

    「同じ値なら無害」を認めると、Envelopeを補足経路へ載せる運用が生まれる。
    値の一致は毎回の偶然であり、規則にならない。
    """
    result = _upcast(
        "ContextBundle", _bundle_v1(), {**BUNDLE_SUPPLEMENT, "producer": "harness/test"}
    )
    assert result["reason_code"] == "SUPPLEMENT_FIELD_NOT_PERMITTED"


# --------------------------------------------------------------------------
# `2.0.0` Schema 全体での検証
# --------------------------------------------------------------------------


def test_supplemented_record_migrates_and_validates_against_two_point_zero() -> None:
    """補足が全て揃えば `MIGRATED`。出力は `2.0.0` Schemaを満たす。"""
    result = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
    assert result["status"] == "MIGRATED", result["missing_fields"]
    assert result["missing_fields"] == []
    assert result["reason_code"] is None
    Draft202012Validator(_schema("ContextBundle", "2.0.0")).validate(result["output"])


def test_incomplete_record_is_unmigratable_and_lists_every_missing_field() -> None:
    """補足なしの `1.0.0` は移行不能。欠落Fieldを全部挙げる。"""
    result = _upcast("ContextBundle", _bundle_v1())
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "REQUIRED_FIELD_NOT_RECONSTRUCTABLE"
    assert result["output"] is None
    assert set(result["missing_fields"]) == set(BUNDLE_SUPPLEMENT)


def test_partial_supplement_still_fails_closed() -> None:
    """補足が1つでも欠ければ移行不能。部分的に埋めて通さない。"""
    short = {k: v for k, v in BUNDLE_SUPPLEMENT.items() if k != "input_read_evidence_hash"}
    result = _upcast("ContextBundle", _bundle_v1(), short)
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["missing_fields"] == ["input_read_evidence_hash"]


def test_wrong_type_is_rejected_even_when_required_is_satisfied() -> None:
    """Requiredを満たしていてもTypeが違えば通さない。"""
    result = _upcast(
        "ContextBundle", {**_bundle_v1(), "total_token_count": "forty-two"}, BUNDLE_SUPPLEMENT
    )
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "SCHEMA_VALIDATION_FAILED"
    assert "$['total_estimated_tokens']: type" in result["validation_errors"]


def test_pattern_violation_is_rejected() -> None:
    """`sha256:` Patternに合わないHashを通さない。"""
    result = _upcast(
        "ContextBundle",
        {**_bundle_v1(), "bundle_hash": "not-a-hash"},
        BUNDLE_SUPPLEMENT,
    )
    assert result["reason_code"] == "SCHEMA_VALIDATION_FAILED"
    assert "$['bundle_hash']: pattern" in result["validation_errors"]


def _fragment_v1() -> dict[str, Any]:
    return {
        **_envelope("ContextFragment", "1.0.0"),
        "fragment_id": "f1",
        "message_role": "SYSTEM_CONTROL",
        "control_authority": True,
        "instruction_eligible": True,
        "fragment_content_hash": "sha256:" + "b" * 64,
        "token_count": 1,
        "input_read_capability_id": "cap-1",
        "source_file_identity": "file-1",
        "classification_scan_evidence_hash": "sha256:" + "d" * 64,
    }


def _fragment_supplement() -> dict[str, Any]:
    return {
        "source_artifact_hash": "sha256:" + "c" * 64,
        "source_span": {"start": 0, "end": 1},
        "fragment_type": "SYSTEM_POLICY",
        "classification_labels": [],
        "trust_level": "TRUSTED_CONTROL",
        "priority": 1,
        "mandatory": True,
        "freshness": "2026-08-16T00:00:00Z",
        # `deduplication_key` は自由文字列ではなくHashである。
        "deduplication_key": "sha256:" + "7" * 64,
    }


def _profile_v1_without_retrieved_at() -> dict[str, Any]:
    """`retrieved_at` を持たない `TokenProfileSnapshot`。

    実Recordでは `retrieved_at` が `1.0.0` の Required なので必ず監査Field欠落に
    なる。ここでは変換経路そのものを通すために、意図的に持たないRecordを作る。
    """
    return {
        **_envelope("TokenProfileSnapshot", "1.0.0"),
        "snapshot_id": "tp-1",
        "snapshot_hash": "sha256:" + "2" * 64,
        "provider": "anthropic",
        "model": "claude",
        "tokenizer_name": "t",
        "tokenizer_version": "1",
        "vocabulary_hash": "sha256:" + "3" * 64,
        "context_limit": 1000,
        "maximum_output_limit": 100,
        "estimate_assurance": "EXACT",
        "expires_at": "2026-08-17T00:00:00Z",
    }


def _policy_v1() -> dict[str, Any]:
    return {
        **_envelope("TokenBudgetPolicy", "1.0.0"),
        "policy_id": "p1",
        "policy_hash": "sha256:" + "8" * 64,
        "reserved_output_tokens": 100,
        "reserved_tool_tokens": 10,
        "compression_max_depth": 2,
    }


def _happy_path(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """5 Schemaそれぞれの「通る」入力。

    拒否側だけを試験すると、「何を渡しても拒否する」実装でも全部PASSしてしまう。
    Fixtureは定義順に依存しないよう関数内で組む。
    """
    if name == "ContextBundle":
        return _bundle_v1(), dict(BUNDLE_SUPPLEMENT)
    if name == "ContextSelectionReceipt":
        return _receipt_v1_without_audit_fields(), _receipt_supplement()
    if name == "ContextFragment":
        return _fragment_v1(), _fragment_supplement()
    if name == "TokenProfileSnapshot":
        return _profile_v1_without_retrieved_at(), {
            "counting_adapter_version": "harness-token-counter/2",
            "overheads": {
                "system_message_overhead": 3,
                "developer_message_overhead": 3,
                "tool_definition_overhead": 0,
                "per_message_overhead": 3,
                "structured_output_overhead": 0,
                "streaming_frame_overhead": 0,
            },
            "reserved_output_tokens": 100,
            "reserved_tool_tokens": 10,
            "retry_fallback_reservation": 5,
        }
    if name == "TokenBudgetPolicy":
        return _policy_v1(), {
            "maximum_input_tokens": 1000,
            "maximum_context_tokens": 900,
            "safety_margin_tokens": 10,
            "retry_reservation": 5,
            "overflow_policy": "FAIL_CLOSED",
            "required_fragment_types": [],
        }
    raise AssertionError(f"happy path 未定義: {name}")


@pytest.mark.parametrize("name", CONTEXT_TOKEN_SCHEMAS)
def test_every_schema_has_a_path_that_actually_migrates(name: str) -> None:
    """5 Schema全てに「通る」経路があること。

    基準が無いと、拒否側の試験が何を測っているのか分からない。
    """
    record, supplement = _happy_path(name)
    result = _upcast(name, record, supplement)
    assert result["status"] == "MIGRATED", result["validation_errors"] or result["reason_code"]
    assert result["lossless"] is True
    Draft202012Validator(_schema(name, "2.0.0")).validate(result["output"])


def test_enum_violation_is_rejected() -> None:
    """Enum外の値を通さない。

    違反を1箇所だけにして測る。基準Fixtureが他所でも落ちていると、
    「Enumを検出できた」つもりの試験が別の違反で通ってしまう。
    """
    record = {**_fragment_v1(), "message_role": "NOT_A_ROLE"}
    result = _upcast("ContextFragment", record, _fragment_supplement())
    assert result["reason_code"] == "SCHEMA_VALIDATION_FAILED"
    assert result["validation_errors"] == ["$['message_role']: enum"]


def test_nested_object_violation_is_rejected() -> None:
    """入れ子Objectの中まで検証すること。表層だけ見ない。"""
    supplement = _receipt_supplement()
    # `excluded_fragments[0]` から必須の `reason` を抜く。
    supplement["excluded_fragments"] = [{"fragment_id": "f2"}]
    result = _upcast("ContextSelectionReceipt", _receipt_v1_without_audit_fields(), supplement)
    assert result["reason_code"] == "SCHEMA_VALIDATION_FAILED"
    assert result["validation_errors"] == ["$['excluded_fragments'][0]: required"]


def test_nested_enum_violation_is_rejected() -> None:
    """入れ子Objectの Enum も検証すること。"""
    supplement = _receipt_supplement()
    supplement["excluded_fragments"] = [{"fragment_id": "f2", "reason": "NOT_A_REASON"}]
    result = _upcast("ContextSelectionReceipt", _receipt_v1_without_audit_fields(), supplement)
    assert result["validation_errors"] == ["$['excluded_fragments'][0]['reason']: enum"]


def test_nested_additional_property_is_rejected() -> None:
    """入れ子Objectの `additionalProperties: false` も効かせること。"""
    supplement = _receipt_supplement()
    supplement["excluded_fragments"] = [{"fragment_id": "f2", "reason": "BUDGET", "sneaked_in": 1}]
    result = _upcast("ContextSelectionReceipt", _receipt_v1_without_audit_fields(), supplement)
    assert result["validation_errors"] == ["$['excluded_fragments'][0]: additionalProperties"]


def test_const_violation_is_rejected() -> None:
    """`schema_version` の `const` を検証すること。

    Upcasterは `2.0.0` を自分で書き込むため通常は起きない。Targetの `const` が
    実際に効いていることを、出力を直接検証して確かめる。
    """
    result = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
    tampered = {**result["output"], "schema_version": "1.0.0"}
    errors = Draft202012Validator(_schema("ContextBundle", "2.0.0")).iter_errors(tampered)
    assert [error.validator for error in errors] == ["const"]


def test_additional_property_is_rejected() -> None:
    """`2.0.0` に無いKeyを含む補足を `MIGRATED` と呼ばないこと。"""
    result = _upcast("ContextBundle", _bundle_v1(), {**BUNDLE_SUPPLEMENT, "not_a_v2_field": "x"})
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "SCHEMA_VALIDATION_FAILED"
    assert "$: additionalProperties" in result["validation_errors"]


def test_validation_errors_do_not_leak_field_values() -> None:
    """違反した実値をEvidenceへ出さないこと（不変条件#7）。

    Migration結果は記録として残る。Pathと違反Keywordだけで十分に説明できる。
    """
    secretish = "sk-live-000000000000000000000000"
    result = _upcast("ContextBundle", {**_bundle_v1(), "bundle_hash": secretish}, BUNDLE_SUPPLEMENT)
    assert result["reason_code"] == "SCHEMA_VALIDATION_FAILED"
    serialized = json.dumps(result, ensure_ascii=False)
    assert secretish not in serialized
    assert result["validation_errors"] == ["$['bundle_hash']: pattern"]


# --------------------------------------------------------------------------
# 欠落方針（Owner Decision M-Q1〜M-Q4。決裁記録は非公開側）
# --------------------------------------------------------------------------


def _receipt_v1_without_audit_fields() -> dict[str, Any]:
    """監査Fieldを含まない `ContextSelectionReceipt`。

    実Recordでは `bundle_id` が Required なので必ず監査Field欠落になる。
    ここでは検証経路を通すために、意図的に監査Fieldの無いRecordを作る。
    """
    return {
        **_envelope("ContextSelectionReceipt", "1.0.0"),
        "receipt_id": "receipt-1",
        "selected_fragment_ids": ["f1"],
        "rejected_input_resources": [],
    }


def _receipt_supplement() -> dict[str, Any]:
    """`2.0.0` を実際に満たす補足。

    `deduplication_result` と `compression_result` は Object ではなく
    **Fragment IDのArray**である。ここを取り違えたまま試験を書くと、
    「入れ子違反を検出できた」つもりの試験が別の型違反で通ってしまう。
    """
    return {
        "decision_hash": "sha256:" + "2" * 64,
        "candidate_fragment_ids": ["f1", "f2"],
        "excluded_fragments": [{"fragment_id": "f2", "reason": "BUDGET"}],
        "deduplication_result": [],
        "compression_result": [],
        "estimated_token_total": 10,
        "token_profile_snapshot_hash": "sha256:" + "3" * 64,
        "budget_policy_hash": "sha256:" + "4" * 64,
        "input_read_capability_set_hash": "sha256:" + "5" * 64,
        "input_read_evidence_hash": "sha256:" + "6" * 64,
        "algorithm_version": "ctx-select/1",
    }


def test_receipt_without_audit_fields_migrates() -> None:
    """監査Fieldを含まないReceiptは実際に移行できること。

    拒否側だけを試験すると「何を渡しても拒否する」実装でも全部通る。
    通る経路が本当に通ることを固定する。
    """
    result = _upcast(
        "ContextSelectionReceipt", _receipt_v1_without_audit_fields(), _receipt_supplement()
    )
    assert result["status"] == "MIGRATED", result["validation_errors"] or result["reason_code"]
    assert result["lossless"] is True
    Draft202012Validator(_schema("ContextSelectionReceipt", "2.0.0")).validate(result["output"])


def test_audit_evidence_field_loss_forbids_migration() -> None:
    """監査・証跡Fieldを落とす移行を許さない。

    `masking_receipt_ids` を落とした `2.0.0` Recordは、Schema上は完全に妥当で
    `MIGRATED` と記録される。しかし後から見た人は「マスクしていなかったのか、
    記録を落としたのか」を区別できない。区別できない状態を作らない。
    """
    receipt = {
        **_receipt_v1_without_audit_fields(),
        "masking_receipt_ids": ["mr-1"],
    }
    result = _upcast("ContextSelectionReceipt", receipt, _receipt_supplement())
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "AUDIT_FIELD_NOT_TRANSFERABLE"
    assert "masking_receipt_ids" in result["dropped_fields"]
    assert result["output"] is None


def test_retrieved_at_loss_forbids_migration() -> None:
    """`TokenProfileSnapshot.retrieved_at` は落とせない。

    `2.0.0` に行き先が無く、`expires_at` だけでは鮮度判定の根拠が片方欠ける。
    """
    assert "retrieved_at" not in _schema("TokenProfileSnapshot", "2.0.0")["properties"]
    snapshot = {
        **_envelope("TokenProfileSnapshot", "1.0.0"),
        "snapshot_id": "tp-1",
        "snapshot_hash": "sha256:" + "2" * 64,
        "provider": "anthropic",
        "model": "claude",
        "tokenizer_name": "t",
        "tokenizer_version": "1",
        "vocabulary_hash": "sha256:" + "3" * 64,
        "context_limit": 1000,
        "maximum_output_limit": 100,
        "estimate_assurance": "EXACT",
        "retrieved_at": "2026-08-16T00:00:00Z",
        "expires_at": "2026-08-17T00:00:00Z",
    }
    result = _upcast("TokenProfileSnapshot", snapshot)
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "AUDIT_FIELD_NOT_TRANSFERABLE"
    assert result["dropped_fields"] == ["retrieved_at"]


def test_receipt_bundle_id_loss_forbids_migration() -> None:
    """`bundle_id` を落とすと受領証が宙に浮く。移行しない。"""
    receipt = {**_receipt_v1_without_audit_fields(), "bundle_id": "bundle-1"}
    result = _upcast("ContextSelectionReceipt", receipt, _receipt_supplement())
    assert result["reason_code"] == "AUDIT_FIELD_NOT_TRANSFERABLE"
    assert "bundle_id" in result["dropped_fields"]


def test_receipt_without_exclusion_reasons_is_unmigratable() -> None:
    """`excluded_fragment_ids` は理由を持たないため移行不能。

    `1.0.0` は「なぜ落ちたか」を保存できなかった。理由を後から作らない。
    """
    receipt = {**_receipt_v1_without_audit_fields(), "excluded_fragment_ids": ["f2"]}
    result = _upcast("ContextSelectionReceipt", receipt)
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "AUDIT_FIELD_NOT_TRANSFERABLE"
    assert "excluded_fragment_ids" in result["dropped_fields"]
    # 同名で写せるFieldを移行不能の理由に混ぜないこと。理由が嘘になる。
    assert "selected_fragment_ids" not in result["dropped_fields"]
    assert "rejected_input_resources" not in result["dropped_fields"]


def test_unclassified_field_loss_forbids_migration() -> None:
    """分類できないFieldを黙って捨てないこと。

    旧Recordに想定外のFieldが入っていた場合、それが監査Fieldか無害かは
    Upcasterには判断できない。判定不能は停止する（不変条件#9）。
    """
    result = _upcast(
        "ContextBundle", {**_bundle_v1(), "legacy_experimental_field": 1}, BUNDLE_SUPPLEMENT
    )
    assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["reason_code"] == "UNCLASSIFIED_FIELD_LOSS"
    assert result["dropped_fields"] == ["legacy_experimental_field"]
    assert result["output"] is None


def test_field_loss_classification_has_three_outcomes() -> None:
    """分類関数が3値を返すこと。既定は `UNKNOWN`（拒否側）である。"""
    classify = UPCASTER.classify_dropped_field
    losses = UPCASTER.FieldLossClass
    assert classify("TokenProfileSnapshot", "retrieved_at") == losses.AUDIT
    assert classify("ContextSelectionReceipt", "masking_receipt_ids") == losses.AUDIT
    assert classify("ContextBundle", "whatever_unknown") == losses.UNKNOWN
    # 無害Fieldは現在1件も無い。空であること自体が現在の判断である。
    assert all(not fields for fields in UPCASTER._BENIGN_DROPPABLE.values())


def test_migrated_declares_lossless_explicitly() -> None:
    """`MIGRATED` と Lossless を別Fieldに保つこと。

    現行のField集合では欠落を伴う移行が禁止されているため、`MIGRATED` は
    常に `lossless=true` になる。それでも両者を分けておく。
    Field集合が変わったときに黙って意味がすり替わらないようにするためである。
    """
    result = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
    assert result["status"] == "MIGRATED"
    assert result["lossless"] is True
    assert result["dropped_fields"] == []


def test_design_section_15_2_states_the_upcaster_contract() -> None:
    """設計書§15.2が実装と同じ規範を述べていること（Owner Decision M-Q2）。

    TASK-DESIGN-MIGRATION-HARDENING-001 の時点では、実装のほうが §15.2 より
    厳しかった。設計書を無断で変更せずBriefへ残し、決裁を経て v1.10 で反映した。
    **設計と実装が同じことを言っている**状態をここで固定する。
    """
    design = (REPO_ROOT / "design-v1.25-runtime-go.md").read_text(encoding="utf-8")
    required_statements = [
        # 結果が持つField（Field名まで定める）
        "`upcaster_code_hash`",
        "`conversion_reason`",
        "`lossless`",
        # MIGRATED と Lossless の分離
        "**`MIGRATED`はLosslessを意味しない。**",
        # 3分類と2つの禁止
        "監査・証跡Field／無害Field／未分類",
        "**監査・証跡Fieldを落とす変換を禁止する。**",
        "**未分類Fieldを落とす変換を禁止する。**",
        # 無害Field追加はOwner Decision
        "無害Fieldの追加はOwner Decisionを要する。",
        # 変換先Schema全体での検証
        "`additionalProperties`のいずれか1つでも満たさない出力を成功としない。",
    ]
    for statement in required_statements:
        assert statement in design, f"§15.2 に記述が無い: {statement}"


def test_design_does_not_carry_migration_tool_vocabulary() -> None:
    """Migration Tool専用の語彙を設計書本文へ持ち込んでいないこと。

    §15.2 は**性質**を規定する。`AUDIT_FIELD_NOT_TRANSFERABLE` のような
    Tool内部のreason codeは Core Schema でも Runtime Error でもないため、
    設計書の規範語彙にしない（Owner Decision D-Q6）。
    """
    design = (REPO_ROOT / "design-v1.25-runtime-go.md").read_text(encoding="utf-8")
    declared = {
        value
        for value in RESULT_VALIDATOR.schema["properties"]["reason_code"]["enum"]
        if value is not None
    }
    for token in declared | {"UNMIGRATABLE_LEGACY"}:
        assert token not in design, f"設計書へMigration Tool語彙が混入している: {token}"


def test_benign_droppable_stays_empty() -> None:
    """`_BENIGN_DROPPABLE` を空のまま運用すること（Owner Decision M-Q4）。

    空でなくなるのはCode変更ではなく**決裁事項**である。
    ここが緩むと、欠落を伴う移行が誰の判断も経ずに増える。
    """
    benign = UPCASTER._BENIGN_DROPPABLE
    assert set(benign) == set(CONTEXT_TOKEN_SCHEMAS), "対象Schemaの網羅が崩れている"
    for name, fields in benign.items():
        assert fields == frozenset(), (
            f"{name} に無害Fieldが追加されている。追加には別Owner Decisionが必要である（M-Q4）"
        )


def test_adding_a_benign_field_without_a_decision_is_detectable() -> None:
    """無害Fieldを無断で足せないこと。

    足した状態では上の試験が落ちる。**落ちることを確かめる**ため、
    分類関数が実際に `BENIGN` を返す経路も併せて示す。
    """
    classify = UPCASTER.classify_dropped_field
    losses = UPCASTER.FieldLossClass

    # 現状、無害と分類されるFieldは存在しない。
    assert not any(
        classify("ContextBundle", field) == losses.BENIGN
        for field in ("retrieved_at", "bundle_id", "whatever", "masking_receipt_ids")
    )

    # 仮に足せば分類は BENIGN へ変わる。決裁なしにこの状態へ入らないよう
    # `test_benign_droppable_stays_empty` が空集合を固定している。
    original = UPCASTER._BENIGN_DROPPABLE["ContextBundle"]
    try:
        UPCASTER._BENIGN_DROPPABLE["ContextBundle"] = frozenset({"harmless_note"})
        assert classify("ContextBundle", "harmless_note") == losses.BENIGN
    finally:
        UPCASTER._BENIGN_DROPPABLE["ContextBundle"] = original
    assert UPCASTER._BENIGN_DROPPABLE["ContextBundle"] == frozenset()


@pytest.mark.parametrize(
    "case,expected_lossless",
    [
        ("migrated", True),
        ("unmigratable", None),
    ],
)
def test_lossless_state_is_consistent_with_status(
    case: str, expected_lossless: bool | None
) -> None:
    """`lossless` が `status` と整合すること。

    `MIGRATED` なら Boolean、`UNMIGRATABLE_LEGACY` なら `null`。
    変換していないものについて欠落の有無を述べない。
    """
    if case == "migrated":
        result = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
        assert result["status"] == "MIGRATED"
    else:
        result = _upcast("ContextBundle", _bundle_v1())
        assert result["status"] == "UNMIGRATABLE_LEGACY"
    assert result["lossless"] is expected_lossless


def test_lossless_true_requires_no_dropped_field() -> None:
    """`lossless=true` と欠落ありの組合せを結果Schemaが拒むこと。"""
    result = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
    inconsistent = {**result, "dropped_fields": ["something"], "lossless": True}
    assert not RESULT_VALIDATOR.is_valid(inconsistent), (
        "lossless=true なのに欠落があるRecordを結果Schemaが通してしまう"
    )
    consistent = {**result, "dropped_fields": ["something"], "lossless": False}
    assert RESULT_VALIDATOR.is_valid(consistent)


def test_migrated_does_not_imply_lossless_in_the_schema() -> None:
    """結果Schemaが「`MIGRATED`なら必ずLossless」を要求していないこと。

    要求してしまうと、欠落を伴う変換を表現する手段が消え、
    `MIGRATED` と Lossless が同義になる（M-Q1が分けた前提が壊れる）。
    現行のField集合では欠落を伴う移行が禁止されているため実際には生じないが、
    **表現できること自体**を保つ。
    """
    template = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
    lossy = {**template, "status": "MIGRATED", "lossless": False, "dropped_fields": ["x"]}
    assert RESULT_VALIDATOR.is_valid(lossy), (
        "MIGRATED かつ lossless=false を結果Schemaが表現できない"
    )


# --------------------------------------------------------------------------
# 不変・決定性
# --------------------------------------------------------------------------


@pytest.mark.parametrize("supplement", [None, BUNDLE_SUPPLEMENT])
def test_source_record_is_not_mutated(supplement: dict[str, Any] | None) -> None:
    """元 `1.0.0` Recordを書き換えないこと（§15.2「Upcasterは元Recordを保持」）。"""
    original = _bundle_v1()
    snapshot = copy.deepcopy(original)
    _upcast("ContextBundle", original, supplement)
    assert original == snapshot


def test_supplement_argument_is_not_mutated() -> None:
    """呼出側の補足Dictも書き換えないこと。"""
    supplement = copy.deepcopy(BUNDLE_SUPPLEMENT)
    snapshot = copy.deepcopy(supplement)
    _upcast("ContextBundle", _bundle_v1(), supplement)
    assert supplement == snapshot


def test_target_schema_argument_is_not_mutated() -> None:
    """Target Schemaを書き換えないこと。"""
    target = _schema("ContextBundle", "2.0.0")
    snapshot = copy.deepcopy(target)
    _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT, target=target)
    assert target == snapshot


def test_upcast_is_deterministic() -> None:
    """同じ入力から同じ結果Bytesを得ること（不変条件#4）。"""
    first = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
    second = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_supplement_key_order_does_not_change_the_result() -> None:
    """補足のKey順がHashや出力へ漏れないこと（§1.11）。"""
    forward = _upcast("ContextBundle", _bundle_v1(), dict(BUNDLE_SUPPLEMENT))
    reversed_supplement = dict(reversed(list(BUNDLE_SUPPLEMENT.items())))
    backward = _upcast("ContextBundle", _bundle_v1(), reversed_supplement)
    assert json.dumps(forward, sort_keys=True) == json.dumps(backward, sort_keys=True)


def test_source_record_hash_reflects_the_source_not_the_output() -> None:
    """`source_record_hash` は元Recordから測ること。"""
    bare = _upcast("ContextBundle", _bundle_v1())
    full = _upcast("ContextBundle", _bundle_v1(), BUNDLE_SUPPLEMENT)
    assert bare["source_record_hash"] == full["source_record_hash"]
    other = _upcast("ContextBundle", {**_bundle_v1(), "bundle_id": "bundle-2"}, BUNDLE_SUPPLEMENT)
    assert other["source_record_hash"] != full["source_record_hash"]


def test_unmigratable_output_is_never_accepted_as_two_point_zero() -> None:
    """`output=null` を `2.0.0` Schema検証へ通さないこと。"""
    result = _upcast("ContextBundle", _bundle_v1())
    assert result["output"] is None
    assert not Draft202012Validator(_schema("ContextBundle", "2.0.0")).is_valid(result["output"])


def test_missing_hashes_are_never_guessed() -> None:
    """欠落Hashを空文字・ゼロ値・ダミーで埋めないこと。"""
    result = _upcast("ContextBundle", _bundle_v1())
    assert result["output"] is None
    assert result["source_record_hash"] != "sha256:" + "0" * 64


# --------------------------------------------------------------------------
# 語彙の閉じ込め
# --------------------------------------------------------------------------


def test_migration_vocabulary_is_not_in_the_registry() -> None:
    """Migration語彙をLedger Event／Error Registryへ足していないこと（D-Q6）。"""
    events = yaml.safe_load((REGISTRIES / "events.yaml").read_text(encoding="utf-8"))
    errors = yaml.safe_load((REGISTRIES / "errors.yaml").read_text(encoding="utf-8"))
    error_codes = {row["error_code"] for row in errors["error_codes"]}
    serialized_events = yaml.safe_dump(events["event_types"])
    declared = set(RESULT_VALIDATOR.schema["properties"]["reason_code"]["enum"]) - {None}
    for token in declared | {"MIGRATED", "UNMIGRATABLE_LEGACY"}:
        assert token not in error_codes, token
        assert token not in serialized_events, token
    assert set(RESULT_VALIDATOR.schema["properties"]["status"]["enum"]) == {
        "MIGRATED",
        "UNMIGRATABLE_LEGACY",
    }


def test_every_declared_reason_code_is_reachable() -> None:
    """宣言したreason codeが全て実際に出ること。

    出ない語彙をenumへ置くと、「起きたら停まるはず」の経路が実在しないまま
    残る。Gate Corpusはそれを見つける場所である。
    """
    declared = {
        value
        for value in RESULT_VALIDATOR.schema["properties"]["reason_code"]["enum"]
        if value is not None
    }
    observed = {
        _upcast("ContextBundle", _bundle_v1())["reason_code"],
        _upcast("ContextBundle", {**_bundle_v1(), "schema_version": "9.0.0"})["reason_code"],
        _upcast("ContextBundle", {**_bundle_v1(), "schema_name": "Other"})["reason_code"],
        _upcast(
            "ContextBundle",
            _bundle_v1(),
            BUNDLE_SUPPLEMENT,
            target=_schema("ContextFragment", "2.0.0"),
        )["reason_code"],
        _upcast("ContextBundle", _bundle_v1(), {**BUNDLE_SUPPLEMENT, "bundle_hash": "x"})[
            "reason_code"
        ],
        _upcast("ContextBundle", _bundle_v1(), {**BUNDLE_SUPPLEMENT, "producer": "p"})[
            "reason_code"
        ],
        _upcast(
            "TokenProfileSnapshot",
            {**_envelope("TokenProfileSnapshot", "1.0.0"), "retrieved_at": "x"},
        )["reason_code"],
        _upcast("ContextBundle", {**_bundle_v1(), "unknown_x": 1}, BUNDLE_SUPPLEMENT)[
            "reason_code"
        ],
        _upcast("ContextBundle", {**_bundle_v1(), "bundle_hash": "nope"}, BUNDLE_SUPPLEMENT)[
            "reason_code"
        ],
    }
    assert declared == observed


# --------------------------------------------------------------------------
# Hash差異（schema_catalog_hash と schema_set_hash の分離）
# --------------------------------------------------------------------------


def _catalog(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return schema_catalog_entries(normalize_core_schemas(rows), REPO_ROOT)


_ONE_VERSION_ROW: dict[str, Any] = {
    "ordinal": 11,
    "schema_name": "ContextBundle",
    "active_write_version": "1.0.0",
    "versions": [
        {
            "version": "1.0.0",
            "path": "schemas/core/ContextBundle/1.0.0.schema.json",
            "read_only": False,
        }
    ],
}

_TWO_VERSION_ROW: dict[str, Any] = {
    "ordinal": 11,
    "schema_name": "ContextBundle",
    "active_write_version": "2.0.0",
    "versions": [
        {
            "version": "1.0.0",
            "path": "schemas/core/ContextBundle/1.0.0.schema.json",
            "read_only": True,
        },
        {
            "version": "2.0.0",
            "path": "schemas/core/ContextBundle/2.0.0.schema.json",
            "read_only": False,
        },
    ],
}


def test_adding_a_catalog_version_does_not_change_an_existing_plan_set_hash() -> None:
    """Catalogへ版が増えても、Planが参照する集合が同じなら`schema_set_hash`は不変。

    分けないと、`1.0.0` を読み取り可能なまま残すだけで**全PlanのHashが動き**、
    既存Approvalが理由なく無効化される。
    """
    used = [SchemaRef("ContextBundle", "1.0.0", ContentHash.parse("sha256:" + "a" * 64))]
    before = compute_schema_set_hash(used)
    assert compute_schema_catalog_hash(_catalog([_ONE_VERSION_ROW])) != compute_schema_catalog_hash(
        _catalog([_TWO_VERSION_ROW])
    )
    assert compute_schema_set_hash(list(used)) == before


def test_plan_set_hash_changes_when_the_plan_uses_a_different_version() -> None:
    """逆に、Planが使う版が変われば`schema_set_hash`は動くこと。"""
    hash_value = ContentHash.parse("sha256:" + "a" * 64)
    old = compute_schema_set_hash([SchemaRef("ContextBundle", "1.0.0", hash_value)])
    new = compute_schema_set_hash([SchemaRef("ContextBundle", "2.0.0", hash_value)])
    assert old != new


def test_catalog_hash_is_order_independent() -> None:
    """Registryの記載順・Filesystem列挙順をHashへ持ち込まないこと（§1.11）。"""
    rows = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text(encoding="utf-8"))["core_schemas"]
    catalog = _catalog(rows)
    baseline = compute_schema_catalog_hash(catalog)
    assert compute_schema_catalog_hash(list(reversed(catalog))) == baseline


def test_catalog_hash_excludes_nondeterministic_values() -> None:
    """生成時刻などの非決定値をHash入力へ混ぜないこと（不変条件#4）。"""
    catalog = _catalog([_TWO_VERSION_ROW])
    baseline = compute_schema_catalog_hash(catalog)
    noisy = copy.deepcopy(catalog)
    for item in noisy:
        item["generated_at"] = "2026-01-01T00:00:00Z"
        item["pid"] = 4242
    assert compute_schema_catalog_hash(noisy) == baseline


def test_catalog_hash_changes_when_the_writable_version_moves() -> None:
    """どの版へ書いてよいかが変われば`schema_catalog_hash`が変わること。"""
    catalog = _catalog([_TWO_VERSION_ROW])
    flipped = copy.deepcopy(catalog)
    for item in flipped:
        item["active_write"] = not item["active_write"]
        item["read_only"] = not item["read_only"]
    assert compute_schema_catalog_hash(flipped) != compute_schema_catalog_hash(catalog)


def test_registered_schema_file_must_exist() -> None:
    """Registryが指すFileが無い状態でCatalogを作らない（Fail-Closed）。"""
    ghost = copy.deepcopy(_TWO_VERSION_ROW)
    ghost["versions"][1]["path"] = "schemas/core/ContextBundle/9.9.9.schema.json"
    with pytest.raises(FileNotFoundError):
        _catalog([ghost])
