<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 19. OperationJournal

共通必須Field：

`operation_journal_id, operation_id, effect_id, run_id, action_id, attempt_id, operation_type, target_resource_identity, state, fencing_token, prepared_event_id, prepared_at, durability_level, store_version, journal_hash`

`operation_type`正規Enum：

`LOCAL_FILE_COMMIT | WORKSPACE_CHANGE | REMOTE_INVOCATION | EXTERNAL_DISPATCH | BUDGET_RESERVATION | COMPENSATION`

状態依存Field：

`before_hash, expected_after_hash, execution_attempted_event_id, execution_attempted_at, observed_hash, observation_method, observed_at, receipt_id, phase_store_type, phase_store_id, error_classification`

状態条件：

| State | 必須 |
|---|---|
| `PREPARED_DURABLE` | `before_hash, expected_after_hash, prepared_event_id, prepared_at, durability_level` |
| `EXECUTION_ATTEMPTED` | PREPARED必須Field＋`execution_attempted_event_id, execution_attempted_at` |
| `EFFECT_OBSERVED` | EXECUTION必須Field＋`observed_hash, observation_method, observed_at` |
| `RECEIPT_DURABLE` | EFFECT_OBSERVED必須Field＋`receipt_id` |
| `EFFECT_UNKNOWN`／`EFFECT_CONFLICT` | `error_classification`必須。自動再実行・Release禁止 |

制約：

* `operation_id`と`effect_id`は一意。
* State Enumで`CONFLICT`を使用しない。全Effect競合は`EFFECT_CONFLICT`へ統一する。
* SQLite規範Migrationは`UNIQUE(operation_id)`, `UNIQUE(effect_id)`, `CHECK(state IN (..., 'EFFECT_CONFLICT'))`, `store_version`によるCASを持つ。
* `PREPARED_DURABLE`はJournal Transaction Commitと必要なfsync完了後だけ設定。
* EffectReceiptの`operation_journal_id`は存在する同一EffectのJournalを参照。
* `REMOTE_INVOCATION`、`EXTERNAL_DISPATCH`、`BUDGET_RESERVATION`は`phase_store_type, phase_store_id`必須。
* Outbox／Remote Registry／Budget StoreのStateは§1.14.1の正規Ledger Eventへ写像し、Journal Stateと矛盾してはならない。
* 過去StateをUPDATEで巻き戻さず、遷移RecordまたはAppend-only Historyで監査可能にする。

Schema完全Valid Fixture（Outbox Prepared。Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "OperationJournal",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000080",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:07:00Z",
  "producer": "effect-executor/1.7.0",
  "content_hash": "sha256:4141414141414141414141414141414141414141414141414141414141414141",
  "operation_journal_id": "018f0000-0000-7000-8000-000000000081",
  "operation_id": "018f0000-0000-7000-8000-000000000082",
  "effect_id": "018f0000-0000-7000-8000-000000000083",
  "action_id": "018f0000-0000-7000-8000-000000000022",
  "attempt_id": "018f0000-0000-7000-8000-000000000021",
  "operation_type": "EXTERNAL_DISPATCH",
  "target_resource_identity": "tenant-001:channel-001",
  "state": "PREPARED_DURABLE",
  "fencing_token": 42,
  "prepared_event_id": "018f0000-0000-7000-8000-000000000084",
  "prepared_at": "2026-08-05T06:07:00Z",
  "durability_level": "DB_AND_STORAGE_SYNC",
  "store_version": 1,
  "before_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "expected_after_hash": "sha256:4242424242424242424242424242424242424242424242424242424242424242",
  "phase_store_type": "TRANSACTIONAL_OUTBOX",
  "phase_store_id": "018f0000-0000-7000-8000-000000000085",
  "journal_hash": "sha256:4343434343434343434343434343434343434343434343434343434343434343"
}
```

拒否Fixture：

* `state=CONFLICT`。
* `EXECUTION_ATTEMPTED`なのに`prepared_event_id`欠落。
* `operation_type=EXTERNAL_DISPATCH`で`phase_store_id`欠落。
* EffectReceiptと異なる`effect_id`。

Upcaster：Effect順序、Durability、Effect SubjectまたはState名の変更はMajor。
