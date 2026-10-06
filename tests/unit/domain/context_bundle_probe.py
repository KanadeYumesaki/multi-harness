"""別Processで`bundle_hash`を計算して標準出力へ返すProbe。

不変条件#5／#6は「同一プロセス内の二重Buildは`PYTHONHASHSEED`依存の非決定性を
検出できない」と定める。`str`のHash値はProcess内で固定されるため、`set`反復が
1箇所でも混入していても二重Buildは常に一致してしまう。

したがって本Fileを`PYTHONHASHSEED`を変えた子Processで実行し、
Hash一致を外から確認する。pytestが収集しないよう`test_`接頭辞を付けない。
"""

from __future__ import annotations

import sys

from harness.domain.context_budget import (
    ContextFragment,
    EstimateAssurance,
    MessageRole,
    TokenBudgetPolicy,
    TokenOverheads,
    TokenProfileSnapshot,
    build_context_bundle,
)
from harness.domain.hashing import hash_canonical


def _fragment(name: str, *, token_count: int, priority: int, mandatory: bool) -> ContextFragment:
    return ContextFragment(
        fragment_id=name,
        fragment_content_hash=hash_canonical(
            {"name": name}, artifact_type="test-value", schema_major=1
        ),
        token_count=token_count,
        message_role=MessageRole.VERIFIED_REFERENCE_DATA,
        control_authority=False,
        instruction_eligible=False,
        mandatory=mandatory,
        priority=priority,
        source_artifact_id=f"artifact-{name}",
        input_read_capability_id=f"capability-{name}",
        classification_scan_evidence_hash=hash_canonical(
            {"scan": name}, artifact_type="test-value", schema_major=1
        ),
    )


def main() -> int:
    policy = TokenBudgetPolicy(
        total_tokens=200,
        reserved_output_tokens=20,
        reserved_tool_tokens=10,
        policy_id="probe-policy",
    )
    profile = TokenProfileSnapshot(
        snapshot_id="probe-snapshot",
        provider="mock",
        model="mock-model",
        tokenizer_name="harness-utf8-byte-upper-bound",
        tokenizer_version="1.0.0",
        counting_adapter_version="harness-token-counter/2",
        context_limit=4096,
        maximum_output_limit=1024,
        estimate_assurance=EstimateAssurance.CONSERVATIVE,
        overheads=TokenOverheads(
            system_message_overhead=3,
            developer_message_overhead=0,
            tool_definition_overhead=0,
            per_message_overhead=2,
            structured_output_overhead=0,
            streaming_frame_overhead=0,
            retry_fallback_reservation=0,
        ),
        retrieved_at="2026-08-16T00:00:00Z",
        expires_at="2026-08-16T01:00:00Z",
    )
    # 語順・優先度・必須指定を混在させ、整列規則が効いていることを外から見る。
    fragments = (
        _fragment("zulu", token_count=10, priority=1, mandatory=False),
        _fragment("alpha", token_count=10, priority=5, mandatory=True),
        _fragment("mike", token_count=10, priority=5, mandatory=False),
        _fragment("bravo", token_count=10, priority=5, mandatory=False),
    )
    assembly = build_context_bundle(
        fragments,
        policy,
        profile,
        bundle_id="probe-bundle",
        receipt_id="probe-receipt",
        input_read_capability_set_hash=hash_canonical(
            {"capabilities": ["probe"]}, artifact_type="test-value", schema_major=1
        ),
        input_read_evidence_hash=hash_canonical(
            {"evidence": ["probe"]}, artifact_type="test-value", schema_major=1
        ),
    )
    sys.stdout.write(
        f"{assembly.bundle.bundle_hash}\n"
        f"{','.join(assembly.bundle.ordered_fragment_ids)}\n"
        f"{assembly.receipt.decision_hash}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
