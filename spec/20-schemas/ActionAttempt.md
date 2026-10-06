<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 4. ActionAttempt

共通必須Field：

`attempt_id, action_id, attempt_number, state, store_version`

状態依存Field候補：

`plan_content_hash, execution_plan_hash, worker_id, claim_id, lease_id, fencing_token, runtime_attestation_hash, operation_journal_id, started_at, ended_at, receipt_ids, error_classification`

JSON SchemaはState別`oneOf`または`allOf`内の`if/then`で次を強制する。

| State | 必須Field | 未設定可／禁止 |
|---|---|---|
| `PLANNING` | 共通必須Field | Plan Hash、Worker、Lease、Runtime、開始／終了、Receiptは未設定 |
| `WAITING_POLICY`／`WAITING_APPROVAL`／`READY` | 共通＋`plan_content_hash, execution_plan_hash` | Worker、Lease、Runtime、開始／終了、Receiptは未設定可 |
| `CLAIMED` | Plan必須Field＋`worker_id, claim_id` | Lease／Runtime／開始／終了情報は未設定可 |
| `LEASED` | CLAIMED必須Field＋`lease_id, fencing_token` | Runtime／開始／終了情報は未設定可 |
| `RUNTIME_VERIFIED` | LEASED必須Field＋`runtime_attestation_hash` | `started_at, ended_at, receipt_ids`は未設定可 |
| `RUNNING` | RUNTIME_VERIFIED必須Field＋`started_at` | `ended_at, receipt_ids`は未設定可 |
| `PREPARED_DURABLE` | RUNNING必須Field＋`operation_journal_id` | Receiptは未設定可 |
| `EFFECT_IN_FLIGHT`／`EFFECT_VERIFIED` | PREPARED必須Field | `EFFECT_VERIFIED`では観測Evidence必須。Receiptは未設定可 |
| `RECEIPT_DURABLE` | PREPARED必須Field＋非空`receipt_ids` | 終了時刻は未設定可 |
| `SUCCEEDED` | Plan Hash、Runtime Attestation、`started_at, ended_at`。Effect Actionは`operation_journal_id`と非空`receipt_ids` | `error_classification`は未設定または`NONE` |
| `FAILED_RETRYABLE`／`FAILED_PERMANENT`／`BLOCKED_*`／`CANCELLED`／`CANCEL_UNKNOWN`／`EFFECT_UNKNOWN` | `ended_at, error_classification` | Plan確定前のBlockだけPlan Hash未設定可。Success Receiptを禁止 |

制約：

* `(action_id, attempt_number)`一意。
* `plan_content_hash`と`execution_plan_hash`は片方だけ存在してはならない。
* 終端Stateから同一Attemptを再開不可。
* Effect Stateでは`fencing_token`と`operation_journal_id`必須。
* `SUCCEEDED`では`runtime_attestation_hash`と必要なReceipt必須。
* `PLANNING`から`PLAN_RESOLVED`前に`execution_plan_hash`を要求しない。
* StateとFieldの組合せが不正なRecordは`schema_conditional_violation`を記録して拒否する。

Schema完全Valid Fixture（`READY`。Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "ActionAttempt",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000020",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:01:00Z",
  "producer": "harness-core/1.7.0",
  "content_hash": "sha256:5555555555555555555555555555555555555555555555555555555555555555",
  "attempt_id": "018f0000-0000-7000-8000-000000000021",
  "action_id": "018f0000-0000-7000-8000-000000000022",
  "attempt_number": 1,
  "state": "READY",
  "store_version": 1,
  "plan_content_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "execution_plan_hash": "sha256:3333333333333333333333333333333333333333333333333333333333333333"
}
```

拒否Fixture：

* `state=RUNNING`で`runtime_attestation_hash`欠落。
* `state=SUCCEEDED`かつEffect Actionで`receipt_ids=[]`。
* `state=PLANNING`で`execution_plan_hash`だけ存在。
* `state=READY`で`plan_content_hash`欠落。

Upcaster：旧`CANCELLED_CONFIRMED`は`CANCELLED`へLossless変換。
