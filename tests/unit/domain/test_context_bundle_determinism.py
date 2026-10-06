"""Context Snapshot（`ContextBundle`）の決定性と予算境界。

検証対象は次の4点。

* 不変条件#4 SnapshotへランダムID・時刻・PID・列挙順を含めない
* 不変条件#5／#6 二重Build一致と、別`PYTHONHASHSEED`での三重Build一致
* spec §3.6 予算境界（ちょうど／1超過／空／単一超過）とTie-breaker
* Provider／Model／Tokenizer／Policyの`bundle_hash`への束縛
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from harness.domain.context_budget import (
    ContextAssembly,
    ContextBundle,
    ContextFragment,
    EstimateAssurance,
    ExclusionReason,
    MessageRole,
    TokenBudgetPolicy,
    TokenOverheads,
    TokenProfileSnapshot,
    build_context_bundle,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_canonical

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE = Path(__file__).with_name("context_bundle_probe.py")


def _fragment(
    name: str,
    *,
    token_count: int,
    priority: int = 0,
    mandatory: bool = False,
    body: str | None = None,
) -> ContextFragment:
    return ContextFragment(
        fragment_id=name,
        fragment_content_hash=hash_canonical(
            {"name": body if body is not None else name},
            artifact_type="test-value",
            schema_major=1,
        ),
        token_count=token_count,
        message_role=MessageRole.VERIFIED_REFERENCE_DATA,
        control_authority=False,
        instruction_eligible=False,
        mandatory=mandatory,
        priority=priority,
        source_artifact_id=f"artifact-{name}",
    )


def _policy(**overrides: int | str) -> TokenBudgetPolicy:
    values: dict[str, int | str] = {
        "total_tokens": 100,
        "reserved_output_tokens": 0,
        "reserved_tool_tokens": 0,
    }
    values.update(overrides)
    return TokenBudgetPolicy(**values)  # type: ignore[arg-type]


def _overheads(**overrides: int) -> TokenOverheads:
    values: dict[str, int] = {
        "system_message_overhead": 0,
        "developer_message_overhead": 0,
        "tool_definition_overhead": 0,
        "per_message_overhead": 0,
        "structured_output_overhead": 0,
        "streaming_frame_overhead": 0,
        "retry_fallback_reservation": 0,
    }
    values.update(overrides)
    return TokenOverheads(**values)


def _profile(**overrides: object) -> TokenProfileSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "snapshot-1",
        "provider": "mock",
        "model": "mock-model",
        "tokenizer_name": "harness-utf8-byte-upper-bound",
        "tokenizer_version": "1.0.0",
        "counting_adapter_version": "harness-token-counter/2",
        "context_limit": 4096,
        "maximum_output_limit": 1024,
        "estimate_assurance": EstimateAssurance.CONSERVATIVE,
        "overheads": _overheads(),
        "retrieved_at": "2026-08-16T00:00:00Z",
        "expires_at": "2026-08-16T01:00:00Z",
    }
    values.update(overrides)
    return TokenProfileSnapshot(**values)  # type: ignore[arg-type]


CAPABILITY_SET_HASH = hash_canonical(
    {"capabilities": ["capability-1"]}, artifact_type="test-value", schema_major=1
)
READ_EVIDENCE_HASH = hash_canonical(
    {"evidence": ["evidence-1"]}, artifact_type="test-value", schema_major=1
)


def _assemble(
    fragments: tuple[ContextFragment, ...],
    *,
    policy: TokenBudgetPolicy | None = None,
    profile: TokenProfileSnapshot | None = None,
    bundle_id: str = "bundle-1",
    receipt_id: str = "receipt-1",
    capability_set_hash: ContentHash = CAPABILITY_SET_HASH,
    read_evidence_hash: ContentHash = READ_EVIDENCE_HASH,
) -> ContextAssembly:
    return build_context_bundle(
        fragments,
        policy or _policy(),
        profile or _profile(),
        bundle_id=bundle_id,
        receipt_id=receipt_id,
        input_read_capability_set_hash=capability_set_hash,
        input_read_evidence_hash=read_evidence_hash,
    )


def _build(
    fragments: tuple[ContextFragment, ...],
    *,
    policy: TokenBudgetPolicy | None = None,
    profile: TokenProfileSnapshot | None = None,
    bundle_id: str = "bundle-1",
    receipt_id: str = "receipt-1",
    capability_set_hash: ContentHash = CAPABILITY_SET_HASH,
    read_evidence_hash: ContentHash = READ_EVIDENCE_HASH,
) -> ContextBundle:
    return _assemble(
        fragments,
        policy=policy,
        profile=profile,
        bundle_id=bundle_id,
        receipt_id=receipt_id,
        capability_set_hash=capability_set_hash,
        read_evidence_hash=read_evidence_hash,
    ).bundle


# --------------------------------------------------------------------------
# 決定性（不変条件#4／#5／#6）
# --------------------------------------------------------------------------


def test_identical_input_produces_identical_bundle_hash() -> None:
    fragments = (_fragment("a", token_count=10), _fragment("b", token_count=10, priority=3))
    assert _build(fragments).bundle_hash == _build(fragments).bundle_hash


def test_candidate_order_does_not_change_the_bundle_hash() -> None:
    """入力の列挙順はSnapshotへ入らない（不変条件#4）。"""
    first = _fragment("first", token_count=10, priority=5)
    second = _fragment("second", token_count=10, priority=3)
    third = _fragment("third", token_count=10, priority=1)

    forward = _build((first, second, third))
    reversed_order = _build((third, second, first))

    assert forward.bundle_hash == reversed_order.bundle_hash
    assert forward.ordered_fragment_ids == reversed_order.ordered_fragment_ids


