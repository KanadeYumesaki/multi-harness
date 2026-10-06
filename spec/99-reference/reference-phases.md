<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

# 7. MVP1-B：Verified Session Resume 詳細設計【参照仕様・初期実装対象外】

## 7.1 目的

Provider Sessionを正本にせず、Context再送量・Latency・Token消費を削減する最適化として、安全にResumeする。

## 7.2 追加コンポーネント

* Session Resume Manager
* Session Descriptor Store
* Session Reference Protector
* Resume Policy Evaluator
* Provider Session Status Adapter
* Cold Start Builder

## 7.3 Session Descriptor

`SessionResumeDescriptor`必須項目：

* `descriptor_id`
* `originating_run_id`
* `originating_action_id`
* `provider_id`
* `provider_account`
* `tenant_id`
* `workspace_id`
* `billing_identity`
* `auth_route`
* `credential_type`
* `account_scope_hash`
* `adapter_version`
* `provider_executable_hash`
* `runtime_envelope_spec_hash`
* `invocation_manifest_hash`
* `runtime_attestation_hash`
* `session_reference_encrypted`
* `session_reference_key_id`
* `model_id`
* `model_digest`
* `context_bundle_hash`
* `instruction_hash`
* `tool_policy_hash`
* `policy_snapshot_hash`
* `token_profile_snapshot_hash`
* `data_classification`
* `trust_zone`
* `technical_capability_snapshot_id`
* `commercial_entitlement_snapshot_id`
* `pricing_catalog_hash`
* `created_at`
* `last_used_at`
* `expires_at`
* `resume_assurance_level`
* `last_confirmed_provider_state`
* `maximum_resume_count`
* `current_resume_count`
* `descriptor_hash`

Session ReferenceはLedgerへ平文保存せず、暗号化ArtifactまたはSecret Storeへ保存する。Descriptorは最適化Cacheであり、Run／Action状態の正本ではない。

## 7.4 Resume Decision Algorithm

```text
Descriptor存在?
  No → Cold Start
  Yes
    → TTL有効?
    → Provider／Account／Tenant／Billing Identity一致?
    → Auth Route／Credential Type一致?
    → Executable／Adapter／Model／Model Digest一致?
    → RuntimeEnvelopeSpec／InvocationManifest／Attestation Policy一致?
    → Capability／Entitlement／Pricing Snapshot有効?
    → Context／Instruction／Tool／Policy／Token Profile Hash一致?
    → Data Classification／Trust Zone一致?
    → Cancel Unknown／Remote Unknownなし?
    → Assurance LevelがPolicy以上?
    → Resume Count上限内?
        All Yes → RESUME_ALLOWED
        Any No  → COLD_START_REQUIRED
        不明    → HUMAN_REVIEW_REQUIRED
```

Decisionは`SessionResumeDecision`として理由Code、全入力Snapshot Hash、Current Plan Content Hash、Current Execution Plan Hashを保存する。Auth Route、Executable、Runtime Manifest、Entitlement、Policyの差異はCold Startだけでなく、必要に応じて再Approvalを要求する。

## 7.5 Resume Flow

1. Current Execution PlanをSession非依存で生成。
2. Descriptorを取得。
3. Resume Decisionを実行。
4. Resume可の場合も新しいActionAttemptを作成。
5. AdapterがSession Referenceを解決してInvoke。
6. Providerの応答にSession継続確認があれば`SessionStateReceipt`へ保存。
7. Resume失敗時はRemote状態を確認。
8. 副作用がないRead-only Actionであり、Remote実行不存在が確認できた場合だけCold Start Attemptを作成。

SessionからApproval、Tool権限、Workspace権限、Budgetを継承しない。

## 7.6 Context差分

完全一致Resumeを標準とする。将来、差分Resumeを許可する場合は以下を要求する。

* ProviderがMessage／State Versionを明示
* 追加Contextだけでなく削除・失効Contextを表現可能
* System Instruction差替えを検証可能
* Tool Policy変更をProviderへ確実に反映可能
* Provider Attestationがある

上記を満たさないProviderでは、Context Hash不一致時にCold Startする。

## 7.7 Failure／Recovery

| 事象 | 処理 |
|---|---|
| Session Expired | Cold Start |
| Session Not Found | Cold Start |
| Provider Account不一致 | Blocked Authentication |
| Resume Timeout | Cancel／Status確認 |
| Providerが新Sessionを返す | 新Descriptorとして保存 |
| Partial Response | Untrusted Artifactへ隔離、採用しない |
| Session Reference復号失敗 | Blocked Security |
| `CANCEL_UNKNOWN` | Cold Start禁止、人間確認 |

## 7.8 Privacy／Retention

* SessionのProvider側RetentionをSnapshotへ記録。
* Personal／Restricted Dataを含むSessionはPolicyで自動Resumeを禁止可能。
* Expiry時はLocal Referenceを破棄し、Provider側削除APIがある場合は独立Actionで削除する。
* Session削除の成功を推測せず、Receiptまたは`DELETE_UNKNOWN`を記録する。

## 7.9 受入Gate

* Context／Instruction／Tool／Policy Hash不一致時Resume 0件
* Executable／Runtime Manifest／Model Digest変更時Resume 0件
* Auth Route／Account／Tenant／Billing Identity変更時Resume 0件
* Capability／Entitlement／Pricing期限切れ時Resume 0件
* Expired Session使用0件
* Resume失敗後の二重送信0件
* SessionなしでCold Start可能
* Session Reference平文ログ0件
* Approval／Lease／Release継承0件
* `CANCEL_UNKNOWN`／Remote Unknown時Cold Start 0件
* Resume Count／TTL適用
* Session削除状態を監査可能
* Descriptor HashとCurrent Planの照合率100%


---
# 8. MVP1-C：Policy-based Provider Routing／Fallback 詳細設計【参照仕様・初期実装対象外】

## 8.1 目的

複数Providerから、Data、Cost、Latency、Capability、Residency、Entitlementを満たすCandidateを決定論的に選択し、許可されたRetryable Errorだけで新AttemptへFallbackする。

## 8.2 追加コンポーネント

* Provider Registry
* Candidate Set Builder
* Capability／Entitlement Snapshot Cache
* Provider Health Collector
* Routing Policy Engine
* Provider Selection Recorder
* Fallback Decision Engine

## 8.3 Candidate Set生成

入力：

* Action Type
* Required Capability
* Context Token量
* Structured Output要件
* Tool要件
* Data Classification／Trust Level
* Region／Residency
* Retention／Training／Logging条件
* Cost Upper Bound
* Latency SLO
* Operator Preference
* Provider Health
* Entitlement Freshness
* Allowed Auth Route
* Credential Type
* Provider Account／Tenant／Workspace
* Billing Identity
* Technical Capability Snapshot
* Commercial Entitlement Snapshot
* Pricing Catalog Hash
* Evidence Hash

Hard Constraintに違反するCandidateを除外した後、Soft Scoreを計算する。

### Candidate Identity

CandidateはProvider名だけでなく、次の組を一意Identityとする。

