<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 2. Run

共通必須Field：

`run_id, task_id, state, created_by, unresolved_action_count, risk_level, store_version`

状態依存Field：

`plan_version, current_plan_content_hash, current_execution_plan_hash, policy_snapshot_hash, release_decision_id, started_at, ended_at, terminal_reason_code, blocked_reason_code, unreconciled_effect_carried_to_run_id, related_run_id`

JSON SchemaはState別`oneOf`または`if/then`で次を強制する。

| State | 必須Field | 禁止／未設定可 |
|---|---|---|
| `CREATED` | 共通必須Field | Plan Hash、Policy Hash、Release、開始／終了時刻は未設定 |
| `PLANNING` | 共通必須Field | Plan Hash、Releaseは未設定。`started_at`は任意 |
| `WAITING_APPROVAL`／`READY`／`RUNNING`／`RECOVERING`／`WAITING_RELEASE` | 共通＋`plan_version, current_plan_content_hash, current_execution_plan_hash, policy_snapshot_hash` | `release_decision_id`は`WAITING_RELEASE`まで未設定 |
| `BLOCKED_REPAIR_REQUIRED` | 共通＋`blocked_reason_code` | `ended_at`、`terminal_reason_code`は禁止。Plan確定後なら両Plan Hashを保持 |
| `COMPLETED` | 共通＋全Plan／Policy Hash＋`release_decision_id, started_at, ended_at` | `unresolved_action_count=0`、Release Decision=`RELEASE` |
| `BLOCKED`／`FAILED`／`CANCELLED` | 共通＋`ended_at, terminal_reason_code` | Plan確定前の終端はPlan Hash未設定可。確定後なら両Plan Hashを保持 |

制約：

* Stateは§1.5のEnum。
* `BLOCKED_REPAIR_REQUIRED`は非終端で`ended_at=null`。
* `EFFECT_UNKNOWN`、`CANCEL_UNKNOWN`、未解決Manual Queue、欠損Artifactを持つRunは全終端状態へ遷移不可。
* `COMPLETED`では`release_decision_id`必須、`unresolved_action_count=0`。
* `current_plan_content_hash`と`current_execution_plan_hash`は片方だけ存在してはならない。
* Plan再発行では`plan_version`を単調増加し、旧HashをLedgerから削除しない。
* `unreconciled_effect_carried_to_run_id`の引継ぎ深度は1、自己参照・循環参照は禁止。
* Mutable更新は`store_version`のCASを必須とする。

Schema完全Valid Fixture（Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "Run",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000010",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:00:00Z",
  "producer": "harness-core/1.7.0",
  "content_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "task_id": "018f0000-0000-7000-8000-000000000013",
  "state": "WAITING_APPROVAL",
  "plan_version": 1,
  "current_plan_content_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "current_execution_plan_hash": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
  "policy_snapshot_hash": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
  "created_by": "operator:local",
  "unresolved_action_count": 1,
  "risk_level": "LOW",
  "store_version": 1
}
```

拒否Fixture：

* `state=COMPLETED`で`release_decision_id`欠落。
* `state=BLOCKED_REPAIR_REQUIRED`で`ended_at`存在。
* 未照合Effectがあるのに`state=FAILED`。
* `state=WAITING_APPROVAL`で両Plan Hashが片方だけ存在。

Upcaster：State追加は未知値として拒否し、Operator更新を要求。
