<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 14. ApprovalGrant

共通必須Field：

`grant_id, plan_content_hash, execution_plan_hash, action_scope, approver_subject_id, approver_tenant_id, authentication_context_class, mfa_performed, authentication_time, issued_at, not_before, expires_at, maximum_clock_skew_seconds, nonce, use_count, revocation_epoch, issuer_id, issuer_key_id, signature_algorithm, signature, status, store_version`

状態依存Field候補：

`consumed_at, consumed_by_actor_id, attempt_id, revoked_at, revoked_by_actor_id, revocation_reason, invalidated_at, invalidation_reason`

JSON Schemaは`allOf`内の`if/then`またはStatus別`$defs`で次を強制する。

| Status | 条件 |
|---|---|
| `ISSUED` | Consume／Revoke／Invalidate Fieldは未設定。`use_count=1` |
| `CONSUMED` | `consumed_at, consumed_by_actor_id, attempt_id`必須。CAS監査Record Hashを署名対象へ束縛 |
| `REVOKED` | `revoked_at, revoked_by_actor_id, revocation_reason`必須。Consume Fieldは禁止 |
| `INVALIDATED` | `invalidated_at, invalidation_reason`必須。Plan／Policy／Runtime前提変更時に使用 |
| `EXPIRED` | `expires_at`が判定時刻を超過。Consume Fieldは禁止 |

制約：

* `use_count=1`。
* Grant ID、Nonce一意。
* Signature検証必須。
* `plan_content_hash`は表示・差分監査用、実行Authorityは`execution_plan_hash`へ束縛する。
* Consume時にActor、Attempt、Consumed Atを同一CASで記録する。
* Secret値禁止。
* StateとFieldの組合せが不正なRecordは`schema_conditional_violation`を記録して拒否する。

Schema完全Valid Fixture（`CONSUMED`。署名・Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "ApprovalGrant",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000050",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:04:00Z",
  "producer": "approval-service/1.7.0",
  "content_hash": "sha256:2525252525252525252525252525252525252525252525252525252525252525",
  "grant_id": "018f0000-0000-7000-8000-000000000051",
  "plan_content_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "execution_plan_hash": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
  "action_scope": ["018f0000-0000-7000-8000-000000000022"],
  "approver_subject_id": "operator:local",
  "approver_tenant_id": "tenant:local",
  "authentication_context_class": "urn:harness:os-login",
  "mfa_performed": false,
  "authentication_time": "2026-08-05T06:03:30Z",
  "issued_at": "2026-08-05T06:04:00Z",
  "not_before": "2026-08-05T06:04:00Z",
  "expires_at": "2026-08-05T06:14:00Z",
  "maximum_clock_skew_seconds": 30,
  "nonce": "f27835d5f82d4f149c5ce11eb703e233",
  "use_count": 1,
  "revocation_epoch": 7,
  "issuer_id": "approval-service:local",
  "issuer_key_id": "key-2026-08",
  "signature_algorithm": "Ed25519",
  "signature": "base64url:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "status": "CONSUMED",
  "store_version": 2,
  "consumed_at": "2026-08-05T06:05:00Z",
  "consumed_by_actor_id": "worker:local-1",
  "attempt_id": "018f0000-0000-7000-8000-000000000021"
}
```

拒否Fixture：

* 同Nonce再利用。
* `status=CONSUMED`で`attempt_id`欠落。
* `status=ISSUED`なのに`consumed_at`存在。
* `execution_plan_hash`だけ変更して署名未更新。

Upcaster：署名対象またはPlan Hash二層構造の変更はMajor、新Grant発行。
