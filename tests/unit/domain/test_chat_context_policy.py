"""Chat Context Policy の試験（Owner Decision CP-1-A／CPM-1-A／CPM-2-A／CPM-3-A）。

## 何を測っているか

* 7 Role が**漏れなく 1 回ずつ**正本にあること
* 3 段の区分が `_CONTROL_ROLES`／`_UNTRUSTED_ROLES` から導けること（CPM-2-A）
* Untrusted Role を `mandatory` へ昇格できないこと
* 未知 Role・未設定を**既定値で補完しない**こと
* 段が同じなら `sequence_number` 降順で優先されること（CPM-3-A）
* 最終列が `sequence_number` 昇順であること
* Code へ priority 数値を手入力していないこと

## 通ることではなく通らないことを測る

Registry を壊した写しを生成器へ渡し、**止まること**を確かめる。止まらない検査は
検査ではない。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain import chat_context_policy as policy
from harness.domain._chat_context_policy_generated import TrustTier
from harness.domain.context_budget import (
    _CONTROL_ROLES,
    _UNTRUSTED_ROLES,
    MessageRole,
)
from harness.domain.conversation import ConversationMessage, derive_message_content_hash
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRIES = REPO_ROOT / "design-source/registries"
POLICY_YAML = REGISTRIES / "chat-context-policy.yaml"
GENERATOR = REPO_ROOT / "tools/generate_chat_context_policy_code.py"
GENERATED = REPO_ROOT / "src/harness/domain/_chat_context_policy_generated.py"
_H = ContentHash.parse("sha256:" + "0" * 64)


def _raw_policy() -> dict[str, Any]:
    return dict(yaml.safe_load(POLICY_YAML.read_text(encoding="utf-8")))


def _render(registries: Path) -> str:
    """生成器の `render` を import して呼ぶ。Script 実行に依存しない。"""
    spec = importlib.util.spec_from_file_location("_gen_chat_policy", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return str(module.render(registries, REPO_ROOT))


def _mutated_registries(tmp_path: Path, mutate: Any) -> Path:
    """正本を写して壊す。**正本そのものは触らない。**"""
    target = tmp_path / "registries"
    target.mkdir()
    shutil.copy(REGISTRIES / "schemas.yaml", target / "schemas.yaml")
    document = _raw_policy()
    mutate(document)
    (target / "chat-context-policy.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


def _message(*, sequence_number: int, role: MessageRole, message_id: str) -> ConversationMessage:
    return ConversationMessage(
        message_id=message_id,
        conversation_id="c1",
        role=role,
        sequence_number=sequence_number,
        content_artifact_hash=_H,
        record_id="r",
        created_at="2026-08-30T00:00:00Z",
        producer="test",
        content_hash=derive_message_content_hash(
            content_artifact_hash=_H, role=role, sequence_number=sequence_number
        ),
    )


# ---------------------------------------------------------------------------
# 正本の在処（CP-1-A / CPM-1-A）
# ---------------------------------------------------------------------------


def test_policy_lives_in_the_registry_directory() -> None:
    """Chat の Mapping が Registry の正本 Directory にあること（CPM-1-A）。"""
    assert POLICY_YAML.is_file()
    assert POLICY_YAML.parent == REGISTRIES


def test_policy_is_bound_to_the_registry_snapshot_hash() -> None:
    """Registry Snapshot が Policy File を Hash 対象にしていること。

    ここに入っていなければ、改変しても `registry_snapshot_hash` が動かない。
    """
    snapshot = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    sources = snapshot["source_registry_hashes"]
    assert "chat-context-policy.yaml" in sources, sorted(sources)
    measured = "sha256:" + hashlib.sha256(POLICY_YAML.read_bytes()).hexdigest()
    assert sources["chat-context-policy.yaml"] == measured


def test_generated_code_is_up_to_date() -> None:
    """Registry を編集して再生成を忘れた状態を検出すること。"""
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, str(GENERATOR), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_generated_source_hash_matches_the_registry() -> None:
    """生成物が名乗る Source Hash が正本の実測と一致すること。"""
    measured = "sha256:" + hashlib.sha256(POLICY_YAML.read_bytes()).hexdigest()
    assert policy.CHAT_CONTEXT_POLICY_SOURCE_HASH == measured


def test_generation_is_deterministic() -> None:
    assert _render(REGISTRIES) == _render(REGISTRIES)


# ---------------------------------------------------------------------------
# 3 段の区分（CPM-2-A）
# ---------------------------------------------------------------------------


def test_every_message_role_is_mapped_exactly_once() -> None:
    """7 Role が漏れなく 1 回ずつ。**未知 Role を既定の段へ落とさない。**"""
    document = _raw_policy()
    listed = [row["role"] for row in document["message_role_tiers"]]
    assert len(listed) == len(MessageRole)
    assert sorted(listed) == sorted(role.value for role in MessageRole)
    assert len(set(listed)) == len(listed)
    assert set(policy.ROLE_TIER) == set(MessageRole)


def test_tiers_are_derived_from_the_existing_trust_boundary() -> None:
    """段の区分が `_CONTROL_ROLES`／`_UNTRUSTED_ROLES` と一致すること（CPM-2-A）。

    **新しい語彙も区分も作っていない**ことの根拠である。中間段は残り物であり、
    ここでも別途定義していない。
    """
    by_tier: dict[TrustTier, set[MessageRole]] = {}
    for role, tier in policy.ROLE_TIER.items():
        by_tier.setdefault(tier, set()).add(role)
    assert by_tier[TrustTier.CONTROL] == set(_CONTROL_ROLES)
    assert by_tier[TrustTier.UNTRUSTED] == set(_UNTRUSTED_ROLES)
    assert by_tier[TrustTier.INTERMEDIATE] == set(MessageRole) - set(_CONTROL_ROLES) - set(
        _UNTRUSTED_ROLES
    )
    assert len(by_tier[TrustTier.CONTROL]) == 2
    assert len(by_tier[TrustTier.INTERMEDIATE]) == 3
    assert len(by_tier[TrustTier.UNTRUSTED]) == 2


def test_the_three_tiers_are_distinguishable_and_ordered() -> None:
    """3 段が区別でき、信頼の高い段ほど priority が大きいこと。

    CPM-2-A は「具体値は 3 段が区別できれば足りる」と決めた。**大小だけを測る。**
    絶対値を試験へ書くと、正本ではなく試験が値を決めてしまう。
    """
    from harness.domain._chat_context_policy_generated import TIER_PRIORITY

    assert len(set(TIER_PRIORITY.values())) == 3
    assert (
        TIER_PRIORITY[TrustTier.CONTROL]
        > TIER_PRIORITY[TrustTier.INTERMEDIATE]
        > TIER_PRIORITY[TrustTier.UNTRUSTED]
    )


def test_no_priority_number_is_hand_written_in_the_code() -> None:
    """解決 Module に priority 数値を手入力していないこと。

    正本は Registry である。Code に数値が現れたら、そちらが二重の正本になる。
    """
    import ast

    source = Path(policy.__file__).read_text(encoding="utf-8")
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, int)
        if not isinstance(node.value, bool)
    }
    assert literals <= {0, 1}, f"priority 数値が Code へ入り込んでいる: {sorted(literals)}"


# ---------------------------------------------------------------------------
# mandatory の境界（CP-2-A）
# ---------------------------------------------------------------------------


def test_only_control_roles_are_mandatory_capable() -> None:
    capable = {role for role in MessageRole if policy.mandatory_capable(role)}
    assert capable == set(_CONTROL_ROLES)


@pytest.mark.parametrize("role", sorted(_UNTRUSTED_ROLES, key=lambda r: r.value))
def test_untrusted_roles_cannot_be_promoted_to_mandatory(role: MessageRole) -> None:
    """**Provider 出力を必須 Context へ昇格させない。**"""
    assert policy.mandatory_capable(role) is False
    assert policy.tier_for(role) is TrustTier.UNTRUSTED


def test_chat_grants_no_control_authority() -> None:
    """Chat に Control Authority の付与経路が無いこと。

    Role だけを根拠に権限を与えない。§1.16.4 の 4 検証が要る。
    """
    assert policy.CONTROL_AUTHORITY_GRANT == "NONE"
    assert set(policy.MANDATORY_PREREQUISITES) == {
        "VERIFIED_SIGNATURE",
        "VERIFIED_POLICY_HASH",
        "VERIFIED_ISSUER",
        "VERIFIED_EXPIRY",
    }


def test_provider_output_role_stays_untrusted() -> None:
    assert policy.PROVIDER_OUTPUT_ROLE == MessageRole.UNTRUSTED_PROVIDER_DATA.value
    assert policy.tier_for(MessageRole.UNTRUSTED_PROVIDER_DATA) is TrustTier.UNTRUSTED


def test_unresolved_input_is_rejected_not_defaulted() -> None:
    """未解決は既定値で補完せず送信ごと拒否すること（CP-3-B）。"""
    assert policy.UNRESOLVED_SELECTION_INPUT == "REJECT_SEND"
    assert policy.UNKNOWN_MESSAGE_ROLE == "REJECT_SEND"


# ---------------------------------------------------------------------------
# 順序（CPM-3-A）
# ---------------------------------------------------------------------------


def test_same_tier_ranks_newer_messages_higher() -> None:
    messages = [
        _message(sequence_number=1, role=MessageRole.USER_TASK, message_id="zzz-old"),
        _message(sequence_number=2, role=MessageRole.USER_TASK, message_id="aaa-new"),
    ]
    priorities = policy.selection_priorities(messages)
    assert priorities["aaa-new"] > priorities["zzz-old"], "新しい Message が下になっている"


def test_tier_outranks_recency() -> None:
    """段が第 1 キーであること。**新しい Untrusted が古い中間を上回らない。**"""
    messages = [
        _message(sequence_number=1, role=MessageRole.USER_TASK, message_id="m1"),
        _message(sequence_number=9, role=MessageRole.UNTRUSTED_PROVIDER_DATA, message_id="m9"),
    ]
    priorities = policy.selection_priorities(messages)
    assert priorities["m1"] > priorities["m9"]


def test_priorities_are_distinct_so_fragment_id_never_decides() -> None:
    """写した整数が Message ごとに相異なること。

    同点が残ると Domain の `fragment_id` 昇順が効いてしまう。**`fragment_id` 順を
    会話の優先順位として使わない。**
    """
    messages = [
        _message(sequence_number=index, role=MessageRole.USER_TASK, message_id=f"m{index}")
        for index in range(1, 6)
    ]
    priorities = policy.selection_priorities(messages)
    assert len(set(priorities.values())) == len(messages)


def test_duplicate_sequence_numbers_are_rejected() -> None:
    """順序が一意に決まらない入力で並べないこと。"""
    messages = [
        _message(sequence_number=1, role=MessageRole.USER_TASK, message_id="a"),
        _message(sequence_number=1, role=MessageRole.USER_TASK, message_id="b"),
    ]
    with pytest.raises(HarnessError) as caught:
        policy.selection_priorities(messages)
    assert caught.value.code is ErrorCode.PLAN_NONDETERMINISTIC


def test_send_order_is_sequence_ascending() -> None:
    messages = [
        _message(sequence_number=index, role=MessageRole.USER_TASK, message_id=f"m{index}")
        for index in (1, 2, 3)
    ]
    # 選択順（新しい順）を渡しても、返るのは会話の順である。
    assert policy.send_order(["m3", "m1", "m2"], messages) == ("m1", "m2", "m3")


def test_send_order_rejects_ids_outside_the_conversation() -> None:
    messages = [_message(sequence_number=1, role=MessageRole.USER_TASK, message_id="m1")]
    with pytest.raises(HarnessError) as caught:
        policy.send_order(["m1", "ghost"], messages)
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


# ---------------------------------------------------------------------------
# Registry を壊したら止まること
# ---------------------------------------------------------------------------


def test_generator_rejects_a_missing_role(tmp_path: Path) -> None:
    def drop_one(document: dict[str, Any]) -> None:
        document["message_role_tiers"] = document["message_role_tiers"][:-1]

    with pytest.raises(SystemExit) as caught:
        _render(_mutated_registries(tmp_path, drop_one))
    assert "message_role_tiers に無い Role" in str(caught.value)


def test_generator_rejects_an_invented_role(tmp_path: Path) -> None:
    def invent(document: dict[str, Any]) -> None:
        document["message_role_tiers"].append({"role": "ASSISTANT", "tier": "CONTROL"})

    with pytest.raises(SystemExit) as caught:
        _render(_mutated_registries(tmp_path, invent))
    assert "§1.16.4 に無い role" in str(caught.value)


def test_generator_rejects_indistinguishable_tiers(tmp_path: Path) -> None:
    """3 段が同じ値なら CPM-2-A の「3 段」が成立しない。"""

    def flatten(document: dict[str, Any]) -> None:
        for tier in document["trust_tiers"]:
            tier["priority"] = 1

    with pytest.raises(SystemExit) as caught:
        _render(_mutated_registries(tmp_path, flatten))
    assert "priority が重複" in str(caught.value)


def test_generator_rejects_mandatory_capable_untrusted(tmp_path: Path) -> None:
    """Untrusted 段を mandatory 可にした Registry を通さないこと。"""

    def promote(document: dict[str, Any]) -> None:
        for tier in document["trust_tiers"]:
            tier["mandatory_capable"] = tier["id"] != "CONTROL"

    with pytest.raises(SystemExit) as caught:
        _render(_mutated_registries(tmp_path, promote))
    assert "mandatory_capable" in str(caught.value)


def test_generator_rejects_a_granted_control_authority(tmp_path: Path) -> None:
    """Chat が Control Authority を与えると書いた Registry を通さないこと。"""

    def grant(document: dict[str, Any]) -> None:
        document["control_authority_grant"] = "ROLE_ONLY"

    with pytest.raises(SystemExit) as caught:
        _render(_mutated_registries(tmp_path, grant))
    assert "control_authority_grant" in str(caught.value)


def test_generator_rejects_a_missing_scalar(tmp_path: Path) -> None:
    """未設定を既定値で補完しないこと。"""

    def remove(document: dict[str, Any]) -> None:
        del document["intra_tier_order"]

    with pytest.raises(SystemExit) as caught:
        _render(_mutated_registries(tmp_path, remove))
    assert "intra_tier_order" in str(caught.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("INTRA_TIER_ORDER", "SEQUENCE_NUMBER_ASC"),
        ("FINAL_MESSAGE_ORDER", "SEQUENCE_NUMBER_DESC"),
    ],
)
def test_resolver_stops_when_the_registry_asks_for_an_unimplemented_order(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """Registry が知らない順序規則を宣言したら、既定の並びで代用せず止めること。

    ここを素通りさせると、Registry だけ書き換えたのに挙動が変わらない状態になる。
    正本を直したつもりで直っていない、がいちばん危ない。
    """
    monkeypatch.setattr(policy, name, value)
    with pytest.raises(HarnessError) as caught:
        policy._require_implemented_orders()
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert value in str(caught.value)


def test_tier_for_fails_closed_on_a_role_outside_the_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写像に無い Role を既定の段へ落とさず止めること（`unknown_message_role`）。

    起動時の検査が 7 Role を覆うので、この枝は通常の呼出しでは通らない。だからと
    いって測らなければ、既定へ落とす実装へ書き換えても誰も気付かない。
    """
    partial = {
        role: tier for role, tier in policy.ROLE_TIER.items() if role is not MessageRole.USER_TASK
    }
    monkeypatch.setattr(policy, "ROLE_TIER", partial)
    with pytest.raises(HarnessError) as caught:
        policy.tier_for(MessageRole.USER_TASK)
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert MessageRole.USER_TASK.value in str(caught.value)


