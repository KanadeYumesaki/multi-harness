<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

# 6. MVP1-A：Harness-mediated Workspace Change 詳細設計

## 6.1 目的

Providerが生成した変更案を`ChangeSet`として受け取り、HarnessがPath、Base Hash、Policy、Test、Approvalを検証したうえでWorkspaceへ適用する。ProviderプロセスにはWorkspace書込み権限を与えない。

## 6.2 追加コンポーネント

* ChangeSet Normalizer
* ChangeSet Schema Validator
* Path／File Type Validator
* Ephemeral Worktree Manager
* Diff Analyzer
* Test／Lint Runner
* Change Approval Manager
* Rollback Snapshot Manager
* Workspace Committer

## 6.3 ChangeSetデータモデル

```text
ChangeSet
  changeset_id
  run_id
  action_id
  generation_plan_hash
  provider_output_artifact_hash
  base_tree_hash
  workspace_id
  operations[]
  adapter_version
  schema_version
  created_at
  content_hash
```

`ChangeOperation`：

* `operation_id`
* `type`: `ADD | MODIFY | DELETE | RENAME`
* `source_path`。RENAME時のみ
* `target_path`
* `base_object_hash`
* `expected_after_hash`
* `content_artifact_hash`。ADD／MODIFY時
* `encoding`
* `line_ending`
* `file_size`
* `binary`
* `executable_bit`
* `reason`
* `source_spans`

### 制約

* `ADD`ではBase Objectが存在しないこと。
* `MODIFY`／`DELETE`／`RENAME`ではBase Object Hashが一致すること。
* `RENAME`先が存在する場合は明示Policyなしに上書きしない。
* 同一Pathへ複数Operationを持たない。
* ParentとChildの競合Operationを拒否する。
* Case-insensitive Filesystemで衝突するPathを拒否する。
* Binary、巨大File、Generated、Vendor、Secret候補はPolicyで個別許可がない限り拒否する。

## 6.4 正規化

Provider固有Diff、Patch、JSON、Tool Outputを直接適用しない。Normalizerが以下を実施する。

1. Raw OutputをArtifact Storeへ保存。
2. Provider形式を中間構造へParse。
3. Workspace相対Pathへ正規化。
4. OperationをCanonical順へ並べる。
5. Contentを個別Artifactへ分離。
6. Expected After HashをHarness側で再計算。
7. ChangeSet Canonical Hashを生成。
8. Provider主張値とHarness計算値の差異をFindingへ記録。

NormalizerはPath解決やWorkspaceへの書込みを行わない。

## 6.5 検証パイプライン

```text
SCHEMA_VALIDATE
  → PATH_BOUNDARY_VALIDATE
  → FILE_TYPE_VALIDATE
  → BASE_HASH_VALIDATE
  → POLICY_VALIDATE
  → PRE_TEST_SECRET_SCAN
  → EPHEMERAL_APPLY
  → ISOLATED_STATIC_ANALYSIS
  → ISOLATED_TEST
  → POST_TEST_SECRET_SCAN
  → DIFF_ANALYSIS
  → CHANGE_APPROVAL
  → COMMIT
```

Provider生成ChangeSetおよび適用後のWorktreeは未信頼コードとして扱う。各段階の結果を`ChangeValidationResult`へ追記し、Sandbox Spec Hash、Test Image Hash、Test Command argv、Test Output Artifact Hash、最終Validation HashをApprovalへ束縛する。

Test Runnerが生成したOutput、Coverage、JUnit、Log、Artifactは全て`UNTRUSTED_TEST_OUTPUT`としてSchema検証・サイズ制限・表示Sanitizeを通す。

## 6.6 Ephemeral Worktree／Untrusted Test Runner

### Worktree

* Run専用Git Worktreeを作成。
* Base Commit／Tree Hashを固定。
* Provider ProcessはWorktreeへアクセスしない。
* ApplyはHarness専用Identityで行う。
* Worktree生成・削除をLedgerへ記録。
* Release前にMain Workspaceへ反映しない。
* Linux専用。Windows／WSL上のTestはMVP2-A Gate後のみ。

