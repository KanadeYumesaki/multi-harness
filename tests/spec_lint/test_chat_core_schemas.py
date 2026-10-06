"""Chat の 3 Core Schema の契約検査。

## Production を実装していない

保存も Context 再構築も本 Task の対象外である。ここで測るのは **契約が自己整合
していること**である。

Hash 規則は試験ローカルの参照実装で確かめる。参照実装は Production ではない。
設計書 §4.11 が定めた規則をそのまま書き下し、その規則が決定論であることを示す
ために置いている。Production を実装するときは、この参照実装と同じ結果になること
を別途確かめる必要がある。

## 何を測っているか

* Schema が受理すべきものを受理し、拒むべきものを拒むこと
* 設計書が「持たせない」と決めた Field が本当に無いこと
* Hash 規則が ID・時刻・PID・列挙順・`PYTHONHASHSEED` に依存しないこと
* Chat を足しても MVP0-A の判定条件が動いていないこと
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

CHAT_SCHEMAS = ("Conversation", "ConversationMessage", "ConversationSnapshot")

_H = "sha256:" + "0" * 64
_TS = "2026-08-28T00:00:00Z"


def _load(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name / "1.0.0.schema.json").read_text(encoding="utf-8"))


def _envelope(name: str) -> dict[str, Any]:
    return {
        "schema_name": name,
        "schema_version": "1.0.0",
        "record_id": "r-1",
        "created_at": _TS,
        "producer": "test",
        "content_hash": _H,
    }


def _instance(name: str) -> dict[str, Any]:
    extra: dict[str, dict[str, Any]] = {
        "Conversation": {"conversation_id": "c-1", "conversation_hash": _H},
        "ConversationMessage": {
            "message_id": "m-1",
            "conversation_id": "c-1",
            "role": "USER_TASK",
            "sequence_number": 1,
        },
        "ConversationSnapshot": {
            "snapshot_id": "s-1",
            "conversation_id": "c-1",
            "snapshot_hash": _H,
            "message_set_hash": _H,
            "schema_set_hash": _H,
            "design_sha256": _H,
        },
    }
    return {**_envelope(name), **extra[name]}


# ---------------------------------------------------------------------------
# 参照実装。**Production ではない。** 設計書 §4.11 の規則をそのまま書き下す。
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    """§1.11 の `json-sorted-compact-utf8/1` と同じ並べ方で Bytes にする。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def message_set_hash(messages: list[dict[str, Any]]) -> str:
    """各 Message の `content_hash` を `sequence_number` 昇順で並べて Hash する。

    ID・時刻・PID は入力に取らない。並びは `sequence_number` だけで決まる。
    """
    ordered = sorted(messages, key=lambda m: m["sequence_number"])
    return _sha(_canonical([m["content_hash"] for m in ordered]))


def schema_set_hash(used: list[tuple[str, str, str]]) -> str:
    """**実際に使用した** Schema の `(名前, 版, Hash)` だけを Hash する。"""
    return _sha(_canonical(sorted(used)))


def snapshot_hash(
    *, conversation_hash: str, msg_set_hash: str, schema_hash: str, design_sha256: str
) -> str:
    """束縛先を束ねて Hash する。**自分自身を入力に取らない。**"""
    return _sha(
        _canonical(
            {
                "conversation_hash": conversation_hash,
                "design_sha256": design_sha256,
                "message_set_hash": msg_set_hash,
                "schema_set_hash": schema_hash,
            }
        )
    )


# ---------------------------------------------------------------------------
# 登録と正常系
# ---------------------------------------------------------------------------


#: 各 Chat Schema の登録されるべき版と active_write。
#: `ConversationMessage` は Owner Decision CMC-6-A により `2.0.0` を足し、
#: `1.0.0` を read_only で残した。他の 2 つは `1.0.0` のままである。
_EXPECTED_VERSIONS: dict[str, tuple[list[str], str]] = {
    "Conversation": (["1.0.0"], "1.0.0"),
    "ConversationMessage": (["1.0.0", "2.0.0"], "2.0.0"),
    "ConversationSnapshot": (["1.0.0"], "1.0.0"),
}


def test_chat_schemas_are_registered_with_the_expected_versions() -> None:
    """3 Schema の版と active_write が Owner 回答どおりであること。"""
    catalog = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text(encoding="utf-8"))
    entries = {e["schema_name"]: e for e in catalog["core_schemas"]}
    assert sorted(_EXPECTED_VERSIONS) == sorted(CHAT_SCHEMAS)
    for name in CHAT_SCHEMAS:
        assert name in entries, f"{name} が schemas.yaml に無い"
        want_versions, want_active = _EXPECTED_VERSIONS[name]
        versions = [v["version"] for v in entries[name]["versions"]]
        assert versions == want_versions, f"{name} の版が {versions}"
        assert entries[name]["active_write_version"] == want_active


