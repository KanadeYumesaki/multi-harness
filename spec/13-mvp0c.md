<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

# 5. MVP0-C：External Provider Read-only 詳細設計

## 5.1 目的

外部ProviderをRead-onlyで利用し、Endpoint Allowlist、認証、Data Classification、Retention／Residency、Commercial Entitlement、Budget、Remote Cancellationを実証する。

## 5.2 追加コンポーネント

* External Provider Adapter
* Egress Policy EnforcerまたはHarness-managed Proxy
* Secret Reference Resolver
* Data Classification Gate
* DLP／PII Scanner
* Commercial Entitlement Collector
* Provider Terms／Retention Registry
* Usage Collector
* Remote Invocation Registry
* Cancel／Status Reconciler

## 5.3 External Invocation Flow

```text
ContextBundle生成
  → Data Classification／Trust評価
  → DLP／PII Scan
  → Technical Capability Snapshot
  → Commercial Entitlement Snapshot
  → Auth Route／Credential Type／Account／Tenant／Billing Identity確定
  → Retention／Residency／Training／Logging Policy評価
  → Pricing Catalog Snapshot
  → Cost Upper Bound算出
  → RuntimeEnvelopeSpec／InvocationManifest確定
  → Plan Content Hash／Execution Plan Hash確定
  → Policy Decision
  → Exact Provider + Exact Auth Route + Exact Account Approval
  → Approval Consume／Claim／Lease
  → Secretを実行時注入
  → Runtime Attestation
  → ACTION_STARTED
  → OperationJournal + Remote Invocation Registryを同一Durable境界でPREPARED_DURABLE
  → REMOTE_INVOCATION_PREPARED + ACTION_PREPARED
  → Registry REQUEST_DISPATCHING + Journal EXECUTION_ATTEMPTEDをDurable Commit
  → REMOTE_REQUEST_DISPATCHING + EXECUTION_ATTEMPTED
  → Egress ProxyでRegistry State／Journal／Fencing Token再検証
  → Egress Invoke
  → REMOTE_ID_RECORDED または REMOTE_INVOCATION_UNCERTAIN + EFFECT_UNKNOWN
  → Output受信
  → Raw Output隔離
  → Usage収集
  → Output Normalize
  → Proposed Artifact
  → Evaluation
  → Release
```

外部・有償Actionでは、Candidate Set ApprovalだけでRuntime GOを出さず、最終的に選択された`provider_id + model_id + auth_route + credential_type + provider_account + tenant + billing_identity + endpoint + entitlement_snapshot + pricing_catalog`をExact Approvalへ束縛する。

## 5.4 Data Classification Gate

共通の§1.13を適用する。

機密度Label：

* `PUBLIC`
* `INTERNAL`
* `CONFIDENTIAL`
* `RESTRICTED`
* `SECRET`

追加取扱Label：

* `PERSONAL_DATA`
* `SPECIAL_CATEGORY_DATA`
* `REGULATED_DATA`

Providerごとに送信可能な機密度と追加取扱LabelをPolicyへ定義する。MVP0-Cでは`PUBLIC`と明示許可された`INTERNAL`だけを許可し、Personal／Special Category／Regulated Labelを持つデータは送信しない。

### Gate入力

* FragmentごとのClassification Labels
* Source Type／Trust Level
* Provider Region
* Data Residency
* Retention／Training条件
* Tenant／Enterprise Data Protection条件
* Prompt／Output Logging条件
* Subprocessor条件
* DLP結果
* Operator Approval
* Auth Route／Account／Tenant
* Commercial Entitlement Snapshot

複数Labelは最も厳しい機密度と全追加LabelのAND条件で評価する。分類不明は`RESTRICTED + UNKNOWN`。自動Downgradeは禁止する。

## 5.5 Endpoint Allowlist

Planへ以下を含める。

* Scheme=`https`
* Host
* Port
* Path Pattern
* Auth Host
* Proxy ID
* DNS Policy
* TLS Policy
* Redirect Policy
* Certificate Validation Mode
* Provider Tenant Scope

Redirectは原則禁止。必要な場合はRedirect先も事前Allowlistへ登録する。IP固定だけに依存せず、DNS Rebinding、Proxy迂回、Environment Proxy混入を防止する。

## 5.6 Secret取扱い

* Execution PlanにはSecret Reference IDとVersionだけを含める。
* Secret値は実行直前に専用Resolverから取得する。
* 子Process／HTTP Clientへ必要最小範囲で注入する。
* EnvironmentよりHeader／Credential Providerを優先する。
* Debug Log、Exception、Runtime Attestationへ値を残さない。
* Secret取得失敗はFail-Closed。
* Tenant／Account ScopeをSnapshotと照合する。
* Rotation後は旧Approvalを再評価する。

## 5.7 Commercial Entitlement／Auth Route Snapshot

`CommercialEntitlementSnapshot`必須項目：

* `snapshot_id`
* `provider_id`
* `provider_account`
* `tenant_id`
* `workspace_id`
* `auth_route`
* `credential_type`
* `billing_identity`
* `subscription_or_contract_type`
* `allowed_models`
* `api_ui_cli_usage_mode`
* `usage_limit`
* `credit_or_quota`
* `additional_charge_condition`
* `data_protection_condition`
* `retention_condition`
* `training_condition`
* `region_scope`
* `technical_capability_snapshot_id`
* `pricing_catalog_hash`
* `evidence_artifact_hash`
* `snapshot_source`
* `retrieved_at`
* `expires_at`
* `assurance_level`

`auth_route`の例は`API_KEY | OAUTH_USER | OAUTH_WORKLOAD | CLOUD_MANAGED_IDENTITY | SUBSCRIPTION_LOGIN | LOCAL_NONE`とし、Credentialの値自体は含めない。

