"""Chat Context Policy の解決（Owner Decision CP-1-A／CPM-1-A／CPM-2-A／CPM-3-A）。

正本は `design-source/registries/chat-context-policy.yaml` である。この Module は
その生成物 `_chat_context_policy_generated.py` を読んで **解決するだけ** であり、
priority も段も順序規則もここで決めない。

## domain 層に置く理由

`context_budget.py` の `ContextFragment` は「Domain で推測して写像を作らない。
`priority` は呼出側が与える明示値である」と書いている。**その文はいまも真である。**
ここが持つのは推測した写像ではなく、Hash 対象の正本から生成した写像である。

この Module は I/O を持たず、`sqlite3` も `os` も `subprocess` も import しない。
`select_context` も `ContextFragment` も変更していない。MVP0-A の他の呼出側は
この Module を import しないので、挙動は動かない。

## priority を発明しない

CPM-2-A は「信頼境界の 3 段で決める。具体値は 3 段が区別できれば足りる」と決めた。
段と Role の写像も、段の順位も Registry にある。ここには**数値が 1 つも無い**。

## 段だけでは順序が決まらない

段が同じ Message が複数あるとき、Domain の `select_context` は同点を
`fragment_id` 昇順で崩す。決定論ではあるが**会話の新しさと無関係**である。

CPM-3-A は「新しい Message ほど高い（`sequence_number` 降順）」と決めた。
`ContextFragment.priority` は整数 1 つなので、段と `sequence_number` の 2 段階を
**1 つの全順序**へ写す。写した結果は Message ごとに相異なるため、`fragment_id`
による同点崩しは起きない。**`fragment_id` 順を会話の優先順位として使わない。**

写した整数は Bundle Hash にも `decision_hash` にも入らない（どちらの射影も
`priority` を含まない）。順位だけが意味を持ち、絶対値は意味を持たない。

## 送る順は選ぶ順ではない

選択は新しい順に行い、Provider へ渡す最終列は `sequence_number` 昇順へ戻す
（`final_message_order`）。降順のまま送ると会話が逆さになる。

## Registry が変わったのに Code が追随していない状態で動かない

`intra_tier_order` と `final_message_order` は、この Module が実装している規則と
一致しなければならない。一致しなければ **Fail-Closed で止める**。知らない規則を
既定の並びで黙って代用しない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from harness.domain._chat_context_policy_generated import (
    CHAT_CONTEXT_POLICY_SOURCE_HASH,
    CHAT_CONTEXT_POLICY_VERSION,
    CONTROL_AUTHORITY_GRANT,
    FINAL_MESSAGE_ORDER,
    INTRA_TIER_ORDER,
    MANDATORY_PREREQUISITES,
    MESSAGE_ROLE_TIER,
    PROVIDER_OUTPUT_ROLE,
    TIER_MANDATORY_CAPABLE,
    TIER_PRIORITY,
    UNKNOWN_MESSAGE_ROLE,
    UNRESOLVED_SELECTION_INPUT,
    TrustTier,
)
from harness.domain.context_budget import MessageRole
from harness.domain.conversation import ConversationMessage
from harness.domain.errors import ErrorCode, HarnessError

__all__ = [
    "CHAT_CONTEXT_POLICY_SOURCE_HASH",
    "CHAT_CONTEXT_POLICY_VERSION",
    "CONTROL_AUTHORITY_GRANT",
    "FINAL_MESSAGE_ORDER",
    "INTRA_TIER_ORDER",
    "MANDATORY_PREREQUISITES",
    "PROVIDER_OUTPUT_ROLE",
    "ROLE_TIER",
    "UNKNOWN_MESSAGE_ROLE",
    "UNRESOLVED_SELECTION_INPUT",
    "TrustTier",
    "mandatory_capable",
    "selection_priorities",
    "send_order",
    "tier_for",
]

#: この Module が実装している規則。Registry 側がこれ以外を宣言したら止める。
_IMPLEMENTED_INTRA_TIER_ORDER: Final[str] = "SEQUENCE_NUMBER_DESC"
_IMPLEMENTED_FINAL_MESSAGE_ORDER: Final[str] = "SEQUENCE_NUMBER_ASC"


def _build_role_tier() -> Mapping[MessageRole, TrustTier]:
    """Registry の写像を `MessageRole` へ束ねる。

    **7 Role をちょうど 1 回ずつ覆っていなければ止める。** 覆えていない Role を
    既定の段へ落とすと、正本に無い方針が既定として固定される。
    """
    resolved: dict[MessageRole, TrustTier] = {}
    for name, tier in MESSAGE_ROLE_TIER.items():
        try:
            role = MessageRole(name)
        except ValueError as exc:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"chat-context-policy.yaml declares an unknown message role: {name}",
            ) from exc
        if role in resolved:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"chat-context-policy.yaml declares {name} more than once",
            )
        if tier not in TIER_PRIORITY or tier not in TIER_MANDATORY_CAPABLE:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"chat-context-policy.yaml maps {name} to an undeclared tier {tier.value}",
            )
        resolved[role] = tier
    missing = sorted(role.value for role in MessageRole if role not in resolved)
    if missing:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"chat-context-policy.yaml does not cover message roles: {', '.join(missing)}",
        )
    return resolved


def _require_implemented_orders() -> None:
    if INTRA_TIER_ORDER != _IMPLEMENTED_INTRA_TIER_ORDER:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"chat-context-policy.yaml requests intra_tier_order={INTRA_TIER_ORDER} "
            f"but this build implements {_IMPLEMENTED_INTRA_TIER_ORDER}",
        )
    if FINAL_MESSAGE_ORDER != _IMPLEMENTED_FINAL_MESSAGE_ORDER:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"chat-context-policy.yaml requests final_message_order={FINAL_MESSAGE_ORDER} "
            f"but this build implements {_IMPLEMENTED_FINAL_MESSAGE_ORDER}",
        )


_require_implemented_orders()

#: `MessageRole` から信頼境界の段への写像。正本は Registry。
ROLE_TIER: Final[Mapping[MessageRole, TrustTier]] = _build_role_tier()


def tier_for(role: MessageRole) -> TrustTier:
    """Role の段。未知 Role は既定へ落とさず止める（`unknown_message_role`）。"""
    tier = ROLE_TIER.get(role)
    if tier is None:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"no chat context policy tier for message role {role.value}",
        )
    return tier


def mandatory_capable(role: MessageRole) -> bool:
    """その Role が mandatory の**必要条件**を満たすか。

    **十分条件ではない。** §1.16.4 は Control Role へ `MANDATORY_PREREQUISITES` の
    検証済み Artifact を要求する。Chat にその検証経路は無い
    （`control_authority_grant: NONE`）。
    """
    return TIER_MANDATORY_CAPABLE[tier_for(role)]


def selection_priorities(messages: Sequence[ConversationMessage]) -> Mapping[str, int]:
    """段と `sequence_number` の 2 段階を、Domain へ渡せる整数 1 つへ写す。

    第 1 キーは段の順位（降順）、第 2 キーは `sequence_number`（降順、CPM-3-A）。
    戻り値は Message ごとに相異なる正の整数であり、大きいほど優先される。

    `sequence_number` が重複していれば順序が一意に決まらない。**推測して並べない。**
    """
    seen: dict[int, str] = {}
    for message in messages:
        duplicate = seen.get(message.sequence_number)
        if duplicate is not None:
            raise HarnessError(
                ErrorCode.PLAN_NONDETERMINISTIC,
                f"messages {duplicate} and {message.message_id} share "
                f"sequence_number {message.sequence_number}",
            )
        seen[message.sequence_number] = message.message_id

    ranked = sorted(
        messages,
        key=lambda m: (-TIER_PRIORITY[tier_for(m.role)], -m.sequence_number),
    )
    total = len(ranked)
    return {message.message_id: total - index for index, message in enumerate(ranked)}


def send_order(
    selected_message_ids: Sequence[str], messages: Sequence[ConversationMessage]
) -> tuple[str, ...]:
    """Provider へ渡す最終列。`sequence_number` 昇順（`final_message_order`）。

    選択順（新しい順）をそのまま送らない。採用されていない Message を混ぜない。
    知らない ID があれば止める。
    """
    by_id = {message.message_id: message for message in messages}
    unknown = sorted(set(selected_message_ids) - set(by_id))
    if unknown:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH,
            f"selected fragments are not in the conversation: {', '.join(unknown)}",
        )
    ordered = sorted(selected_message_ids, key=lambda mid: by_id[mid].sequence_number)
    return tuple(ordered)