@pytest.mark.parametrize("name", CHAT_SCHEMAS)
def test_valid_instance_passes(name: str) -> None:
    """正常インスタンスが通ること。"""
    Draft202012Validator(_load(name)).validate(_instance(name))


@pytest.mark.parametrize("name", CHAT_SCHEMAS)
def test_missing_required_field_is_rejected(name: str) -> None:
    """必須 Field を 1 つ落とすたびに拒否されること。

    **1 件でも通れば、その Field は実質必須ではない。**
    """
    schema = _load(name)
    validator = Draft202012Validator(schema)
    required = schema["required"]
    assert required, f"{name} に必須 Field が無い"
    for field in required:
        broken = {k: v for k, v in _instance(name).items() if k != field}
        assert not validator.is_valid(broken), f"{name}: {field} を落としても通ってしまう"


#: Owner 回答が定めた必須 Field。**Schema 側から採らない。**
#: Schema から採ると、`required` から消えた Field を検査対象から取りこぼす。
_OWNER_REQUIRED: dict[str, tuple[str, ...]] = {
    # CC-5-A
    "Conversation": ("conversation_id", "conversation_hash"),
    # CC-6-A。`sequence_number` は §1.3 の共通識別子を順序キーとして使う。
    "ConversationMessage": ("message_id", "conversation_id", "role", "sequence_number"),
    # CC-7-C
    "ConversationSnapshot": (
        "snapshot_id",
        "conversation_id",
        "snapshot_hash",
        "message_set_hash",
        "schema_set_hash",
        "design_sha256",
    ),
}

_ENVELOPE_REQUIRED = (
    "schema_name",
    "schema_version",
    "record_id",
    "created_at",
    "producer",
    "content_hash",
)


@pytest.mark.parametrize("name", CHAT_SCHEMAS)
def test_required_fields_match_the_owner_decision(name: str) -> None:
    """必須 Field の集合が Owner 回答どおりであること。

    増えても減っても落ちる。`message_count` や `token_count` が紛れ込んだ場合も
    ここで落ちる。
    """
    expected = [*_ENVELOPE_REQUIRED, *_OWNER_REQUIRED[name]]
    assert _load(name)["required"] == expected, f"{name} の必須 Field が Owner 回答とずれた"


@pytest.mark.parametrize("name", CHAT_SCHEMAS)
def test_unknown_field_is_rejected(name: str) -> None:
    """知らない Field を足すと拒否されること。"""
    schema = _load(name)
    assert schema["additionalProperties"] is False
    bad = {**_instance(name), "surprise_field": "x"}
    assert not Draft202012Validator(schema).is_valid(bad)


def test_role_enum_is_the_existing_vocabulary() -> None:
    """`role` は §1.16.4 の既存語彙をそのまま使うこと。

    Chat 用に `user`／`assistant` を作っていないこと、および
    `ContextFragment.message_role` から**ずれていない**ことを見る。
    """
    role = _load("ConversationMessage")["properties"]["role"]["enum"]
    fragment = _load("ContextFragment")["properties"]["message_role"]["enum"]
    assert role == fragment, f"role が ContextFragment とずれた: {role} != {fragment}"
    assert "user" not in role and "assistant" not in role


# ---------------------------------------------------------------------------
# 持たせないと決めた Field
# ---------------------------------------------------------------------------


def test_conversation_does_not_carry_message_ids() -> None:
    """親が子を列挙しないこと。二重管理を作らない（CC-9-A）。"""
    schema = _load("Conversation")
    assert "message_ids" not in schema["properties"]
    assert "message_ids" not in schema["required"]


def test_conversation_does_not_carry_message_count() -> None:
    """件数の正本を 2 か所に持たないこと（不変条件#18）。"""
    schema = _load("Conversation")
    assert "message_count" not in schema["properties"]


def test_message_does_not_carry_token_count() -> None:
    """Token 会計を Message 本体へ持たないこと。"""
    schema = _load("ConversationMessage")
    assert "token_count" not in schema["properties"]


def test_message_points_at_its_parent() -> None:
    """子 → 親の単方向であること。"""
    schema = _load("ConversationMessage")
    assert "conversation_id" in schema["required"]


