"""`ConversationMessage@2.0.0` の契約検査。

## Production を実装していない

保存も CAS 書込みも本 Task の対象外である。ここで測るのは **契約が自己整合して
いること**と、**`1.0.0` が 1 byte も動いていないこと**である。

Hash 規則と Scanner 境界は試験ローカルの参照実装で確かめる。参照実装は
Production ではない。設計書 §4.11 が定めた規則をそのまま書き下し、その規則が
決定論であること・拒否側が効くことを示すために置いている。
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
import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas/core"
REGISTRIES = REPO_ROOT / "design-source/registries"
SNAPSHOT = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
DESIGN = REPO_ROOT / f"design-v{SNAPSHOT['design_version']}-runtime-go.md"

_H = "sha256:" + "0" * 64
_BODY = "sha256:" + "a" * 64
_TS = "2026-08-28T00:00:00Z"

#: `1.0.0` は発行済みの契約である。**この値が動いたら Major 規則が破れている。**
#: 値は Owner Decision CMC-6-A（`1.0.0` を read_only で残す）に対応する。
_V1_FROZEN_REQUIRED = (
    "schema_name",
    "schema_version",
    "record_id",
    "created_at",
    "producer",
    "content_hash",
    "message_id",
    "conversation_id",
    "role",
    "sequence_number",
)


def _load(name: str, version: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name / f"{version}.schema.json").read_text(encoding="utf-8"))


def _instance_v2() -> dict[str, Any]:
    return {
        "schema_name": "ConversationMessage",
        "schema_version": "2.0.0",
        "record_id": "r-1",
        "created_at": _TS,
        "producer": "test",
        "content_hash": _H,
        "message_id": "m-1",
        "conversation_id": "c-1",
        "role": "USER_TASK",
        "sequence_number": 1,
        "content_artifact_hash": _BODY,
    }


# ---------------------------------------------------------------------------
# 参照実装。**Production ではない。** 設計書 §4.11 の規則を書き下す。
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def message_content_hash(*, body_hash: str, role: str, sequence_number: int) -> str:
    """本文 Hash・`role`・`sequence_number` から導出する（CMC-3-A）。

    ID・時刻・PID を入力に取らない。Hash 自身も入力に取らない。
    """
    return _sha(_canonical({"body": body_hash, "role": role, "sequence_number": sequence_number}))


def conversation_hash(*, conversation_id: str, created_at: str, producer: str) -> str:
    """`conversation_id` と作成時の不変メタだけから導出する（CMC-4-A）。

    **Message を入力に取らない。** だから Message 追加で動かない。
    """
    return _sha(
        _canonical(
            {"conversation_id": conversation_id, "created_at": created_at, "producer": producer}
        )
    )


#: 保存を拒否する分類（不変条件#21／CMC-5-A）。マスクして保存しない。
_REJECT_CATEGORIES = frozenset(
    {"SECRET_GENERIC", "API_KEY", "PRIVATE_KEY", "NATIONAL_ID", "SPECIAL_CATEGORY_DATA"}
)


class ScannerNotRun(RuntimeError):
    """Scanner 未実行。**判定不能は保存しない。**"""


class StorageRejected(RuntimeError):
    """Reject 分類を含む。CAS へも SQLite へも書かない。"""


def store_body(body: str, *, scan_result: frozenset[str] | None) -> str:
    """保存経路の参照実装。書けたときだけ Hash を返す。

    `scan_result` が `None` は「Scanner 未実行」である。素通しさせない。
    """
    if scan_result is None:
        raise ScannerNotRun("SCANNER_NOT_RUN")
    hit = scan_result & _REJECT_CATEGORIES
    if hit:
        raise StorageRejected(f"STORAGE_REJECTED: {sorted(hit)}")
    return _sha(body.encode())


# ---------------------------------------------------------------------------
# 1.0.0 の不変
# ---------------------------------------------------------------------------


def test_v1_is_registered_read_only_and_v2_is_active() -> None:
    """`1.0.0` は read_only、`2.0.0` が active_write であること（CMC-6-A）。"""
    catalog = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text(encoding="utf-8"))
    entry = next(e for e in catalog["core_schemas"] if e["schema_name"] == "ConversationMessage")
    assert entry["active_write_version"] == "2.0.0"
    by_version = {v["version"]: v for v in entry["versions"]}
    assert by_version["1.0.0"]["read_only"] is True, "1.0.0 が read_only でない"
    assert by_version["2.0.0"]["read_only"] is False
    assert sorted(by_version) == ["1.0.0", "2.0.0"]


def test_v1_id_and_required_did_not_move() -> None:
    """`1.0.0` の `$id` と必須 Field が動いていないこと。

    **発行済みの契約である。** 動けば §15.2 の Major 規則が破れている。
    """
    v1 = _load("ConversationMessage", "1.0.0")
    assert v1["$id"].endswith("/ConversationMessage/1.0.0.schema.json")
    assert tuple(v1["required"]) == _V1_FROZEN_REQUIRED
    assert v1["properties"]["schema_version"] == {"const": "1.0.0"}


def test_v1_has_no_body_or_body_reference() -> None:
    """`1.0.0` へ本文 Field も本文参照 Field も足していないこと。"""
    props = _load("ConversationMessage", "1.0.0")["properties"]
    assert "content_artifact_hash" not in props
    for name in ("content", "text", "body", "payload"):
        assert name not in props


def test_v1_bytes_match_the_committed_file() -> None:
    """`1.0.0` の Bytes が git の版と一致すること。**再生成で動いていない。**"""
    rel = "schemas/core/ConversationMessage/1.0.0.schema.json"
    committed = subprocess.run(  # noqa: S603
        ["git", "show", f"HEAD:{rel}"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    assert (REPO_ROOT / rel).read_bytes() == committed


# ---------------------------------------------------------------------------
# 2.0.0 の契約
# ---------------------------------------------------------------------------


def test_v2_required_fields() -> None:
    """`2.0.0` の必須 Field が Owner 回答どおりであること（CMC-2-A）。"""
    expected = [*_V1_FROZEN_REQUIRED, "content_artifact_hash"]
    assert _load("ConversationMessage", "2.0.0")["required"] == expected


def test_v2_adds_exactly_one_required_field() -> None:
    """`1.0.0` との差が `content_artifact_hash` だけであること。"""
    v1 = set(_load("ConversationMessage", "1.0.0")["required"])
    v2 = set(_load("ConversationMessage", "2.0.0")["required"])
    assert sorted(v2 - v1) == ["content_artifact_hash"]
    assert v1 - v2 == set(), "1.0.0 の必須 Field を落としている"


def test_v2_valid_instance_passes() -> None:
    Draft202012Validator(_load("ConversationMessage", "2.0.0")).validate(_instance_v2())


def test_v2_missing_content_artifact_hash_is_rejected() -> None:
    """`content_artifact_hash` を落とすと拒否されること。"""
    schema = _load("ConversationMessage", "2.0.0")
    broken = {k: v for k, v in _instance_v2().items() if k != "content_artifact_hash"}
    assert not Draft202012Validator(schema).is_valid(broken)


@pytest.mark.parametrize("field", (*_V1_FROZEN_REQUIRED, "content_artifact_hash"))
def test_v2_missing_required_field_is_rejected(field: str) -> None:
    schema = _load("ConversationMessage", "2.0.0")
    broken = {k: v for k, v in _instance_v2().items() if k != field}
    assert not Draft202012Validator(schema).is_valid(broken), f"{field} を落としても通る"


def test_v2_unknown_field_is_rejected() -> None:
    schema = _load("ConversationMessage", "2.0.0")
    assert schema["additionalProperties"] is False
    assert not Draft202012Validator(schema).is_valid({**_instance_v2(), "surprise": "x"})


def test_v2_has_no_inline_body_field() -> None:
    """本文を inline で持つ Field が無いこと（CMC-1-A）。"""
    props = _load("ConversationMessage", "2.0.0")["properties"]
    for name in ("content", "text", "body", "payload", "message_content", "message_text"):
        assert name not in props, f"inline 本文 Field {name} がある"


def test_content_artifact_hash_matches_context_fragment() -> None:
    """`content_artifact_hash` が `ContextFragment@2.0.0` と同名・同義であること。"""
    mine = _load("ConversationMessage", "2.0.0")["properties"]["content_artifact_hash"]
    theirs = _load("ContextFragment", "2.0.0")["properties"]["content_artifact_hash"]
    assert mine == theirs, f"定義がずれた: {mine} != {theirs}"


def test_content_artifact_hash_rejects_malformed_values() -> None:
    """Hash 形式でない値を拒否すること。"""
    schema = _load("ConversationMessage", "2.0.0")
    validator = Draft202012Validator(schema)
    for bad in ("", "deadbeef", "sha256:xyz", "md5:" + "0" * 32, "sha256:" + "0" * 63):
        assert not validator.is_valid({**_instance_v2(), "content_artifact_hash": bad}), bad


def test_v2_role_enum_is_the_existing_vocabulary() -> None:
    """`role` が §1.16.4 の 7 値のままであること。"""
    role = _load("ConversationMessage", "2.0.0")["properties"]["role"]["enum"]
    assert role == _load("ContextFragment", "1.0.0")["properties"]["message_role"]["enum"]
    assert len(role) == 7
    assert "user" not in role and "assistant" not in role


def test_v2_rejects_role_outside_the_vocabulary() -> None:
    schema = _load("ConversationMessage", "2.0.0")
    validator = Draft202012Validator(schema)
    for bad in ("user", "assistant", "system", "SYSTEM", ""):
        assert not validator.is_valid({**_instance_v2(), "role": bad}), bad


# ---------------------------------------------------------------------------
# Artifact 参照先の存在
# ---------------------------------------------------------------------------


def _artifact_exists(manifest: dict[str, str], artifact_hash: str) -> bool:
    return artifact_hash in manifest


def test_missing_artifact_reference_is_rejected() -> None:
    """参照先が Manifest に無い Message を受理しないこと。"""
    manifest = {_BODY: "artifacts/aa"}
    assert _artifact_exists(manifest, _instance_v2()["content_artifact_hash"])
    assert not _artifact_exists(manifest, "sha256:" + "b" * 64)


# ---------------------------------------------------------------------------
# Hash 契約
# ---------------------------------------------------------------------------


def test_content_hash_is_deterministic() -> None:
    args = {"body_hash": _BODY, "role": "USER_TASK", "sequence_number": 1}
    assert message_content_hash(**args) == message_content_hash(**args)


def test_content_hash_detects_role_change() -> None:
    """`role` の差し替えを検出すること（CMC-3-A）。"""
    base = message_content_hash(body_hash=_BODY, role="USER_TASK", sequence_number=1)
    moved = message_content_hash(body_hash=_BODY, role="UNTRUSTED_PROVIDER_DATA", sequence_number=1)
    assert base != moved


def test_content_hash_detects_sequence_change() -> None:
    """`sequence_number` の差し替えを検出すること。"""
    base = message_content_hash(body_hash=_BODY, role="USER_TASK", sequence_number=1)
    moved = message_content_hash(body_hash=_BODY, role="USER_TASK", sequence_number=2)
    assert base != moved


def test_content_hash_detects_body_change() -> None:
    base = message_content_hash(body_hash=_BODY, role="USER_TASK", sequence_number=1)
    moved = message_content_hash(
        body_hash="sha256:" + "c" * 64, role="USER_TASK", sequence_number=1
    )
    assert base != moved


def test_content_hash_ignores_ids_times_and_pid() -> None:
    """ID・時刻・PID が Hash 入力に入らないこと（不変条件#4）。

    参照実装の引数名に無いことで示す。増やせば落ちる。
    """
    import inspect

    params = set(inspect.signature(message_content_hash).parameters)
    assert params == {"body_hash", "role", "sequence_number"}
    for forbidden in ("message_id", "conversation_id", "created_at", "record_id", "pid"):
        assert forbidden not in params
    # PID が変わっても同じ値になる
    args = {"body_hash": _BODY, "role": "USER_TASK", "sequence_number": os.getpid() % 7 + 1}
    assert message_content_hash(**args) == message_content_hash(**args)


def test_content_hash_does_not_take_itself_as_input() -> None:
    import inspect

    assert "content_hash" not in set(inspect.signature(message_content_hash).parameters)


def test_content_hash_does_not_depend_on_pythonhashseed() -> None:
    """別 `PYTHONHASHSEED` の子 Process でも同じ値になること（不変条件#5）。"""
    script = (
        "import hashlib,json\n"
        "d={'body':'sha256:'+'a'*64,'role':'USER_TASK','sequence_number':1}\n"
        "b=json.dumps(d,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()\n"
        "print('sha256:'+hashlib.sha256(b).hexdigest())\n"
    )
    seen = set()
    for seed in ("0", "1", "9999"):
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        seen.add(out.stdout.strip())
    assert len(seen) == 1, f"PYTHONHASHSEED で Hash が変わった: {seen}"
    assert seen.pop() == message_content_hash(body_hash=_BODY, role="USER_TASK", sequence_number=1)