```text
provider_id
+ model_id
+ endpoint_or_region
+ auth_route
+ credential_type
+ provider_account
+ tenant_id
+ workspace_id
+ billing_identity
+ technical_capability_snapshot_id
+ commercial_entitlement_snapshot_id
+ pricing_catalog_hash
```

### Hard Constraint例

* Context Limit不足
* Data Classification非対応
* Region／Residency不一致
* Retention／Training条件非準拠
* Entitlementなし／期限切れ
* Auth Route非許可
* Account／Tenant／Billing Identity不一致
* Pricing Catalog不明
* Adapter Version非互換
* Tool Policy不適合
* Budget超過
* Health=`UNAVAILABLE`

### Soft Score例

```text
score =
  quality_weight * quality_score
+ cost_weight * normalized_cost_score
+ latency_weight * normalized_latency_score
+ reliability_weight * success_rate
+ locality_weight * locality_score
```

Weight、正規化式、Tie-breakerをVersion管理する。同点時は固定Provider Priority、Candidate Identityの辞書順で決定し、ランダム選択しない。

## 8.4 ProviderCandidateSet

* Candidate Set ID
* Candidate一覧。各Candidateは§8.3のCandidate Identity全項目を持つ
* 各Technical Capability Snapshot ID／Hash。IDは監査参照用でCandidate Set Hash対象外
* 各Commercial Entitlement Snapshot ID／Hash。IDは監査参照用でCandidate Set Hash対象外
* 各Pricing Catalog Hash
* 各Evidence Hash
* Hard Constraint結果
* Soft Score内訳
* Policy Version
* Input Requirement Hash
* Candidate Set Hash
* Expires At

Approvalは次の二層とする。

1. `CANDIDATE_POLICY_APPROVAL`：選択可能集合と最大条件を承認
2. `EXACT_PROVIDER_APPROVAL`：最終CandidateのProvider、Model、Endpoint、Auth Route、Credential Type、Account、Tenant、Billing Identity、Entitlement、Pricingを承認

外部または有償Actionでは`EXACT_PROVIDER_APPROVAL`を必須とする。Read-onlyかつ同一Trust／Cost／Retention条件のローカルCandidateだけ、Versioned PolicyでCandidate Set Approvalを許可できる。

## 8.5 Selection Flow

```text
Requirements
  → Snapshot取得
  → Hard Filter
  → Score
  → Deterministic Sort
  → Candidate Set確定
  → Candidate Policy Approval
  → Exact Candidate選択
  → Exact Provider Approval
  → Execution Plan再Hash
  → Approval Consume
  → New Attempt
```

`ProviderSelectionReceipt`には採用理由だけでなく、上位候補を除外した理由、Auth Route／Account／Tenant／Billing／Pricing差異、Approval IDを記録する。最終Candidate選択後はProvider等の意味内容が変わるため`plan_content_hash`と`execution_plan_hash`を再生成し、新`execution_plan_hash`へExact Approvalを束縛する。

## 8.6 Fallback Decision

Fallback許可条件：

* 前Attemptが`FAILED_RETRYABLE`
* Remote停止確認済み
* Effectなし
* Candidate Set内
* 新Candidateが同等以上のData／Retention／Residency条件
* Run／Action Budget内
* Candidate Snapshot有効
* Retry回数上限内
* 新Attempt作成済み
* 新CandidateのExact Provider Approval済み
* Auth Route、Account、Tenant、Billing Identity、Region、Costが元Approval範囲内

Fallback禁止条件：

* `CANCEL_UNKNOWN`
* `EFFECT_UNKNOWN`
* `REMOTE_INVOCATION_UNCERTAIN`
* Authentication／Authorization
* Policy Denied
* Entitlement Error
* Data Classification不一致
* Non-idempotent Action
* Approval Scope外
* Candidate条件劣化
* Unknown Error

Fallback先でProvider、Region、Auth Route、Account、Tenant、Billing Identity、Pricing Catalog、Entitlement Snapshot、Cost Upper Boundのいずれかが変わる場合、必ずExecution Planを再生成し、Exact Provider再承認を要求する。同一Attempt内での暗黙切替は禁止する。

## 8.7 Health情報

Healthは補助情報であり、CapabilityやEntitlementを上書きしない。

* Provider Status
* Recent Success Rate
* P95 Latency
* Rate Limit状態
* Circuit State
* Last Updated
* Source Assurance

Healthが古い場合は未知として扱う。Circuit BreakerはProvider／Model／Account Scope単位で管理し、連続失敗5回を初期Open条件とする。

## 8.8 監査

* Candidate Set入力Hash
* 全Candidateと除外理由
* Score内訳
* Selected Provider
* Fallback理由
* Previous Attempt状態
* Cost差
* Data／Region／Retention差
* Approval再利用可否

## 8.9 受入Gate

* Candidate Set外選択0件
* 同一入力から同一Selection
* `AT-FALLBACK-001`：Region、Cost、Account、Auth Routeが異なるFallbackで再承認要求
* Exact Provider／Model／Endpoint／Auth Route／Account／Tenant／Billing Identity Approval
* `CANCEL_UNKNOWN`／`EFFECT_UNKNOWN`／Remote Unknown後Fallback 0件
* Retryable以外のFallback 0件
* Region／Retention／Cost／Entitlement劣化時再承認
* Non-idempotent自動Fallback 0件
* Candidate Snapshot期限切れ選択0件
* Circuit Open Provider選択0件
* Tie-break再現性
* Fallbackごとに新Attempt
* Candidate Selection ReceiptとPlan Hashの一致率100%


---
# 10. MVP1-E：Approved External Side Effect 詳細設計【参照仕様・初期実装対象外】

## 10.1 目的

Email、Slack／Teams、Git Push、Pull Request、Webhook、Ticket等の外部副作用を、生成と送信を分離したTransactional Outboxで安全に実行する。

## 10.2 追加コンポーネント

* Payload Normalizer
* Destination Allowlist Manager
* DLP／PII Gate
* Transactional Outbox
* Dispatch Approval Manager
* Dispatch Adapter
* External Effect Reconciler
* Correction／Compensation Planner

## 10.3 Action分離

1. `CONTENT_GENERATION`
2. `PAYLOAD_NORMALIZATION`
3. `DLP_PII_CHECK`
4. `OUTBOX_PREPARE`。Exact Payloadを`PREPARED_DURABLE`へ永続化
5. `DISPATCH_APPROVAL`
6. `EXTERNAL_DISPATCH`
7. `EXTERNAL_EFFECT_RECONCILIATION`
8. `CORRECTION_OR_COMPENSATION`。必要な場合

生成Actionへ送信権限を与えない。

## 10.4 OutboxRecord

### 必須項目