def test_snapshot_identifies_its_conversation() -> None:
    """どの Conversation の Snapshot か、Evidence 単体で分かること（CC-7-C）。"""
    required = _load("ConversationSnapshot")["required"]
    for field in ("conversation_id", "message_set_hash", "schema_set_hash", "design_sha256"):
        assert field in required, f"{field} が必須でない"


# ---------------------------------------------------------------------------
# Hash 規則
# ---------------------------------------------------------------------------


def _messages(*, ids: tuple[str, ...] = ("m-1", "m-2", "m-3")) -> list[dict[str, Any]]:
    return [
        {"message_id": ids[i], "sequence_number": i + 1, "content_hash": f"sha256:{i:064d}"}
        for i in range(3)
    ]


def test_message_set_hash_ignores_ids_times_and_pid() -> None:
    """ID・時刻・PID を変えても Hash が動かないこと（不変条件#4）。"""
    base = _messages()
    noisy = [
        {
            **m,
            "message_id": f"other-{i}",
            "record_id": f"r-{os.getpid()}-{i}",
            "created_at": "2027-01-01T00:00:00Z",
        }
        for i, m in enumerate(base)
    ]
    assert message_set_hash(base) == message_set_hash(noisy)


def test_message_order_is_deterministic() -> None:
    """入力の並びが変わっても、順序キーが同じなら同じ Hash になること。"""
    base = _messages()
    assert message_set_hash(base) == message_set_hash(list(reversed(base)))


def test_same_history_yields_same_message_set_hash() -> None:
    """同じ履歴からは同じ Hash が得られること。"""
    assert message_set_hash(_messages()) == message_set_hash(_messages())


def test_different_history_yields_different_hash() -> None:
    """内容が変われば Hash も変わること。**検査が何も見ていない状態を避ける。**"""
    changed = _messages()
    changed[1]["content_hash"] = "sha256:" + "f" * 64
    assert message_set_hash(_messages()) != message_set_hash(changed)


def test_message_set_hash_does_not_depend_on_pythonhashseed() -> None:
    """別 `PYTHONHASHSEED` の子 Process でも同じ Hash になること（不変条件#5）。"""
    script = (
        "import hashlib,json\n"
        "ms=[{'sequence_number':i+1,'content_hash':'sha256:%064d'%i} for i in range(3)]\n"
        "o=sorted(ms,key=lambda m:m['sequence_number'])\n"
        "b=json.dumps([m['content_hash'] for m in o],ensure_ascii=False,"
        "sort_keys=True,separators=(',',':')).encode()\n"
        "print('sha256:'+hashlib.sha256(b).hexdigest())\n"
    )
    seen = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        seen.add(out.stdout.strip())
    assert len(seen) == 1, f"PYTHONHASHSEED で Hash が変わった: {seen}"
    assert seen.pop() == message_set_hash(_messages())


def test_snapshot_hash_is_deterministic() -> None:
    """同じ束縛先からは同じ Snapshot Hash が得られること。"""
    args = {
        "conversation_hash": _H,
        "msg_set_hash": message_set_hash(_messages()),
        "schema_hash": _H,
        "design_sha256": SNAPSHOT["design_sha256"],
    }
    assert snapshot_hash(**args) == snapshot_hash(**args)


def test_snapshot_hash_moves_when_any_binding_moves() -> None:
    """束縛先が 1 つでも動けば Snapshot Hash が動くこと。"""
    args = {
        "conversation_hash": _H,
        "msg_set_hash": message_set_hash(_messages()),
        "schema_hash": _H,
        "design_sha256": SNAPSHOT["design_sha256"],
    }
    baseline = snapshot_hash(**args)
    for key in args:
        moved = {**args, key: "sha256:" + "a" * 64}
        assert snapshot_hash(**moved) != baseline, f"{key} を変えても Hash が動かない"


def test_snapshot_hash_does_not_take_itself_as_input() -> None:
    """Hash 自身を Hash 入力へ含めないこと。

    参照実装の引数名に `snapshot_hash` が無いことで示す。
    """
    import inspect

    params = set(inspect.signature(snapshot_hash).parameters)
    assert "snapshot_hash" not in params
    assert params == {"conversation_hash", "msg_set_hash", "schema_hash", "design_sha256"}


def test_schema_set_hash_ignores_unused_schemas() -> None:
    """未使用 Schema を足しても Snapshot の `schema_set_hash` が動かないこと。"""
    used = [
        ("Conversation", "1.0.0", _H),
        ("ConversationMessage", "1.0.0", _H),
        ("ConversationSnapshot", "1.0.0", _H),
    ]
    baseline = schema_set_hash(used)
    catalog_grew = [*used]  # 使用集合は変えない
    assert schema_set_hash(catalog_grew) == baseline
    # 使った Schema が増えれば動く。**何も見ていない検査にしない。**
    assert schema_set_hash([*used, ("Run", "1.0.0", _H)]) != baseline


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