def test_bundle_id_and_receipt_id_do_not_change_the_bundle_hash() -> None:
    """採番IDはContentへ入らない（不変条件#4）。"""
    fragments = (_fragment("a", token_count=10),)
    first = _build(fragments, bundle_id="bundle-1", receipt_id="receipt-1")
    second = _build(fragments, bundle_id="bundle-2", receipt_id="receipt-2")
    assert first.bundle_hash == second.bundle_hash
    assert first.bundle_id != second.bundle_id


def test_bundle_hash_matches_across_processes_with_different_hash_seeds() -> None:
    """別`PYTHONHASHSEED`の子Processで三重Buildし、Hash一致を確認する。

    同一Process内の二重Buildでは`set`反復による非決定性を検出できない
    （不変条件#6）。ここだけが `PYTHONHASHSEED` 依存を捕まえられる。
    """
    outputs = [_run_probe(seed) for seed in ("0", "1", "4242")]
    assert outputs[0] == outputs[1] == outputs[2]
    # 3行（bundle_hash / 採用ID列 / decision_hash）が返ることを確認する。
    assert len(outputs[0].splitlines()) == 3
    assert outputs[0].startswith("sha256:")


def _run_probe(seed: str) -> str:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = seed
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    completed = subprocess.run(  # noqa: S603 - 固定引数のlist[str]。shellを使わない
        [sys.executable, str(PROBE)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_bundle_is_immutable_and_detects_tampering() -> None:
    bundle = _build((_fragment("a", token_count=10),))
    bundle.assert_integrity()

    with pytest.raises(AttributeError):
        bundle.bundle_id = "other"  # type: ignore[misc]

    tampered = replace(
        bundle, bundle_hash=hash_canonical({"x": 1}, artifact_type="t", schema_major=1)
    )
    with pytest.raises(HarnessError) as error:
        tampered.assert_integrity()
    assert error.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT


# --------------------------------------------------------------------------
# Provider／Model／Tokenizer／Policyの束縛
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "other-provider"),
        ("model", "other-model"),
        ("tokenizer_name", "other-tokenizer"),
        ("tokenizer_version", "2.0.0"),
        ("context_limit", 8192),
    ],
)
def test_token_profile_change_produces_a_new_bundle(field: str, value: str | int) -> None:
    """tokenizer／model／providerがSnapshotへ束縛されていること。"""
    fragments = (_fragment("a", token_count=10),)
    baseline = _build(fragments)
    changed = _build(fragments, profile=_profile(**{field: value}))
    assert baseline.bundle_hash != changed.bundle_hash


def test_budget_policy_change_produces_a_new_bundle() -> None:
    """Policy変更を同一Snapshotとして扱わない。"""
    fragments = (_fragment("a", token_count=10),)
    baseline = _build(fragments)
    changed = _build(fragments, policy=_policy(reserved_output_tokens=10))
    assert baseline.bundle_hash != changed.bundle_hash
    # 採用結果が同じでもPolicyが違えば別Snapshotである。
    assert baseline.ordered_fragment_ids == changed.ordered_fragment_ids


def test_fragment_content_change_produces_a_new_bundle() -> None:
    fragments = (_fragment("a", token_count=10, body="original"),)
    changed = (_fragment("a", token_count=10, body="edited"),)
    assert _build(fragments).bundle_hash != _build(changed).bundle_hash


# --------------------------------------------------------------------------
# 予算境界（spec §3.6）
# --------------------------------------------------------------------------