* `outbox_id`
* `effect_id`
* `operation_journal_id`
* `run_id`
* `action_id`
* `attempt_id`
* `effect_type`
* `destination`
* `destination_canonical_id`
* `destination_allowlist_id`
* `payload_artifact_hash`
* `payload_hash`
* `attachment_hashes`
* `visibility`
* `idempotency_key`
* `approval_id`
* `dispatch_adapter`
* `dispatch_account_scope`
* `auth_route`
* `billing_identity`
* `expected_effect`
* `confirmation_target`
* `reconciliation_method`
* `retry_policy`
* `rate_limit_bucket`
* `fencing_token`
* `expires_at`
* `status`
* `prepared_event_id`
* `prepared_at`
* `durability_level`
* `dispatch_started_at`
* `last_reconciled_at`
* `provider_receipt_hash`
* `remote_object_reference_encrypted`

### 一意制約

* `UNIQUE(outbox_id)`
* `UNIQUE(effect_id)`
* `UNIQUE(effect_type, destination_canonical_id, idempotency_key)`
* ProviderがGlobal Idempotency Scopeを持つ場合は、そのScopeを一意Keyへ含める。
* 同じPayloadでも宛先やVisibilityが異なる場合は別Approval・別Idempotency Key。
* Idempotency KeyはHarnessがCSPRNGまたはDomain-separated Hashで生成し、Provider出力をそのまま採用しない。

### 状態遷移

```text
DRAFT
  → VALIDATED
  → WAITING_APPROVAL
  → READY
  → PREPARED_DURABLE
  → DISPATCHING
  → ACCEPTED
  → SENT
  → DELIVERED_OR_APPLIED
  → RECONCILED

任意状態
  → REJECTED
  → EXPIRED
  → CANCELLED
  → STATUS_UNKNOWN
  → EFFECT_CONFLICT
  → MANUAL_RECONCILIATION
  → COMPENSATION_REQUIRED
```

状態意味：

* `PREPARED_DURABLE`：Exact Payload、Destination、Approval、Idempotency Key、Fencing TokenがOutbox Storeへ耐久保存済みで、Network送信は未開始。
* `DISPATCHING`：送信開始直前のDurable Commitが完了し、最初のNetwork Byte送信以降を含み得る。
* `ACCEPTED`：ProviderがRequestを受理した。
* `SENT`：Providerが送信／Remote Object作成を確認した。
* `DELIVERED_OR_APPLIED`：宛先配信または対象Systemへの適用を確認した。
* `RECONCILED`：要求したConfirmation Levelまで照合済み。
* `STATUS_UNKNOWN`：送信開始後、受理／送信／適用のどこまで進んだか不明。
* `MANUAL_RECONCILIATION`：自動照合手段がなく、Operator確認Queueへ移送済み。

Outboxの状態名はStore内部Stateであり、Ledger Event名ではない。各遷移は§1.14.1の正規Eventへ写像し、`STATUS_UNKNOWN`は必ず共通`EFFECT_UNKNOWN`へ写像する。

曖昧な`CONFIRMED`状態は新規利用しない。

## 10.5 Destination正規化

例：

* Email：Unicode正規化、Domain正規化、Alias Policy
* Slack／Teams：Workspace／Tenant＋Channel ID
* Git：Remote Repository ID＋Branch／PR Base
* Webhook：Scheme／Host／Path＋Allowlist
* Ticket：Tenant＋Project＋Issue Type

表示名だけでAllowlist判定しない。宛先の不変IDとTenant Scopeを使う。

## 10.6 Exact Payload Approval

Approvalへ束縛する対象：

* Exact Payload Hash
* Attachment Hash
* Destination Canonical ID
* Visibility
* Mention／Recipient
* Thread／Channel
* Git Base／Head
* Webhook Method／Path
* Dispatch Account
* Idempotency Key
* Expiry
* Cost Upper Bound
* Correction手順

Payload変更、宛先変更、公開範囲変更、添付変更、Account変更でApprovalを無効化する。

## 10.7 Dispatch Protocol

1. Outbox Record、Exact Payload Approval、Budget Reservationを検証。
2. 同一TransactionのCASで`READY`へ遷移し、Dispatch Claim、Lease、最新Fencing Tokenを取得。
3. Outbox Storeの一意制約でDestination＋Idempotency Keyの重複を拒否。
4. Exact Payload Hash、Destination、Account、Idempotency Key、Fencing Tokenを含むOutbox Recordと`OperationJournal(operation_type=EXTERNAL_DISPATCH)`を、同一Durable境界で`PREPARED_DURABLE`へ永続化する。`OUTBOX_PREPARED`と`ACTION_PREPARED`をAppendする。
5. Outboxの`DISPATCHING`とJournalの`EXECUTION_ATTEMPTED`をDurable Commitし、Dispatch開始時刻を保存する。`OUTBOX_DISPATCHING`と`EXECUTION_ATTEMPTED`をAppendする。
6. Dispatch Adapterの最終送信点で`PREPARED_DURABLE`、`DISPATCHING`、最新Fencing Tokenを再検証する。
7. AdapterへIdempotency Key付きで送信する。送信前のDurable Stateが欠落している場合は送信せずManual Reconciliationへ移送する。
8. Provider ReceiptをArtifact Storeへ保存。
9. Remote Object IDを暗号化またはTokenized保存。
10. Provider応答の保証範囲に応じ`ACCEPTED`または`SENT`へ遷移。
11. Read API、Sent Folder、Remote Ref、Object GET等で再照合。
12. 要求Confirmation Levelを満たした場合に、同一`effect_id`と`operation_journal_id`を参照するEffect Receiptを保存する。
13. Receipt Hash、Remote状態、Outbox Stateを照合し`RECONCILED`へ。
14. Release Gateは`RECONCILED`またはPolicyで明示された低いConfirmation Levelだけを受け付ける。

### `DISPATCHING`停止後のRecovery

```text
DISPATCHINGで停止
  → Provider Idempotency Lookup可能?
      Yes → Keyで照合
      No
        → Remote Object／Sent Folder／Read APIで照合可能?
            Yes → Payload Hash・Destination・時間窓で照合
            No → STATUS_UNKNOWN
  → 一意に存在確認 → Receipt補完、RECONCILED
  → 不存在を証明 → 新Approval有効かつPolicy許可時のみ新Attempt
  → 複数候補／判定不能 → MANUAL_RECONCILIATION
```

Provider Receipt取得失敗時は自動成功扱いせず、照合可能ならRemote Readで補完し、不可能ならManual Queueへ送る。ProviderがIdempotencyを提供しない場合、`STATUS_UNKNOWN`から自動Dispatchしない。

## 10.8 Effect別Reconciliation

| Effect | 自動照合 | Confirmation Level |
|---|---|---|
| Email | Provider Message ID、Sent Folder、Delivery Status。可能な範囲 | `ACCEPTED \| SENT \| DELIVERED` |
| Slack／Teams | Message IDとTenant／ChannelでGET | `APPLIED` |
| Git Push | Remote Ref Hash | `APPLIED` |
| Pull Request | Repository、PR Number、Head／Base SHA | `APPLIED` |
| Webhook | Provider Receipt＋対象System Read API。存在する場合 | `ACCEPTED \| APPLIED` |
| Ticket | Ticket ID、Project、Payload主要Field | `APPLIED` |

