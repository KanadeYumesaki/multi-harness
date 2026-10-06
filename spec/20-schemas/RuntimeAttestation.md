<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 16. RuntimeAttestation

共通必須Field：

`runtime_attestation_id, runtime_envelope_spec_hash, invocation_manifest_hash, launcher_hash, executable_identity, executable_hash, adapter_hash, provider_id, model_id, model_digest, auth_route, credential_type, provider_account_id, tenant_id, billing_identity, effective_argv, effective_cwd_identity, effective_user, effective_environment_hash, config_tree_hash, workspace_mount_id, workspace_filesystem_type, workspace_mount_options_hash, filesystem_policy_evidence, network_policy_evidence, sandbox_status, tool_plugin_hook_mcp_evidence, resource_limit_evidence, process_tree_root, fault_injection_enabled, started_at, assurance_level, attestation_hash`

External／Paid条件Field：

`technical_capability_snapshot_hash, commercial_entitlement_snapshot_hash, pricing_catalog_hash, entitlement_evidence_artifact_hash, budget_reservation_id, budget_reservation_hash, cost_approval_hash, fault_injection_plan_hash, emergency_recovery_profile_version, emergency_recovery_profile_hash`

JSON Schema条件：

| Runtime | 条件 |
|---|---|
| Mock | Provider／Modelは計画値。Auth／Account／Tenant／Billingは明示値`NONE`。External条件Fieldはnullまたは未設定 |
| Local | Provider／Model／Digestを実測。Auth／Account条件はLocal PolicyとPlanに一致 |
| External | Auth Route、Credential Type、Provider Account、Tenant、Billing Identity、Capability／Entitlement／Pricing／Evidence Hashを全て必須 |
| Paid | External条件に加えBudget Reservation ID／HashとCost Approval Hashを必須 |
| Fault Injection有効 | `fault_injection_plan_hash`必須。Policy許可と一致 |
| Emergency Recovery | Profile Version／Hash必須。通常実行ではnullまたは未設定 |

制約：

* Spec不一致Fieldを明示。
* `sandbox_status=ENFORCED`以外はSandbox必須Action不可。
* Secret Valueを含めない。
* `runtime_envelope_spec_hash`、`invocation_manifest_hash`、Provider／Model／Auth／Account／Tenant／Billing、Capability／Entitlement／PricingがExecutionPlanと完全一致する。
* Runtimeが観測できないIdentity Fieldを`UNKNOWN`で埋めて実行してはならない。
* Workspace Mount情報は起動時検査結果と一致する。
* `fault_injection_enabled=false`ではPlan Hashをnull、trueではPlan Hashを必須とする。

Schema完全Valid Fixture（Mock。Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "RuntimeAttestation",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000060",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:05:00Z",
  "producer": "runtime-launcher/1.7.0",
  "content_hash": "sha256:2626262626262626262626262626262626262626262626262626262626262626",
  "runtime_attestation_id": "018f0000-0000-7000-8000-000000000061",
  "runtime_envelope_spec_hash": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "invocation_manifest_hash": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "launcher_hash": "sha256:2727272727272727272727272727272727272727272727272727272727272727",
  "executable_identity": "/opt/harness/bin/mock-provider",
  "executable_hash": "sha256:2828282828282828282828282828282828282828282828282828282828282828",
  "adapter_hash": "sha256:2929292929292929292929292929292929292929292929292929292929292929",
  "provider_id": "mock",
  "model_id": "mock-deterministic-v1",
  "model_digest": "sha256:3030303030303030303030303030303030303030303030303030303030303030",
  "auth_route": "NONE",
  "credential_type": "NONE",
  "provider_account_id": "NONE",
  "tenant_id": "NONE",
  "billing_identity": "NONE",
  "effective_argv": ["/opt/harness/bin/mock-provider", "--request", "/run/request.json"],
  "effective_cwd_identity": "linux:dev=8:inode=1001:mount=23",
  "effective_user": "uid:10001",
  "effective_environment_hash": "sha256:3131313131313131313131313131313131313131313131313131313131313131",
  "config_tree_hash": "sha256:3232323232323232323232323232323232323232323232323232323232323232",
  "filesystem_policy_evidence": {
    "status": "ENFORCED"
  },
  "network_policy_evidence": {
    "status": "DENY_ALL"
  },
  "sandbox_status": "ENFORCED",
  "tool_plugin_hook_mcp_evidence": {
    "status": "DISABLED"
  },
  "resource_limit_evidence": {
    "cpu_seconds": 30,
    "memory_bytes": 268435456
  },
  "process_tree_root": "pid:4100",
  "started_at": "2026-08-05T06:05:00Z",
  "assurance_level": "VERIFIED",
  "attestation_hash": "sha256:3434343434343434343434343434343434343434343434343434343434343434"
}
```

拒否Fixture：

* External RuntimeでEntitlement／Pricing Hash欠落。
* Executable Hash不一致。
* `sandbox_status=WARNING`。
* PlanのBilling IdentityとAttestationが不一致。

Upcaster：EvidenceまたはRuntime Identity追加は新Attestation。
