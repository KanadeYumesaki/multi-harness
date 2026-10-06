<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 5. ExecutionPlan

必須Field：

`execution_plan_id, plan_version, run_id, issued_at, expires_at, planner_identity, authority_scope, action_graph, context_bundle_hash, input_read_capability_set_hash, input_read_evidence_hash, control_data_policy_hash, token_budget_policy_hash, token_profile_snapshot_hash, runtime_envelope_spec_hash, invocation_manifest_hash, policy_snapshot_hash, schema_set_hash, cost_upper_bound, hash_profile_version, plan_content_hash, execution_plan_hash`

制約：

* Approval前に全Content HashとAuthority Fieldを確定する。
* `hash_profile_version=1`。
* `plan_content_hash`は§1.11.1の`PlanContentProjection`から計算する。
* `execution_plan_hash`は§1.11.1のAuthority Envelopeから計算する。
* `run_id, execution_plan_id, plan_version, issued_at, expires_at`を`plan_content_hash`へ含めない。
* External／PaidではExact Auth／Account／Entitlement／Pricing参照必須。
* Content変更時は両Hashが変わる。Authority再発行だけの場合はContent Hashを維持しExecution Hashを変更する。
* `expires_at > issued_at`。
* Plan ContentのCanonical再計算結果と保存`plan_content_hash`が一致しないRecordは拒否する。
* `action_graph`はランダムAction IDではなく`semantic_action_key`を使用し、Topological Order＋Keyで決定的に整列する。

Schema完全Valid Fixture（Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "ExecutionPlan",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000030",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:02:00Z",
  "producer": "harness-core/1.7.0",
  "content_hash": "sha256:6666666666666666666666666666666666666666666666666666666666666666",
  "execution_plan_id": "018f0000-0000-7000-8000-000000000031",
  "plan_version": 1,
  "issued_at": "2026-08-05T06:02:00Z",
  "expires_at": "2026-08-05T06:17:00Z",
  "planner_identity": "harness-planner/1.7.0",
  "authority_scope": "ACTION_EXECUTION",
  "action_graph": {
    "nodes": ["action:sha256:4545454545454545454545454545454545454545454545454545454545454545"],
    "edges": []
  },
  "context_bundle_hash": "sha256:7777777777777777777777777777777777777777777777777777777777777777",
  "input_read_capability_set_hash": "sha256:8888888888888888888888888888888888888888888888888888888888888888",
  "input_read_evidence_hash": "sha256:9999999999999999999999999999999999999999999999999999999999999999",
  "control_data_policy_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "token_budget_policy_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "token_profile_snapshot_hash": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "runtime_envelope_spec_hash": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "invocation_manifest_hash": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "policy_snapshot_hash": "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
  "schema_set_hash": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "cost_upper_bound": {
    "currency": "JPY",
    "amount": "0"
  },
  "hash_profile_version": 1,
  "plan_content_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "execution_plan_hash": "sha256:3333333333333333333333333333333333333333333333333333333333333333"
}
```

拒否Fixture：

* `run_id`だけ変更したのに`execution_plan_hash`を再計算していない。
* 同じContent Projectionで`plan_content_hash`が異なる。
* `expires_at <= issued_at`。
* Runtime Spec Hashがnull。

Upcaster：Hash対象変更、Plan Content Projection変更はMajorで再Plan必須。