# ---------------------------------------------------------------------------
# Conversation Hash と役割分離
# ---------------------------------------------------------------------------


def test_conversation_hash_does_not_move_when_messages_are_added() -> None:
    """Message を足しても `conversation_hash` が動かないこと（CMC-4-A）。"""
    args = {"conversation_id": "c-1", "created_at": _TS, "producer": "test"}
    before = conversation_hash(**args)
    # Message を 100 件足したことにしても、入力に Message が無いので動かない。
    after = conversation_hash(**args)
    assert before == after


def test_conversation_hash_does_not_take_messages() -> None:
    """`conversation_hash` が Message 集合を入力に取らないこと。"""
    import inspect

    params = set(inspect.signature(conversation_hash).parameters)
    assert params == {"conversation_id", "created_at", "producer"}
    for forbidden in ("messages", "message_set_hash", "message_ids"):
        assert forbidden not in params


def test_conversation_hash_moves_when_identity_moves() -> None:
    """**何も見ていない検査にしない。** 識別が変われば動く。"""
    base = conversation_hash(conversation_id="c-1", created_at=_TS, producer="test")
    other = conversation_hash(conversation_id="c-2", created_at=_TS, producer="test")
    assert base != other


def test_three_hashes_have_distinct_roles() -> None:
    """3 つの Hash が別物であること。片方でもう片方を代用しない。"""
    body = _BODY
    content = message_content_hash(body_hash=body, role="USER_TASK", sequence_number=1)
    conv = conversation_hash(conversation_id="c-1", created_at=_TS, producer="test")
    message_set = _sha(_canonical([content]))
    assert len({body, content, conv, message_set}) == 4


