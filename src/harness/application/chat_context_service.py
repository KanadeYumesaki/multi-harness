"""`ConversationSnapshot` から Context を選ぶ Application Service（設計書 §3.6／§4.11）。

## 選択アルゴリズムを作り直さない

`domain/context_budget.py` の `select_context` と `build_context_bundle` が §3.6 を
既に実装している。ここがするのは **Snapshot と Message を Fragment へ写すこと**
だけである。Budget 判定も重複除去も除外理由も、既存の実装へ渡す。

## priority は正本から解決する。ここで決めない

Owner Decision `CP-1-A`／`CPM-1-A`／`CPM-2-A` により、Role 別の priority は
`design-source/registries/chat-context-policy.yaml` が正本である。写像も段の順位も
`chat_context_policy` が解決する。**この Module に数値は 1 つも無い。**

`CPM-3-A` により、段が同じ Message どうしは `sequence_number` 降順で優先する。
`fragment_id` 順を会話の優先順位として使わない。

## 送る順は選ぶ順ではない

選択は新しい順に行う。Provider へ渡す最終列は `sequence_number` 昇順へ戻す
（`final_message_order`）。`ContextBundle.ordered_fragment_ids` は **選択順** であり、
そのまま送ると会話が逆さになる。`ChatContextResult.send_order` が最終列である。

## mandatory にできる Role は絞る

Owner Decision `CP-2-A` により、`mandatory` にできるのは `SYSTEM_CONTROL` と
`DEVELOPER_CONTROL` だけである。許可集合も Registry から導く。

**段は必要条件であって十分条件ではない。** §1.16.4 は Control Role へ署名・
Policy Hash・Issuer・Expiry を検証できる Z0 Control Plane Artifact を要求する。
Chat にその検証経路は無い（`control_authority_grant: NONE`）。したがって
**いまの Chat はどの Message も mandatory にできない。** 権限を捏造して回避しない。

**Domain の `select_context` は触らない。** そこを変えると MVP0-A の他の呼出側まで
挙動が変わる。Chat の境界であるこの Service で拒否する。

## 未解決は送信ごと止める

Owner Decision `CP-3-B` により、`mandatory` が未指定の Message が 1 件でもあれば
**Chat 送信全体を拒否する**。黙って候補から外さない。既定値でも補完しない。
部分送信も Retry も Fallback も Queue 投入もしない。

判定は Artifact を読む**前**に全 Message へ通す。1 件でも未解決なら、本文を 1 件も
読まずに止まる。

## Token 計数を偽装しない

`TokenCounterPort` が返す `TokenCount` をそのまま使う。`len(text)` で代用しない。
`estimate_assurance` が `UNKNOWN` の Fragment は Fail-Closed で止める（§1.12）。

## 何も書き換えない

Snapshot も Conversation も Message も読むだけである。選択結果は Projection と
Receipt であり、保存しない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from harness.domain.chat_context_policy import (
    CONTROL_AUTHORITY_GRANT,
    MANDATORY_PREREQUISITES,
    mandatory_capable,
    selection_priorities,
    send_order,
)
from harness.domain.context_budget import (
    ContextAssembly,
    ContextFragment,
    EstimateAssurance,
    MessageRole,
    TokenBudgetPolicy,
    TokenProfileSnapshot,
    build_context_bundle,
)
from harness.domain.conversation import ConversationMessage
from harness.domain.conversation_snapshot import (
    ConversationSnapshot,
    derive_message_set_hash,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash
from harness.ports.artifact_store import ArtifactStorePort
from harness.ports.conversation_store import (
    ConversationMessageStorePort,
    ConversationStorePort,
)
from harness.ports.token_counter import TokenCounterPort

#: `mandatory` にできる Role（Owner Decision CP-2-A）。
#: **手入力しない。** 正本は `chat-context-policy.yaml` の段別 `mandatory_capable`。
MANDATORY_CAPABLE_ROLES: Final[frozenset[MessageRole]] = frozenset(
    role for role in MessageRole if mandatory_capable(role)
)

__all__ = [
    "MANDATORY_CAPABLE_ROLES",
    "ChatContextResult",
    "ChatContextService",
    "MessageSelectionInput",
]


@dataclass(frozen=True, slots=True)
class MessageSelectionInput:
    """1 件の Message に呼出側が与える選択方針。

    **既定値を持たない。** `mandatory` を明示させる。既定を置くと、正本に無い方針を
    黙って固定することになる。

    `priority` は持たない。Owner Decision `CP-1-A` により Role からの解決が正本で
    あり、呼出側が上書きできると Registry が正本でなくなる。
    """

    message_id: str
    mandatory: bool


@dataclass(frozen=True, slots=True)
class ChatContextResult:
    """Context の組立て結果と、Provider へ渡す最終 Message 列。

    `assembly.bundle.ordered_fragment_ids` は **選択順**（新しい順）である。
    `send_order` は `sequence_number` 昇順の **送信順** である。混同しない。
    """

    assembly: ContextAssembly
    send_order: tuple[str, ...]


class ChatContextService:
    def __init__(
        self,
        *,
        conversations: ConversationStorePort,
        messages: ConversationMessageStorePort,
        artifacts: ArtifactStorePort,
        token_counter: TokenCounterPort,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._artifacts = artifacts
        self._counter = token_counter

    def build_context(
        self,
        *,
        snapshot: ConversationSnapshot,
        policy: TokenBudgetPolicy,
        profile: TokenProfileSnapshot,
        selection: Mapping[str, MessageSelectionInput],
        bundle_id: str,
        receipt_id: str,
        input_read_capability_set_hash: ContentHash,
        input_read_evidence_hash: ContentHash,
        now: str,
    ) -> ChatContextResult:
        """Snapshot の Message 集合から Context を組む。

        Budget 超過・必須不足は `build_context_bundle` が既存 Code で止める。
        ここで握り潰さず、Retry も Fallback も Queue もしない。
        """
        # UNKNOWN と期限切れは既にある検査が見る。条件を書き直さない（§1.12）。
        profile.require_usable(now=now)
        messages = self._messages.list_for_conversation(snapshot.conversation_id)
        self._require_snapshot_matches(snapshot, messages)
        # 本文を読む前に全件見る。1 件でも未解決なら I/O を起こさずに止まる。
        self._require_resolved_selection(messages, selection)

        fragments = self._to_fragments(messages, selection, profile)
        # Budget 判定・重複除去・除外理由は既存実装へ渡す。作り直さない。
        assembly = build_context_bundle(
            fragments,
            policy,
            profile,
            bundle_id=bundle_id,
            receipt_id=receipt_id,
            input_read_capability_set_hash=input_read_capability_set_hash,
            input_read_evidence_hash=input_read_evidence_hash,
        )
        # 選択順のまま送らない。会話の順へ戻す（`final_message_order`）。
        return ChatContextResult(
            assembly=assembly,
            send_order=send_order(assembly.bundle.ordered_fragment_ids, messages),
        )

    def _require_snapshot_matches(
        self, snapshot: ConversationSnapshot, messages: Sequence[ConversationMessage]
    ) -> None:
        """Snapshot が指す Message 集合と、いま読めた集合が同じであること。

        食い違えば **推測で進めず止める**。Snapshot は過去の集合を指しており、
        いまの集合で組んだ Context は別物である。
        """
        measured = derive_message_set_hash([m.content_hash for m in messages])
        if measured != snapshot.message_set_hash:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"snapshot {snapshot.snapshot_id} does not match the current message set",
            )

    def _require_resolved_selection(
        self,
        messages: Sequence[ConversationMessage],
        selection: Mapping[str, MessageSelectionInput],
    ) -> None:
        """`CP-3-B`。1 件でも未解決なら送信全体を拒否する。

        **部分送信にしない。** 未解決の Message を黙って候補から外さない。
        """
        for message in messages:
            plan = selection.get(message.message_id)
            if plan is None:
                # 方針を与えられていない Message を既定で拾わない。
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                    f"no selection input for message {message.message_id}; "
                    "mandatory must be supplied explicitly",
                )
            if not plan.mandatory:
                continue
            if message.role not in MANDATORY_CAPABLE_ROLES:
                # Owner Decision CP-2-A。Untrusted な入力を必須 Context にしない。
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                    f"message {message.message_id} has role {message.role.value} "
                    "which cannot be marked mandatory",
                )
            # 段は必要条件でしかない。§1.16.4 の検証済み Artifact が無ければ
            # Control Role でも mandatory にしない。権限を捏造しない。
            raise HarnessError(
                ErrorCode.CONTROL_DATA_ROLE_ESCALATION,
                f"message {message.message_id} cannot be mandatory without a verified "
                f"control authority artifact ({', '.join(MANDATORY_PREREQUISITES)}); "
                f"chat grants none (control_authority_grant={CONTROL_AUTHORITY_GRANT})",
            )
        known = {message.message_id for message in messages}
        extra = sorted(set(selection) - known)
        if extra:
            # 会話に無い Message の方針を受け取ったら、指している集合が違う。
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                f"selection input for messages outside the conversation: {', '.join(extra)}",
            )

    def _to_fragments(
        self,
        messages: Sequence[ConversationMessage],
        selection: Mapping[str, MessageSelectionInput],
        profile: TokenProfileSnapshot,
    ) -> tuple[ContextFragment, ...]:
        # 段と `sequence_number` 降順を 1 つの全順序へ写す。未知 Role はここで止まる。
        priorities = selection_priorities(messages)
        fragments: list[ContextFragment] = []
        for message in messages:
            plan = selection[message.message_id]
            body = self._artifacts.get(message.content_artifact_hash)
            try:
                # §1.11「Text Artifact の論理正規化は LF」。UTF-8 以外は扱わない。
                decoded = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                # 置換文字で読み替えると、数えた対象と保存した対象が別物になる。
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                    f"message {message.message_id} body is not valid UTF-8",
                ) from exc
            count = self._counter.count(decoded, profile=profile)
            if count.estimate_assurance is EstimateAssurance.UNKNOWN:
                # §1.12「UNKNOWN は停止条件」。上界を保証できない値で予算を組まない。
                raise HarnessError(
                    ErrorCode.TOKEN_PROFILE_DRIFT_DETECTED,
                    f"token count for message {message.message_id} is UNKNOWN; "
                    "cannot bound the context budget",
                )
            fragments.append(
                ContextFragment(
                    fragment_id=message.message_id,
                    fragment_content_hash=message.content_artifact_hash,
                    token_count=count.tokens,
                    message_role=message.role,
                    # Provider 出力を Control Role へ昇格させない。Role は Message が
                    # 持つ値をそのまま使い、権限は与えない。
                    control_authority=False,
                    instruction_eligible=False,
                    mandatory=plan.mandatory,
                    priority=priorities[message.message_id],
                )
            )
        return tuple(fragments)