`EffectReceipt`には、要求Confirmation Level、実際のConfirmation Level、Observation Method、Observed At、Remote Object Identity、Payload Hash、Destination Canonical ID、Fencing Tokenを含める。

外部側Read APIがなくProvider Receiptだけの場合は`ACCEPTED_BY_PROVIDER`とし、`DELIVERED`や`APPLIED`と表現しない。業務要件が配信・適用確認を要求する場合、ReceiptのみではRelease不可とする。

## 10.9 Compensation

外部Effectは完全Rollbackできない場合がある。

* Email：訂正送信。削除不可を前提。
* Chat：削除または訂正。Audit上は元投稿を保持。
* Git Push：Force Push禁止。Revert Commitまたは新Branch。
* Pull Request：Close、Comment、Revert。
* Webhook：補償APIがあれば独立Action。
* Ticket：Close／Correction。

Compensationも新規Plan、Approval、Outbox、Receiptを持つ。

## 10.10 Prompt Injection対策

外部入力から自動返信／自動送信する場合：

* InputをUntrusted External Dataへ分類
* InstructionとDataを分離
* 宛先をInput本文から自由抽出しない
* Tool／Effectを固定
* Payload Templateを固定
* DLP／PII
* Human Approval
* URL／添付の再取得禁止またはAllowlist

External Effectは低リスク定型通知を含め、Enterprise SoD Gate合格までは`HUMAN_APPROVAL_ONLY`とする。Gate合格後にPolicy Approvalを解禁する場合も、事前承認Template、固定宛先Allowlist、変数Schema、件数／頻度上限、公開範囲、DLP、署名Rule Set、独立Policy主体、`AT-POLICY-APPROVAL-001`合格を必須とする。

## 10.11 受入Gate

* Payload変更後Approval再利用0件
* Allowlist外送信0件
* `AT-OUTBOX-001`：送信成功後Receipt消失時に重複送信0件
* `PREPARED_DURABLE`または`DISPATCHING`未Commit状態で外部送信0件
* Destination＋Idempotency Key一意制約
* `DISPATCHING`停止後にRemote照合
* `STATUS_UNKNOWN`後自動再送0件
* Provider Idempotency非対応時のManual Queue移送
* Stale Fencing TokenによるDispatch 0件
* Accepted／Sent／Delivered／Appliedを混同しない
* Secret／禁止PII検出時送信0件
* EffectとReceipt不一致をConflict化
* 宛先表示名Spoof拒否
* 生成ActionからDispatch権限分離
* Compensationが独立Action
* Rate Limit／大量送信Policy適用
* Outbox Store／Ledger／Receipt片側障害から復旧


---
# 11. MVP2-A：Windows／WSL Path Boundary 詳細設計【参照仕様・不採用】

## 11.1 目的

WindowsとWSLの異なるFilesystem意味論をRaw Path文字列変換で扱わず、各OS境界内のTrusted Agent／Workspace Brokerが発行するCapabilityで安全に操作する。

## 11.2 構成

```text
Harness Core
  ├─ Capability Registry
  ├─ Broker Client
  └─ Cross-boundary Coordinator
        |
        +-- Authenticated Local RPC --> Windows Runtime Agent
        |                               └─ Windows Workspace Broker
        |
        +-- Authenticated Local RPC --> WSL Runtime Agent
                                        └─ Linux Workspace Broker
```

Harness Coreは他方OSのRaw Pathを解釈せず、`workspace_id`と`relative_path`だけを扱う。

## 11.3 WorkspaceCapability

必須項目：

* Capability ID
* Workspace ID
* OS Boundary
* Broker ID
* Broker Version
* Broker Binary Hash
* Volume／Filesystem Identity
* Root File Identity
* Canonical Root Descriptor
* Allowed Operations
* Allowed Relative Path Pattern
* File Type Policy
* ACL Policy Hash
* Hardlink Policy
* Reparse／Symlink Policy
* Issued To Subject／Worker
* Run／Action／Attempt Scope
* Fencing Token
* Issued At
* Not Before
* Expires At
* Nonce
* Revocation Epoch
* Issuer Key ID
* Signature Algorithm
* Broker Signature
* Capability Schema Version

CapabilityはBearer Tokenとして扱い、Ledgerへ平文保存しない。LedgerにはCapability Hash、Scope、Issuer Key ID、Expiryだけを保存する。

署名・Replay要件：

* Broker専用署名鍵をOS保護Key Storeへ保持。
* CoreはBroker Key Registry、Key Status、Revocation Epochを検証。
* `capability_id + nonce + run_id + action_id + attempt_id`をBroker Journalで一回限り使用。
* Clock Skew上限を超えるCapabilityを拒否。
* Broker再起動時もConsumed Nonceを失わない。
* Capability更新は新ID／新Nonce／新署名とし、旧Capabilityを延長しない。

## 11.4 Broker責務

### Windows Broker

* Win32／NT PathのCanonicalization
* Volume Serial／File ID照合
* Reparse Point／Junction／Mount検査
* UNC／Device Path／NT Namespace拒否
* Alternate Data Stream拒否
* Reserved Device Name拒否
* 8.3 Alias衝突検査
* Case-insensitive衝突検査
* Trailing Dot／Space拒否
* Atomic Replace可否判定
* ACL、Owner、Integrity Level、継承ACL検証
* ACLが検証時からCommit時まで変わっていないことの確認
* Hardlink CountとLink Identity検査
* HardlinkがWorkspace外Objectを共有する可能性がある場合は拒否
* Open Handleを使ったTarget／Parent Identity固定
* Broker JournalとFencing TokenのStorage側検証

### WSL Broker

* `openat2`／`openat`系のDirectory Handle基準解決
* Symlink／Bind Mount検査
* Device／Proc／Sys等特殊Filesystem拒否
* `/mnt/*`越境制御
* Case Sensitivity検査
* UID／GID／Mode／ACL保持可否判定
* Hardlink CountとDevice／inode検査
* Atomic Rename可否判定
* Windows側への暗黙Path変換禁止
* Broker JournalとFencing TokenのStorage側検証

### 共通拒否規則

ACL、Owner、Volume、Root Identity、Hardlink Set、Reparse Metadata、Case Collision Setのいずれかが検証後に変化した場合は`PATH_IDENTITY_CONFLICT`とし、再承認なしにCommitしない。

## 11.5 Local RPC

* OS Local Socket／Named Pipeを使用
* Mutual Authentication
* Broker Binary Hash／Version検証
* Request／Response Schema Version
* Correlation ID
* Request ID
* Replay防止Nonce
* Monotonic SequenceまたはConsumed Nonce Store
* Request Timeout
* Maximum Message Size
* Capability Signature／Issuer Key Status検証
* Peer Process Identity／OS Subject検証
* ACLでHarness CoreとBroker Identityだけを許可
* Audit Event
* Request Hash／Response Hash

Broker API例：