def test_budget_boundary_exact_fit() -> None:
    bundle = _build((_fragment("a", token_count=70), _fragment("b", token_count=30)))
    assert bundle.total_token_count == 100
    assert sorted(bundle.ordered_fragment_ids) == ["a", "b"]


def test_budget_boundary_one_token_over() -> None:
    assembly = _assemble(
        (_fragment("a", token_count=70, priority=5), _fragment("b", token_count=31, priority=1))
    )
    assert assembly.bundle.ordered_fragment_ids == ("a",)
    assert assembly.bundle.total_token_count == 70
    assert [item.fragment_id for item in assembly.receipt.excluded_fragments] == ["b"]
    assert assembly.receipt.excluded_fragments[0].reason is ExclusionReason.BUDGET


def test_budget_boundary_empty_history() -> None:
    assembly = _assemble(())
    assert assembly.bundle.ordered_fragment_ids == ()
    assert assembly.bundle.total_token_count == 0
    assert assembly.receipt.excluded_fragments == ()
    # 空でも決定的なHashを持ち、再計算で一致する。
    assembly.bundle.assert_integrity()


def test_budget_boundary_single_fragment_exceeds_budget() -> None:
    """必須なら停止し、任意なら理由付きで除外する。"""
    with pytest.raises(HarnessError) as error:
        _build((_fragment("huge", token_count=101, mandatory=True),))
    assert error.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED

    assembly = _assemble((_fragment("huge", token_count=101),))
    assert assembly.bundle.ordered_fragment_ids == ()
    assert assembly.receipt.excluded_fragments[0].reason is ExclusionReason.BUDGET


def test_equal_priority_uses_a_fixed_tie_breaker() -> None:
    """同順位はfragment_idのCode Point昇順で決める。呼出順に依存しない。"""
    same_priority = (
        _fragment("charlie", token_count=40, priority=5),
        _fragment("alpha", token_count=40, priority=5),
        _fragment("bravo", token_count=40, priority=5),
    )
    forward = _assemble(same_priority)
    backward = _assemble(tuple(reversed(same_priority)))
    assert forward.bundle.ordered_fragment_ids == ("alpha", "bravo")
    assert forward.bundle.bundle_hash == backward.bundle.bundle_hash
    assert forward.receipt.tie_breaker == (
        "mandatory-first,priority-desc,fragment_id:unicode-code-point-ascending"
    )


def test_mandatory_fragments_are_selected_before_optional_ones() -> None:
    assembly = _assemble(
        (
            _fragment("optional-high", token_count=60, priority=99),
            _fragment("required", token_count=60, mandatory=True, priority=0),
        )
    )
    assert assembly.bundle.ordered_fragment_ids == ("required",)
    assert assembly.receipt.excluded_fragment_ids == ("optional-high",)


# --------------------------------------------------------------------------
# §1.12 Overhead の予算反映と束縛
# --------------------------------------------------------------------------


def test_fixed_overhead_is_deducted_from_the_available_budget() -> None:
    profile = _profile(overheads=_overheads(system_message_overhead=30))
    assembly = _assemble((_fragment("a", token_count=71),), profile=profile)
    # 予算100 - 固定Overhead 30 = 70。71 Tokenは入らない。
    assert assembly.selection.available_input_tokens == 70
    assert assembly.bundle.ordered_fragment_ids == ()

    fits = _assemble((_fragment("a", token_count=70),), profile=profile)
    assert fits.bundle.ordered_fragment_ids == ("a",)


def test_per_message_overhead_is_charged_for_each_selected_fragment() -> None:
    profile = _profile(overheads=_overheads(per_message_overhead=10))
    assembly = _assemble(
        (_fragment("a", token_count=40), _fragment("b", token_count=40)), profile=profile
    )
    # (40+10) * 2 = 100 でちょうど収まる。
    assert assembly.bundle.total_token_count == 100
    assert sorted(assembly.bundle.ordered_fragment_ids) == ["a", "b"]

    over = _assemble(
        (_fragment("a", token_count=41, priority=5), _fragment("b", token_count=40, priority=1)),
        profile=profile,
    )
    assert over.bundle.ordered_fragment_ids == ("a",)


def test_overhead_change_produces_a_new_bundle_hash() -> None:
    fragments = (_fragment("a", token_count=10),)
    baseline = _build(fragments)
    changed = _build(fragments, profile=_profile(overheads=_overheads(streaming_frame_overhead=1)))
    assert baseline.bundle_hash != changed.bundle_hash


def test_counting_adapter_version_is_bound_to_the_bundle() -> None:
    fragments = (_fragment("a", token_count=10),)
    baseline = _build(fragments)
    changed = _build(fragments, profile=_profile(counting_adapter_version="other-adapter/9"))
    assert baseline.bundle_hash != changed.bundle_hash