def test_mandatory_capable_fails_closed_on_a_role_outside_the_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """段が引けない Role を「mandatory 不可」で黙って通さず、止めること。

    False を返して済ませると、写像の欠落が **拒否と同じ見た目**になる。
    """
    partial = {
        role: tier
        for role, tier in policy.ROLE_TIER.items()
        if role is not MessageRole.SYSTEM_CONTROL
    }
    monkeypatch.setattr(policy, "ROLE_TIER", partial)
    with pytest.raises(HarnessError) as caught:
        policy.mandatory_capable(MessageRole.SYSTEM_CONTROL)
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_resolver_stops_when_a_role_is_missing_from_the_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写像が 1 Role でも欠けたら、既定の段へ落とさず止めること。"""
    partial = {
        name: tier
        for name, tier in policy.MESSAGE_ROLE_TIER.items()
        if name != MessageRole.USER_TASK.value
    }
    monkeypatch.setattr(policy, "MESSAGE_ROLE_TIER", partial)
    with pytest.raises(HarnessError) as caught:
        policy._build_role_tier()
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert MessageRole.USER_TASK.value in str(caught.value)


def test_resolver_stops_on_a_role_outside_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§1.16.4 に無い role 名を写像へ入れられないこと。"""
    extended = dict(policy.MESSAGE_ROLE_TIER)
    extended["ASSISTANT"] = TrustTier.CONTROL
    monkeypatch.setattr(policy, "MESSAGE_ROLE_TIER", extended)
    with pytest.raises(HarnessError) as caught:
        policy._build_role_tier()
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert "ASSISTANT" in str(caught.value)