```text
issue_capability(workspace_identity, scope, ttl)
inspect_path(capability, relative_path)
read_object(capability, relative_path)
prepare_write(capability, operation_manifest)
commit_write(capability, prepared_write_id, fencing_token)
reconcile_effect(capability, effect_id)
revoke_capability(capability_id)
```

Named Pipe／SocketのACL設定不能、Peer Identity不明、Nonce Store不調、署名鍵失効時はFail-Closedとする。

## 11.6 File Identity

Path文字列以外に以下を照合する。

* Filesystem／Volume Identity
* Root Directory File ID／inode
* Target File ID／inode。存在する場合
* Parent Directory Identity
* Base Content Hash
* File Type
* Reparse／Symlink Metadata
* Case-normalized Collision Set

検証とCommitの間にIdentityが変化した場合は`PATH_IDENTITY_CONFLICT`。

## 11.7 Cross-filesystem Write

Atomic Replace不能な場合は`Copy-Verify-Swap Protocol`を使う。

1. Target Broker側にTemp Object作成。
2. Content ArtifactをChunk転送。
3. 各Chunk HashとTotal Hashを照合。
4. Metadataを適用。
5. Target Parent／Base Identityを再確認。
6. Broker内で可能な最小原子操作を実行。
7. 対象を再読込してHash照合。
8. Effect Receiptを返却。
9. Atomicity保証レベルを記録。

保証できない多File更新は自動実行せず、同一OS側Worktreeへ集約する。

## 11.8 Windows拒否Corpus

* UNC
* Extended-length Device Path
* NT Object Manager Path
* Reparse Point
* Junction
* Volume Mount Point
* Alternate Data Stream
* 8.3 Alias
* Reserved Device Name
* Trailing Dot／Space
* Case Collision
* Different Volume
* Symlink
* Hardlink Policy違反
* ACL変更
* Executable／Script Policy違反

## 11.9 WSL拒否Corpus

* 未許可`/mnt/*`
* Symlink Escape
* Bind Mount Escape
* `/proc`、`/sys`、`/dev`
* Case差で別Object
* Windows Metadata不整合
* Cross-filesystem Rename
* Socket／Device／FIFO
* Permission／Owner変更
* Windows Path文字列の暗黙解釈

## 11.10 復旧

BrokerはPrepared Write Journalを持つ。

* `PREPARED`：Target未変更ならAbort可能
* `COMMITTING`：Effect照合
* Expected Hash一致：Committed補完
* Base Hash一致：再Commit可
* 別Hash：Conflict
* Broker停止：再起動後Journal走査
* Capability期限切れ：新Capability発行後、Effect照合だけ許可
* Signature不一致：処理停止、Security Finding

## 11.11 受入Gate

* MVP0-A〜MVP1-EのLinux Gateを維持
* Raw Pathだけの越境判定0件
* UNC／Device／ADS／Reparse／Junction拒否
* 未承認`/mnt/*`拒否
* Case／8.3 Alias Escape 0件
* HardlinkによるWorkspace外Object変更0件
* ACL／Owner／Integrity Level変更時Commit 0件
* Capability期限切れ、Replay Nonce、古いRevocation Epochの操作0件
* Broker Signature／Issuer Key不一致操作0件
* Local RPC Peer Identity不一致操作0件
* Cross-filesystem途中停止から復旧
* File／Parent／Root／Volume Identity変更時Commit 0件
* Broker AuditとCore LedgerをCorrelation可能
* Capability漏えい0件
* Windows拒否Corpus全件PASS
* WSL拒否Corpus全件PASS


---
# 12. MVP2-B：Multi-worker／Parallel Execution 詳細設計【参照仕様・初期実装対象外】

## 12.1 目的

Action Dependency Graphに基づき、安全に並列化できるActionだけを複数Workerへ割り当て、Resource Conflict、Deadlock、Stale Worker、Partial Failureを制御する。

## 12.2 追加コンポーネント

* Action DAG Builder
* Scheduler
* Worker Registry
* Resource Conflict Analyzer
* Distributed／Resource Lock Manager
* Lease Renewal Service
* Deadlock Detector
* Partial Failure Coordinator
* Work Queue
* Capacity／Quota Manager

## 12.3 Action DAG

各Actionは以下を宣言する。

* Dependencies
* Read Resources
* Write Resources
* External Effect Resources
* Provider Session Resource
* Budget Reservation Resource
* Required Capability
* Priority
* Maximum Attempts
* Timeout
* Cancellation Semantics

DAGは循環を拒否する。Dynamic Action追加は新Plan VersionとPolicy Decisionを必要とする。

## 12.4 Resource Key

Canonical Resource Key例：

* `workspace:{workspace_id}`
* `path:{workspace_id}:{canonical_path}`
* `artifact:{content_hash}`
* `provider-session:{provider}:{session_hash}`
* `budget:{budget_scope}`
* `outbox:{destination}:{idempotency_key}`
* `release:{run_id}`
* `ledger-stream:{stream_id}`

Path LockはParent／Child競合を解析する。単純文字列前方一致ではなくCanonical Path Segmentで判定する。

## 12.5 Lock Mode

* `READ`
* `WRITE`
* `INTENT_WRITE`
* `EXCLUSIVE_EFFECT`
* `RESERVATION`

初期実装では複雑なLock Promotionを避け、必要LockをPlan時に列挙して一括取得する。

## 12.6 Lock順序

Deadlock防止のため、Resource Type Order＋Canonical Keyで全順序を定義する。

```text
BUDGET
 → PROVIDER_SESSION
 → WORKSPACE
 → PATH
 → ARTIFACT
 → OUTBOX
 → RELEASE
```

Workerは順序外取得を要求した時点で失敗する。取得待ちTimeout後はすべて解放し、新Attemptへ。

## 12.7 Scheduler／Atomic Claim

並列化条件：

* Dependencyが全て成功
* Required Approval有効
* Budget Reservation確保可能
* Candidate Runtime利用可能
* Resource Conflictなし
* RunがCancel／Block状態でない
* Worker Capability一致
* Risk Policyが並列を許可

同一Actionを2 Workerが取得しないよう、ClaimはDB Compare-and-Swapで行う。

```sql
UPDATE action_attempt
SET state = 'CLAIMED',
    worker_id = :worker_id,
    claim_id = :claim_id,
    claimed_at = :now,
    store_version = store_version + 1
WHERE attempt_id = :attempt_id
  AND state = 'READY'
  AND worker_id IS NULL
  AND store_version = :expected_store_version;
```

影響行数が1の場合だけClaim成功とする。実装では次を同一Scheduling Transactionへ含める。

1. Action State=`READY`と`store_version`検証
2. Approval Grant消費。未消費の場合
3. Budget Reservation確認または取得
4. Resource Lock一括取得
5. Lease取得
6. Monotonic Fencing Token発行
7. Worker Attestation Hash束縛
8. `ACTION_CLAIMED`／`LEASE_ACQUIRED` Event Append

単一DBで完結できないStoreはPrepare／Commit Journalを使用し、片側成功時にActionを`CLAIM_UNKNOWN`として実行へ進めない。