Plan／Approvalへの束縛：

* `technical_capability_snapshot_id`
* `commercial_entitlement_snapshot_id`
* `auth_route`
* `credential_type`
* `provider_account`
* `tenant_id`
* `workspace_id`
* `billing_identity`
* `pricing_catalog_hash`
* `evidence_artifact_hash`

取得不能な項目を推測で補わない。Read-onlyでも追加課金可能性がある場合はBudget Envelopeを要求する。Auth Route、Account、Tenant、Billing Identity、Pricing、Entitlement Evidenceのいずれかが変わればApprovalを無効化する。

## 5.8 Remote Invocation Registry

Remote呼出前後の不明状態を縮小するため、Registryを次の状態機械で管理する。

```text
PREPARED_DURABLE
  → REQUEST_DISPATCHING
  → REMOTE_ID_RECORDED
  → RUNNING
  → COMPLETED / CANCELLED / FAILED
  → RECONCILED

任意状態 → REMOTE_INVOCATION_UNCERTAIN
```

必須項目：

* Local Attempt ID
* `operation_journal_id`
* `effect_id`
* Provider ID、Model ID
* Auth Route、Credential Type
* Provider Account、Tenant、Billing Identity
* Endpoint ID
* Remote Invocation ID。暗号化またはTokenized
* Request Hash
* Invocation Manifest Hash
* Idempotency Key。Provider対応時
* Technical Capability Snapshot ID
* Commercial Entitlement Snapshot ID
* Pricing Catalog Hash
* Started At
* Last Known Status
* Cancel Capability
* Poll Capability
* Expected Timeout
* Last Poll At
* Fencing Token
* Reconciliation Status
* Last Ledger Event ID／Sequence

Remote Registryの状態名はStore内部Stateであり、Ledger Event名ではない。各遷移は§1.14.1の正規Ledger Eventへ写像する。

`PREPARED_DURABLE`はRequest Hash、Invocation Manifest Hash、Endpoint、Auth／Account Scope、Idempotency Key、Fencing Tokenを含め、同一`effect_id`の`OperationJournal`とRemote送信前に永続化する。本実装ではRemote Invocation RegistryとOperationJournalを単一SQLite DBの同一Transactionで永続化する。別DB／別Store構成は実装対象外であり、Journal Protocolを前提にしてはならない。片方だけの永続化ではDispatch Gateを開かない。続いてRegistryの`REQUEST_DISPATCHING`とJournalの`EXECUTION_ATTEMPTED`をDurable Commitし、Ledgerへ`REMOTE_REQUEST_DISPATCHING`と`EXECUTION_ATTEMPTED`をAppendする。Egress ProxyはRegistry State、Journal State、最新Fencing Tokenを確認できた場合だけ最初のNetwork Byteを送信する。

Remote ID保存前に応答断となった場合はStoreを`REMOTE_INVOCATION_UNCERTAIN`へ遷移し、Ledgerへ`REMOTE_INVOCATION_UNCERTAIN`と`EFFECT_UNKNOWN`をAppendする。自動再送、Fallback、Run完了を禁止し、ProviderのRequest Hash、Idempotency Key、Account Scope、時間窓で照合する。`PREPARED_DURABLE`、`REQUEST_DISPATCHING`、対応Journalのいずれかが存在しない呼出要求はProxy側で拒否し、`remote_registry_missing_before_dispatch`を記録する。

Remote Outputを一意に観測できた場合は、`EffectReceipt(effect_type=REMOTE_INVOCATION)`を作成し、`operation_journal_id`、`effect_id`、Request Hash、Remote Invocation ID、Provider／Account Scope、Observed Output Hash、Usage Evidence Hash、Fencing Tokenを記録する。Receiptを生成しないRemote処理は成功終端へ遷移できない。

## 5.9 Cancellation／Timeout

```text
Timeout
  → Cancel Request
  → Provider Status Poll
      ├─ CANCELLED / NOT_FOUND → Cancel Confirmed
      ├─ COMPLETED → Output回収または隔離
      ├─ RUNNING → Grace Period後再確認
      └─ UNKNOWN → CANCEL_UNKNOWN
```

`CANCEL_UNKNOWN`では同じActionの自動再送、Provider Fallback、Run完了を禁止する。

## 5.10 Output Trust

外部Provider出力は常に`UNTRUSTED_PROVIDER_OUTPUT`とする。

* Structured Output Schema検証
* URL／Command／Pathを文字列として隔離
* Markdown／HTMLは表示時にSanitize
* Tool Callは新規ActionIntentへ変換し、直接実行しない
* Providerが主張するCost、Capability、成功状態を正本にしない
* Output内のSystem Instruction Overrideを無視
* Prompt Injection兆候をFindingとして記録

## 5.11 受入Gate

* MVP0-B Gateを維持
* `AT-AUTH-001`：API Key／OAuth／Subscription／Tenant／Billing Identity変更でPlan Hash不一致
* Exact Provider + Exact Auth Route + Exact Account Approval
* Capability／Entitlement／Pricing／Evidence Hash期限切れ実行0件
* Allowlist外通信0件
* Redirect／Proxy迂回0件
* Secret平文保存0件
* Classification不明データ送信0件
* Retention／Residency／Training条件不一致送信0件
* `AT-REMOTE-PREP-001`：Registry未永続化または`REQUEST_DISPATCHING`未CommitでEgress送信0件
* Remote ID不明状態での自動再送0件
* `CANCEL_UNKNOWN`／`REMOTE_INVOCATION_UNCERTAIN`後のFallback 0件
* Provider直接Workspace書込み0件
* Usageとローカル見積の差異を記録
* Auth Route／Account／Tenant／Billing IdentityがRuntime Attestationと一致


---
