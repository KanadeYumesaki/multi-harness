"""Snapshot から Context を選ぶ経路の試験（設計書 §3.6／§4.11）。

選択アルゴリズムは `domain/context_budget.py` が既に実装している。ここで測るのは
**橋渡しが契約どおりか**である。Budget 判定そのものは既存 Domain の試験が見ている。

## 何を測っているか

* Snapshot の Message 集合から `ContextBundle` と Receipt が組めること
* Token 計数が決定論で、`len(text)` の近似で偽装されていないこと
* Budget 超過・必須不足・UNKNOWN / 期限切れ Profile で **止まる** こと
* Retry / Fallback / Queue が無いこと
* Snapshot・Message・Conversation を書き換えないこと
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from chat.test_conversation_store import _Cas, _Gate, _Uow
from harness.application.chat_context_service import (
    ChatContextService,
    MessageSelectionInput,
)
from harness.application.conversation_service import ConversationService
from harness.domain.context_budget import (
    EstimateAssurance,
    ExclusionReason,
    MessageRole,
    TokenBudgetPolicy,
    TokenOverheads,
    TokenProfileSnapshot,
)
from harness.domain.conversation_snapshot import ConversationSnapshot
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes
from harness.infrastructure.schema.registry import CoreSchemaRegistry
from harness.infrastructure.sqlite.conversation_repository import (
    SqliteConversationMessageRepository,
    SqliteConversationRepository,
    SqliteConversationSnapshotRepository,
)
from harness.ports.token_counter import TokenCount

REPO_ROOT = Path(__file__).resolve().parents[3]
_TS = "2026-08-28T00:00:00Z"
_NOW = "2026-08-28T00:00:00Z"
_PRODUCER = "test"
_H = ContentHash.parse("sha256:" + "0" * 64)


class _Counter:
    """決定論的な Token 計数器の代役。

    **`len(text)` の近似ではない。** 語で数え、どの Tokenizer が数えたかを名乗る。
    同じ入力から常に同じ値を返す。
    """

    def __init__(self, assurance: EstimateAssurance = EstimateAssurance.EXACT) -> None:
        self._assurance = assurance
        self.calls = 0

    def count(self, text: str, *, profile: TokenProfileSnapshot) -> TokenCount:
        self.calls += 1
        return TokenCount(
            tokens=len(text.split()),
            tokenizer_name="test-tokenizer",
            tokenizer_version="1",
            counting_adapter_version="1",
            estimate_assurance=self._assurance,
        )


def _profile(
    *,
    assurance: EstimateAssurance = EstimateAssurance.EXACT,
    expires_at: str = "2027-01-01T00:00:00Z",
    context_limit: int = 10_000,
) -> TokenProfileSnapshot:
    return TokenProfileSnapshot(
        snapshot_id="tp-1",
        provider="mock",
        model="mock-1",
        tokenizer_name="test-tokenizer",
        tokenizer_version="1",
        counting_adapter_version="1",
        context_limit=context_limit,
        maximum_output_limit=1_000,
        estimate_assurance=assurance,
        overheads=TokenOverheads(
            system_message_overhead=0,
            developer_message_overhead=0,
            tool_definition_overhead=0,
            per_message_overhead=0,
            structured_output_overhead=0,
            streaming_frame_overhead=0,
            retry_fallback_reservation=0,
        ),
        retrieved_at=_TS,
        expires_at=expires_at,
        vocabulary_hash=_H,
    )


def _policy(*, total: int = 1_000) -> TokenBudgetPolicy:
    return TokenBudgetPolicy(
        total_tokens=total,
        reserved_output_tokens=0,
        reserved_tool_tokens=0,
        safety_margin_tokens=0,
        compression_max_depth=0,
        overflow_policy="FAIL_CLOSED",
        policy_id="bp-1",
    )


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    from harness.infrastructure.sqlite.migrations import _MIGRATION_0006, _MIGRATION_0007

    conn = sqlite3.connect(tmp_path / "chat.db", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    for statement in (*_MIGRATION_0006, *_MIGRATION_0007):
        conn.execute(statement)
    return conn


@pytest.fixture
def design_hash() -> ContentHash:
    import json

    snapshot = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    design = REPO_ROOT / f"design-v{snapshot['design_version']}-runtime-go.md"
    return hash_bytes(design.read_bytes())


@pytest.fixture
def world(
    connection: sqlite3.Connection, design_hash: ContentHash
) -> tuple[ConversationService, _Cas, Any]:
    cas = _Cas()
    store = ConversationService(
        unit_of_work=_Uow(connection),
        conversations=SqliteConversationRepository(connection),
        messages=SqliteConversationMessageRepository(connection),
        artifacts=cas,
        masking_gate=_Gate(),
        snapshots=SqliteConversationSnapshotRepository(connection),
        schema_catalog=CoreSchemaRegistry.bundled(),
        design_sha256=design_hash,
    )
    return store, cas, connection


def _context_service(
    connection: sqlite3.Connection, cas: _Cas, counter: _Counter | None = None
) -> ChatContextService:
    return ChatContextService(
        conversations=SqliteConversationRepository(connection),
        messages=SqliteConversationMessageRepository(connection),
        artifacts=cas,
        token_counter=counter or _Counter(),
    )


def _seed(
    store: ConversationService, design_hash: ContentHash, bodies: list[bytes]
) -> tuple[ConversationSnapshot, list[str]]:
    conversation = store.create_conversation(
        conversation_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
    )
    ids: list[str] = []
    for body in bodies:
        stored = store.append_message(
            conversation_id=conversation.conversation_id,
            role=__import__(
                "harness.domain.context_budget", fromlist=["MessageRole"]
            ).MessageRole.USER_TASK,
            body=body,
            message_id=str(uuid.uuid4()),
            record_id=str(uuid.uuid4()),
            created_at=_TS,
            producer=_PRODUCER,
            artifact_id=str(uuid.uuid4()),
        )
        ids.append(stored.message.message_id)
    snapshot = store.build_snapshot(
        conversation_id=conversation.conversation_id,
        snapshot_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
        design_sha256=design_hash,
    )
    return snapshot, ids


def _selection(ids: list[str], *, mandatory: set[str] | None = None) -> dict[str, Any]:
    """呼出側が明示する選択方針。**Service は既定を持たない。**

    `priority` は渡さない。Owner Decision `CP-1-A` により Role からの解決が正本で
    あり、呼出側は上書きできない。
    """
    mandatory = mandatory or set()
    return {mid: MessageSelectionInput(message_id=mid, mandatory=mid in mandatory) for mid in ids}


def _build(
    service: ChatContextService,
    snapshot: ConversationSnapshot,
    ids: list[str],
    *,
    policy: TokenBudgetPolicy | None = None,
    profile: TokenProfileSnapshot | None = None,
    mandatory: set[str] | None = None,
    now: str = _NOW,
) -> Any:
    """組立て結果だけを返す。最終送信列を見る試験は `_build_result` を使う。"""
    return _build_result(
        service,
        snapshot,
        ids,
        policy=policy,
        profile=profile,
        mandatory=mandatory,
        now=now,
    ).assembly


def _build_result(
    service: ChatContextService,
    snapshot: ConversationSnapshot,
    ids: list[str],
    *,
    policy: TokenBudgetPolicy | None = None,
    profile: TokenProfileSnapshot | None = None,
    mandatory: set[str] | None = None,
    now: str = _NOW,
) -> Any:
    return service.build_context(
        snapshot=snapshot,
        policy=policy or _policy(),
        profile=profile or _profile(),
        selection=_selection(ids, mandatory=mandatory),
        bundle_id=str(uuid.uuid4()),
        receipt_id=str(uuid.uuid4()),
        input_read_capability_set_hash=_H,
        input_read_evidence_hash=_H,
        now=now,
    )


# ---------------------------------------------------------------------------
# 再構築
# ---------------------------------------------------------------------------


def test_context_bundle_is_rebuilt_from_the_snapshot(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"one two", b"three four five"])
    assembly = _build(_context_service(connection, cas), snapshot, ids)
    assert set(assembly.bundle.ordered_fragment_ids) == set(ids)
    assert assembly.receipt.estimated_token_total == 5


def test_empty_conversation_produces_an_empty_bundle(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [])
    assembly = _build(_context_service(connection, cas), snapshot, ids)
    assert assembly.bundle.ordered_fragment_ids == ()
    assert assembly.receipt.estimated_token_total == 0


def test_token_count_is_not_len_of_text(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """計数が文字数の近似で偽装されていないこと。"""
    store, cas, connection = world
    body = b"alpha beta gamma"
    snapshot, ids = _seed(store, design_hash, [body])
    assembly = _build(_context_service(connection, cas), snapshot, ids)
    assert assembly.receipt.estimated_token_total == 3
    assert assembly.receipt.estimated_token_total != len(body)


def test_counting_is_deterministic(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a b", b"c d e"])
    service = _context_service(connection, cas)

    def run() -> Any:
        # ID は固定する。`decision_hash` は `CP-4-A` により ID を入力に取らないが、
        # ここで測りたいのは計数の決定論なので、他の入力も動かさない。
        return service.build_context(
            snapshot=snapshot,
            policy=_policy(),
            profile=_profile(),
            selection=_selection(ids),
            bundle_id="fixed-bundle",
            receipt_id="fixed-receipt",
            input_read_capability_set_hash=_H,
            input_read_evidence_hash=_H,
            now=_NOW,
        ).assembly

    first, second = run(), run()
    assert first.receipt.estimated_token_total == second.receipt.estimated_token_total
    assert first.bundle.ordered_fragment_ids == second.bundle.ordered_fragment_ids
    assert first.receipt.decision_hash == second.receipt.decision_hash
    assert first.bundle.bundle_hash == second.bundle.bundle_hash


def test_bundle_hash_is_stable_for_the_same_inputs(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """同じ Snapshot + Policy + Profile から同じ選択になること。

    `bundle_id` と `receipt_id` は毎回違うので、Hash ではなく **選択と計数**が
    一致することを見る。
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"x y", b"z"])
    service = _context_service(connection, cas)
    a, b = _build(service, snapshot, ids), _build(service, snapshot, ids)
    assert a.bundle.ordered_fragment_ids == b.bundle.ordered_fragment_ids
    assert a.bundle.total_token_count == b.bundle.total_token_count
    assert a.receipt.excluded_fragments == b.receipt.excluded_fragments


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_selection_fits_within_the_budget(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a b c", b"d e f", b"g h i"])
    assembly = _build(_context_service(connection, cas), snapshot, ids, policy=_policy(total=6))
    assert assembly.bundle.total_token_count <= 6
    assert len(assembly.bundle.ordered_fragment_ids) == 2


def test_over_budget_optional_fragments_are_excluded_with_a_reason(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """予算に入らない任意 Fragment は **0 件で隠さず** 除外理由を付ける。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a b c", b"d e f"])
    assembly = _build(_context_service(connection, cas), snapshot, ids, policy=_policy(total=3))
    excluded = assembly.receipt.excluded_fragments
    assert len(excluded) == 1
    assert excluded[0].reason is ExclusionReason.BUDGET
    # 除外を空配列で隠していない
    assert set(assembly.bundle.ordered_fragment_ids) | {
        item.fragment_id for item in excluded
    } == set(ids)


def test_optional_over_budget_is_excluded_not_raised(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """任意 Fragment が予算に入らないときは、止めずに除外へ載せること。

    必須 Fragment の予算超過（`CONTEXT_BUDGET_EXCEEDED`）は Domain の試験が見ている。
    Chat からその経路へは入れない。理由は
    `test_chat_cannot_currently_mark_any_message_mandatory` に書いた。
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a b c", b"d e f"])
    assembly = _build(_context_service(connection, cas), snapshot, ids, policy=_policy(total=3))
    assert len(assembly.receipt.excluded_fragments) == 1
    assert assembly.receipt.excluded_fragments[0].reason is ExclusionReason.BUDGET


# ---------------------------------------------------------------------------
# CP-2-A: mandatory にできる Role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_name", ["SYSTEM_CONTROL", "DEVELOPER_CONTROL"])
def test_control_role_messages_are_rejected_without_verified_authority(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash, role_name: str
) -> None:
    """Control Role の Chat Message は、検証済み権限が無いので拒否されること。

    §1.16.4 は、Control Role へ昇格できるのは Z0 Control Plane が生成し署名・
    Policy Hash・Issuer・Expiry を検証できる Artifact だけだと定める。Chat には
    その検証経路が無い。Service は `control_authority=False` を渡す（**権限を
    捏造しない**）ので、Domain が `CONTROL_DATA_ROLE_ESCALATION` で止める。
    """
    from harness.domain.context_budget import MessageRole

    store, cas, connection = world
    snapshot, ids = _seed_with_role(store, design_hash, MessageRole[role_name], b"a")
    with pytest.raises(HarnessError) as caught:
        _build(_context_service(connection, cas), snapshot, ids)
    assert caught.value.code is ErrorCode.CONTROL_DATA_ROLE_ESCALATION


def test_chat_cannot_currently_mark_any_message_mandatory(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """いまの Chat では、どの Role でも `mandatory` にできないこと。

    CP-2-A が `mandatory` を Control 系 2 値へ絞り、§1.16.4 が Control Role へ
    検証済み権限を要求する。Chat にその経路が無いため、**両方を満たす Message が
    存在しない**。仕様どおりの帰結である。権限を与えて回避しない。
    """
    from harness.domain.context_budget import MessageRole

    store, cas, connection = world
    for role in MessageRole:
        snapshot, ids = _seed_with_role(store, design_hash, role, b"a")
        with pytest.raises(HarnessError) as caught:
            _build(_context_service(connection, cas), snapshot, ids, mandatory=set(ids))
        assert caught.value.code in {
            ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
            ErrorCode.CONTROL_DATA_ROLE_ESCALATION,
        }, f"{role.value} が mandatory で通ってしまった"


@pytest.mark.parametrize(
    "role_name",
    [
        "USER_TASK",
        "TOOL_DEFINITION",
        "VERIFIED_REFERENCE_DATA",
        "UNTRUSTED_ARTIFACT_DATA",
        "UNTRUSTED_PROVIDER_DATA",
    ],
)
def test_non_control_roles_cannot_be_mandatory(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash, role_name: str
) -> None:
    """Control 系以外を `mandatory` にできないこと（CP-2-A）。

    **とくに Provider 出力を必須 Context へ昇格させない。**
    """
    from harness.domain.context_budget import MessageRole

    store, cas, connection = world
    snapshot, ids = _seed_with_role(store, design_hash, MessageRole[role_name], b"a")
    with pytest.raises(HarnessError) as caught:
        _build(_context_service(connection, cas), snapshot, ids, mandatory=set(ids))
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION
    assert "cannot be marked mandatory" in str(caught.value)


def test_mandatory_capable_roles_are_exactly_the_control_roles() -> None:
    """許可集合が §1.16.4 の Control Role と一致すること。"""
    from harness.application.chat_context_service import MANDATORY_CAPABLE_ROLES
    from harness.domain.context_budget import MessageRole

    assert MANDATORY_CAPABLE_ROLES == frozenset(
        {MessageRole.SYSTEM_CONTROL, MessageRole.DEVELOPER_CONTROL}
    )
    assert len(MANDATORY_CAPABLE_ROLES) == 2


def test_non_control_role_is_still_selectable_without_mandatory(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """`mandatory` でなければ、どの Role も候補になること。

    **CP-2-A は「必須にできない」であって「使えない」ではない。**
    """
    from harness.domain.context_budget import MessageRole

    store, cas, connection = world
    snapshot, ids = _seed_with_role(store, design_hash, MessageRole.UNTRUSTED_PROVIDER_DATA, b"a b")
    assembly = _build(_context_service(connection, cas), snapshot, ids)
    assert set(assembly.bundle.ordered_fragment_ids) == set(ids)


# ---------------------------------------------------------------------------
# CP-3-B: 未設定は送信ごと拒否
# ---------------------------------------------------------------------------


def test_one_unspecified_message_rejects_the_whole_send(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """1 件でも未設定なら **送信全体** を止めること（CP-3-B）。

    残りを黙って送らない。部分送信にしない。
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a", b"b", b"c"])
    service = _context_service(connection, cas)
    partial = {mid: MessageSelectionInput(mid, mandatory=False) for mid in ids[:-1]}
    with pytest.raises(HarnessError) as caught:
        service.build_context(
            snapshot=snapshot,
            policy=_policy(),
            profile=_profile(),
            selection=partial,
            bundle_id="b",
            receipt_id="r",
            input_read_capability_set_hash=_H,
            input_read_evidence_hash=_H,
            now=_NOW,
        )
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION
    assert ids[-1] in str(caught.value)


class _CountingCas:
    """`get` の回数を数える CAS の覆い。Bytes は元の CAS が持つ。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.get_calls = 0

    def put(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.put(*args, **kwargs)

    def get(self, content_hash: Any) -> bytes:
        self.get_calls += 1
        return bytes(self._inner.get(content_hash))

    def verify(self, content_hash: Any) -> Any:
        return self._inner.verify(content_hash)


def test_rejection_happens_before_any_body_is_read(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """未解決なら本文を **1 件も読まずに** 止まること。

    拒否したことだけでは足りない。読んでから拒否しているなら、Provider 呼出しの
    手前という保証にならない。**読取り回数 0 が根拠である。**
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a", b"b", b"c"])
    counting = _CountingCas(cas)
    service = ChatContextService(
        conversations=SqliteConversationRepository(connection),
        messages=SqliteConversationMessageRepository(connection),
        artifacts=counting,  # type: ignore[arg-type]
        token_counter=_Counter(),
    )
    partial = {mid: MessageSelectionInput(mid, mandatory=False) for mid in ids[:-1]}
    with pytest.raises(HarnessError) as caught:
        service.build_context(
            snapshot=snapshot,
            policy=_policy(),
            profile=_profile(),
            selection=partial,
            bundle_id="b",
            receipt_id="r",
            input_read_capability_set_hash=_H,
            input_read_evidence_hash=_H,
            now=_NOW,
        )
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION
    assert counting.get_calls == 0, f"本文を {counting.get_calls} 件読んでから拒否している"


def test_selection_for_a_message_outside_the_conversation_is_rejected(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """会話に無い Message の方針を受け取ったら止まること。

    指している集合が違う。黙って無視すると、送ったつもりの Message が入らない。
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a"])
    service = _context_service(connection, cas)
    selection = {mid: MessageSelectionInput(mid, mandatory=False) for mid in ids}
    selection["ghost"] = MessageSelectionInput("ghost", mandatory=False)
    with pytest.raises(HarnessError) as caught:
        service.build_context(
            snapshot=snapshot,
            policy=_policy(),
            profile=_profile(),
            selection=selection,
            bundle_id="b",
            receipt_id="r",
            input_read_capability_set_hash=_H,
            input_read_evidence_hash=_H,
            now=_NOW,
        )
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
    assert "ghost" in str(caught.value)


def test_no_default_completion_for_unspecified(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """未設定を既定値で補完しないこと。

    補完していれば送信は通ってしまう。通らないことが根拠である。
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a"])
    service = _context_service(connection, cas)
    with pytest.raises(HarnessError):
        service.build_context(
            snapshot=snapshot,
            policy=_policy(),
            profile=_profile(),
            selection={},
            bundle_id="b",
            receipt_id="r",
            input_read_capability_set_hash=_H,
            input_read_evidence_hash=_H,
            now=_NOW,
        )


# ---------------------------------------------------------------------------
# CP-4-A: decision_hash から Receipt 固有 ID を外す
# ---------------------------------------------------------------------------


def test_decision_hash_ignores_receipt_and_bundle_ids(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """同じ判断なら Receipt ID / Bundle ID が違っても同じ `decision_hash`（CP-4-A）。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a b", b"c"])
    service = _context_service(connection, cas)

    def run(bundle_id: str, receipt_id: str) -> Any:
        return service.build_context(
            snapshot=snapshot,
            policy=_policy(),
            profile=_profile(),
            selection=_selection(ids),
            bundle_id=bundle_id,
            receipt_id=receipt_id,
            input_read_capability_set_hash=_H,
            input_read_evidence_hash=_H,
            now=_NOW,
        ).assembly

    first = run("bundle-one", "receipt-one")
    second = run("bundle-two", "receipt-two")
    assert first.receipt.receipt_id != second.receipt.receipt_id
    assert first.receipt.decision_hash == second.receipt.decision_hash


def test_decision_hash_changes_when_the_decision_changes(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """判断が変われば `decision_hash` も変わること。

    **何も見ていない検査にしない。** ID を無視するだけの Hash にしていない。
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a b c", b"d e f"])
    service = _context_service(connection, cas)

    def run(total: int) -> Any:
        return service.build_context(
            snapshot=snapshot,
            policy=_policy(total=total),
            profile=_profile(),
            selection=_selection(ids),
            bundle_id="same-bundle",
            receipt_id="same-receipt",
            input_read_capability_set_hash=_H,
            input_read_evidence_hash=_H,
            now=_NOW,
        ).assembly

    wide, tight = run(1_000), run(3)
    assert wide.receipt.selected_fragment_ids != tight.receipt.selected_fragment_ids
    assert wide.receipt.decision_hash != tight.receipt.decision_hash


def test_decision_hash_inputs_exclude_the_ids() -> None:
    """`receipt_projection` に `receipt_id`／`bundle_id` が無いこと。

    値の比較だけでなく、**入力そのもの**を見る。
    """
    import ast

    source = (REPO_ROOT / "src/harness/domain/context_budget.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "receipt_projection" for t in node.targets
        ):
            assert isinstance(node.value, ast.Dict)
            keys = {
                k.value
                for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    assert keys, "receipt_projection を見つけられていない"
    assert "receipt_id" not in keys
    assert "bundle_id" not in keys
    # 判断の中身は残っている
    for expected in ("candidate_fragment_ids", "selected_fragment_ids", "excluded_fragments"):
        assert expected in keys


def test_policy_beyond_the_provider_limit_is_rejected(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """Policy が Provider Context Limit を超えていれば止まること。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a"])
    with pytest.raises(HarnessError) as caught:
        _build(
            _context_service(connection, cas),
            snapshot,
            ids,
            policy=_policy(total=100),
            profile=_profile(context_limit=10),
        )
    assert caught.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


# ---------------------------------------------------------------------------
# Token Profile
# ---------------------------------------------------------------------------


def test_unknown_assurance_profile_is_rejected(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """`UNKNOWN` の Profile では組まないこと（§1.12）。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a"])
    with pytest.raises(HarnessError):
        _build(
            _context_service(connection, cas),
            snapshot,
            ids,
            profile=_profile(assurance=EstimateAssurance.UNKNOWN),
        )


def test_expired_profile_is_rejected(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """期限切れの Profile では組まないこと。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a"])
    with pytest.raises(HarnessError):
        _build(
            _context_service(connection, cas),
            snapshot,
            ids,
            profile=_profile(expires_at="2026-08-28T00:00:01Z"),
            now="2027-01-01T00:00:00Z",
        )


def test_unknown_token_count_is_rejected(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """計数器が `UNKNOWN` を返したら止まること。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a"])
    service = _context_service(connection, cas, _Counter(EstimateAssurance.UNKNOWN))
    with pytest.raises(HarnessError) as caught:
        _build(service, snapshot, ids)
    assert caught.value.code is ErrorCode.TOKEN_PROFILE_DRIFT_DETECTED


# ---------------------------------------------------------------------------
# 決定論と方針
# ---------------------------------------------------------------------------


def test_missing_selection_input_is_rejected(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """方針を与えられていない Message を既定で拾わないこと。

    **優先順位の既定を Service が持たない**ことの根拠である。
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a", b"b"])
    service = _context_service(connection, cas)
    with pytest.raises(HarnessError) as caught:
        service.build_context(
            snapshot=snapshot,
            policy=_policy(),
            profile=_profile(),
            selection={ids[0]: MessageSelectionInput(ids[0], mandatory=False)},
            bundle_id="b",
            receipt_id="r",
            input_read_capability_set_hash=_H,
            input_read_evidence_hash=_H,
            now=_NOW,
        )
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def _seed_mixed(
    store: ConversationService,
    design_hash: ContentHash,
    rows: list[tuple[MessageRole, bytes]],
) -> tuple[ConversationSnapshot, list[str]]:
    """Role を Message ごとに変えて並べる。`sequence_number` は渡した順に増える。"""
    conversation = store.create_conversation(
        conversation_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
    )
    ids: list[str] = []
    for role, body in rows:
        stored = store.append_message(
            conversation_id=conversation.conversation_id,
            role=role,
            body=body,
            message_id=str(uuid.uuid4()),
            record_id=str(uuid.uuid4()),
            created_at=_TS,
            producer=_PRODUCER,
            artifact_id=str(uuid.uuid4()),
        )
        ids.append(stored.message.message_id)
    snapshot = store.build_snapshot(
        conversation_id=conversation.conversation_id,
        snapshot_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
        design_sha256=design_hash,
    )
    return snapshot, ids


def test_selection_input_carries_no_priority(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """呼出側が `priority` を上書きできないこと（`CP-1-A`）。

    上書きできれば Registry は正本でなくなる。**Field が無いことが根拠である。**

    以前ここには「呼出側が priority を入れ替えれば選択が変わる」試験があった。
    `CP-1-A` は `CP-1-B`（呼出側が毎回明示）を**選ばなかった**ので、その前提ごと
    無くなっている。後続の 2 件が、代わりに「どの Message が残るか」を固定する。
    """
    import dataclasses

    names = {f.name for f in dataclasses.fields(MessageSelectionInput)}
    assert names == {"message_id", "mandatory"}, names


def test_higher_tier_wins_over_a_newer_untrusted_message(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """段が上なら、より古くても先に入ること。

    段（`CPM-2-A`）が第 1 キーであり、`sequence_number`（`CPM-3-A`）は第 2 キーで
    ある。**Untrusted な新しい発話が、中間 Role の古い発話を押しのけない。**
    """
    store, cas, connection = world
    snapshot, ids = _seed_mixed(
        store,
        design_hash,
        [
            (MessageRole.USER_TASK, b"older"),
            (MessageRole.UNTRUSTED_PROVIDER_DATA, b"newer"),
        ],
    )
    assembly = _build(_context_service(connection, cas), snapshot, ids, policy=_policy(total=1))
    assert assembly.bundle.ordered_fragment_ids == (ids[0],)
    excluded = [item.fragment_id for item in assembly.receipt.excluded_fragments]
    assert excluded == [ids[1]]


def test_same_tier_drops_the_oldest_first(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """段が同じなら、予算が足りないとき**古い発話から**落ちること（`CPM-3-A`）。

    以前ここには「同点は `fragment_id` 昇順で決まる」試験があった。`CPM-3-A` が
    その同点を会話の新しさで崩すと決めたので、期待値が変わっている。緩めていない
    ——どの Message が残るかを名指しで固定している。
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a", b"b", b"c"])
    assembly = _build(_context_service(connection, cas), snapshot, ids, policy=_policy(total=2))
    assert set(assembly.bundle.ordered_fragment_ids) == {ids[1], ids[2]}
    excluded = [item.fragment_id for item in assembly.receipt.excluded_fragments]
    assert excluded == [ids[0]], "落ちたのが最古の Message ではない"


def test_selection_order_is_newest_first(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """選択順が新しい順であること。`fragment_id` 順に依っていないこと。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a", b"b", b"c"])
    assembly = _build(_context_service(connection, cas), snapshot, ids)
    assert assembly.bundle.ordered_fragment_ids == tuple(reversed(ids))


def test_send_order_is_sequence_number_ascending(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """Provider へ渡す最終列が `sequence_number` 昇順であること。

    **選択順と送信順を混同しない。** 降順のまま送ると会話が逆さになる。
    """
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a", b"b", b"c"])
    result = _build_result(_context_service(connection, cas), snapshot, ids)
    assert result.send_order == tuple(ids)
    assert result.assembly.bundle.ordered_fragment_ids == tuple(reversed(ids))
    assert result.send_order != result.assembly.bundle.ordered_fragment_ids


def test_send_order_contains_exactly_the_selected_messages(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """除外された Message を最終列へ混ぜないこと。落とした分だけ短いこと。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a", b"b", b"c"])
    result = _build_result(
        _context_service(connection, cas), snapshot, ids, policy=_policy(total=2)
    )
    assert result.send_order == (ids[1], ids[2])
    assert set(result.send_order) == set(result.assembly.bundle.ordered_fragment_ids)


def test_selection_and_send_order_are_stable_across_runs(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """同じ入力なら毎回同じ選択・同じ最終列になること。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a", b"b", b"c"])
    service = _context_service(connection, cas)
    seen = {
        (
            _build_result(service, snapshot, ids, policy=_policy(total=2)).send_order,
            _build_result(
                service, snapshot, ids, policy=_policy(total=2)
            ).assembly.bundle.ordered_fragment_ids,
        )
        for _ in range(5)
    }
    assert len(seen) == 1, f"選択が揺れた: {seen}"


def test_excluded_fragments_order_is_deterministic(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a b", b"c d", b"e f"])
    service = _context_service(connection, cas)
    orders = {
        tuple(
            item.fragment_id
            for item in _build(
                service, snapshot, ids, policy=_policy(total=2)
            ).receipt.excluded_fragments
        )
        for _ in range(5)
    }
    assert len(orders) == 1


def test_stale_snapshot_is_rejected(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """Snapshot 生成後に Message が増えたら、その Snapshot では組まないこと。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a"])
    store.append_message(
        conversation_id=snapshot.conversation_id,
        role=__import__(
            "harness.domain.context_budget", fromlist=["MessageRole"]
        ).MessageRole.USER_TASK,
        body=b"later",
        message_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
        artifact_id=str(uuid.uuid4()),
    )
    with pytest.raises(HarnessError) as caught:
        _build(_context_service(connection, cas), snapshot, ids)
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_results_are_stable_across_processes() -> None:
    """`PYTHONHASHSEED` を変えても同じ結果になること。"""
    import os
    import subprocess
    import sys

    script = (
        "from harness.domain.context_budget import *\n"
        "from harness.domain.hashing import ContentHash\n"
        "h = ContentHash.parse('sha256:' + '0'*64)\n"
        "frs = [ContextFragment(fragment_id=f'f{i}', fragment_content_hash=h,"
        " token_count=1, message_role=MessageRole.USER_TASK, control_authority=False,"
        " instruction_eligible=False, priority=0) for i in range(5)]\n"
        "p = TokenBudgetPolicy(total_tokens=3, reserved_output_tokens=0,"
        " reserved_tool_tokens=0, safety_margin_tokens=0, compression_max_depth=0,"
        " overflow_policy='FAIL_CLOSED', policy_id='p')\n"
        "s = select_context(frs, p)\n"
        "print(s.selected_fragment_ids, s.excluded_fragment_ids)\n"
    )
    seen = set()
    for seed in ("0", "1", "7777"):
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        seen.add(out.stdout.strip())
    assert len(seen) == 1, f"PYTHONHASHSEED で結果が変わった: {seen}"


# ---------------------------------------------------------------------------
# 安全境界
# ---------------------------------------------------------------------------


def test_nothing_is_written_back(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """Snapshot・Message・Conversation が動かないこと。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"a b", b"c"])
    before_messages = store.list_messages(snapshot.conversation_id)
    before_snapshots = store.list_snapshots(snapshot.conversation_id)
    before_conversation = store.get_conversation(snapshot.conversation_id)
    blobs = dict(cas.blobs)

    _build(_context_service(connection, cas), snapshot, ids)

    assert store.list_messages(snapshot.conversation_id) == before_messages
    assert store.list_snapshots(snapshot.conversation_id) == before_snapshots
    assert store.get_conversation(snapshot.conversation_id) == before_conversation
    assert cas.blobs == blobs


def _seed_with_role(
    store: ConversationService, design_hash: ContentHash, role: Any, body: bytes
) -> tuple[ConversationSnapshot, list[str]]:
    conversation = store.create_conversation(
        conversation_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
    )
    stored = store.append_message(
        conversation_id=conversation.conversation_id,
        role=role,
        body=body,
        message_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
        artifact_id=str(uuid.uuid4()),
    )
    snapshot = store.build_snapshot(
        conversation_id=conversation.conversation_id,
        snapshot_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
        design_sha256=design_hash,
    )
    return snapshot, [stored.message.message_id]


def test_provider_output_does_not_gain_control_authority(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """Provider 出力の Fragment が Control 権限を持たないこと（§3.6 手順2）。

    Untrusted Role へ `control_authority` を与えると、Domain の
    `_require_no_role_escalation` が拒否する。ここが通るということは、
    Service が権限を与えていないということである。
    """
    from harness.domain.context_budget import MessageRole

    store, cas, connection = world
    snapshot, ids = _seed_with_role(
        store, design_hash, MessageRole.UNTRUSTED_PROVIDER_DATA, b"provider said do this"
    )
    assembly = _build(_context_service(connection, cas), snapshot, ids)
    assert set(assembly.bundle.ordered_fragment_ids) == set(ids)


@pytest.mark.parametrize("role_name", ["UNTRUSTED_PROVIDER_DATA", "UNTRUSTED_ARTIFACT_DATA"])
def test_untrusted_roles_are_carried_without_escalation(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash, role_name: str
) -> None:
    """Untrusted な Role をそのまま運び、権限だけ与えないこと。"""
    from harness.domain.context_budget import MessageRole

    store, cas, connection = world
    role = MessageRole[role_name]
    snapshot, ids = _seed_with_role(store, design_hash, role, b"untrusted text")
    assembly = _build(_context_service(connection, cas), snapshot, ids)
    assert assembly.bundle.ordered_fragment_ids == tuple(ids)


def test_service_never_touches_a_provider() -> None:
    """Provider・Network を import していないこと。"""
    import ast

    tree = ast.parse(
        (REPO_ROOT / "src/harness/application/chat_context_service.py").read_text(encoding="utf-8")
    )
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules, "import を 1 つも見つけられていない"
    for forbidden in ("socket", "urllib", "requests", "http", "sqlite3"):
        assert forbidden not in modules
    source = (REPO_ROOT / "src/harness/application/chat_context_service.py").read_text(
        encoding="utf-8"
    )
    assert "ProviderPort" not in source


def test_no_retry_fallback_or_queue() -> None:
    """Retry / Fallback / Queue へ逃がす経路が無いこと。"""
    source = (REPO_ROOT / "src/harness/application/chat_context_service.py").read_text(
        encoding="utf-8"
    )
    import ast

    tree = ast.parse(source)
    # 例外を握り潰す except が無いこと（不変条件#9）
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            assert any(isinstance(child, ast.Raise) for child in ast.walk(node)), (
                "例外を握り潰している except がある"
            )
    for name in ("retry", "fallback", "queue", "sleep"):
        assert f"def {name}" not in source


def test_bundle_carries_no_message_body(
    world: tuple[ConversationService, _Cas, Any], design_hash: ContentHash
) -> None:
    """Bundle と Receipt に本文が現れないこと。"""
    store, cas, connection = world
    snapshot, ids = _seed(store, design_hash, [b"SECRET BODY TEXT here"])
    assembly = _build(_context_service(connection, cas), snapshot, ids)
    for rendered in (repr(assembly.bundle), repr(assembly.receipt)):
        assert "SECRET BODY TEXT" not in rendered


def test_exclusion_reasons_use_the_existing_vocabulary() -> None:
    """除外理由が既存 Enum の値だけであること。"""
    assert {m.value for m in ExclusionReason} == {"BUDGET", "DUPLICATE"}