SchedulerはEffect ActionよりRead-only Actionを優先できるが、Priority Inversionをメトリクス化する。

## 12.8 Worker Protocol／Attestation

```text
REGISTER
  → ATTEST_WORKER
  → HEARTBEAT
  → CLAIM_ACTION_CAS
  → ACQUIRE_RESOURCES_ATOMIC
  → VERIFY_PLAN_AND_RUNTIME_SPEC
  → EXECUTE
  → REPORT_RESULT
  → RELEASE_RESOURCES
```

Worker Attestation：

* Worker ID
* Binary／Image Hash
* Version
* Runtime Capability
* OS／Kernel／Container Runtime
* Trust Zone
* Supported Sandbox
* Network／Filesystem Enforcement Capability
* Current Load
* Registered At
* Attestation Expires At
* Issuer／Signature
* Worker Attestation Hash

実行中報告：

* Heartbeat
* Action／Attempt／Claim ID
* Lease ID
* Fencing Token
* Runtime Attestation Hash
* Last Safe Point
* Process／Container Identity

SchedulerはWorker Attestation HashをExecution PlanとClaimへ束縛する。Attestation期限切れ、Binary Drift、Sandbox Capability不足、Trust Zone不一致ではClaimしない。

Heartbeat切れだけで即再実行せず、Lease Expiry、Claim状態、Effect Journal、Remote状態を照合する。

## 12.9 Fencing

すべてのWrite／Effect Commitで最新Fencing Tokenを検証する。TokenはLock／Lease StoreがResource単位で単調増加させ、同じ値を再発行しない。

検証場所：

* Workspace Broker
* Effect／Operation Journal
* Artifact Manifest Commit
* Budget Reservation／Settlement
* Transactional Outbox Dispatch
* Session Descriptor更新
* Release Gate

古いWorkerが処理を継続しても、最終Effect実行点がCommitを拒否する。Application内の事前確認だけでは不十分である。

Stale Token拒否時は`FENCING_REJECTED` Event、Worker ID、Claim ID、Resource Key、Presented／Current Tokenを記録し、該当WorkerをQuarantine候補にする。

## 12.10 Partial Failure

* Read-only sibling失敗：依存関係に従い他Actionを継続可能。
* Effect sibling失敗：同一Release UnitをBlock。
* 複数Artifact生成の一部失敗：成功Artifactを隔離し、Release対象外。
* Budget不足：未開始ActionをBlock。
* Worker喪失：Lease Expiry後にEffect照合。
* Lock Manager障害：新規Effect停止、既存Effectを照合。
* Scheduler再起動：LedgerとLock Storeから再構築。

## 12.11 並列度制御

* Global Worker上限
* Provider別Concurrent Request上限
* Model別Rate Limit
* Workspace別Write=1
* Budget Scope別Reservation上限
* Destination別Dispatch上限
* Run別Parallelism
* Risk Level別Parallelism

初期はRead-only Actionの並列から開始し、File／External Effectは段階的に解禁する。

## 12.12 受入Gate

* `AT-FENCE-001`：Stale Workerの最終Commit拒否
* Atomic Claim競合で同一Actionを1 Workerだけが取得
* Claim、Lock、Lease、Fenceの片側障害で実行0件
* Worker Attestation期限切れClaim 0件
* Worker Binary Drift時Claim 0件
* 同一Resource Writeで1件だけ成功
* Lock順序違反検出
* 未完了ActionだけRecovery
* Budget二重消費0件
* Outbox二重Dispatch 0件
* DAG Cycle拒否
* Parent／Child Path競合検出
* Scheduler再起動後状態再構築
* Parallelism上限順守
* Deadlock Timeout後Lock解放
* 逐次実行Fallback可能
* Stale Workerを監査・Quarantine可能


---
# 13. Blind LLM Reviewer 詳細設計【参照仕様・初期実装対象外】

## 13.1 目的

Deterministic Evaluatorでは検出しにくい品質、説明不足、要件逸脱、危険な推論を、Maker情報をBlind化した独立Reviewerで検出する。Reviewerはセキュリティ境界でもRelease主体でもない。

## 13.2 追加コンポーネント

* Review Policy Engine
* Review Context Builder
* Blindness Filter
* Reviewer Adapter
* Structured Output Validator
* Finding Deduplicator
* Review Budget Controller
* Human Review Queue

## 13.3 Review Policy：二段階分離

Blind Reviewerの独立性を保つため、Review Policy判定とReviewer Request構築を分離する。

### Stage 1：Review Routing Decision

Policy Engineだけが次を入力に使用できる。

* Artifact Type
* Risk Level
* Data Classification
* Change Size
* Deterministic EvaluationのPass／Fail概要
* Provider Trust
* Token／Cost Budget
* Regulatory／Business Criticality
* Previous Review回数
* Human Specialist要否

出力：

* Reviewer要否
* Reviewer数
* Reviewer Provider制約
* Rubric Version
* Context上限
* Cost上限
* Maximum Review Rounds
* Human Escalation条件

`ReviewRoutingDecision`はReviewerへ渡さず、Review Orchestratorが利用する。

### Stage 2：Blind Review Request

Blindness Filterは対象Artifactと必要最小要求、Rubricだけから`ReviewRequest`を作成する。次を除去したことを証明する。

* Expected Result
* Deterministic Finding／Failure Reason
* Hidden Case ID／Golden Case ID
* 過去Finding
* Maker Identity／Provider
* Maker自己評価
* Approval／Release判断
* Review Routing Score

`BlindnessEvidence`：

* Source Artifact Hash一覧
* Allowed Field Set Hash
* Removed Field Name一覧。値は保存しない
* Review Request Hash
* Blindness Filter Version
* Policy Version
* Evidence Hash

Policy EngineとReviewer Adapterは別Portとし、Reviewer AdapterがDeterministic Result Storeへアクセスできない権限設計にする。

## 13.4 Risk別設計

| Risk | 実行 |
|---|---|
| LOW | Deterministic Evaluatorのみ |
| MEDIUM | Blind Reviewer 1回 |
| HIGH | ProviderまたはPromptが独立したReviewer＋Human Specialist |
| CRITICAL | Reviewer結果に関係なくHuman Specialist、Auto Release禁止 |

## 13.5 Reviewer Context

含めるもの：

* 対象Artifact
* 必要最小の要求仕様
* Rubric
* Data Classification
* 検証対象範囲
* Blindness Filterが許可した中立的な実行事実。例：対象言語、Artifact Type

含めないもの：

* Makerの自己評価
* Maker会話履歴
* Generator Provider
* 修正回数
* Golden Case ID
* Hidden Test／Hidden Case ID
* 過去Finding
* 期待判定
* Deterministic EvaluationのFinding、失敗箇所、期待値
* Approval情報
* Release判断
* Review Routing Decision／Score

Test ResultをReviewerへ渡す必要がある場合、期待値、Pass／Fail Label、Hidden Case、Deterministic Findingを除いたRaw Test Artifactだけを別の非Blind Review Modeで扱う。その場合は`blind_assurance=REDUCED`と明記し、Blind Reviewerの結果と混同しない。