### Test Runner隔離

Providerが生成した変更案は未信頼コードであるため、Test Runnerは通常のHarness ProcessやProvider Sandboxとは別のDisposable Sandboxで実行する。

必須要件：

* Disposable Container、MicroVM、Sandboxed Job等、破棄可能な実行境界
* Test Image／Runtime Binary HashをApproval前に固定
* Host Secret、Provider Config、Approval Key、Ledger DB、Artifact Store Credentialへアクセス禁止
* Worktreeは必要最小範囲をRead-write Mountし、他WorkspaceをMountしない
* Network=`DENY_ALL`
* Loopbackも原則遮断。必要時はHarnessが用意した明示Mock EndpointだけをAllowlist
* Cloud Metadata Endpoint、Host Gateway、Unix Socket、Named Pipe、Docker Socketを遮断
* DNS無効またはSandbox内固定
* Process Tree全体をcgroup／PID Namespace等で管理
* CPU、Memory、PIDs、Disk、Open File、Output Size、Wall Clock上限
* Privilege Escalation、Setuid、Capability、Device、Kernel Interfaceを禁止
* Sandbox unavailable時はFail-Closed
* Unsandboxed Retry禁止
* Test前後にSecret Scan
* Test終了後にWorktree差分を再取得し、TestがChangeSet外を変更していないことを確認
* Test OutputはUntrustedとして隔離
* Testが外部依存を必要とする場合は別Action・別Approvalとし、MVP1-Aでは禁止

### Test Runner Runtime Spec

`TestRuntimeSpec`へ以下を含め、Change Approvalへ束縛する。

* Image／Executable Hash
* argv
* cwd
* Mount Manifest
* Environment Allowlist
* Network Policy
* Secret Policy
* Resource Limits
* Process Policy
* Output Policy
* Sandbox Implementation／Assurance
* Toolchain／Dependency Lock Hash

## 6.7 Change Approval

Enterprise SoD Gate合格前後を問わず、Workspace Writeの既定は`HUMAN_APPROVAL_ONLY`とする。Policy Approvalを将来解禁する場合でも、Enterprise SoD Gate、Code Ownerの明示委任、固定Repository／Branch Scope、Diff／Risk上限、署名Rule Setを満たす独立ADRと追加受入試験を必要とし、本書の初期実装では解禁しない。

Approval画面／CLIに表示する内容：

* ChangeSet Hash
* Validation Hash
* Base Tree Hash
* 対象Path一覧
* Add／Modify／Delete／Rename件数
* Diff Summary
* Test／Lint結果
* Secret Scan結果
* Binary／Executable変更
* Rollback Snapshot Hash
* Risk Level
* Expiry

Approval後にChangeSet、Validation、Base、Policy、Test Result、Target Workspaceが変わった場合は再承認する。

## 6.8 Commit Protocol

1. Main WorkspaceのBase Tree／対象File Hash／File Identityを再確認。
2. Workspace Leaseと対象Path Resource Lockを取得。
3. Rollback SnapshotをCASへ保存。
4. `OperationJournal`を作成し、全Operationへ一意`effect_id`を割り当てる。
5. `ACTION_PREPARED`、Expected Tree Hash、Before Identity、After Hash、Fencing TokenをTransaction Commitし、`PREPARED_DURABLE`へ。
6. 最終Workspace StorageでFencing Tokenを検証。
7. `EXECUTION_ATTEMPTED`を記録。
8. Ephemeral Worktreeの承認済みBytesをMain WorkspaceへOperation順に適用。
9. 各Operation後に対象File／Parent／Treeを再読込し、Operation Receiptを保存。
10. Git Indexを使う場合はIndex Lockも取得し、Index Effectを別OperationとしてJournal化。
11. 全OperationのObserved HashとExpected Tree Hashを照合。
12. 全ReceiptがDurableであることを確認。
13. Aggregate Effect Receiptを保存。
14. `ACTION_COMMITTED`を記録。
15. Human Release。

`OperationJournal`必須項目：

* Journal ID、Run／Action／Attempt
* Base Tree Hash、Expected Tree Hash
* Operation ID／Effect ID
* Target Resource Identity
* Before Hash、Expected After Hash、Observed Hash
* Temp／Backup Artifact Hash
* Fencing Token
* State=`PREPARED_DURABLE | EXECUTION_ATTEMPTED | EFFECT_OBSERVED | RECEIPT_DURABLE | EFFECT_CONFLICT`
* Durability Level
* Error／Recovery Decision

Git Commit作成は独立Actionとし、初期MVP1-AではWorking Tree反映とCommitを同一視しない。

## 6.9 Rollback

Rollbackは「逆Patch」ではなく、承認前に保存したBefore Artifactから復元する。

* Rollback対象をExact PathとBefore Hashへ束縛。
* 現在HashがExpected After Hashの場合だけ自動Rollback。
* 第三者変更がある場合は`ROLLBACK_CONFLICT`。
* Rollbackも新規Action、Approval、Effect Receiptを持つ。
* Delete復元、Rename復元、Mode復元を試験する。
* Release後のRollbackは運用変更として別Runにする。

## 6.10 状態遷移

```text
PROPOSED
 → NORMALIZED
 → VALIDATING
 → VALIDATED
 → TESTING
 → WAITING_CHANGE_APPROVAL
 → APPROVED
 → APPLYING
 → EFFECT_OBSERVED
 → RELEASED

任意状態
 → REJECTED
 → BLOCKED_CONFLICT
 → ROLLBACK_REQUIRED
 → ROLLED_BACK
```

## 6.11 障害復旧

* Apply前停止：`PREPARED_DURABLE`とBase Identityを再検証し、Effect未実行を証明できる場合のみ継続。
* 一部File適用後停止：Operation Journalと各Target Receiptを照合し、`PARTIAL_EFFECT`へ。自動で残りを適用しない。
* Atomicな単一File操作：Expected Hash一致ならReceipt／Commitを補完。
* `EXECUTION_ATTEMPTED`後にBase Hashのままでも、Effect未実行を証明できない場合は`EFFECT_UNKNOWN`。
* 複数File Transaction：Filesystem原子性を前提にせずOperation Journalで復旧。
* Receiptあり／Ledgerなし：Receiptと実体一致時にCompensating Eventを追加。
* Ledgerあり／Receiptなし：`REPAIR_REQUIRED`、Release禁止。
* Test後・Approval前停止：Validation Hash、Test Runtime Spec、Test Output Hashが同一ならApproval待ちを復元。
* Approval後・Base変更：Approvalを無効化し、再生成またはRebaseへ。
* Rollback中停止：各PathのBefore／After HashとRollback Effect Receiptを再照合。
* Test Runner異常終了：Process Tree終了、Network閉鎖、Secret Scan、Worktree差分確認後にのみ再試行可。

## 6.12 受入Gate

* Provider直接書込み0件
* `AT-SANDBOX-TEST-001`：Test Sandbox unavailable時Commit 0件
* Test RunnerからHost Secret、Provider Config、Ledger、Docker Socket、Metadata Endpointへのアクセス0件
* Test RunnerのLoopback／外部通信0件
* Test終了後の孤児Process 0件
* TestによるChangeSet外変更を検出
* Test前後Secret Scan合格
* Test OutputをTrusted入力として利用0件
* 1 byte変更でApproval再利用不可
* Base Hash不一致で自動上書き0件
* Symlink／Mount／Traversal拒否
* 部分適用を検出
* Test失敗時Commit 0件
* Secret検出時Commit 0件
* RollbackでBefore Hashへ復帰
* Approval済みExact ChangeSet以外の反映0件
* Effect後・Ledger前停止から復旧
* Receipt／Ledger相互失敗から復旧
* Stale Fencing Token Commit 0件


---
