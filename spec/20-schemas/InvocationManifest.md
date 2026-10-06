<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 7. InvocationManifest

共通必須Field：

`invocation_id, invocation_mode, provider_id, model_id, billing_mode, provider_operation, request_artifact_hash, context_bundle_hash, instruction_hash, message_role_manifest_hash, control_data_policy_hash, tool_definition_hash, output_schema_hash, endpoint_identity, auth_route, credential_reference_version, provider_account_id, tenant_id, billing_identity, retry_policy, cancel_policy, idempotency_policy, token_profile_snapshot_hash, expected_effect, invocation_manifest_hash`

External／Paid条件Field：

`technical_capability_snapshot_hash, commercial_entitlement_snapshot_hash, pricing_catalog_hash, entitlement_evidence_artifact_hash`

JSON Schema条件：

| 条件 | 必須 |
|---|---|
| `invocation_mode=MOCK` | Auth／Account／Tenant／Billingは明示値`NONE`。External条件Fieldはnullまたは未設定 |
| `invocation_mode=LOCAL` | EndpointはLoopback／Local Socket。Auth条件はLocal Policyに従う。External Entitlement／Pricingは未設定可 |
| `invocation_mode=EXTERNAL` | External／Paid条件Fieldを全て必須。Endpoint、Auth Route、Credential Version、Provider Account、Tenant、Billing Identityは`NONE`不可 |
| `billing_mode=PAID` | Commercial Entitlement、Pricing Catalog、Billing Identity、Cost Approval参照を必須 |
| `expected_effect != NONE` | Idempotency Policy、Operation Journal Policy、Reconciliation Policyを必須 |

制約：

* `expected_effect=NONE`はMock／Local Read-onlyの初期値。
* ExternalではEndpoint、Capability、Entitlement、Pricing、Evidence参照必須。
* `billing_mode=UNKNOWN`ではRuntime GO不可。
* Non-idempotentでは自動Retry最大0。明示再送は新Attempt＋再照合。
* Exact Provider／Model／Auth／Account／Tenant／Billing IdentityとSnapshot HashをPlan Contentへ含める。Snapshot Record IDはSemantic Hashへ含めない。
* Secret値を含めない。

Schema完全Valid Fixture（External Read-only。Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "InvocationManifest",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000040",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:03:00Z",
  "producer": "harness-core/1.7.0",
  "content_hash": "sha256:1010101010101010101010101010101010101010101010101010101010101010",
  "invocation_id": "018f0000-0000-7000-8000-000000000041",
  "invocation_mode": "EXTERNAL",
  "provider_id": "provider-a",
  "model_id": "model-a",
  "billing_mode": "PAID",
  "provider_operation": "INFER",
  "request_artifact_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "context_bundle_hash": "sha256:1212121212121212121212121212121212121212121212121212121212121212",
  "instruction_hash": "sha256:1313131313131313131313131313131313131313131313131313131313131313",
  "message_role_manifest_hash": "sha256:1414141414141414141414141414141414141414141414141414141414141414",
  "control_data_policy_hash": "sha256:1515151515151515151515151515151515151515151515151515151515151515",
  "tool_definition_hash": "sha256:1616161616161616161616161616161616161616161616161616161616161616",
  "output_schema_hash": "sha256:1717171717171717171717171717171717171717171717171717171717171717",
  "endpoint_identity": "https://api.provider.example/v1/infer",
  "auth_route": "OAUTH_WORKLOAD",
  "credential_reference_version": "secret/provider-a/v3",
  "provider_account_id": "acct-001",
  "tenant_id": "tenant-001",
  "billing_identity": "billing-001",
  "retry_policy": {
    "maximum_attempts": 1
  },
  "cancel_policy": {
    "supported": true
  },
  "idempotency_policy": {
    "supported": true,
    "scope": "ACCOUNT"
  },
  "token_profile_snapshot_hash": "sha256:1818181818181818181818181818181818181818181818181818181818181818",
  "technical_capability_snapshot_hash": "sha256:1919191919191919191919191919191919191919191919191919191919191919",
  "commercial_entitlement_snapshot_hash": "sha256:2020202020202020202020202020202020202020202020202020202020202020",
  "pricing_catalog_hash": "sha256:2121212121212121212121212121212121212121212121212121212121212121",
  "entitlement_evidence_artifact_hash": "sha256:2323232323232323232323232323232323232323232323232323232323232323",
  "expected_effect": "REMOTE_INVOCATION",
  "invocation_manifest_hash": "sha256:2424242424242424242424242424242424242424242424242424242424242424"
}
```

拒否Fixture：

* `invocation_mode=EXTERNAL`で`commercial_entitlement_snapshot_hash`欠落。
* Externalで`billing_identity=NONE`。
* Non-idempotentなのに自動Retry>0。
* Secret値を含むCredential Field。

Upcaster：Auth Route、Account／Tenant／Billing、Entitlement／Pricing条件の変更はMajor。