def _append_only_violations(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    """既存 Record の書き換えと削除を見つける。追記だけなら空を返す。"""
    old = {r["record_id"]: r for r in before}
    new = {r["record_id"]: r for r in after}
    violations = [f"DELETED:{rid}" for rid in sorted(set(old) - set(new))]
    violations += [f"UPDATED:{rid}" for rid in sorted(set(old) & set(new)) if old[rid] != new[rid]]
    return violations


def test_append_only_violation_is_detected() -> None:
    """書き換えと削除が検出できること。**追記は通ること。**"""
    before = [{"record_id": "a", "content_hash": _H}, {"record_id": "b", "content_hash": _H}]
    appended = [*before, {"record_id": "c", "content_hash": _H}]
    assert _append_only_violations(before, appended) == []

    updated = [{"record_id": "a", "content_hash": "sha256:" + "1" * 64}, before[1]]
    assert _append_only_violations(before, updated) == ["UPDATED:a"]

    deleted = [before[0]]
    assert _append_only_violations(before, deleted) == ["DELETED:b"]


def test_appending_a_message_does_not_rewrite_the_parent() -> None:
    """Message を足しても親 Record が動かないこと（CC-9-A / CC-10-B）。"""
    parent = {"record_id": "c-1", "conversation_id": "c-1", "conversation_hash": _H}
    before, after = [parent], [dict(parent)]
    assert _append_only_violations(before, after) == []
    # 親が子を列挙していたら、ここで親が動いてしまう。列挙していないことは
    # test_conversation_does_not_carry_message_ids が見ている。
    assert "message_ids" not in parent


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_mvp0a_counts_did_not_move() -> None:
    """Chat を足しても MVP0-A の判定条件が動いていないこと（CC-15-A）。"""
    scope = SNAPSHOT["scopes"]["MVP0-A"]
    assert scope["required_case_count"] == 108
    assert scope["required_test_id_count"] == 31
    assert SNAPSHOT["totals"]["gate_count"] == 45
    assert SNAPSHOT["totals"]["case_count"] == 128
    assert SNAPSHOT["release_enabled_phases"] == ["MVP0-A"]


def test_chat_added_no_events_errors_or_states() -> None:
    """Event・Error Code・State を勝手に足していないこと。"""
    totals = SNAPSHOT["totals"]
    assert totals["event_type_count"] == 79
    assert totals["error_code_count"] == 76
    assert totals["state_namespace_count"] == 27


def test_current_core_schema_totals_and_chat_membership() -> None:
    """公開版は現Registryと実Fileを照合する。過去Commitの増分実測とは区別する。"""
    schemas = yaml.safe_load((REGISTRIES / "schemas.yaml").read_text())["core_schemas"]
    names = {entry["schema_name"] for entry in schemas}
    versions = {
        (entry["schema_name"], v["version"]) for entry in schemas for v in entry["versions"]
    }
    assert len(names) == len(schemas), "Registry has duplicate logical schemas"
    assert SNAPSHOT["totals"]["core_schema_count"] == len(names)
    assert SNAPSHOT["totals"]["core_schema_version_count"] == len(versions)
    assert set(SNAPSHOT["core_schemas"]) == names
    assert set(CHAT_SCHEMAS) <= names
    for entry in schemas:
        for version in entry["versions"]:
            assert (REPO_ROOT / version["path"]).is_file()


def test_chat_section_lives_in_the_mvp0b_chapter() -> None:
    """Chat の契約が MVP0-B（第4章）に書かれていること（CC-14-A）。"""
    text = DESIGN.read_text(encoding="utf-8")
    section = text.index("## 4.11 Chat基盤のCore Schema")
    chapter5 = text.index("# 5. MVP0-C：External Provider Read-only 詳細設計")
    chapter4 = text.index("## 4.1 目的とOperator Surfaceの範囲")
    assert chapter4 < section < chapter5, "§4.11 が第4章の外にある"


def test_design_states_the_hash_relationship() -> None:
    """Hash の相互関係が設計本文に書かれていること。"""
    text = DESIGN.read_text(encoding="utf-8")
    body = text[text.index("## 4.11 Chat基盤のCore Schema") :][:6000]
    for term in ("content_hash", "conversation_hash", "message_set_hash", "snapshot_hash"):
        assert term in body, f"§4.11 に {term} の説明が無い"