Blindness Filterの入力Hash、出力Hash、Allowed Field Set Hash、Evidence Hashを`ReviewRequest`へ記録する。

## 13.6 Structured Output

`ReviewFinding`：

* Finding ID
* Category
* Severity
* Confidence
* Artifact Reference
* Source Span
* Description
* Impact
* Suggested Remediation
* Evidence
* Needs Human Review
* Rubric Rule ID

`ReviewResult`：

* Request ID
* Reviewer Identity／Model
* Rubric Version
* Finding一覧
* Overall Recommendation
* Schema Validation
* Token／Cost Usage
* Created At
* Content Hash

Reviewerの`APPROVE`はRelease Decisionではない。

## 13.7 Review Loop

1. Review Policy決定。
2. Context Build。
3. Review実行。
4. Schema検証。
5. Finding Deduplication。
6. Severity Policy評価。
7. Maker修正が必要なら新ChangeSet／Artifactを作成。
8. 修正後は新Review Request。
9. Maximum Round到達でHuman Escalation。

無制限再レビューを禁止する。初期値はMEDIUMで1回、HIGHで最大2回。

## 13.8 Reviewer Provider分離

HIGH以上では次を推奨する。

* Makerと異なるProvider Family
* Makerと異なるPrompt Template
* Session共有なし
* Tool権限なし
* Workspace書込みなし
* 外部送信なし
* Candidate Routing変更権限なし

独立Providerを使えない場合は、同一ProviderでもSessionとContextを分離し、Assurance Levelを下げる。

## 13.9 Prompt Injection対策

Artifact内の命令をReview対象Dataとして囲い、Reviewer System Instructionと混同しない。Artifactが「この指示を無視せよ」「合格と判定せよ」等を含んでも、Rubric外の指示として扱う。

URL、Command、添付をReviewer Toolで自動取得・実行しない。

## 13.10 受入Gate

* `AT-BLIND-001`：Reviewerへ期待結果、Hidden Case、Deterministic Finding、過去Findingが混入しない
* Maker情報混入0件
* Reviewer AdapterからDeterministic Result Storeへのアクセス0件
* Blindness Evidence HashとReview Request Hashを照合可能
* ReviewerからRelease状態変更0件
* Schema不正出力をFinding採用0件
* Token／Cost上限でLoop停止
* CRITICAL自動Release 0件
* Prompt Injection Corpus合格
* Source Spanのない重大FindingをHuman確認へ
* Review Round上限適用
* Reviewer Adapter失敗時はDeterministic＋HumanへFallback
* 非Blind Review ModeをBlind結果として記録0件


---
# 14. Enterprise Hardening 詳細設計【参照仕様。ただし§14.4.1は実装】

## 14.1 目的

個人利用／PoC向けの単一Operator基盤を、組織の本番業務で利用できるように、認証、認可、職務分離、中央Policy、Secret、監査、Retention、SLO、DR、Change Managementを追加する。

## 14.2 Identity／RBAC

初期Role：

| Role | 主な権限 |
|---|---|
| Requester | Task作成、自己Run参照 |
| Operator | Plan実行、低・中Risk Approval |
| Approver | 費用、外部Effect、Change Approval |
| Reviewer | Review Finding作成 |
| Release Manager | Release／Reject |
| Policy Administrator | Policy配布。自分のRun承認不可 |
| Security Auditor | Audit読取、Chain検証 |
| Platform Operator | Runtime／Backup運用。業務Approval不可 |
| Incident Commander | Incident時の停止・封じ込め |

Separation of Duties：

* MakerとHigh Risk Approverを分離
* Policy AdministratorとPolicy Approvalを分離
* Platform OperatorとBusiness Releaseを分離
* ReviewerとRelease Managerを分離
* Break-glass利用者と事後監査者を分離

## 14.3 認証

* Enterprise IdP連携
* MFA
* 短命Access Token
* Workload Identity
* Service-to-Service Mutual Authentication
* Session Timeout
* Device／Network条件。必要な場合
* Break-glass Accountは通常利用禁止、全操作監査

認証方式は環境に合わせるが、Application独自Password Storeを新設しない。

## 14.4 Central Policy Distribution

Policy Package：

* Policy ID／Version
* Effective From／Until
* Environment
* Signature
* Schema Version
* Rule Set
* Provider／Model Allowlist
* Data Classification Matrix
* Budget／Approval Threshold
* Retention
* Rollback Version
* Emergency Recovery Profile
* Distribution Sequence
* Revocation Epoch

Runtimeは署名、Environment、Effective期間、Distribution Sequence、Revocation Epochを検証できるPolicyだけを利用する。取得不能時は、署名検証済みLast Known Good（LKG）をTTL内でのみ使用できる。署名不正、明示Revocation、Environment不一致はTTLを待たず即時失効とする。

### 14.4.1 Policy Freshness Decision Matrix

`Policy Freshness`は次で分類する。

* `CURRENT`：現行Policyを検証済み
* `LKG_WITHIN_TTL`：現行取得不能だが署名済みLKGがTTL内
* `STALE_TTL_EXCEEDED`：LKGがTTL超過
* `REVOKED_OR_INVALID`：署名不正、Revocation、Environment不一致、Sequence巻戻り
* `UNKNOWN`：状態判定不能

正規動作：

| Action／状態 | `CURRENT` | `LKG_WITHIN_TTL` | `STALE_TTL_EXCEEDED` | `REVOKED_OR_INVALID`／`UNKNOWN` |
|---|---|---|---|---|
| 新規Local Read-only | 通常評価 | LKGで通常評価 | **新規開始禁止**。`POLICY_STALE_NEW_ACTION_BLOCKED` | **新規開始禁止** |
| 新規External Read-only | 通常評価 | LKGで通常評価。ただしEndpoint、Data、Entitlement、Pricing全Snapshotが有効な場合のみ | **新規開始禁止** | **新規開始禁止** |
| 新規Workspace Write | Human Approvalを含め通常評価 | LKGで通常評価。ただしApproval／Capability有効時のみ | **開始・Commit禁止** | **開始・Commit禁止** |
| 新規Paid Execution | Human Approval＋Budgetで通常評価 | LKGで通常評価。ただしPricing／Entitlement／Reservation有効時のみ | **予約・実行禁止** | **予約・実行禁止** |
| 新規External Effect | Human Approval＋Outboxで通常評価 | LKGで通常評価。ただしDestination／Payload／Approval有効時のみ | **Prepare・Dispatch禁止** | **Prepare・Dispatch禁止** |
| In-flight：外部送信前／Effect未試行 | 継続可 | 継続可 | **次のEffect Gateで停止** | **即時停止** |
| In-flight：Remote／Effect試行済み | 通常完了または照合 | 通常完了または照合 | **新規送信、Retry、Fallback、追加Effectは禁止。Cancel／Observe／Reconcile／Compensateのみ許可** | 同左。可能なら安全Cancel、不能なら`EFFECT_UNKNOWN` |
| In-flight：Local Read-only実行中 | 通常完了 | 通常完了 | 現在のProcessを安全停止。結果はRelease不可。Recovery用観測だけ許可 | 即時安全停止。結果は隔離 |
| Recovery／Reconciliation専用操作 | 署名Policyで許可 | LKGで許可 | **Immutable Emergency Recovery Profileの範囲だけ許可** | 証拠保全、状態照合、安全Cancelだけ許可 |