# ---------------------------------------------------------------------------
# 保存境界
# ---------------------------------------------------------------------------


def test_clean_body_is_stored() -> None:
    """Reject 分類が無ければ保存できること。"""
    assert store_body("hello", scan_result=frozenset()).startswith("sha256:")


@pytest.mark.parametrize(
    "category", ["SECRET_GENERIC", "API_KEY", "PRIVATE_KEY", "NATIONAL_ID", "SPECIAL_CATEGORY_DATA"]
)
def test_reject_category_is_not_stored(category: str) -> None:
    """Reject 分類は保存を拒否すること（CMC-5-A／不変条件#21）。

    **マスクして保存しない。** 例外で止まり、Hash を返さない。
    """
    with pytest.raises(StorageRejected):
        store_body("secret", scan_result=frozenset({category}))


def test_unscanned_body_is_not_stored() -> None:
    """Scanner 未実行の本文を保存しないこと。判定不能は Fail-Closed。"""
    with pytest.raises(ScannerNotRun):
        store_body("hello", scan_result=None)


def test_storage_boundary_is_separate_from_the_send_gate() -> None:
    """保存境界が送信前 Gate とは別だと設計へ書かれていること。"""
    text = DESIGN.read_text(encoding="utf-8")
    body = text[text.index("#### 保存境界（v1.22）") :][:2000]
    assert "Masking Gateとは別の境界" in body
    for term in ("Artifact CAS", "SQLite", "Evidence", "Log"):
        assert term in body, f"保存境界の記述に {term} が無い"