def test_fixed_overhead_exceeding_the_budget_is_rejected() -> None:
    with pytest.raises(HarnessError) as error:
        _build(
            (_fragment("a", token_count=1),),
            profile=_profile(overheads=_overheads(system_message_overhead=101)),
        )
    assert error.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


# --------------------------------------------------------------------------
# Read証跡の束縛（§3.6 手順1）
# --------------------------------------------------------------------------


def test_read_capability_and_evidence_hashes_are_bound_to_the_bundle() -> None:
    fragments = (_fragment("a", token_count=10),)
    baseline = _build(fragments)
    other_capability = _build(
        fragments,
        capability_set_hash=hash_canonical(
            {"capabilities": ["other"]}, artifact_type="test-value", schema_major=1
        ),
    )
    other_evidence = _build(
        fragments,
        read_evidence_hash=hash_canonical(
            {"evidence": ["other"]}, artifact_type="test-value", schema_major=1
        ),
    )
    assert baseline.bundle_hash != other_capability.bundle_hash
    assert baseline.bundle_hash != other_evidence.bundle_hash
    assert other_capability.bundle_hash != other_evidence.bundle_hash


# --------------------------------------------------------------------------
# Record用 content_hash の分離
# --------------------------------------------------------------------------


def test_record_content_hash_is_separate_from_the_semantic_hashes() -> None:
    """`plan.py`と同じ分離。Record監査Hashへ`bundle_hash`をそのまま入れない。"""
    assembly = _assemble((_fragment("a", token_count=10),))
    bundle_record = assembly.bundle.to_record(
        record_id="record-1", created_at="2026-08-16T00:00:00Z", producer="harness/test"
    )
    receipt_record = assembly.receipt.to_record(
        record_id="record-2", created_at="2026-08-16T00:00:00Z", producer="harness/test"
    )

    assert bundle_record["content_hash"] != bundle_record["bundle_hash"]
    assert receipt_record["content_hash"] != str(assembly.receipt.decision_hash)

    # 同じBundleでもRecord IDが違えばRecordのcontent_hashは変わる。
    # 一方でbundle_hashは意味内容のHashなので変わらない。
    other_record = assembly.bundle.to_record(
        record_id="record-9", created_at="2026-08-16T00:00:00Z", producer="harness/test"
    )
    assert other_record["bundle_hash"] == bundle_record["bundle_hash"]
    assert other_record["content_hash"] != bundle_record["content_hash"]


# --------------------------------------------------------------------------
# TokenProfileSnapshot の停止条件（§1.12）
# --------------------------------------------------------------------------


def test_unknown_assurance_snapshot_is_rejected() -> None:
    profile = _profile(estimate_assurance=EstimateAssurance.UNKNOWN)
    with pytest.raises(HarnessError) as error:
        profile.require_usable(now="2026-08-16T00:30:00Z")
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_expired_snapshot_is_fail_closed() -> None:
    profile = _profile()
    profile.require_usable(now="2026-08-16T00:59:59Z")
    with pytest.raises(HarnessError) as error:
        profile.require_usable(now="2026-08-16T01:00:00Z")
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


@pytest.mark.parametrize(
    "tokenizer_name,tokenizer_version,counting_adapter_version",
    [
        ("tiktoken-cl100k", "1.0.0", "harness-token-counter/2"),
        ("harness-utf8-byte-upper-bound", "9.9.9", "harness-token-counter/2"),
        ("harness-utf8-byte-upper-bound", "1.0.0", "harness-token-counter/1"),
    ],
)
def test_counter_identity_mismatch_is_rejected(
    tokenizer_name: str, tokenizer_version: str, counting_adapter_version: str
) -> None:
    """Tokenizer名・version・計数Adapter Versionのどれが違っても停止する（§1.12）。"""
    profile = _profile()
    profile.require_counter_identity(
        tokenizer_name="harness-utf8-byte-upper-bound",
        tokenizer_version="1.0.0",
        counting_adapter_version="harness-token-counter/2",
    )
    with pytest.raises(HarnessError) as error:
        profile.require_counter_identity(
            tokenizer_name=tokenizer_name,
            tokenizer_version=tokenizer_version,
            counting_adapter_version=counting_adapter_version,
        )
    assert error.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_budget_exceeding_provider_context_limit_is_rejected() -> None:
    with pytest.raises(HarnessError) as error:
        _build(
            (_fragment("a", token_count=1),),
            policy=_policy(total_tokens=9000),
            profile=_profile(context_limit=4096),
        )
    assert error.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED
