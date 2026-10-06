<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

# 3. MVP0-A：Token-aware Mock Execution 詳細設計

## 3.1 目的

トークン上限内のContext構成、Execution PlanとApprovalの完全束縛、Event Ledger、Effect Protocol、Recovery、Human Releaseを、外部通信なし・Mock Providerで実証する。

## 3.2 対象機能

* Markdown／Text／JSON／Sourceを入力するTask Loader
* Context候補の収集、分類、重複除去、選択、圧縮
* Mock Providerによる決定論的なProposed Artifact生成
* Git Worktree内へのHarness-mediated Local File Commit
* Approval Lifecycle
* SQLite Event Ledger
* Content-addressed Artifact Store
* LeaseとFencing Token
* Fault Injection
* Deterministic Evaluator
* Human Release

### 非スコープ

* 外部ネットワーク
* 実Provider
* 機密・個人情報
* Windows／WSL越境
* 複数Worker
* 外部送信
* 有償実行

## 3.3 コンポーネント配置

```text
src/harness/
  domain/
    models/
    states.py
    events.py
    errors.py
    transitions.py
    invariants/
  application/
    planning/
    context_budget/
    approvals/
    execution/
    recovery/
    release/
    unit_of_work.py
  ports/
    clock.py
    id_generator.py
    signer.py
    provider.py
    filesystem.py
    process_launcher.py
    repositories.py
    fault_injector.py
  infrastructure/
    sqlite/
      connection_factory.py
      migrations/
      repositories/
      triggers.sql
    artifacts/local_cas/
    input_read/linux_safe_reader/
    filesystem/git_worktree/
    process/sandbox/
    crypto/
  adapters/
    mock/
    ollama/
    external/
  policy/
  presentation/cli/

tests/
  spec_lint/
  unit/
  property/
  schema/
  integration/sqlite/
  integration/filesystem/
  contract/providers/
  crash/
  security_corpus/
  acceptance/
  performance/
```

Domain層ではPath、Hash、Token量、Approval、Effectを値オブジェクト化し、文字列のまま受け渡さない。CLIはApplication Serviceだけを呼び、Repositoryへ直接アクセスしない。通常実行とEmergency RecoveryはCommand TypeとCapabilityを分離する。

## 3.4 Action構成

1. `TASK_LOAD`
2. `CONTEXT_BUILD`
3. `MOCK_INFERENCE`
4. `PROPOSED_ARTIFACT_VALIDATE`
5. `LOCAL_FILE_COMMIT`
6. `EFFECT_RECONCILIATION`
7. `DETERMINISTIC_EVALUATION`
8. `HUMAN_RELEASE`

各Actionは独立した`ActionIntent`と`ActionAttempt`を持つ。

## 3.5 処理シーケンス

```text
Operator
  → task create
  → 明示Workspace／Root／File指定
  → Linux Root Directory Handle／Workspace Identity確定
  → InputReadCapability署名発行
  → SafeInputReaderでTask Loader／Context候補をRead
  → Classification／Secret Scan
  → 許可ArtifactをCAS保存
  → 拒否Path／理由をSelection Receiptへ記録
  → Context candidates生成
  → TokenProfileSnapshot取得
  → Context Budget Manager
  → ContextBundle／SelectionReceipt生成
  → Capability Snapshot取得
  → RuntimeEnvelopeSpec確定
  → InvocationManifest確定
  → Execution Planner
  → ExecutionPlan Hash確定
  → Policy Engine
  → OperatorへPlan／Runtime／Effect表示
  → Signed Approval発行
  → Approval CAS消費＋Action Claim＋Lease＋Fence
  → Runtime Attestation
  → Spec一致検証
  → Mock Inference
  → Proposed ArtifactをCAS保存
  → Effect Journal／ACTION_PREPAREDをDurable Commit
  → EXECUTION_ATTEMPTED
  → Ephemeral Worktreeへ適用
  → Atomic Replace
  → 実ファイル／Identity再読込
  → EFFECT_OBSERVED
  → EffectReceipt Durable保存
  → ACTION_COMMITTED
  → Deterministic Evaluation
  → Human Release
  → Run COMPLETED
```

RuntimeEnvelopeSpec、InvocationManifest、TokenProfileSnapshot、Policy SnapshotのいずれかがApproval後に変化した場合は、実行せずPlan再生成と再承認へ戻る。

## 3.6 Token Budget Manager

### 入力

* Provider／Model Profile
* Token Budget Policy
* System／Developer Instruction
* Task本文
* Repository候補
* 過去Artifact候補
* Tool定義
* InputReadCapability Set Hash
* Input Read Evidence Hash
* Control／Data Message Role Manifest
* 予約出力Token
* 予約Tool Token

### 優先順位

1. SystemおよびSecurity Policy
2. Execution Planに必要な制約
3. Approval対象情報
4. 現在のTask
5. 直接参照されたArtifact
6. 依存関係上必要なSource
7. 最新の構造化状態
8. 要約済み過去情報
9. 補助資料

### 選択アルゴリズム

1. §1.16のInputReadCapability、File Identity、Classification／Secret Scan Evidenceを検証し、合格した入力だけをFragment候補にする。
2. 各FragmentのControl／Data Message RoleをSchema検証し、Untrusted入力のControl Role昇格を拒否する。
3. 各FragmentをCanonicalizeする。
4. Content Hashで完全重複を除去する。
5. MVP0-Aでは類似度判定を実装しない。Content Hash完全一致だけを重複除去し、同一出典・同一版でもHashが異なる場合は別Fragmentとして保持する。
6. 必須Fragmentを先に確保する。
7. 残BudgetへPriority、Freshness、Dependency、Trust Levelを用いて追加する。
8. 収まらない必須Fragmentがある場合は`CONTEXT_BUDGET_EXCEEDED`で停止する。
9. 圧縮可能Fragmentは、原文参照と圧縮深度が条件を満たす場合だけ圧縮する。
10. 選択・除外・圧縮・Read拒否結果を`ContextSelectionReceipt`へ保存する。

### 圧縮規則

* 圧縮深度は初期値1、最大2。
* 圧縮済みArtifactを無制限に再圧縮しない。
* Summaryに含まれるFactはSource SpanまたはSource Artifact IDへ紐づける。
* Security Policy、Approval条件、Secret取扱い、禁止事項を要約対象にしない。
* 圧縮器がMock以外になる段階では、圧縮結果もUntrusted Artifactとして検証する。

## 3.7 Execution Plan

`ExecutionPlan`はApproval前に完全解決し、意味内容と個別Run Authorityを分離して保持する。

### 3.7.1 Plan Content Projection

`plan_content_hash`の入力となる`PlanContentProjection`は最低限次を含める。

* Action Graph
* Context Bundle Hash
* InputReadCapability Set Hash
* Input Read Evidence Hash
* Control／Data Policy Hash、Message Role Manifest Hash
* Token Budget Policy Hash
* Token Profile Snapshot Hash
* Provider ID=`mock`
* Mock Adapter Version
* Input Artifact Hash
* Expected Output Type／Output Schema Hash
* Workspace ID、OS Boundary=`LINUX`
* Canonical Target Path
* Base Object Hash／Parent Directory Identity
* Expected Operations
* `RuntimeEnvelopeSpec` Semantic Hash
* `InvocationManifest` Semantic Hash
* Executable Absolute Path／SHA-256
* argv配列。Shell文字列、`shell=true`、暗黙Shell展開は禁止
* Working Directory Identity
* Adapter／Model／Model Digest
* Auth Route=`NONE`、Account／Tenant／Billing Identity=`NONE`
* Config／Plugin／MCP／Hook／Instruction Set Hash
* Environment Allowlist Hash
* Filesystem／Network／Tool Policy Hash
* Timeout、Token、Memory、CPU、Output Size上限
* Evaluation Policy Hash
* Policy Snapshot Hash
* Schema Version Set Hash
* Cost Upper Bound=`0`
* Planner Algorithm Version
* `hash_profile_version=1`

`run_id`、`execution_plan_id`、`plan_version`、`issued_at`、`expires_at`はPlan Contentへ含めない。

Plan Content決定性規則：

* Action GraphのNodeはランダム`action_id`ではなく、`semantic_action_key = H(action_type, normalized_inputs, normalized_outputs, dependency_keys)`で参照する。
* Node配列はTopological Order＋`semantic_action_key`昇順でCanonical化する。
* `RuntimeEnvelopeSpec`と`InvocationManifest`のSemantic Hashは、Record ID、生成時刻、表示用Metadataを除いた内容Projectionから計算する。
* Content-addressed Artifact ID以外のランダムIDをPlan Content Projectionへ含めない。
* Candidate列挙、File列挙、Environment Key、Tool定義は明示Sort Keyで整列する。
* 同順位のTie-breaker、Planner Algorithm Version、Normalizer VersionをPlan Contentへ含める。
* Clock、乱数、Process ID、Filesystem列挙順、Map iteration順へ依存するPlan生成を禁止する。
* `set`／`frozenset`の反復結果へ依存しない。集合が必要な場合はCode Point昇順で`sorted`した`tuple`へ変換してから使用する。禁止はAST検査で強制する（§1.11.1）。
* Locale依存の比較・整列・大小変換をPlan Content生成へ使用しない。
* Snapshot Record ID、取得時刻、UUIDv7等の非決定値はPlan Contentへ含めず、ExecutionPlanレコードの非Hash Metadataとして保持する。
* Plan Contentへ追加できるFieldは`registries/plan-content-fields.yaml`に登録されたものだけとする。
* 外部状態は1回だけFrozen Inputへ固定し、同じFrozen InputからPlanを2回BuildしてHash一致を検査する。

### 3.7.2 Execution Authority Envelope

`execution_plan_hash`の入力となるAuthority Envelopeは次を含める。

* `plan_content_hash`
* `run_id`
* `execution_plan_id`
* `plan_version`
* `issued_at`
* `expires_at`
* `planner_identity`
* `authority_scope`
* `hash_profile_version`

`RuntimeEnvelopeSpec`と`InvocationManifest`のどちらかが未確定、またはHash不一致の場合、Planを`RESOLVED`にしない。

### RuntimeEnvelopeSpec

最低限、次を含める。

* Runtime Type、Launcher Version
* Executable Absolute Path、Executable SHA-256
* argv配列
* Working Directory、Workspace Identity、OS Boundary
* Provider、Adapter、Model、Model Digest
* Auth Route、Credential Type、Account、Tenant、Billing Identity
* Config Root、Config Hash
* Repository Instruction Policy
* Plugin／Hook／MCP AllowlistとHash
* Environment Allowlist
* Filesystem、Network、Tool、Process Policy
* Child Process Policy
* Output Schema Hash
* Timeout、Token、CPU、Memory、Disk、Output Size上限
* Sandbox Implementation、Required Assurance Level
* Runtime Spec Version

### InvocationManifest

* Invocation ID
* Invocation Mode=`MOCK | LOCAL | EXTERNAL`
* Provider Operation
* Provider／Model
* Billing Mode=`FREE | PAID | UNKNOWN`
* Request Artifact Hash
* Context Bundle Hash
* Instruction Hash
* Message Role Manifest Hash
* Control／Data Policy Hash
* Tool Definition Hash
* Output Schema Hash
* Endpoint／Socket／Port
* Auth Route、Credential Reference Version
* Provider Account／Tenant／Billing Identity
* Retry Policy、Cancel Policy
* Idempotency Policy
* Token Profile Snapshot Hash
* Technical Capability Snapshot Hash。該当時
* Commercial Entitlement Snapshot Hash。ExternalまたはPaid時
* Pricing Catalog Hash。ExternalまたはPaid時
* Entitlement Evidence Artifact Hash。ExternalまたはPaid時
* Expected Remote／Local Effect=`NONE | REMOTE_INVOCATION | WORKSPACE_WRITE | EXTERNAL_EFFECT`

### Canonicalization

共通規約§1.11を適用する。

```text
plan_content_hash =
  SHA-256(
    "FDE-HARNESS/plan-content/1/" UTF-8 bytes
    || RFC8785-JCS(PlanContentProjection)
  )

execution_plan_hash =
  SHA-256(
    "FDE-HARNESS/execution-plan-authority/1/" UTF-8 bytes
    || RFC8785-JCS(ExecutionAuthorityEnvelope)
  )
```

Secret値、署名値、表示用要約はHash入力へ含めない。Secret Reference、Version、Auth Route、Account Scope、Issuer Key IDはPlan Contentへ含める。

## 3.8 Approval Lifecycle／承認UX

### 3.8.1 状態

```text
NOT_REQUIRED
REQUESTED
ISSUED
CONSUMED
EXPIRED
REVOKED
INVALIDATED
REPLAY_DENIED
```

### 3.8.2 承認粒度

| Risk Level | 粒度 |
|---|---|
| `LOW`／`MEDIUM` | Run単位。`action_scope`へ全Actionを列挙 |
| `HIGH`／`CRITICAL` | Effectを持つAction単位 |

Run単位承認でも、Plan、Context、Target Path、Base Hash、Provider、Executable、argv、cwd、Runtime Spec、Auth Route、Account、Policy、Schema、Token Profile、Pricing、Entitlementのいずれかが変化した場合はApprovalを無効化する。

### 3.8.3 既定表示

承認画面の既定表示は次の7項目とする。

```text
1. 何をするか：Action種別と件数
2. 対象：最大10 Path。超過分は件数
3. Provider／Model
4. コスト上限
5. 変更規模：File数、追加／削除行、Before／After Hash確認
6. Risk Level
7. 有効期限：絶対時刻と残り時間

Plan Content Hash／Execution Plan Hash：先頭8桁
完全表示：harness plan <run-id> --full
```

2回目以降の承認では、前回承認済みPlanとの差分と無効化理由を強調表示する。変更なしの項目も「変更なし」と明示する。

### 3.8.4 発行・消費

Approval Serviceは§1.10の署名Grantを発行する。消費時は署名、Issuer、Subject、Tenant、Nonce、Expiry、Revocation、Clock Skew、`execution_plan_hash`を検証し、CASで1回だけ消費する。同時Consume 2件は成功1件だけとする。

### 3.8.5 バイパス禁止

次を実装しない。

* `--yes`、`--auto-approve`、`--force`、`--skip-approval`相当のFlag
* 環境変数による承認Skip
* Predicateを持たない自動承認。および「前回と同じ」を根拠とする自動承認
  （人間が事前に承認した宣言型Predicateといま解決されたPlanの照合による
  `DelegationGrant`はADR-006で許可する。過去の実行履歴は判定根拠にしない）
* Approval Managerを通らない直接実行API

CI非対話モードは署名済みApproval Artifactを要求し、承認省略として扱わない。

### 3.8.6 受入基準

| 指標 | 基準 | 測定 |
|---|---|---|
| 既定表示項目 | 7以下 | 静的検査 |
| 再承認差分 | 変更点と理由を特定可能 | 自動試験 |
| バイパス機構 | 0件 | AST＋全文検索 |
| 操作時間 | 10回実測の中央値≤30秒 | 表示から入力完了 |

承認時間はHuman UX指標であり、CIの固定Sleepで代替しない。測定前提、被験者、Run内容をEvidenceへ記録する。

## 3.9 Local File Commit

### 対象OS

MVP0-AのLocal File CommitはWSL2 Linux側Filesystem専用とする。Windows Native、UNC、`/mnt/*`、Windows側Filesystem、未知Network／FUSE／Overlayへの書込みは`WORKSPACE_ON_FOREIGN_FS_DENIED`で停止する。MVP2-Aは参照仕様であり、初期実装では解禁しない。

### Path検証

1. Workspace RootをDirectory File Descriptor、Mount ID、Filesystem Identityで固定する。
2. 相対Pathだけを受け付ける。
3. `..`、絶対Path、NUL、制御文字を拒否する。
4. `openat2`の`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV`、または安全なDirectory Walkを使用する。
5. 各ComponentでSymlink、Mount越境、Hardlink Policy違反、特殊Fileを拒否する。
6. Base Object Hash、Target inode、Parent inode、Device ID、Mount IDを取得する。
7. 書込み直前にParent／Target IdentityとBase Hashを再確認する。
8. Case Collision、Unicode正規化衝突を拒否する。
9. `/proc`、`/sys`、`/dev`、`/run`、Socket、FIFO、Deviceを拒否する。

### Effect／Operation Journal

単一Fileでも`OperationJournal`を作成する。

* `effect_id`
* `operation_journal_id`
* `attempt_id`
* `workspace_id`
* `target_relative_path`
* `filesystem_id`
* `mount_id`
* `parent_file_identity`
* `target_file_identity_before`
* `base_object_hash`
* `expected_after_hash`
* `temp_object_identity`
* `fencing_token`
* `durability_level`
* `state`
* `prepared_at`
* `execution_attempted_at`
* `observed_at`
* `receipt_id`
* `store_version`

DBに`UNIQUE(effect_id)`、`UNIQUE(operation_journal_id, operation_id)`を設ける。

### Commit Protocol

1. Approval消費、Action Claim、Lease取得後、最新Fencing Tokenを取得する。
2. Workspace、Mount、Target Identityを再検証する。
3. 同一Directory／同一FilesystemにTemp Fileを`O_CREAT|O_EXCL`で作成する。
4. Bytesを書込み、Fileをflush、fsyncする。
5. Temp HashをExpected After Hashと照合する。
6. `ACTION_PREPARED`とOperation Journalを単一SQLite TransactionでCommitし、`PREPARED_DURABLE`とする。
7. 最終Storage層でFencing TokenとEffect ID未使用を検証する。
8. `EXECUTION_ATTEMPTED`を記録する。
9. Atomic Replaceを実行する。
10. Parent Directoryをfsyncする。
11. 対象をDirectory Handle相対で再Openし、IdentityとObserved Hashを取得する。
12. 一致時に`EFFECT_OBSERVED`を記録する。不一致または観測不能は`EFFECT_UNKNOWN`。
13. `EffectReceipt`と対応Ledger Eventを同一SQLite Transactionで永続化する。
14. Receipt再読込とHash検証後に`ACTION_COMMITTED`を記録する。

`durability_level=STORAGE_SYNC`は「FileとDirectoryに対しfsyncを呼び出し、SQLiteをFULL同期でCommitした」ことを意味する。デバイスが電源断まで同期したことの証明ではない。この限界をGate Evidenceへ必ず記録する。

## 3.10 Recovery／Emergency Recovery

Recovery Engineは起動時とOperator要求時に、Ledger、Operation Journal、Receipt、Artifact、実Filesystemを照合する。

| 最終Event／Journal State | 観測結果 | Recovery Decision |
|---|---|---|
| `ACTION_STARTED` | Journalなし | Read-onlyなら新Attempt。Effect Actionは人間確認 |
| `PREPARED_DURABLE` | Base Hash一致、Target未変更 | 新Leaseと同一Effect IDで安全なCommit継続可 |
| `PREPARED_DURABLE` | Expected After Hash一致 | `EFFECT_OBSERVED`とReceiptを補完 |
| `PREPARED_DURABLE` | 別Hash | `EFFECT_CONFLICT` |
| `EXECUTION_ATTEMPTED` | Expected After Hash一致 | Receipt補完後Commit |
| `EXECUTION_ATTEMPTED` | Base Hash一致 | Effect未実行が証明された場合だけOperator承認付き新Attempt |
| `EXECUTION_ATTEMPTED` | 判定不能 | `EFFECT_UNKNOWN`、自動再実行禁止 |
| `EFFECT_OBSERVED` | Receiptなし | Evidence検証後Receipt補完 |
| `RECEIPT_DURABLE` | Action未Commit | Receiptと実体一致時にCommit補完 |
| Receiptあり／Ledgerなし | Receiptと実体一致 | Compensating Event Append |
| Ledgerあり／Receiptなし | 根拠不足 | `BLOCKED_REPAIR_REQUIRED` |
| Artifact Manifestあり／Bytesなし | 欠損 | `BLOCKED_REPAIR_REQUIRED` |
| `CANCEL_REQUESTED` | Process Tree不存在 | `CANCEL_CONFIRMED` |
| 不明 | 判定不能 | `EFFECT_UNKNOWN`または`BLOCKED_REPAIR_REQUIRED` |

Recovery規則：

* 過去Event、Receipt、JournalをUPDATE／DELETEしない。
* 補完は`RECOVERY_STARTED`、`RECOVERY_DECIDED`、Compensating Eventで行う。
* 古いFencing TokenでのCommitを最終Storage層が拒否する。
* Effect不在を証明できない限り、自動再実行しない。
* 未照合Effectを持つRunは全終端状態への遷移を拒否する。
* OperatorのCompensating Decisionで別Runへ引き渡す場合、承認、理由、Evidence Hash、引継ぎ先Runを記録する。

### 3.10.1 Emergency Recovery Profile

Policyが`STALE_TTL_EXCEEDED`でも、実行中Effectの照合、停止、証拠保全だけを可能にする。

配置：

```text
policy/emergency-recovery-profile.v1.json
policy/emergency-recovery-profile.v1.sig
policy/emergency-trust-anchors.json
```

Profileは署名済みImmutable Artifactとし、Repositoryに同梱する。Runtimeは`minimum_accepted_profile_version`を保持し、古いVersionへのRollbackを拒否する。Issuer Key Rotationと旧Key失効はTrust Anchor更新として署名管理する。

許可操作：Store Read、Ledger Chain検証、Artifact Hash検証、Projection再構築、Recovery Event Append、Filesystem Read-only照合、安全Cancel、承認付きCompensating Event、Evidence Export。

禁止操作は次の**9操作**とする。名称を固定し、実装・Registry・試験がこの表を正本とする。

| # | 操作名 | 内容 |
|---:|---|---|
| 1 | `NEW_ACTION` | 新しいActionの起票 |
| 2 | `NEW_EFFECT_PREPARE` | Operation Journalの`PREPARED_DURABLE`確定 |
| 3 | `NEW_EFFECT_EXECUTE` | 準備済みEffectの実行 |
| 4 | `PROVIDER_CALL` | Provider呼出 |
| 5 | `BUDGET_RESERVATION` | Budget予約 |
| 6 | `OUTBOX_DISPATCH` | Outbox Dispatch |
| 7 | `WORKSPACE_WRITE` | Workspaceへの書込み |
| 8 | `POLICY_OR_FLAG_CHANGE` | Policy／Feature Flagの変更 |
| 9 | `LEDGER_JOURNAL_RECEIPT_MUTATION` | Ledger／Journal／ReceiptのUPDATE／DELETE |

**2と3を別操作として数える。** 不変条件#2は「Effect実行前にOperationJournalを
`PREPARED_DURABLE`へCommitする」と定めており、この境界は§26.5のCrash Recoveryが
「作用が起きたか」を3値判定する際の基準そのものである。準備と実行は別の時点で
別の痕跡を残す操作であり、1つに数えるとEmergency Recoveryが何を禁じているのかが
本文から決まらなくなる。

拒否したすべての操作について`new_effects == 0`を**測定**する。禁止していることと、
実際に何も起きなかったことは別であり、後者は数えなければ言えない。


```text
harness recover <run-id> --emergency
```

署名不正は`EMERGENCY_PROFILE_SIGNATURE_INVALID`、範囲外操作は`EMERGENCY_OPERATION_NOT_ALLOWED`で拒否する。通常PolicyをSkipした事実、Profile Version／Hash、Trust Anchor Hashを`POLICY_STALE_RECOVERY_ONLY`イベントとRuntime Attestationへ記録する。

### 3.10.2 Fault Injection

Fault Pointは単一Enumと`FaultInjectorPort.maybe_fault()`で実装する。

```text
BEFORE_ACTION_PREPARED
AFTER_ACTION_PREPARED
BEFORE_EXECUTION_ATTEMPTED
AFTER_EXECUTION_ATTEMPTED
AFTER_TEMP_WRITE_BEFORE_FSYNC
AFTER_ATOMIC_REPLACE_BEFORE_OBSERVE
AFTER_EFFECT_OBSERVED
BEFORE_RECEIPT_STORE
AFTER_RECEIPT_STORE_BEFORE_COMMIT
DURING_LEDGER_APPEND
```

モードは`CRASH`（`os._exit(137)`）、`EXCEPTION`、`DELAY`、`DISK_FULL`、`IO_ERROR`とする。`DISK_FULL`／`IO_ERROR`は単純な例外Hookではなく、Faultable File I/O Adapter、SQLite書込み境界、または容量制限Filesystemを使用して実I/O地点へ注入する。

`HARNESS_FAULT_INJECTION`に署名またはTest Harness生成の注入計画がある場合だけ有効にする。Runtime Attestationへ`fault_injection_enabled`と計画Hashを記録し、Policyの`fault_injection_permitted=false`時はProcess起動前に`FAULT_INJECTION_NOT_PERMITTED`で停止する。

`os._exit(137)`はProcessを即時終了するがOS Page Cacheは生存するため、この手法で検証できるのはApplication層の順序整合までである。**fsync境界そのものは検証できない。** Gate 8／9／11の文言をこの限界を超えて解釈してはならない。

そのため耐久性検証を3段階へ分離し、各CaseがどのTierで検証されたかをEvidenceへ記録する。

| Tier | 手法 | 検証できる範囲 | 適用 |
|---|---|---|---|
| `T1_PROCESS_KILL` | `os._exit(137)`による即時終了 | Application層のEvent／Journal順序、重複Effect不在 | 全10 Fault Point |
| `T2_CACHE_DROP` | ループバックImage上のext4へWorkspaceとDBを配置し、Process終了後に強制umount／`echo 3 > /proc/sys/vm/drop_caches`／再mountしてから照合 | fsync漏れによるデータ喪失。OS Cache依存の偽の成功 | `AT-CRASH-001`の`BEFORE_PREPARED`／`AFTER_PREPARED`／`TEMP_BEFORE_FSYNC`／`LEDGER_APPEND` |
| `T3_DEVICE_POWER_LOSS` | 実デバイス電源断またはdm-flakey等のI/O障害注入 | デバイス同期の実効性 | **未実施。`UNVERIFIED`として明示する** |

`EvidenceManifest`の各Crash Caseは`durability_tier`を必須とする。`T2`未実施のCaseを`T2`相当として報告してはならない。`T3`は本構成では実施せず、残余リスク（§22）として維持する。

プロセスクラッシュ試験で検証できるのはApplication／OS Cache層までであり、電源断相当のデバイス耐久性は未検証として記録する。

## 3.11 Deterministic Evaluator

初期Evaluator：

* Expected File存在
* File Hash一致
* UTF-8妥当性
* Markdown／JSON等の形式検証
* 禁止Pattern不在
* Diff Size上限
* Workspace外変更なし
* Ledger Chain正常
* Effect Receipt整合
* 未解決Actionなし

Evaluator結果は`EvaluationResult`としてArtifact HashとPolicy Versionへ束縛する。

## 3.12 CLI

```text
harness task create --workspace <workspace-id> --read-root . --file task.md
harness input-read inspect <run-id>
harness plan <run-id>
harness approval request <run-id>
harness approve <run-id> --plan-hash <hash> --auth-session <reference>
harness run <run-id>
harness inspect <run-id>
harness recover <run-id>
harness cancel <run-id>
harness release <run-id>
harness verify-ledger <run-id>
harness verify-evidence <run-id>
harness repair <run-id>
harness gc --dry-run
harness backup create
harness backup verify <backup-id>
harness deploy drain --check
```

危険操作は対話入力だけに依存せず、認証済み主体、署名Grant、明示引数、実行内容再表示を必要とする。CLIへGrant署名鍵やSecret値を渡さない。CI向け非対話モードでは署名済みApproval ArtifactとIssuer検証を要求する。

## 3.13 受入Gate

MVP0-Aは`registries/gates.yaml`が定めるMVP0-A Gateを全て満たすまでRuntime GOを出さない。件数とTest参照は`registries/gates.yaml`から自動生成する。

各GateのTest参照は、**MVP0-A Scopeに属するCaseだけ**を指してよい。Scope外Caseを参照するGateはSpec Lintが`GATE_REFERENCES_OUT_OF_SCOPE_TEST`で拒否する。v1.6ではGate 4が`AT-CONFIG-001`（MVP0-B以降）を、Gate 13が`AT-EVENT-MAPPING-001`（MVP0-C以降）を参照していた。v1.8では次で解消した。

* `AT-CONFIG-001/DRIFT`：Mock AdapterもRuntimeEnvelopeSpecとRuntime Attestationを持つため、Config DriftはMVP0-Aから検証可能。`phase_scope`へMVP0-Aを追加した。
* `AT-EVENT-MAPPING-001`：Test IDが複数Phaseの関心事を束ねていたため、Case単位で`phase_scope`を分離した。`LEDGER_EVENT_MISSING`はLocal File Commitでも発生する汎用不整合なのでMVP0-A以降、`REMOTE_UNCERTAIN`はMVP0-C以降、`BUDGET_UNKNOWN`はMVP1-D、`OUTBOX_UNKNOWN`はMVP1-Eとした。

| # | Gate条件 | Test ID |
|---:|---|---|
| 1 | WorkspaceがWSL2 Linux native FS上にあり、Foreign FSを拒否 | `AT-WSL-BOUNDARY-001` |
| 2 | MVP0-A割当のTest Manifestが全件PASS | `Scope内Manifest全体` |
| 3 | RuntimeEnvelopeSpecとInvocationManifestをApproval前に固定 | `AT-PLAN-001` |
| 4 | argv／cwd／env／Executable／Model／Auth／Policy変更時の起動拒否 | `AT-CONFIG-001, AT-PLAN-001` |
| 5 | Approval署名、Nonce、Expiry、Revocation、Replay合格 | `AT-APPROVAL-001` |
| 6 | Approval同時Consumeで成功1件だけ | `AT-APPROVAL-002` |
| 7 | 許容Clock Skew超過でFail-Closed | `AT-CLOCK-SKEW-001` |
| 8 | Fault Point 10点の全Crash Caseから手動DB改変なしで復旧または安全停止 | `AT-CRASH-001` |
| 9 | ENOSPC／EIO Faultから部分Commitなしで復旧または安全停止 | `AT-FAULT-IO-001` |
| 10 | Policy禁止下でFault Injection実行0件 | `AT-FAULT-GUARD-001` |
| 11 | PREPARED_DURABLE前のEffect実行0件 | `AT-CRASH-001` |
| 12 | EFFECT_UNKNOWNからの自動再実行0件 | `AT-CRASH-001, AT-RUN-TERMINAL-001` |
| 13 | Receipt欠落／Ledger欠落を双方検出 | `AT-CRASH-001, AT-EVENT-MAPPING-001` |
| 14 | Ledger Hash Chain改ざん検出 | `AT-LEDGER-TAMPER-001` |
| 15 | Stale Fencing TokenによるCommit 0件 | `AT-FENCE-001` |
| 16 | 未照合Effectを持つRunの終端0件 | `AT-RUN-TERMINAL-001` |
| 17 | Approval回避機構0件 | `AT-APPROVAL-001, AT-APPROVAL-UX-001` |
| 18 | 承認既定表示7項目、差分表示、操作時間中央値30秒以下 | `AT-APPROVAL-UX-001` |
| 19 | 重複Effect 0件 | `AT-CRASH-001` |
| 20 | Workspace Escape 0件 | `AT-INPUT-PATH-001, AT-PATH-001` |
| 21 | Symlink／Mount／特殊File／Virtual FS Input Read拒否 | `AT-INPUT-PATH-001` |
| 22 | Read CapabilityなしのTask／Context読取り0件 | `AT-INPUT-PATH-001` |
| 23 | Artifact Put前のClassification／Secret Scan実施率100% | `AT-CONTROL-DATA-001, AT-INPUT-PATH-001` |
| 24 | Untrusted入力のControl Role昇格0件 | `AT-CONTROL-DATA-001` |
| 25 | Token上限超過Provider呼出0件 | `AT-PLAN-001` |
| 26 | 定義済みSecret CanaryのDB／Log／Artifact漏えい0件 | `AT-CONTROL-DATA-001` |
| 27 | LedgerからProjection再構築成功 | `AT-EVENT-ORDER-001` |
| 28 | Release DecisionなしのRun完了0件 | `AT-RUN-TERMINAL-001` |
| 29 | Plan非決定性を検出し停止 | `AT-PLAN-DETERMINISM-001` |
| 30 | Core Schema全件、Conditional、Cross-reference全件合格 | `AT-SCHEMA-COMPLETE-001, AT-SCHEMA-CONDITIONAL-001` |
| 31 | Policy期限切れ動作がDecision Matrixと一致 | `AT-POLICY-STALE-001` |
| 32 | Emergency Recovery範囲外操作0件、署名／Rollback検証 | `AT-EMERGENCY-RECOVERY-001` |
| 33 | 制御処理p50≤5秒、p95≤8秒 | `AT-PERF-001` |
| 34 | Migration、Backup／Restore、Integrity Check合格 | `AT-MIGRATION-001` |
| 35 | Active Run／承認待ちがあるDeployをDrainで拒否 | `AT-DRAIN-001` |
| 36 | GC Dry-runが参照Artifactを削除対象にしない | `AT-GC-001` |
| 37 | Predicate外の自動承認0件。委任による委任の生成・拡大0件 | `AT-DELEGATION-001` |
| 38 | 失効・縮小が即時反映され、拡大が新規承認なしに成立しない | `AT-DELEGATION-001` |
| 39 | 自動承認された全実行がDelegationGrantと生成元Runへ追跡可能 | `AT-DELEGATION-001` |
| 40 | マスク後の再スキャン不合格・変換不正・Masker不在で通過0件 | `AT-MASKING-001` |
| 41 | 認証情報がMaskerへ渡らず、MaskingReceipt／Ledger／LogへSecret Canary漏えい0件 | `AT-CONTROL-DATA-001, AT-MASKING-001` |
| 42 | risky Grapheme codepoint・NATIONAL_ID・Normalization Profile不一致は全てFail-Closed | `AT-MASKING-001` |
| 43 | Raw PII経路のMasker隔離不成立時にMasker呼出しと未マスク通過が0件 | `AT-MASKING-001` |
| 44 | Python 3.11／3.12でUCD14 Profile Hashと正規化Hashが一致し、Artifact欠落時はFail-Closed | `AT-MASKING-001` |
| 45 | Mask ratioはLLM追加分だけに適用され、同一カテゴリContainment Expansion以外のSpan衝突は0件通過 | `AT-MASKING-001` |

Gate証跡にはTest Run ID、Expectation Descriptor Hash、Input Fixture Hash、Plan Content Hash、Execution Plan Hash、Runtime Attestation Hash、Event Chain Head、Evidence Manifest Hash、`durability_tier`を含める。電源断相当のDevice Durability（`T3`）は未検証として明示する。

MVP0-A Scopeの規模はRegistryから導出され、次のとおりである。

```text
Release Scope : MVP0-A
必要Gate      : 45
必要Test ID   : 31
必要Case      : 108
必要Evidence領域 : 12（`approval_ux`はMVP0-B以降。§26.2）
```

**この件数を本文へ手入力してはならない。** 上記は`tools/build_registry_snapshot.py`の出力を転記した参考値であり、判定に用いる値はVerifierがRegistryから読む。

## 3.14 成果物

* 統合正本とRegistry
* Spec Linter／Test Manifest Validator
* Domain Model／State Transition表
* Canonicalization／Plan Content Field台帳
* JSON Schema 22種とValid／Invalid Fixture
* SQLite Migration、Backup／Restore、Drain手順
* CLI
* Mock Adapter
* Recovery Simulator／Emergency Recovery Profile
* Fault Injection 10点＋I/O Fault Suite
* Linux Safe Reader Corpus
* Golden Corpus形式仕様
* Operator Runbook
* Gate Decision Report／TestEvidenceManifest