# ---------------------------------------------------------------------------
# 既存条件
# ---------------------------------------------------------------------------


def test_current_core_schema_versions_match_registry() -> None:
    """論理件数と版件数をRegistryから導出し、旧版保持と現行書込版を検証する。"""
    schemas = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text())["core_schemas"]
    names = {entry["schema_name"] for entry in schemas}
    versions = [
        (entry["schema_name"], v["version"]) for entry in schemas for v in entry["versions"]
    ]
    assert len(versions) == len(set(versions)), "Registry has duplicate versions"
    assert SNAPSHOT["totals"]["core_schema_count"] == len(names)
    assert SNAPSHOT["totals"]["core_schema_version_count"] == len(versions)
    measured = {(v["schema_name"], v["schema_version"]) for v in SNAPSHOT["core_schema_versions"]}
    assert measured == set(versions)
    conversation = next(s for s in schemas if s["schema_name"] == "ConversationMessage")
    assert conversation["active_write_version"] == "2.0.0"
    assert {v["version"]: v["read_only"] for v in conversation["versions"]} == {
        "1.0.0": True,
        "2.0.0": False,
    }
    assert SNAPSHOT["core_schema_active_write_versions"]["ConversationMessage"] == "2.0.0"


def test_mvp0a_conditions_did_not_move() -> None:
    scope = SNAPSHOT["scopes"]["MVP0-A"]
    assert scope["required_case_count"] == 108
    assert scope["required_test_id_count"] == 31
    assert SNAPSHOT["totals"]["gate_count"] == 45
    assert SNAPSHOT["totals"]["case_count"] == 128


def test_no_new_events_errors_or_states() -> None:
    totals = SNAPSHOT["totals"]
    assert totals["event_type_count"] == 79
    assert totals["error_code_count"] == 76
    assert totals["state_namespace_count"] == 27
