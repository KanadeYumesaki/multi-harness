from __future__ import annotations

import pytest

from harness.domain.context_budget import (
    ContextFragment,
    ExclusionReason,
    MessageRole,
    TokenBudgetPolicy,
    select_context,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical

pytestmark = pytest.mark.unit


def _fragment(
    name: str,
    *,
    token_count: int,
    mandatory: bool = False,
    priority: int = 0,
    message_role: MessageRole = MessageRole.VERIFIED_REFERENCE_DATA,
    control_authority: bool = False,
    instruction_eligible: bool = False,
) -> ContextFragment:
    return ContextFragment(
        fragment_id=name,
        fragment_content_hash=hash_canonical(
            {"name": name}, artifact_type="test-value", schema_major=1
        ),
        token_count=token_count,
        message_role=message_role,
        control_authority=control_authority,
        instruction_eligible=instruction_eligible,
        mandatory=mandatory,
        priority=priority,
        source_artifact_id=f"artifact-{name}",
    )


def test_context_selection_is_deterministic_and_obeys_reserved_token_budget() -> None:
    policy = TokenBudgetPolicy(total_tokens=100, reserved_output_tokens=20, reserved_tool_tokens=10)
    low = _fragment("low", token_count=30, priority=1)
    required = _fragment("required", token_count=30, mandatory=True, priority=0)
    high = _fragment("high", token_count=30, priority=10)

    receipt = select_context((low, required, high), policy)

    assert receipt.available_input_tokens == 70
    assert receipt.selected_fragment_ids == ("required", "high")
    assert receipt.excluded_fragment_ids == ("low",)
    assert receipt.selected_tokens == 60


def test_context_selection_deduplicates_only_exact_content_hash() -> None:
    policy = TokenBudgetPolicy(total_tokens=100, reserved_output_tokens=0, reserved_tool_tokens=0)
    original = _fragment("original", token_count=20, priority=1)
    duplicate = ContextFragment(
        fragment_id="duplicate",
        fragment_content_hash=original.fragment_content_hash,
        token_count=20,
        message_role=MessageRole.VERIFIED_REFERENCE_DATA,
        control_authority=False,
        instruction_eligible=False,
        mandatory=False,
        priority=99,
        source_artifact_id="artifact-duplicate",
    )

    receipt = select_context((duplicate, original), policy)

    assert receipt.selected_fragment_ids == ("duplicate",)
    assert receipt.deduplicated_fragment_ids == ("original",)


def test_context_selection_rejects_untrusted_control_role_and_mandatory_overflow() -> None:
    policy = TokenBudgetPolicy(total_tokens=10, reserved_output_tokens=0, reserved_tool_tokens=0)
    untrusted_control = _fragment(
        "untrusted-control",
        token_count=1,
        message_role=MessageRole.UNTRUSTED_ARTIFACT_DATA,
        control_authority=True,
    )
    with pytest.raises(HarnessError) as escalation:
        select_context((untrusted_control,), policy)
    assert escalation.value.code is ErrorCode.CONTROL_DATA_ROLE_ESCALATION

    too_large_required = _fragment("required", token_count=11, mandatory=True)
    with pytest.raises(HarnessError) as overflow:
        select_context((too_large_required,), policy)
    assert overflow.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


# --------------------------------------------------------------------------
# Schema制約（`ContextFragment` / `ContextSelectionReceipt`）
# --------------------------------------------------------------------------


def test_untrusted_roles_cannot_claim_instruction_eligibility() -> None:
    """`UNTRUSTED_*`は`instruction_eligible=false`でなければならない。"""
    policy = TokenBudgetPolicy(total_tokens=100, reserved_output_tokens=0, reserved_tool_tokens=0)
    provider_output = _fragment(
        "provider-output",
        token_count=1,
        message_role=MessageRole.UNTRUSTED_PROVIDER_DATA,
        instruction_eligible=True,
    )
    with pytest.raises(HarnessError) as error:
        select_context((provider_output,), policy)
    assert error.value.code is ErrorCode.CONTROL_DATA_ROLE_ESCALATION


def test_control_roles_require_verified_control_authority() -> None:
    """`SYSTEM_CONTROL`／`DEVELOPER_CONTROL`は検証済み`control_authority`必須。"""
    policy = TokenBudgetPolicy(total_tokens=100, reserved_output_tokens=0, reserved_tool_tokens=0)
    unverified_system = _fragment(
        "system",
        token_count=1,
        message_role=MessageRole.SYSTEM_CONTROL,
        control_authority=False,
    )
    with pytest.raises(HarnessError) as error:
        select_context((unverified_system,), policy)
    assert error.value.code is ErrorCode.CONTROL_DATA_ROLE_ESCALATION

    verified_system = _fragment(
        "system",
        token_count=1,
        message_role=MessageRole.SYSTEM_CONTROL,
        control_authority=True,
    )
    assert select_context((verified_system,), policy).selected_fragment_ids == ("system",)


def test_every_excluded_fragment_carries_a_reason_code() -> None:
    """「各Excluded FragmentにReason Code必須」「理由なし除外は拒否例」。"""
    policy = TokenBudgetPolicy(total_tokens=10, reserved_output_tokens=0, reserved_tool_tokens=0)
    kept = _fragment("kept", token_count=10, priority=5)
    over_budget = _fragment("over-budget", token_count=10, priority=1)
    duplicate = ContextFragment(
        fragment_id="duplicate",
        fragment_content_hash=kept.fragment_content_hash,
        token_count=10,
        message_role=MessageRole.VERIFIED_REFERENCE_DATA,
        control_authority=False,
        instruction_eligible=False,
        priority=0,
        source_artifact_id="artifact-duplicate",
    )

    selection = select_context((kept, over_budget, duplicate), policy)

    reasons = {item.fragment_id: item.reason for item in selection.excluded_fragments}
    assert reasons == {
        "duplicate": ExclusionReason.DUPLICATE,
        "over-budget": ExclusionReason.BUDGET,
    }
    # CandidateはSelectedとExcludedで完全に説明される。
    accounted = sorted([*selection.selected_fragment_ids, *selection.excluded_fragment_ids])
    assert accounted == sorted(selection.candidate_fragment_ids)


def test_mandatory_fragment_wins_deduplication_against_higher_priority_optional() -> None:
    """回帰試験。Mandatoryが重複除去で消えてはならない。

    以前は重複除去の順位付けがPriorityだけを見ていたため、同一Content Hashを持つ
    高PriorityのOptionalがMandatoryを押しのけて残り、Mandatory側が`DUPLICATE`として
    除外されていた。`ContextFragment`制約「Mandatory FragmentはSelectionから除外不可」
    に反する。
    """
    policy = TokenBudgetPolicy(total_tokens=100, reserved_output_tokens=0, reserved_tool_tokens=0)
    shared = hash_canonical({"name": "shared"}, artifact_type="test-value", schema_major=1)

    def _with_shared_content(name: str, *, mandatory: bool, priority: int) -> ContextFragment:
        return ContextFragment(
            fragment_id=name,
            fragment_content_hash=shared,
            token_count=10,
            message_role=MessageRole.VERIFIED_REFERENCE_DATA,
            control_authority=False,
            instruction_eligible=False,
            mandatory=mandatory,
            priority=priority,
            source_artifact_id=f"artifact-{name}",
        )

    mandatory = _with_shared_content("mandatory", mandatory=True, priority=0)
    optional = _with_shared_content("optional", mandatory=False, priority=99)

    for candidates in ((mandatory, optional), (optional, mandatory)):
        selection = select_context(candidates, policy)
        assert selection.selected_fragment_ids == ("mandatory",)
        assert selection.deduplicated_fragment_ids == ("optional",)
        assert selection.excluded_fragment_ids == ("optional",)


def test_two_mandatory_duplicates_keep_the_lowest_fragment_id() -> None:
    """Mandatory同士の完全重複は、Code Point昇順で1件だけ残す。"""
    policy = TokenBudgetPolicy(total_tokens=100, reserved_output_tokens=0, reserved_tool_tokens=0)
    shared = hash_canonical({"name": "shared"}, artifact_type="test-value", schema_major=1)
    first = ContextFragment(
        fragment_id="alpha",
        fragment_content_hash=shared,
        token_count=10,
        message_role=MessageRole.VERIFIED_REFERENCE_DATA,
        control_authority=False,
        instruction_eligible=False,
        mandatory=True,
    )
    second = ContextFragment(
        fragment_id="bravo",
        fragment_content_hash=shared,
        token_count=10,
        message_role=MessageRole.VERIFIED_REFERENCE_DATA,
        control_authority=False,
        instruction_eligible=False,
        mandatory=True,
    )
    selection = select_context((second, first), policy)
    assert selection.selected_fragment_ids == ("alpha",)
    assert selection.deduplicated_fragment_ids == ("bravo",)


def test_duplicate_fragment_ids_are_rejected() -> None:
    policy = TokenBudgetPolicy(total_tokens=100, reserved_output_tokens=0, reserved_tool_tokens=0)
    first = _fragment("same-id", token_count=1)
    second = _fragment("same-id", token_count=2)
    with pytest.raises(HarnessError) as error:
        select_context((first, second), policy)
    assert error.value.code is ErrorCode.PLAN_NONDETERMINISTIC


# --------------------------------------------------------------------------
# TokenBudgetPolicy（`TokenBudgetPolicy` Schema制約）
# --------------------------------------------------------------------------


def test_token_budget_policy_rejects_negative_and_over_reserved_values() -> None:
    with pytest.raises(ValueError):
        TokenBudgetPolicy(total_tokens=-1, reserved_output_tokens=0, reserved_tool_tokens=0)
    with pytest.raises(ValueError):
        TokenBudgetPolicy(total_tokens=10, reserved_output_tokens=8, reserved_tool_tokens=8)
    with pytest.raises(ValueError):
        TokenBudgetPolicy(
            total_tokens=10,
            reserved_output_tokens=0,
            reserved_tool_tokens=0,
            compression_max_depth=3,
        )
    with pytest.raises(ValueError):
        TokenBudgetPolicy(
            total_tokens=10,
            reserved_output_tokens=0,
            reserved_tool_tokens=0,
            overflow_policy="BEST_EFFORT",
        )


def test_safety_margin_reduces_available_input_tokens() -> None:
    policy = TokenBudgetPolicy(
        total_tokens=100,
        reserved_output_tokens=20,
        reserved_tool_tokens=10,
        safety_margin_tokens=5,
    )
    assert policy.available_input_tokens == 65


# --------------------------------------------------------------------------
# TASK-DESIGN-B-02：CONTEXT_BUDGET_EXCEEDED と RUNTIME_SPEC_MISMATCH の境界
# --------------------------------------------------------------------------


def test_budget_exhaustion_uses_context_budget_exceeded() -> None:
    """予算超過は CONTEXT_BUDGET_EXCEEDED を使う（spec §3.6 手順8）。

    Step 3-b で当該Codeが errors.yaml と §1.7.1 表へ登録されるまでは
    RUNTIME_SPEC_MISMATCH を代替に使っていた。Codeが揃った以上、
    予算超過とRuntime Spec不一致を同じCodeで報告しない。
    混ざっているとAlertを受けても原因を切り分けられない。
    """
    policy = TokenBudgetPolicy(total_tokens=10, reserved_output_tokens=0, reserved_tool_tokens=0)
    too_large = _fragment("required", token_count=11, mandatory=True)
    with pytest.raises(HarnessError) as error:
        select_context((too_large,), policy)
    assert error.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


def test_context_budget_exceeded_is_a_non_retryable_validation_error() -> None:
    """Classification は VALIDATION_ERROR、自動再試行は不可。"""
    from harness.domain.errors import ErrorClassification, classification_of
    from harness.domain.transitions import is_retryable_classification

    classification = classification_of(ErrorCode.CONTEXT_BUDGET_EXCEEDED)
    assert classification is ErrorClassification.VALIDATION_ERROR
    assert not is_retryable_classification(classification)


def test_context_budget_exceeded_maps_to_failed_permanent() -> None:
    """終端State は ACTION_FAILED -> FAILED_PERMANENT（§1.4.2 の写像）。

    Policy Engine の拒否ではないため BLOCKED_POLICY を使わない。
    """
    from harness.domain.errors import classification_of
    from harness.domain.events import EventType
    from harness.domain.transitions import TransitionContext, resolve_next_state

    state = resolve_next_state(
        "RUNNING",
        EventType.ACTION_FAILED,
        TransitionContext(
            error_classification=classification_of(ErrorCode.CONTEXT_BUDGET_EXCEEDED)
        ),
    )
    assert state == "FAILED_PERMANENT"


def test_runtime_spec_mismatch_still_covers_profile_and_counter_mismatch() -> None:
    """RUNTIME_SPEC_MISMATCH の残存範囲を固定する。

    予算の話ではない「宣言と実体の食い違い」はこちらのままである。
    差し替えの行き過ぎを防ぐため、代表経路を明示的に押さえる。
    """
    from harness.domain.context_budget import (
        EstimateAssurance,
        TokenOverheads,
        TokenProfileSnapshot,
    )

    zero = TokenOverheads(
        system_message_overhead=0,
        developer_message_overhead=0,
        tool_definition_overhead=0,
        per_message_overhead=0,
        structured_output_overhead=0,
        streaming_frame_overhead=0,
        retry_fallback_reservation=0,
    )
    profile = TokenProfileSnapshot(
        snapshot_id="s",
        provider="mock",
        model="m",
        tokenizer_name="tok",
        tokenizer_version="1.0.0",
        counting_adapter_version="adapter/1",
        context_limit=4096,
        maximum_output_limit=1024,
        estimate_assurance=EstimateAssurance.UNKNOWN,
        overheads=zero,
        retrieved_at="2026-08-16T00:00:00Z",
        expires_at="2026-08-16T01:00:00Z",
    )
    with pytest.raises(HarnessError) as unknown_assurance:
        profile.require_usable(now="2026-08-16T00:30:00Z")
    assert unknown_assurance.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH

    with pytest.raises(HarnessError) as counter_mismatch:
        profile.require_counter_identity(
            tokenizer_name="other", tokenizer_version="1.0.0", counting_adapter_version="adapter/1"
        )
    assert counter_mismatch.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH
