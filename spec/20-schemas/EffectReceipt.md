<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 18. EffectReceipt

共通必須Field：

`receipt_id, effect_id, operation_journal_id, run_id, action_id, attempt_id, effect_type, effect_subject_type, effect_subject_id, target_resource_identity, before_hash, expected_after_hash, observed_hash, observation_method, confirmation_level, fencing_token, prepared_event_id, execution_attempted_event_id, observed_at, durability_level, receipt_hash`

状態／Effect依存Field：

`remote_invocation_registry_id, outbox_id, budget_reservation_id, remote_object_reference_hash, provider_receipt_hash, usage_evidence_hash`

制約：

* `effect_id`一意。
* `operation_journal_id`は存在する同一`effect_id`のJournalを参照する。
* Expected／Observed不一致はCommitted不可。
* Fencing TokenはCurrent Tokenと一致。
* `EFFECT_UNKNOWN`では成功Receiptを作成しない。
* Multi-fileではOperation ReceiptとAggregate Receiptを分離。
* `effect_type=REMOTE_INVOCATION`では`remote_invocation_registry_id`必須。
* `effect_type=EXTERNAL_DISPATCH`では`outbox_id`必須。
* `effect_type=BUDGET_RESERVATION`では`budget_reservation_id`必須。
* Remote／Outbox／BudgetのReceiptは、各Storeの正規Stateと§1.14.1のLedger Event Sequenceを照合する。
* Receipt保存前にOperationJournalが`EFFECT_OBSERVED`以上でない場合は拒否する。

Schema完全Valid Fixture（Remote Invocation。Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "EffectReceipt",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000070",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:06:00Z",
  "producer": "effect-reconciler/1.7.0",
  "content_hash": "sha256:3535353535353535353535353535353535353535353535353535353535353535",
  "receipt_id": "018f0000-0000-7000-8000-000000000071",
  "effect_id": "018f0000-0000-7000-8000-000000000072",
  "operation_journal_id": "018f0000-0000-7000-8000-000000000073",
  "action_id": "018f0000-0000-7000-8000-000000000022",
  "attempt_id": "018f0000-0000-7000-8000-000000000021",
  "effect_type": "REMOTE_INVOCATION",
  "effect_subject_type": "REMOTE_INVOCATION_REGISTRY",
  "effect_subject_id": "018f0000-0000-7000-8000-000000000074",
  "target_resource_identity": "provider-a:tenant-001:request",
  "before_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "expected_after_hash": "sha256:3636363636363636363636363636363636363636363636363636363636363636",
  "observed_hash": "sha256:3636363636363636363636363636363636363636363636363636363636363636",
  "observation_method": "PROVIDER_RESPONSE_AND_STATUS_LOOKUP",
  "confirmation_level": "REMOTE_RESPONSE_OBSERVED",
  "fencing_token": 42,
  "prepared_event_id": "018f0000-0000-7000-8000-000000000075",
  "execution_attempted_event_id": "018f0000-0000-7000-8000-000000000076",
  "observed_at": "2026-08-05T06:06:00Z",
  "durability_level": "DB_AND_STORAGE_SYNC",
  "remote_invocation_registry_id": "018f0000-0000-7000-8000-000000000074",
  "remote_object_reference_hash": "sha256:3737373737373737373737373737373737373737373737373737373737373737",
  "provider_receipt_hash": "sha256:3838383838383838383838383838383838383838383838383838383838383838",
  "usage_evidence_hash": "sha256:3939393939393939393939393939393939393939393939393939393939393939",
  "receipt_hash": "sha256:4040404040404040404040404040404040404040404040404040404040404040"
}
```

拒否Fixture：

* Observed Hash不一致でSuccess。
* Remote Invocationなのに`operation_journal_id`またはRegistry ID欠落。
* Journalが`PREPARED_DURABLE`のままなのにReceipt保存。
* Fencing Token不一致。

Upcaster：Observation、Effect Subject、Journal参照意味の変更はMajor。