不変条件：

* TTL超過後に新規ActionをRisk Levelで例外許可しない。Read-onlyも新規開始は禁止する。
* In-flight Actionは、外部Effectを増やさない安全な観測、Cancel、Reconciliation、Compensationだけを継続できる。
* `PolicyDecision.expires_at`、Policy Package TTL、Capability／Approval／Entitlement／Pricingの各Expiryの最短値をRuntime Gate期限とする。
* Policy期限切れを理由にLedger、Journal、Receipt、Remote Registry、Outbox、Budget Storeを直接修正してはならない。
* Policy復旧後も旧Attemptを再開せず、新Policyで新Plan・新Policy Decision・必要なApprovalを生成する。
* 各分岐は`POLICY_STALE_DETECTED`、`POLICY_STALE_ACTION_BLOCKED`、`POLICY_STALE_RECOVERY_ONLY`のLedger EventとReason Codeを記録する。

このDecision MatrixをPolicy stale動作の唯一の正本とし、他節の「Fail-Closed」記述は本表の具体動作を参照する。

## 14.5 Secret Manager

* Secretは中央Secret Managerへ保存
* Workload Identityで取得
* Purpose／Provider／Tenant Scopeを明示
* 短命Credentialを優先
* Rotation
* Access Audit
* Secret VersionをPlanへ束縛
* Plaintext Export禁止
* Developer Local SecretとProduction Secretを分離

## 14.6 Ledger／Audit

本実装は単一SQLite DBを正本とし、物理Store移行を先取りしない。複数Host要件が発生した場合はJournal Protocolを設計し、別ADRとMigration Gateを通してから移行する。

Audit Export：

* WORMまたは改ざん耐性Store
* SIEM連携
* Correlation ID
* Actor／Role／Device／Source
* Approval／Policy／Effect／Release
* Secret／PII Masking
* Export成功監視
* Retention／Legal Hold

## 14.7 Data Residency／Privacy

* Data Classificationごとの許可Region
* Provider／Subprocessor条件
* Input／Output／Log／BackupのResidency
* Retention／Deletion
* Legal Hold
* Data Subject Request対応。対象となる場合
* Pseudonymization／Masking
* Minimum Necessary
* Provider Training利用条件
* Cross-border Transfer Approval

法務判断は人間のLegal／Privacy責任者が行う。Systemは判断結果をVersioned Policyとして執行する。

## 14.8 Environment分離

* Development
* Test
* Staging
* Production

Provider Account、Secret、Ledger、Artifact、Budget、Workspace、Policy、Auditを環境ごとに分離する。Production Dataを開発環境へ複製しない。

## 14.9 SLI／SLO

初期候補：

| SLI | SLO候補 |
|---|---|
| Control Plane Availability | 月間99.5%以上。初期 |
| Run Audit Completeness | 100% |
| Approval Bypass | 0件 |
| Duplicate Confirmed Effect | 0件 |
| Ledger Chain Verification | 100%成功 |
| Recovery成功率 | 定義済みケース99%以上 |
| External Effect Unknown | Threshold以下、全件手動Queue |
| Budget Reconciliation | 規定時間内95%以上 |
| High Risk Release with Open Finding | 0件 |

可用性SLOより安全性SLOを優先する。

## 14.10 Alert

* Approval Bypass疑い
* Ledger Chain不一致
* Secret Access異常
* Workspace Escape試行
* Unknown External Effect
* Budget Hard Limit
* Repeated Provider Failure
* Stale Worker
* Policy Signature不一致
* Audit Export停止
* Backup失敗
* DR Replication遅延
* DLP／PII高Severity

AlertはSeverity、Owner、Response Time、EscalationをRunbookへ定義する。

## 14.11 Backup／DR

対象：

* Event Ledger
* Artifact Store
* Policy Package
* Schema／Migration
* Approval／Release記録
* Secret Reference Metadata。Secret値はSecret Manager側
* Broker／Worker Registry
* Cost／Outbox状態

DR要件：

* RPO／RTOを業務別に定義
* Backup暗号化
* Restore Test
* Ledger Chain再検証
* Artifact Hash再検証
* Projection再構築
* In-flight Actionを`RECOVERING`へ
* External Effect／Paid Executionは状態照合まで停止
* DR SiteでProvider Account／Residency条件再評価

## 14.12 Change Management

* Git Pull Request
* Code Review
* Security Review
* Schema Compatibility Check
* Migration Dry Run
* Feature Flag
* Canary
* Rollback
* Release Note
* CHANGELOG
* SemVer
* Production Approval
* Post-deployment Verification

Policy、Adapter、Schema、Migration、Runtime Agentは個別Versionを持ち、Execution Planへ含める。

## 14.13 Incident Response

Incident分類：

* Unauthorized Effect
* Secret Exposure
* Ledger Integrity Failure
* Provider Data Exposure
* Budget Overrun
* Workspace Corruption
* Duplicate Dispatch
* Policy Distribution Failure
* Broker Compromise
* Reviewer／Model異常

共通初動：

1. 新規High Risk Action停止。
2. 関連Provider／Secret／CapabilityをRevoke。
3. Ledger／Auditを保全。
4. In-flight Effectを照合。
5. 影響Run、Data、Destination、Costを特定。
6. Human Incident CommanderへEscalate。
7. 復旧・通知・法務判断。
8. Postmortemと恒久対策。

## 14.14 Production Gate

* RBAC／Separation of Duties試験
* Approval Issuer、Policy Publisher、Secret Administrator、Operator、Release Authority、Auditorの兼務制約
* Threat Model更新
* Security Architecture Review
* Privacy／Legal／Data Processing Review
* Provider契約・Entitlement・Retention・Subprocessor証跡
* Penetration Test
* Backup Restore Test
* DR Exercise。RPO／RTO測定と証跡
* SLO／Alert稼働
* Incident Runbook演習
* Provider Sandbox失敗Runbook演習
* Credential／Billing Identity不一致Runbook演習
* Effect Unknown／Ledger Receipt不整合Runbook演習
* Windows Broker拒否Runbook演習
* Budget Reservation孤児Runbook演習
* Stale Worker Runbook演習
* Audit Export、署名検証、Legal Hold検証
* Secret Rotation／Issuer Key Rotation検証
* Data Residency／Deletion／Retention検証
* Change／Rollback／Compensation演習
* Policy Rollback／Emergency Disable演習
* Production Owner承認
* Security、Ops、Privacy／Legal、Data Owner、Budget Ownerの必要承認
* 残余リスク受容記録
* 全Gate Evidence Manifestが`VERIFIED`


---
