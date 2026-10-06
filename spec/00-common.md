<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

# 0. 本書の目的・適用範囲・正本規則

本書は、統合要求仕様書 v3.2の能力を、単独開発・Python・AI支援実装・Windows 11上のWSL2という実装条件へ落とし込み、設計、実装、Migration、障害復旧、試験、Delivery Gateまでを一貫して定義する。

本書はv1.3とv1.4を統合した全文版であり、実装者、AIコーディングエージェント、試験ハーネス、公開リポジトリの利用者は、本書および本書から定義されるGit管理Registryだけを参照する。v1.3とv1.4は履歴資料として保持できるが、実装上の正本ではない。

## 0.1 確定した実装前提

| 前提 | 確定値 | 設計への影響 |
|---|---|---|
| 開発体制 | **1名** | Maker／Approver／Operatorは同一人物。Enterprise SoDは参照仕様 |
| 実装方式 | **AI支援実装（バイブコーディング）** | ファイル横断不変条件を実行時・CI・Schemaで強制 |
| 実装言語 | **Python 3.11以上**。初期CIは3.11／3.12 | `openat2`、SQLite、JSON Schema、Ed25519のPoCを先行 |
| 実行環境 | **Windows 11 + WSL2のLinux側ファイルシステム** | `/mnt/*`、Windows Native Workspaceへの書込みを拒否 |
| 状態Store | **単一SQLite DB** | Storeは論理分離、物理的には同一DB Transaction境界 |
| Artifact Bytes | Linux側Content-addressed Filesystem | BytesのみDB外。ManifestはSQLite |
| 期限 | なし | 納期よりも垂直スライスとドッグフーディングを優先 |
| 配布 | Git公開／MITライセンス | 未検証状態、脅威モデル、非対応能力をREADMEへ明示 |
| 脅威モデル | 単一UIDでの**改ざん検知** | 同一UID攻撃への改ざん耐性・否認防止は主張しない |

## 0.2 正本と生成物

正本の優先順位は次のとおりとする。

```text
法令・セキュリティ・プライバシー
  > 本書 v1.14
  > Git管理された正規Registry／Schema／Migration
  > 自動生成されたspec shard
  > 実装上の都合
```

編集可能な仕様正本は本書と次のRegistryだけとする。

```text
design-v1.14-runtime-go.md
design-source/
  registries/
    states.yaml
    events.yaml
    errors.yaml
    schemas.yaml
    tests.yaml      # test_id / case_id / 期待値 / phase_scope
    gates.yaml      # gate_id / phase / condition / test_refs
    evidence-areas.yaml  # area / evidence_path / human_measured / phase_scope
```

`spec/*.md`、READMEの検証状況表、Test Manifest表、Gate表、件数表示は上記正本から自動生成する。生成物の直接編集を禁止し、生成物には`source_version`、`source_hash`、`generated_at`を持たせる。

生成物のうち`registry-snapshot.json`はRuntime GO Verifierの入力であり、次の性質を持つ。

* `tools/build_registry_snapshot.py`がRegistry YAMLから生成する。
* 各Registry YAMLのSHA-256、設計書のSHA-256、生成時刻を含む。
* 自己のCanonical Hash`registry_snapshot_hash`を持ち、Verifierが再計算して改変を検出する。
* Release Scopeごとの必要Case集合、必要Gate集合、必要Evidence領域集合、`test_manifest_hash`を含む。
* Evidence領域の定義（Path／`human_measured`／再利用束縛Subtree）は`evidence_areas`が持つ。**定義と要求を分ける。** 定義Catalogを要求として読むと、Scope別化した意味が消えて全領域必須へ戻る。

**件数をコード、Schema `const`、本文の3箇所へ重複記載してはならない。** 件数の唯一の正本はRegistryであり、VerifierとSchemaはRegistry由来値を参照する。v1.6ではVerifierが`86`／`37`／`36`をハードコードし、Schemaが同じ値を`const`で固定し、本文が別途記載していたため、正本が3重化していた。

## 0.3 実装対象と参照仕様

| フェーズ | 区分 | 理由 |
|---|---|---|
| MVP0-A Token-aware Mock Execution | **実装** | 制御ループ、Ledger、Approval、Recoveryの土台 |
| MVP0-B Local Provider Read-only | **実装** | ローカルLLMで最初に実利用可能となる |
| MVP1-A Harness-mediated Workspace Change | **実装** | 本命機能。生成変更を検証後に適用 |
| MVP0-C External Provider Read-only | **実装** | 実用モデル利用 |
| MVP1-D Controlled Paid Execution | **実装** | 自費API利用の上限管理 |
| MVP1-B Verified Session Resume | 参照仕様 | 最適化。初期利用に必須でない |
| MVP1-C Provider Routing／Fallback | 参照仕様 | 単一Provider構成では不要 |
| MVP1-E Approved External Side Effect | 参照仕様 | メール／Slack等の外部送信は初期対象外 |
| MVP2-A Windows／WSL Path Boundary | **参照仕様・不採用** | WSL2 Linux側FS限定により不要 |
| MVP2-B Multi-worker | 参照仕様 | 1人・1台では不要。ただしCAS／Fencing概念はMVP0-Aから使用 |
| Blind LLM Reviewer | 参照仕様 | 有用だが初期必須でない |
| Enterprise Hardening | 参照仕様 | SoD、RBAC、IdP、DRは単独構成では到達不能。ただしPolicy Freshness Matrixは実装 |

実装順序は次で固定する。

```text
MVP0-A → MVP0-B → MVP1-A → MVP0-C → MVP1-D
```

MVP0-A単体は利用価値が限定されるため、計画上はMVP0-AとMVP0-Bを1区間として扱う。

## 0.4 Architecture Decision Records

### ADR-001：全状態Storeを単一SQLite DBへ固定

Event Ledger、Artifact Manifest、InputReadCapability Registry、Operation Journal、Effect Receipt、Budget Reservation、Session Descriptor、Projection等の状態を単一SQLite DBへ格納する。論理Portは分離するが、物理Storeを分離しない。Artifact BytesだけをLinux側CASへ置く。

SQLiteはWAL、`synchronous=FULL`、外部キー有効、`busy_timeout`設定を必須とする。Effect ProtocolのDurable境界は`BEGIN IMMEDIATE`から`COMMIT`までの単一Transactionで実現する。複数DBやPostgreSQLへの先行移行は禁止し、複数ホスト要件が発生した時点でJournal Protocolを新規設計する。

Artifact境界の順序は次で固定する。

```text
Bytesを同一FilesystemのTempへwrite
→ File fsync
→ Atomic Rename／Replace
→ Parent Directory fsync
→ SQLiteへArtifactManifest登録
```

DBにManifestがありBytesがない場合は`BLOCKED_REPAIR_REQUIRED`、Bytesだけがある場合は孤児としてGC候補とする。

### ADR-002：WSL2 Linux側Filesystem限定

Harness CoreとWorkspaceはWSL2 Linux側Filesystemへ配置する。`/mnt/*`、drvfs、9p、Network FS、明示AllowlistのないFUSE／OverlayをWorkspaceにしない。起動時とInputReadCapability発行時に`/proc/self/mountinfo`から最長Prefix一致でMountを特定し、Policy Allowlist外なら`WORKSPACE_ON_FOREIGN_FS_DENIED`で停止する。

Filesystem種別名は推測で固定せず、スパイクで実測した値とMount IDをRuntime Attestationへ記録する。

### ADR-003：Python 3.11以上へ固定

Harness Core、Adapter、CLI、Migration、試験ハーネスをPython 3.11以上で実装する。初期CI Matrixは3.11／3.12とし、それ以降は依存ライブラリとC API相当箇所の検証後に追加する。

`openat2`は`ctypes`によるsyscall直呼びをスパイクで検証する。利用不能時は各Path Componentを`openat`、`O_NOFOLLOW`、`fstat`で検証する安全なDirectory WalkへFail-Closedで切り替える。SQLite authorizerが利用不能でも、DB Trigger、接続Factory、静的検査を併用するため、直ちに言語変更とはしない。

### ADR-004：単一UIDの承認は改ざん検知

ローカル構成はApprovalの二重利用防止、対象Hash不一致検出、履歴改変検出、誤操作防止を提供する。一方、悪意ある同一UID、改変済みHarness、第三者に対する否認防止、AAL2相当本人性は保証しない。

Approval署名鍵は`$XDG_DATA_HOME/harness/keys/approval-signing.key`へ保存し、ownerが実行UID、modeが`0600`でなければ`APPROVAL_KEY_PERMISSION_INVALID`で停止する。複数人運用へ移行する場合はApproval Serviceを別UID／別Processへ分離し、Unix socketのPeer Credentialを検証する。

### ADR-005：Plan決定性は同一ホスト・同一Workspace内

`plan_content_hash`の再現性は同一ホスト、同一Workspace、同一Frozen Inputに限定する。File Identityのdevice／inode／mount IDはホスト間で不変でないため、ホスト跨ぎ一致を主張しない。

Plan Builderは外部状態を直接取得せず、1回だけ凍結した`PlanBuildInput`を受け取る純粋関数とする。同一`PlanBuildInput`から2回Buildし、Hashが異なる場合は`PLAN_NONDETERMINISTIC`で停止する。

### ADR-006：段階的委任を宣言型Predicateで行う

確認のたびに「次回以降どうするか」を決め、徐々に自動化したい運用要求がある。
§3.8.5は「前回と同じ場合の自動承認」を禁止しており、これは§0.1（開発体制1名）と
ADR-004に由来する意図的な判断である。

本ADRは禁止条項を撤廃せず、**判定根拠を置き換える**。

| | 引き続き禁止 | 本ADRが導入するもの |
|---|---|---|
| 判定根拠 | 過去の実行履歴との一致 | 人間が事前に承認した**宣言型Predicate**と、**いま解決されたPlan**の照合 |

`DelegationGrant`はFlagではなく、人間が承認して発行する署名付きArtifactである。
作成そのものが独立した承認Decisionであり、委任範囲を見ずに成立する経路を作らない。
成立時はその実行専用の`DerivedApprovalGrant`を毎回新規発行し、`DelegationGrant`を
実行権限として使い回さない。消費は§1.10のCAS経路をそのまま通る。

`--yes`、`--auto-approve`、`--force`、`--skip-approval`の禁止は維持する。

Predicateは§3.8.2の無効化項目を全て照合対象へ含める。任意コード、正規表現、Wildcard、
OR結合、否定条件を禁止した宣言型Schemaとし、Path指定は完全一致またはDirectory prefixの
明示列挙だけとする。

非委任床は`design-source/registries/delegation-floor.yaml`を正本とし、Resolverは本Registryを
読んで評価する。条件をコードへ直書きしない。**委任による委任の作成・拡大を禁止する**規則を
含み、これが無いと委任範囲が無限に拡大する。

失効とEffect開始の競合は、不変条件#3（Fencing Tokenの最終Storage書込み直前の再検証）と
同じ扱いとする。外部FSのAtomic ReplaceはSQLite Transactionへ含められないため、
`BEGIN IMMEDIATE`とCAS更新をDB内の**Effect線形化点**とし、Commit後にのみ外部Effectを開始する。
線形化点より後の失効はEffectを止められないため、`DELEGATION_REVOKED_AFTER_EFFECT_START`として
監査記録しReconciliation対象とする。「失効競合時は常にEffect 0件」とは主張しない。

**単独開発においてDelegationGrantは安全統制ではない。** Maker／Approver／Operatorが同一人物で
ある以上、委任は独立した第三者統制を提供しない。提供するのは確認操作の削減、自動承認された
実行の完全な監査証跡、委任範囲の明示化と即時失効の3点だけである（§22）。

### ADR-007：マスキングはLLMにSpanだけを提案させ、置換は決定論Rewriterが行う

個人情報・機密情報のマスキングにLLMを用いる。ただしLLMに本文を生成させると、
欠落・重複・並べ替え・意味改変を決定論的に検出できない。断片の順序と部分文字列一致を
検査する方式では、断片を原文の短い部分文字列へ縮める改変が検出できない。

したがってLLMには座標（Span）だけを返させる。

```text
Bytes読込
  → UCD 14.0割当済み符号位置Guard → 標準 unicodedata.normalize('NFC')
  → Deterministic Scan #1（権限を持つのはここと#2だけ）
       REJECT   : 保存・候補化を拒否。LLMへ渡さない
       CLEAN    : マスク不要
       MASKABLE : 決定論候補Spanを生成してLLMへ
  → LLMは {start, end, category} だけを返す
  → Span検証（Schema厳格・範囲・昇順・非重複・非入れ子・カテゴリ・数・比率）
  → 決定論RewriterがMask Tokenへ置換（LLMの返した文字列は使わない）
  → Deterministic Scan #2 が実質のGate
```

LLMが影響できるのは「どこを隠すか」だけであり、「何が書かれるか」には影響できない。
Prompt Injectionが成功しても返せるのはSpanだけであり、過少マスクはScan #2が検出する。

Secret、Password、API Key、Bearer Token、Session Cookie、Private Key、Cloud Credential、
`SPECIAL_CATEGORY_DATA`、`NATIONAL_ID`は**マスクせず即Reject**しLLMへ渡さない。
マスクして使う利益より、マスクのためにLLMへ渡す危険が大きい。境界は
`design-source/registries/masking-policy.yaml`を正本とする。

Python 3.11の`unicodedata`はUCD 14.0.0、3.12は15.0.0であり、15.0で追加された符号位置を
含む文字列はNFC結果が処理系間で食い違い得る。`source_normalized_hash`が処理系依存になると
ADR-006のPredicate照合まで処理系依存になる。Unicode Normalization Stability Policyは
「あるVersionで割当済みの文字だけから成る文字列の正規化形は以降のVersionでも変化しない」と
保証するため、**入力をUCD 14.0割当済み符号位置へ限定したうえで標準NFCを使う**。
NFCアルゴリズムを自前実装しない。割当済み集合は139,264 byteの固定Bitmap Artifactとして
同梱し、公式UCD入力Hashとともに由来を記録する。Artifact欠落・Hash不一致・未割当符号位置は
正規化前にFail-Closedとする。

Maskerはローカル実行のみとし、Network egress拒否、Telemetry無効、Prompt Log無効、
Core Dump無効、一時File禁止を起動前に検証する。Swapとメモリダンプへの対策は完全ではなく、
単一UIDでは`/proc/<pid>/mem`の読出しを防げない（§22）。

## 0.5 非機能目標

| 分類 | 初期目標 |
|---|---|
| 正確性 | Approval対象と実行内容のHash一致率100% |
| 重複防止 | 同一Effectの重複確定0件 |
| 境界安全性 | Workspace Escape、Allowlist外通信、定義済みSecret Canary漏えい0件 |
| 復旧性 | 全Fault Pointと定義済みI/O Faultから手動DB修復なしで復旧または安全停止 |
| 再現性 | 同一ホスト・同一Workspace・同一Frozen Inputから同一`plan_content_hash` |
| 監査性 | Run、Action、Attempt、Approval、Effect、ReleaseをCorrelation可能 |
| Token統制 | Provider上限超過呼出0件 |
| 費用統制 | Budget未確定または上限超過状態での新規有償Action 0件 |
| 可用性 | MVPでは可用性より安全停止を優先 |
| 保守性 | Domain層がProvider SDK、CLI、DB、OS実装へ直接依存しない |
| 性能 | 8 Action／60 Eventの制御処理でp50≤5秒、p95≤8秒。Provider時間除外 |
| 証拠性 | 実行していない試験をPASSとせず、証跡なしは`UNVERIFIED` |

---
## 0.6 Runtime GOのEvidence整合性境界

Runtime GOは設計書の文言、セルフレビュー、生成済みコード、Test件数の記載だけでは成立しない。**Runtime GOはRelease Scope単位で宣言する。** 次を全て満たし、リポジトリrootの`verify_runtime_go.py`が`--release-scope <PHASE>`付きで終了Code 0を返した場合に限り、当該PhaseのRuntime GOを宣言できる。

1. 判定対象のGit Commit SHAとDirtyでないSource Treeが固定されている。
2. WSL2 Linux側Filesystem上でRuntime Environment Manifestを取得し、Evidenceとして実Fileが存在する。
3. Registry Snapshotの自己Hashが再計算値と一致し、その`design_sha256`が実際の設計書Bytesと一致する。
4. 当該ScopeのEvidence領域が**全て**存在し`PASS`である。領域集合はRegistry由来のScope別値であり、Manifest側で削減できない。Scope外の領域をManifestへ足すこともできない（§26.2）。
5. 当該ScopeのGateが全て`PASS`であり、集合がRegistry導出値と過不足なく一致する。
6. 当該ScopeのCaseが全て`PASS`であり、集合がRegistry導出値と過不足なく一致する。Skip、XFail、未実行が0件である。
7. 全Case・全Gate・全領域に実Evidence Fileが存在し、Bytes Hashが一致し、**内容がRegistryの期待値と一致する**。Evidence FileのJSONは`observed_state`、`observed_error_code`、`observed_event_sequence`、`assertions`を持ち、Registryの期待値と突合される。
8. 同一Evidence Fileを複数のCase、Gate、領域へ流用していない。
9. `test_manifest_hash`がRegistry導出のScope別値と一致する。
10. Runtime GO Release ManifestがVerifierによって生成され、そのSHA-256をRelease Tagへ記録している。Release Manifestは判定に使用したVerifier自身のSHA-256、Registry Snapshot Hash、Manifest SHA-256を含む。

Evidenceが不足する場合の唯一の正しい判定は`BLOCKED_EVIDENCE_MISSING`または`UNVERIFIED`である。人間またはAIが手動で`PASS`へ書き換えることを禁止する。

この境界が保証するのは、実Evidence Bytes、Fixture、Raw Result、Runner Source、Commit、Environment、Schema Set、Migration Headの**相互束縛と整合性**である。単一UIDの攻撃者がEvidence ProducerとVerifierの両方を改変することへの改ざん耐性や否認防止は保証しない。それらが必要な場合は、Evidence Producerと署名鍵を別UIDまたは外部CI／Attestation Serviceへ分離する。

### 0.6.1 v1.6 Verifierで成立していた偽装（是正済み）

非偽装境界は主張ではなく試験で示す。v1.6のVerifier v1.0では次が成立していた。

| 偽装手口 | v1.0の挙動 | v1.1の対策 | 検出試験 |
|---|---|---|---|
| `required_evidence_areas`を空配列にする | 全領域要件が消滅しGO | 必須集合をRegistryから導出し過不足を検査 | `AT-VERIFIER-002` |
| Scopeの必要領域を空にしてGOを取る | （v1.12で新設したScope別集合への攻撃） | Scope別必要集合が空のSnapshotは`INPUT_INVALID`で拒否 | `AT-VERIFIER-002` |
| Scope外の領域Evidenceを足して通す | （同上） | Scope外領域は`out of release scope`でFAIL | `AT-VERIFIER-002` |
| 同一ダミーFileを全Case／Gateへ流用 | Hash一致のためGO | Evidence Hashの一意性を検査 | `AT-VERIFIER-003` |
| Gate Evidenceへ絶対Pathを指定しroot外を参照 | escape検査がCaseにしか無くGO | cases／gates／areasで検査を統一 | `AT-VERIFIER-004` |
| Evidence Fileの中身を`{}`にする | Hashしか見ずGO | JSONをパースし期待値と突合 | `AT-VERIFIER-009` |
| `python_version`に`2.7.0`を書く | 真偽判定のみでGO | 3.11以上を実際に比較（ADR-003） | `AT-VERIFIER-008` |
| Test Manifestを丸ごと差し替える | `test_manifest_hash`が形式検査のみでGO | Registry導出値と突合 | `AT-VERIFIER-012` |

`AT-VERIFIER-*`は`tests/test_verify_runtime_go.py`に実装し、`verifier_self_test`をEvidence必須領域とする。**GO判定器自身が無検証であってはならない。**

# 1. 共通アーキテクチャ

## 1.1 論理構成

```text
Operator / Automation Client
        |
        v
CLI / Control API
        |
        v
Application Orchestrator
  ├─ Task Loader
  ├─ Context Budget Manager
  ├─ Execution Planner
  ├─ Policy Engine
  ├─ Approval Manager
  ├─ Action Scheduler
  ├─ Recovery Engine
  └─ Release Gate
        |
        +-------------------------------+
        |                               |
        v                               v
Domain Services                    Infrastructure Ports
  ├─ State Machines                  ├─ Event Ledger
  ├─ Invariants                      ├─ Artifact Store
  ├─ Canonicalizer                   ├─ Lease / Lock Store
  ├─ Error Taxonomy                  ├─ Runtime Launcher
  └─ Decision Models                 ├─ Provider Adapters
                                     ├─ Workspace Broker
                                     ├─ Outbox / Dispatch
                                     └─ Audit / Metrics
```

## 1.2 Trust Zone

| Zone | 内容 | 信頼レベル |
|---|---|---|
| Z0 Control Plane | Planner、Policy、Approval、Ledger、Release | Trusted Computing Base |
| Z1 Local Execution | Sandbox、Mock、Local Provider、Worktree | 制限付き信頼 |
| Z2 External Provider | Cloud AI、外部API | Untrusted Remote |
| Z3 External Effect | Email、GitHub、Slack、Webhook等 | 副作用を持つUntrusted Remote |
| Z4 Operator Surface | CLI、Control API、承認UI | 認証済みだが誤操作を想定 |
| Z5 Artifact Input | Repository、添付、Provider出力 | Untrusted Data |

Z1からZ0への入力、Z2/Z3からZ0への入力、Z5から実行可能なActionへの変換では必ずSchema検証とPolicy Decisionを通す。

## 1.3 共通識別子

すべての記録で次を使用する。

* `run_id`
* `action_id`
* `attempt_id`
* `record_id`
* `correlation_id`
* `causation_id`
* `sequence_number`
* `schema_name`
* `schema_version`
* `content_hash`
* `created_at`
* `producer`

時刻は表示・TTL・監査に使用し、状態順序は`sequence_number`で判定する。

## 1.4 共通Action実行契約

### 1.4.1 規範イベント順序

Event名は過去形、State名は現在状態として区別する。すべてのActionは、実行内容をApproval前に完全解決し、以下の順序を満たす。

```text
INTENT_CREATED
  → CAPABILITY_SNAPSHOT_CAPTURED
  → RUNTIME_SPEC_RESOLVED
  → INVOCATION_MANIFEST_RESOLVED
  → PLAN_RESOLVED
  → POLICY_DECIDED
  → APPROVAL_ISSUED（必要な場合）
  → APPROVAL_CONSUMED（必要な場合）
  → ACTION_CLAIMED
  → LEASE_ACQUIRED
  → RUNTIME_ATTESTED
  → ACTION_STARTED
  → ACTION_PREPARED（Effectがある場合）
  → EXECUTION_ATTEMPTED（Effectがある場合）
  → EFFECT_OBSERVED（Effectがある場合）
  → EFFECT_RECEIPT_STORED（Effectがある場合）
  → ACTION_COMMITTED / ACTION_FAILED / ACTION_BLOCKED
  → EVALUATION_COMPLETED
  → RELEASE_DECIDED
```

`RuntimeEnvelopeSpec`と`InvocationManifest`は`ExecutionPlan`のHash対象であり、Approval後に生成・変更してはならない。`RuntimeAttestation`は起動直前に取得し、Specとの完全一致を検証する。不一致時はプロセス起動前に`RUNTIME_SPEC_MISMATCH`を記録し、Attemptを`BLOCKED_POLICY`へ遷移する。

### 1.4.2 EventからStateへの正本遷移

| Event | ActionAttempt State | 備考 |
|---|---|---|
| `INTENT_CREATED` | `PLANNING` | Intent Schema検証済み |
| `PLAN_RESOLVED` | `WAITING_POLICY` | `plan_content_hash`と`execution_plan_hash`固定済み |
| `PLAN_NONDETERMINISTIC` | `BLOCKED_CONFLICT` | 同一意味入力から異なるContent Hash。実行禁止 |
| `POLICY_DECIDED(ALLOW)` | `WAITING_APPROVAL`または`READY` | Approval要否による |
| `POLICY_DECIDED(DENY)` | `BLOCKED_POLICY` | 終端 |
| `APPROVAL_CONSUMED` | `READY` | CAS成功時のみ |
| `ACTION_CLAIMED` | `CLAIMED` | WorkerとAttemptを一意束縛 |
| `LEASE_ACQUIRED` | `LEASED` | Fencing Token発行 |
| `RUNTIME_ATTESTED` | `RUNTIME_VERIFIED` | Spec一致時のみ |
| `ACTION_STARTED` | `RUNNING` | Read-only実行開始 |
| `ACTION_PREPARED` | `PREPARED_DURABLE` | Effect Journal永続化済み |
| `EXECUTION_ATTEMPTED` | `EFFECT_IN_FLIGHT` | 副作用開始を記録 |
| `EFFECT_OBSERVED` | `EFFECT_VERIFIED` | 実体再照合済み |
| `EFFECT_RECEIPT_STORED` | `RECEIPT_DURABLE` | Receipt一意制約済み |
| `ACTION_COMMITTED` | `SUCCEEDED` | 終端 |
| `ACTION_FAILED` | `FAILED_RETRYABLE`または`FAILED_PERMANENT` | Error分類による |
| `ACTION_BLOCKED` | `BLOCKED_*` | 理由Codeを必須化 |
| `CANCEL_CONFIRMED` | `CANCELLED` | 終端 |
| `CANCEL_UNKNOWN` | `CANCEL_UNKNOWN` | 自動再実行禁止 |
| `EFFECT_UNKNOWN` | `EFFECT_UNKNOWN` | 自動再実行禁止 |
| `REMOTE_INVOCATION_UNCERTAIN`／`OUTBOX_STATUS_UNKNOWN`／`BUDGET_STATUS_UNKNOWN` | `EFFECT_UNKNOWN` | §1.14.1の正規写像を適用 |

本表をEvent／State遷移の正本とし、各フェーズの図は本表の部分集合または追加Substateとして扱う。`POLICY_EVALUATED`、`ACTION_SUCCEEDED`、`RELEASE_EVALUATED`、`CANCELLED_CONFIRMED`は非正規名として新規利用を禁止する。

### 1.4.3 ActionAttemptの終端状態

* `SUCCEEDED`
* `FAILED_RETRYABLE`
* `FAILED_PERMANENT`
* `BLOCKED_POLICY`
* `BLOCKED_APPROVAL`
* `BLOCKED_CONFLICT`
* `CANCELLED`
* `CANCEL_UNKNOWN`
* `EFFECT_UNKNOWN`

終端状態から同じAttemptを再開しない。再試行・Fallback・Recovery再実行は新しい`attempt_id`を発行する。

### 1.4.4 共通Effect Protocol

副作用を伴うActionは、次の永続化順序を必須とする。

```text
PREPARED_DURABLE
  → EXECUTION_ATTEMPTED
  → EFFECT_OBSERVED
  → RECEIPT_DURABLE
  → ACTION_COMMITTED
```

必須不変条件：

* `ACTION_PREPARED`はLedger Transaction Commit、Journal flush、必要な耐久性レベルのfsync完了後にのみ`PREPARED_DURABLE`とする。
* `effect_id`は全体一意で、`UNIQUE(effect_id)`をStorage側に設ける。
* Receiptは`effect_id`、`attempt_id`、Target Resource Identity、Before／Expected After Hash、Observed Hash、Fencing Token、Observation Methodへ束縛する。
* Fencing TokenはApplicationだけでなく、Workspace Broker、Outbox Store、Budget Store等の最終Effect実行点で比較する。
* Receipt StoreとLedgerは単一DB Transaction、またはOutbox／Journalによる回復可能な二相Protocolで接続する。
* Receipt保存またはLedger Appendの一方だけが成功した場合、Recoveryは実体とJournalを照合して補完し、推測で成功にしない。
* 多File変更は`OperationJournal`にOperation単位の状態、File Identity、Before／After Hash、Durability Levelを保存する。
* 状態判定不能は`EFFECT_UNKNOWN`とし、自動再実行、Fallback、Releaseを禁止する。

## 1.5 共通Run状態

```text
CREATED
  → PLANNING
  → WAITING_APPROVAL
  → READY
  → RUNNING
  → RECOVERING
  → WAITING_RELEASE
  → COMPLETED

非終端状態
  → BLOCKED_REPAIR_REQUIRED
  → CANCELLING
  → CANCELLED
  → FAILED

非終端状態
  → BLOCKED
```

`BLOCKED_REPAIR_REQUIRED`は非終端状態であり、`ended_at`を設定しない。Operatorが`harness repair <run-id>`を明示実行し、Recovery Engineが実体照合に成功した場合だけ`RECOVERING`へ遷移する。自動で`COMPLETED`、`FAILED`、`CANCELLED`へ落としてはならない。

終端状態から元Runを再開しない。終端後に不整合が発見された場合は、元Runを改変せず、新しいRepair Runを作成して`related_run_id`へ束縛する。

### Run不変条件

* `COMPLETED`には`ReleaseDecision=RELEASE`が必要。
* `EFFECT_UNKNOWN`、`CANCEL_UNKNOWN`、未解決Manual Reconciliation、欠損Artifact、未解決Ledger／Store不整合を持つRunは、`COMPLETED`、`FAILED`、`CANCELLED`のいずれへも遷移しない。遷移先は`BLOCKED_REPAIR_REQUIRED`だけとする。
* 未解決ActionがあるRunは`WAITING_RELEASE`へ遷移しない。
* Approval対象Hashが変化した場合、Runは`WAITING_APPROVAL`へ戻る。
* Projectionが失われてもLedgerから再構築できる。
* `BLOCKED`は終端、`BLOCKED_REPAIR_REQUIRED`は非終端としてSchema、Projection、CLI表示を分離する。
* 照合不能Effectを別Runへ引き渡す場合、`unreconciled_effect_carried_to_run_id`を必須とし、引継ぎ深度は1、循環参照は禁止する。
* 終端への遷移はApplication Serviceの単一Transition APIだけを通し、Store層やCLIから直接更新しない。

## 1.6 共通Port契約

Domain／Application層は以下の抽象Portだけへ依存する。

| Port | Operation | 入力 | 出力 |
|---|---|---|---|
| EventLedgerPort | `append` | Events、`expected_stream_sequence` | Append Result |
| EventLedgerPort | `load_stream` | Stream ID、After Sequence | Domain Events |
| EventLedgerPort | `verify_chain` | Stream ID | Chain Verification Result |
| ArtifactStorePort | `put` | Bytes、Artifact Metadata | Artifact Manifest |
| ArtifactStorePort | `get` | Content Hash | Bytes |
| ArtifactStorePort | `verify` | Content Hash | Verification Result |
| InputReadCapabilityPort | `issue` | Workspace Identity、Allowed Scope、Policy | Signed InputReadCapability |
| SafeInputReaderPort | `open_read` | Capability、Relative Path | Verified File Handle／Read Evidence |
| SafeInputReaderPort | `enumerate` | Capability、Relative Directory | Verified Candidate Entries |
| LeasePort | `acquire` | Resource Key、Holder ID、TTL | Lease |
| LeasePort | `renew` | Lease ID、Fencing Token | Renewed Lease |
| LeasePort | `release` | Lease ID、Fencing Token | Release Result |
| RuntimeLauncherPort | `launch` | Runtime Envelope | Runtime Handle |
| RuntimeLauncherPort | `request_cancel` | Runtime Handle | Cancel Result |
| RuntimeLauncherPort | `inspect` | Runtime Handle | Runtime Status |
| ClockPort | `now` | — | Aware UTC datetime |
| IdGeneratorPort | `new_uuid7` | Namespace | UUIDv7 |
| FaultInjectorPort | `maybe_fault` | Fault Point | None／Injected Fault |
| UnitOfWorkPort | `begin_immediate` | — | Transaction Scope |

Port実装規則：

* Domain層は`sqlite3`、`os`、`subprocess`、Provider SDKをimportしない。
* Repositoryは`commit()`しない。Transaction所有者はApplication層のUnit of Workとする。
* DB接続は単一`ConnectionFactory`からだけ生成し、アプリケーションコードの`sqlite3.connect()`直接使用を静的検査で拒否する。
* 時刻、UUID、乱数、Fault InjectionはPort経由で注入し、Plan Content生成中に直接参照しない。

## 1.7 共通エラー分類・Retry規則

| Classification | 自動再試行 | 条件 | 上限 | 待機 |
|---|---:|---|---:|---|
| `VALIDATION_ERROR` | しない | — | — | — |
| `POLICY_DENIED` | しない | — | — | — |
| `APPROVAL_REQUIRED` | しない | — | — | — |
| `CONFLICT` | しない | — | — | — |
| `AUTHENTICATION_ERROR` | しない | — | — | — |
| `ENTITLEMENT_ERROR` | しない | — | — | — |
| `RATE_LIMITED` | 条件付き | `Retry-After`取得済み、Read-only、Budget内 | 3 | `Retry-After` |
| `TRANSIENT_PROVIDER_ERROR` | 条件付き | Read-onlyかつ冪等 | 3 | 指数バックオフ＋ジッター |
| `TIMEOUT` | 条件付き | Remote照合でEffect未実行が証明された場合だけ | 3 | 指数バックオフ＋ジッター |
| `CANCEL_UNKNOWN` | しない | — | — | — |
| `EFFECT_UNKNOWN` | しない | — | — | — |
| `INTERNAL_ERROR` | 原則しない | — | — | — |

非冪等Actionは自動再試行しない。Idempotency KeyがRemote側で有効であり、状態照合により未実行が証明できた場合だけ、Operator承認付きの新Attemptとして再開できる。`max_attempts`と`retry_count`を別フィールドにし、「初回実行1回」と「再試行0回」を混同しない。

### 1.7.1 Error Code Registry

`expected_error_code`、Ledger Payload、Runbook TriggerはGit管理された`design-source/registries/errors.yaml`の正規Codeだけを使用する。ClassificationとCodeを混同しない。

下表は`errors.yaml`の写しである。乖離した場合は`errors.yaml`を正とし、Spec Lintが両者のCode集合とClassificationの完全一致を検査する（`ERROR_CODE_IN_DESIGN_NOT_IN_REGISTRY`／`ERROR_CODE_IN_REGISTRY_NOT_IN_DESIGN`／`ERROR_CODE_CLASSIFICATION_MISMATCH`）。v1.8時点で本表はADR-006（委任）とADR-007（マスキング）が追加したCodeを取り込んでおらず、「正本を名乗るものが2つあり突き合わせが無い」状態だった。

| Classification | Error Code |
|---|---|
| `VALIDATION_ERROR` | `HASH_PATTERN_INVALID`, `FIXTURE_HASH_FORMAT_INVALID`, `HASH_COLUMN_MISSING`, `AMBIGUOUS_EXPECTED_STATE`, `EXPECTED_STATE_SUBJECT_MISMATCH`, `SCHEMA_CONDITIONAL_VIOLATION`, `EVENT_ORDER_VIOLATION`, `JOURNAL_REFERENCE_MISSING`, `PHASE_LEDGER_EVENT_MISSING`, `RUNTIME_SPEC_MISMATCH`, `PLAN_NONDETERMINISTIC`, `TOKEN_PROFILE_DRIFT_DETECTED`, `DELEGATION_PREDICATE_AMBIGUOUS`, `MASKING_SPAN_INVALID`, `MASKING_CATEGORY_UNKNOWN`, `MASKER_OUTPUT_MALFORMED`, `MASKING_GRAPHEME_SPLIT`, `MASKING_NORMALIZATION_PROFILE_MISMATCH`, `MASKING_NORMALIZATION_ARTIFACT_MISSING`, `MASKING_UNSUPPORTED_CODEPOINT`, `MASKING_SPAN_CONFLICT`, `CONTEXT_BUDGET_EXCEEDED` |
| `POLICY_DENIED` | `PATH_OUTSIDE_CAPABILITY`, `SYMLINK_DENIED`, `MOUNT_CROSSING_DENIED`, `SPECIAL_FILE_DENIED`, `WORKSPACE_ON_FOREIGN_FS_DENIED`, `CONTROL_DATA_ROLE_ESCALATION`, `BLINDNESS_CONTEXT_LEAK`, `SANDBOX_UNAVAILABLE`, `POLICY_APPROVAL_BEFORE_SOD`, `POLICY_STALE_NEW_ACTION_BLOCKED`, `POLICY_STALE_EFFECT_BLOCKED`, `POLICY_STALE_PAID_BLOCKED`, `POLICY_STALE_EXTERNAL_EFFECT_BLOCKED`, `POLICY_STALE_RECOVERY_ONLY`, `APPROVAL_KEY_PERMISSION_INVALID`, `FAULT_INJECTION_NOT_PERMITTED`, `EMERGENCY_OPERATION_NOT_ALLOWED`, `EMERGENCY_PROFILE_SIGNATURE_INVALID`, `DEPLOY_DRAIN_REQUIRED`, `DELEGATION_SCOPE_EXCEEDED`, `DELEGATION_SELF_MODIFICATION_DENIED`, `DELEGATION_NOT_DELEGABLE_ACTION`, `MASKING_VERIFICATION_FAILED`, `MASKING_RATIO_EXCEEDED`, `MASKER_UNAVAILABLE`, `MASKER_ISOLATION_INCOMPLETE` |
| `APPROVAL_REQUIRED` | `APPROVAL_REQUIRED`, `APPROVAL_INVALIDATED`, `APPROVAL_REPLAY`, `CLOCK_SKEW_EXCEEDED`, `RELEASE_DECISION_REQUIRED`, `DELEGATION_EXPIRED`, `DELEGATION_REVOKED` |
| `CONFLICT` | `STALE_FENCING_TOKEN`, `UNRECONCILED_EFFECT_PRESENT`, `LEDGER_CHAIN_TAMPERED`, `ARTIFACT_CONTENT_CONFLICT`, `DELEGATION_REVOKED_MID_FLIGHT`, `DELEGATION_REVOKED_AFTER_EFFECT_START` |
| `AUTHENTICATION_ERROR` | `APPROVAL_ISSUER_UNTRUSTED`, `APPROVAL_SUBJECT_MISMATCH`, `DELEGATION_SIGNATURE_INVALID`, `DELEGATION_TRUST_ANCHOR_INVALID`, `DELEGATION_SUBJECT_MISMATCH` |
| `ENTITLEMENT_ERROR` | `ENTITLEMENT_SNAPSHOT_INVALID` |
| `EFFECT_UNKNOWN` | `EFFECT_UNKNOWN`, `REMOTE_INVOCATION_UNCERTAIN`, `REMOTE_STATUS_UNKNOWN`, `BUDGET_STATUS_UNKNOWN` |
| `INTERNAL_ERROR` | `REMOTE_REGISTRY_NOT_DURABLE`, `REMOTE_DISPATCH_STATE_NOT_DURABLE`, `STORAGE_WRITE_FAILED`, `MIGRATION_FAILED`, `BACKUP_RESTORE_FAILED` |

`CONTEXT_BUDGET_EXCEEDED`は§3.6 手順8で、必須FragmentがToken Budgetへ収まらない場合に使用する。

* Classificationは`VALIDATION_ERROR`。
* §1.7の分類別再試行表に従い**自動再試行しない**。Fragment集合、Budget、Token Profileの
  いずれも変わらない限り結果は同一であり、再試行は同じ停止を繰り返すだけである。
* 終端Stateは§1.4.2の写像に従い`ACTION_FAILED` → `FAILED_PERMANENT`とする。
  Policy Engineの拒否ではないため`BLOCKED_POLICY`を使わない。再開は§1.4.3のとおり
  新しい`attempt_id`で行い、同一Attemptを再開しない。
* 本Codeは`RUNTIME_SPEC_MISMATCH`から分離するために設ける。分離しないと、Runtime Spec不一致・
  Token Profile使用不可・予算超過・Mandatory重複除去違反が同一Codeへ集約され、Alertから
  原因を切り分けられない。

未知Code、Classification不一致、廃止CodeはSchema検証で拒否する。Code追加はMinor、意味変更・Classification変更はMajorとし、Test Manifest、Runbook、State Mappingを同時更新する。

## 1.8 共通監査イベント

Event Typeの正本は以下とし、Payload Schema Versionを必須とする。

* `RUN_CREATED`
* `INPUT_READ_CAPABILITY_ISSUED`
* `INPUT_READ_STARTED`
* `INPUT_READ_DENIED`
* `INPUT_ARTIFACT_CLASSIFIED`
* `INTENT_CREATED`
* `CAPABILITY_SNAPSHOT_CAPTURED`
* `RUNTIME_SPEC_RESOLVED`
* `INVOCATION_MANIFEST_RESOLVED`
* `PLAN_RESOLVED`
* `PLAN_NONDETERMINISTIC`
* `POLICY_DECIDED`
* `APPROVAL_ISSUED`
* `APPROVAL_CONSUMED`
* `APPROVAL_REPLAY_DENIED`
* `ACTION_CLAIMED`
* `LEASE_ACQUIRED`
* `RUNTIME_ATTESTED`
* `RUNTIME_SPEC_MISMATCH`
* `ACTION_STARTED`
* `REMOTE_INVOCATION_PREPARED`
* `REMOTE_REQUEST_DISPATCHING`
* `REMOTE_ID_RECORDED`
* `REMOTE_INVOCATION_RUNNING`
* `REMOTE_INVOCATION_COMPLETED`
* `REMOTE_INVOCATION_RECONCILED`
* `REMOTE_INVOCATION_UNCERTAIN`
* `OUTBOX_PREPARED`
* `OUTBOX_DISPATCHING`
* `OUTBOX_ACCEPTED`
* `OUTBOX_SENT`
* `OUTBOX_EFFECT_CONFIRMED`
* `OUTBOX_RECONCILED`
* `OUTBOX_STATUS_UNKNOWN`
* `MANUAL_RECONCILIATION_ENQUEUED`
* `BUDGET_RESERVATION_PREPARED`
* `BUDGET_RESERVED`
* `BUDGET_CONSUMING`
* `BUDGET_RECONCILIATION_PENDING`
* `BUDGET_SETTLED`
* `BUDGET_RELEASED`
* `BUDGET_RESERVATION_EXPIRED`
* `BUDGET_STATUS_UNKNOWN`
* `POLICY_STALE_DETECTED`
* `POLICY_STALE_ACTION_BLOCKED`
* `POLICY_STALE_RECOVERY_ONLY`
* `EFFECT_CONFLICT_DETECTED`
* `ACTION_PREPARED`
* `EXECUTION_ATTEMPTED`
* `EFFECT_OBSERVED`
* `EFFECT_RECEIPT_STORED`
* `ACTION_COMMITTED`
* `ACTION_FAILED`
* `ACTION_BLOCKED`
* `CANCEL_REQUESTED`
* `CANCEL_CONFIRMED`
* `CANCEL_UNKNOWN`
* `EFFECT_UNKNOWN`
* `FENCING_REJECTED`
* `RECOVERY_STARTED`
* `RECOVERY_DECIDED`
* `EVALUATION_COMPLETED`
* `RELEASE_DECIDED`
* `ARTIFACT_PAYLOAD_DELETED`

Secret、Session Reference、認証Token、Provider生レスポンス本文、PIIをイベント本文へ直接記録しない。必要な本文はArtifact Storeへ暗号化保存し、LedgerにはHashと分類だけを保持する。

## 1.9 共通メトリクス

* Run成功率、失敗率、Blocked率
* Action成功率、Error分類別件数
* P50／P95／P99 Action時間
* Context選択Token、除外Token、圧縮Token
* Approval待ち時間
* Recovery件数、Effect Conflict件数
* Provider別成功率、Latency、Usage、Cost
* Workspace Escape拒否件数
* DLP／PII拒否件数
* Ledger Chain検証結果
* Stale Lease／Fencing拒否件数
* `plan_runtime_mismatch`
* `approval_replay_denied`
* `sandbox_unavailable_blocked`
* `provider_config_drift`
* `effect_unknown`
* `receipt_missing`
* `fencing_reject`
* `orphan_budget_reservation`
* `policy_stale`
* `path_identity_conflict`
* `outbox_duplicate_prevented`
* `remote_registry_missing_before_dispatch`
* `unsafe_input_read_denied`
* `schema_conditional_violation`
* `event_order_violation`
* `policy_approval_before_sod`
* `policy_stale_effect_blocked`
* `runtime_hash_field_mismatch`


### 1.9.1 Metric Owner／Alert／Runbook対応

以下の統制メトリクスは、単なる計測名ではなくOwner、Alert条件、一次対応Runbookへ束縛する。Threshold変更はPolicy／SLO変更としてレビューする。

| Metric | Owner | Alert条件 | Severity | Runbook |
|---|---|---|---|---|
| `remote_registry_missing_before_dispatch` | External Runtime Owner | 1件以上／即時 | Critical | Remote Registry未永続化 |
| `unsafe_input_read_denied` | Platform Security Owner | 拒否自体はInfo。未知Reasonまたは同一Runで5件超はAlert | Warning | Context Read境界違反 |
| `schema_conditional_violation` | Schema／Domain Owner | Production／Gate環境で1件以上 | Critical | Schema Conditional失敗 |
| `event_order_violation` | Ledger Owner | 1件以上／即時 | Critical | Event順序・Projection不整合 |
| `policy_approval_before_sod` | Security Governance Owner | 1件以上／即時 | Critical | Policy Approval誤解禁 |
| `policy_stale_effect_blocked` | Policy Operations Owner | Block発生時Warning。既送信検出時Critical | Warning／Critical | Policy期限切れ |
| `runtime_hash_field_mismatch` | Runtime Security Owner | 1件以上／即時 | Critical | Runtime Spec／Attestation不一致 |
| `plan_nondeterministic` | Planner／Domain Owner | 同一Content入力で1件以上 | High | Plan再現性不一致 |
| `phase_event_mapping_violation` | Ledger／Effect Owner | Store StateとLedger写像不一致1件以上 | Critical | Phase State／Ledger写像不整合 |
| `schema_complete_failure` | Schema／Release Owner | Core SchemaのValid／Invalid Suite失敗（件数は`registries/schemas.yaml`） | High | Schema Suite失敗 |
| `manifest_subject_mismatch` | Test Platform Owner | Manifest検証で1件以上 | High | Test Manifest対象型不一致 |

Critical Alertでは該当Feature Flagを自動またはOperator操作でOFFにし、新規Effectを停止する。メトリクスだけでLedger／Storeを修正せず、RunbookからRecovery／Compensating Eventへ接続する。

## 1.10 Approval Grant本人性・Replay防止

`ApprovalGrant`は認証済みApproval Serviceが発行する署名付き一回限りGrantとする。ローカル単一利用者構成でも、単なるCLI引数やPlan Hash表示だけを本人性の根拠にしない。

必須項目：

* `grant_id`
* `plan_content_hash`
* `execution_plan_hash`
* `action_scope`
* `approver_subject_id`
* `approver_tenant_id`
* `authentication_context_class`
* `mfa_performed`
* `authentication_time`
* `issued_at`
* `not_before`
* `expires_at`
* `maximum_clock_skew_seconds`
* `nonce`
* `use_count=1`
* `revocation_epoch`
* `issuer_id`
* `issuer_key_id`
* `signature`
* `grant_schema_version`

消費規則：

1. Issuer署名、Key Status、Tenant、Subject、時刻、Plan Hashを検証する。
2. `nonce`と`grant_id`をDB一意制約でReplay防止する。
3. Approval消費、Action Claim、Lease取得、Fencing Token発行を単一SQLite TransactionのCASで行う。
4. `status=ISSUED AND consumed_at IS NULL AND expires_at > now - clock_skew AND revocation_epoch=current`をCAS条件とする。
5. 成功時に`consumed_at`、`consumed_by_actor_id`、`attempt_id`を記録する。
6. 失敗時は実行へ進まず`BLOCKED_APPROVAL`とする。
7. 許容Clock Skew超過は`CLOCK_SKEW_EXCEEDED`でFail-Closedとする。
8. 同一Grantへの並行Consumeは成功1件だけとする。

ローカル署名鍵は`$XDG_DATA_HOME/harness/keys/approval-signing.key`へ保存する。ownerがHarness実行UID、modeが`0600`、通常File、Linux側Filesystem上であることを起動時に検査する。不一致は`APPROVAL_KEY_PERMISSION_INVALID`で停止する。

単独構成における`authentication_context_class`と`mfa_performed`はSchema互換のため保持するが、OSログインセッション以上の保証を主張しない。本構成は改ざん検知であり、同一UIDの悪意あるProcessへの改ざん耐性や第三者への否認防止を提供しない。

CI向け非対話モードではOIDC等の認証済み主体から発行された短寿命Grantを要求し、Issuer、Audience、Repository、Workflow Refを検証する。

## 1.11 Canonical JSON／Hash規約

Plan、Approval、Policy、Receipt、Schema SetのHashは以下で固定する。

* Canonical JSON：RFC 8785 JCS準拠
* 文字コード：UTF-8
* Unicode：入力検証前にNFCへ正規化。ただしArtifact Binary Hashは原Bytes
* 数値：JCS表現。NaN、Infinity、負の0、実装依存Decimalを禁止
* 重複Object Key：Parse時に拒否
* 不正Unicode、未対Unicode Surrogate：拒否
* 改行：Text Artifactの論理正規化はLF、Content Hashは保存BytesのHash
* Hash Algorithm：SHA-256
* Domain Separation：`FDE-HARNESS/<artifact-type>/<schema-major>/`をCanonical Bytesの前へ付加
* Hash Profile Version：`hash_profile_version=1`
* Path：OS別BrokerまたはLinux Directory Handle基準で生成したWorkspace相対Canonical Path
* Secret値：対象外。Secret Reference ID、Version、Auth Route、Account Scopeだけを含める
* Timestamp：意味のあるSnapshot時刻はContentへ含める。Run発行時刻、表示時刻、Approval操作時刻はAuthority Envelope側へ分離する
* Schema Set：各Schema ID／Version／HashのCanonical配列を含める

### 1.11.1 Plan Hashの二層分離

Planの再現性とApprovalの単回Authority束縛を同一Hashへ混在させない。次の2つを正本とする。

| Hash | 目的 | 含める | 含めない |
|---|---|---|---|
| `plan_content_hash` | 意味的なPlan再現性、差分検証、Cache Key | Action Graph、Context、Input Read Evidence、Runtime／Invocation仕様、Provider／Model／Auth／Account／Entitlement／Pricing、Policy／Schema／Token Snapshot、Resource上限、Effect内容 | `run_id`、`execution_plan_id`、`plan_version`、Plan発行時刻、`expires_at`、Approval情報 |
| `execution_plan_hash` | 個別Runの実行Authority、Approval束縛、Replay防止 | `plan_content_hash`、`run_id`、`execution_plan_id`、`plan_version`、`issued_at`、`expires_at`、Planner Identity、Authority Scope、Hash Profile Version | Signature Value、表示用要約、Secret値 |

計算式：

```text
plan_content_hash =
  SHA-256(
    "FDE-HARNESS/plan-content/1/" UTF-8 bytes
    || RFC8785-JCS(PlanContentProjection)
  )

execution_plan_hash =
  SHA-256(
    "FDE-HARNESS/execution-plan-authority/1/" UTF-8 bytes
    || RFC8785-JCS({
         "plan_content_hash": plan_content_hash,
         "run_id": run_id,
         "execution_plan_id": execution_plan_id,
         "plan_version": plan_version,
         "issued_at": issued_at,
         "expires_at": expires_at,
         "planner_identity": planner_identity,
         "authority_scope": authority_scope,
         "hash_profile_version": hash_profile_version
       })
  )
```

不変条件：

* 同一ホスト・同一Workspace上で、同一Frozen PlanBuildInput、同一Policy、同一Capability／Entitlement／Pricing／Token／Schema Snapshot、同一Planner Algorithm Versionからは同一`plan_content_hash`を生成する。ホストを跨いだ一致は保証しない。
* `run_id`、`execution_plan_id`、`plan_version`、`issued_at`、`expires_at`のいずれかが異なるAuthority Envelopeは、同一Contentでも異なる`execution_plan_hash`を生成する。
* ApprovalGrantは`execution_plan_hash`へ束縛し、監査・差分表示用に`plan_content_hash`も保持する。
* Content変更は両Hashを変更する。Authorityだけの再発行は`plan_content_hash`を維持し、`execution_plan_hash`を変更する。
* `AT-PLAN-DETERMINISM-001`は、Content Hash同値とAuthority Hash差異を別Assertionとして検証する。
* Plan再生成時に同一`plan_content_hash`が得られない場合、差分を構造化して`PLAN_NONDETERMINISTIC`で停止する。

Hash Algorithm、Domain Separation、Canonicalization Version、Plan Content Projectionのいずれかを変更する場合はMajor変更とし、旧Plan／Approvalを再利用しない。

Plan Builderは次のFrozen Inputだけを受け取る純粋関数とする。

```python
@dataclass(frozen=True)
class PlanBuildInput:
    intent_hash: str
    workspace_snapshot_hash: str
    input_read_evidence_hash: str
    context_bundle_hash: str
    policy_snapshot_hash: str
    token_profile_hash: str
    schema_set_hash: str
    capability_snapshot_hash: str | None
    entitlement_snapshot_hash: str | None
    normalized_actions: tuple[NormalizedAction, ...]
```

Workspace、Clock、UUID、Policy Store、Filesystem、EnvironmentをBuild中に再読込してはならない。外部状態を1回だけ取得して`PlanBuildInput`へ凍結した後、`build(input)`を2回実行する。`plan_content_hash`またはCanonical Projectionが異なる場合は`PLAN_NONDETERMINISTIC`で停止する。設定`plan_determinism_double_build`は既定`true`であり、無効化はTest環境の明示Fixture以外で許可しない。

**同一プロセス内の二重Buildだけでは不十分である。** Pythonの`str`のHash値は`PYTHONHASHSEED`に依存し、プロセス内では固定される。したがってPlannerに`set`／`frozenset`の反復が1箇所でも混入した場合、二重Buildは常に一致し、次回起動時に`plan_content_hash`が変化する。この最も発生しやすい非決定性を二重Buildは原理的に検出できない。

そのため次の3層で強制する。

| 層 | 内容 | 検出できるもの |
|---|---|---|
| 同一プロセス二重Build | `build(input)`を2回実行しHash一致を確認 | 時刻・乱数・UUID・可変状態の混入 |
| **別プロセス三重Build** | `PYTHONHASHSEED`を変えた子プロセスで3回目をBuildし、Hash一致を確認 | Hash順序依存（`set`反復、`hash()`利用） |
| **静的検査** | Planner Packageにおける`set`／`frozenset`の反復、`dict`順序への暗黙依存、`sorted`なしの`.keys()`／`.values()`／`.items()`反復をAST検査で拒否 | 上記の混入そのもの |

Plan Build文脈では次のProcess環境を固定し、`PlanBuildInput`ではなく`RuntimeAttestation`と`plan_build_environment_hash`へ記録する。

```text
PYTHONHASHSEED  : 三重Buildで意図的に変える。通常実行では固定しない
LC_ALL          : C.UTF-8
LANG            : C.UTF-8
TZ              : UTC
PYTHONUTF8      : 1
```

Locale依存の大小比較（`str.lower()`、`locale.strcoll`、`sorted`のkeyなし比較）をPlan Content生成へ使用しない。整列はUnicode Code Point昇順を明示する。`AT-PLAN-DETERMINISM-001`は`CROSS_PROCESS`Caseで別`PYTHONHASHSEED`によるHash一致を検証する。

`spec/01-plan-content.md`または対応Registryには全Projection Fieldについて「決定的／非決定的、根拠、採否」を記録し、台帳にないFieldをHash入力へ追加しない。

### 会話SnapshotのHash束縛（v1.20）

Owner Decision `CHAT-2-A`により、会話のContext SnapshotはPlan Contentと同じ決定論規則を使う。別規則を作らない。

* SnapshotのContentへランダムID、採番ID、時刻、PID、Filesystem列挙順を含めない（不変条件#4）
* SnapshotはHashだけを含める。参照先の本文を埋め込まない
* 1回凍結した入力から2回Buildし、さらに別`PYTHONHASHSEED`の子プロセスで3回目をBuildしてHash一致を検証する（不変条件#5）
* `set`／`frozenset`を反復しない。Code Point昇順で`sorted`してから使う（不変条件#6）
* 同一履歴から同一Hashが得られる。`PYTHONHASHSEED`に依存しない
* Build手順は設計と試験で同一のものを使う。試験側へ別実装を置かない

Snapshotは次の3つへ束縛する。どれか1つでも動けばSnapshotは別物である。

| 束縛先 | 意味 |
|---|---|
| Message集合のHash | どの会話履歴から作ったか |
| 規範Schema SetのHash（`schema_catalog_hash`） | どのSchemaで解釈したか |
| 設計正本のHash（`design_sha256`） | どの契約のもとで作ったか |

**Message集合の正規化規則と順序キーは未確定である。** Messageの必須Fieldが決まって
いないため、何を正規化し何で並べるかを書けない。`docs/decision/DCR-CHAT-CONTRACT.md`の
`CC-3`／`CC-4`で決める。決まるまでSnapshotのBuild手順を実装しない。採番IDと時刻は
順序キーにできない（不変条件#4）。

## 1.12 Token Profile Snapshot

Provider呼出を持つPlanは`TokenProfileSnapshot`を必須とする。

* Provider、Model、Endpoint／Runtime
* Tokenizer Name、Tokenizer Version、Vocabulary Hash
* Counting Library／Adapter Version
* System Message Overhead
* Developer Message Overhead
* Tool Definition Overhead
* Per-message Overhead
* Structured Output Overhead
* Streaming Frame Overhead
* Reserved Output Tokens
* Reserved Tool Tokens
* Retry／Fallback Reservation
* Context Limit
* Maximum Output Limit
* Estimate Assurance=`EXACT | CONSERVATIVE | UNKNOWN`
* Retrieved／Created At、Expires At
* Snapshot Hash

`UNKNOWN`、Version不明、ModelとTokenizer不一致、Overhead算定不能の場合は実行停止する。

Provider実測Usageと推定値の差を毎回`token_profile_drift`へ記録する。同一Provider／Modelで3回連続して乖離がPolicy閾値（既定±10%）を超えた場合、`TOKEN_PROFILE_DRIFT_DETECTED`を記録し、次回Plan生成前にSnapshot再取得を要求する。SnapshotがExpiry内なら現在Runは継続できるが、Expiry超過時はFail-Closedとする。

## 1.13 Trust Level／Data Classification

Trust Levelは次の順序で固定する。

```text
TRUSTED_CONTROL
  > VERIFIED_INTERNAL
  > AUTHENTICATED_EXTERNAL
  > UNTRUSTED_PROVIDER_OUTPUT
  > UNTRUSTED_EXTERNAL_INPUT
  > UNKNOWN
```

Trust Levelは信頼度であり、Data Classificationとは独立する。Classificationは複数Labelを許し、機密度の実効値は次の優先順位で決める。

```text
SECRET
  > RESTRICTED
  > CONFIDENTIAL
  > INTERNAL
  > PUBLIC
```

`PERSONAL_DATA`、`SPECIAL_CATEGORY_DATA`、`REGULATED_DATA`は追加取扱Labelであり、`PUBLIC + PERSONAL_DATA`のように併存し得る。送信Gateでは、最も厳しい機密度と全ての追加取扱LabelのPolicyをAND条件で適用する。分類不明は`RESTRICTED + UNKNOWN`としてFail-Closedにする。自動Downgradeは禁止し、Downgradeは根拠Artifactと人間承認を持つ独立Decisionとする。

## 1.14 状態を持つStoreの正本関係

| Store | 役割 | 正本範囲 | Repair方針 |
|---|---|---|---|
| Event Ledger | Domain状態遷移 | Run／Action／Attempt／Approval／Releaseの状態正本 | Hash ChainとEventから再構築 |
| Artifact Store | Bytes内容 | Artifact内容の正本 | ManifestとContent Hashで検証 |
| Input Read Capability Registry | 読取り許可・取消状態 | Capability Scope、Nonce、Revocation、Read Evidence | 署名、Root Identity、Ledger Event、Read Evidenceを照合 |
| Effect Journal／Receipt Store | Effect実行証跡 | 実Effectの準備・観測・Receipt | 実体再照合後にLedgerへ補完Event |
| Transactional Outbox | Dispatch業務状態 | Dispatch準備・一意性・Remote照合状態 | LedgerとRemote双方を照合 |
| Budget Reservation Store | 金額拘束状態 | 予約・消費・保留額 | Ledger、Provider Usage、残高を照合 |
| Session Descriptor Store | 最適化Cache | Session ReferenceとResume Metadata | 不整合時は破棄しCold Start |
| Broker Journal | OS境界Effect状態 | Prepared WriteとFile Identity | Broker実体照合後にLedger補完 |
| Projection／Search Index | 表示・検索 | 正本ではない | Ledgerから再構築 |

「Event Ledgerが状態の正本」は、外部副作用の実在をLedgerだけで断定する意味ではない。Effect系Storeは実世界との照合正本であり、LedgerはそのDecisionと状態遷移を記録する。相互不整合は自動上書きせず`REPAIR_REQUIRED`として監査する。

### 物理永続化境界

上表のStoreは論理分類であり、実装対象フェーズでは全て単一SQLite DBへ格納する。Artifact BytesだけがDB外にある。SQLite接続は`ConnectionFactory`から生成し、WAL、`synchronous=FULL`、`foreign_keys=ON`を接続ごとに検証する。

LedgerのUPDATE／DELETE禁止は次の多層防御で強制する。

1. SQLite authorizerでRuntime接続のUPDATE／DELETEを拒否する。
2. LedgerテーブルにUPDATE／DELETE拒否Triggerを置く。
3. Migration専用接続とRuntime接続を分離する。
4. `sqlite3.connect`の直接利用とLedger更新SQLをAST／静的検索で拒否する。
5. Authorizer／Triggerを迂回した接続が存在しないことを受入試験で検証する。

Artifact Bytesの書込みはTemp write、File fsync、Atomic Rename、Parent Directory fsync、Manifest登録の順とする。同一Hashの既存Bytesがある場合は内容を再Hashし、不一致なら`ARTIFACT_CONTENT_CONFLICT`で停止する。


### 1.14.1 Phase Store State／Ledger Event／ActionAttempt State写像

Phase固有StoreのState名をLedger Eventとして直接流用してはならない。Store State、Ledger Event、共通ActionAttempt Stateの写像を次の表へ固定する。Ledger EventがActionAttempt Stateを変更しない場合は、Stateを維持し、Phase Projectionだけを更新する。

#### Remote Invocation Registry

| Store State | 正規Ledger Event | ActionAttempt State | 補足 |
|---|---|---|---|
| `PREPARED_DURABLE` | `REMOTE_INVOCATION_PREPARED` | `RUNNING` | OperationJournalとRegistryを送信前に永続化 |
| `REQUEST_DISPATCHING` | `REMOTE_REQUEST_DISPATCHING` | `RUNNING` | 最初のNetwork Byte送信前のDurable Commit |
| `REMOTE_ID_RECORDED` | `REMOTE_ID_RECORDED` | `RUNNING` | Remote IDは暗号化またはTokenized |
| `RUNNING` | `REMOTE_INVOCATION_RUNNING` | `RUNNING` | Pollで実行継続を確認 |
| `COMPLETED` | `REMOTE_INVOCATION_COMPLETED` | `RUNNING` | Output検証・Receipt保存前は成功終端にしない |
| `CANCELLED` | `CANCEL_CONFIRMED` | `CANCELLED` | Provider側停止を確認 |
| `FAILED` | `ACTION_FAILED` | Error分類に応じた`FAILED_*` | Retryは新Attempt |
| `RECONCILED` | `REMOTE_INVOCATION_RECONCILED` | 直前の共通Stateを維持 | その後のOutput／Receipt処理へ進む |
| `REMOTE_INVOCATION_UNCERTAIN` | `REMOTE_INVOCATION_UNCERTAIN`＋`EFFECT_UNKNOWN` | `EFFECT_UNKNOWN` | 自動再送、Fallback、Release禁止 |

#### Transactional Outbox

| Store State | 正規Ledger Event | ActionAttempt State | 補足 |
|---|---|---|---|
| `PREPARED_DURABLE` | `OUTBOX_PREPARED`＋`ACTION_PREPARED` | `PREPARED_DURABLE` | OperationJournal必須 |
| `DISPATCHING` | `OUTBOX_DISPATCHING`＋`EXECUTION_ATTEMPTED` | `EFFECT_IN_FLIGHT` | 最終送信点でFence再検証 |
| `ACCEPTED` | `OUTBOX_ACCEPTED` | `EFFECT_IN_FLIGHT` | Required Confirmation未達なら継続 |
| `SENT` | `OUTBOX_SENT` | `EFFECT_IN_FLIGHT` | 配信・適用を意味しない |
| `DELIVERED_OR_APPLIED` | `OUTBOX_EFFECT_CONFIRMED`＋`EFFECT_OBSERVED` | `EFFECT_VERIFIED` | Confirmation Target達成時のみ |
| `RECONCILED` | `OUTBOX_RECONCILED` | `RECEIPT_DURABLE`または直前State | EffectReceipt保存済みなら`RECEIPT_DURABLE` |
| `STATUS_UNKNOWN` | `OUTBOX_STATUS_UNKNOWN`＋`EFFECT_UNKNOWN` | `EFFECT_UNKNOWN` | 自動Dispatch禁止 |
| `MANUAL_RECONCILIATION` | `MANUAL_RECONCILIATION_ENQUEUED` | `EFFECT_UNKNOWN` | Manual Queueを正本化 |
| `EFFECT_CONFLICT` | `EFFECT_CONFLICT_DETECTED`＋`ACTION_BLOCKED` | `BLOCKED_CONFLICT` | `CONFLICT`単独名はOperationJournalで禁止 |

#### Budget Reservation Store

| Store State | 正規Ledger Event | ActionAttempt State | 補足 |
|---|---|---|---|
| `PREPARED_DURABLE` | `BUDGET_RESERVATION_PREPARED`＋`ACTION_PREPARED` | `PREPARED_DURABLE` | Reservation OperationJournal必須 |
| `RESERVED` | `BUDGET_RESERVED`＋`EFFECT_OBSERVED` | `EFFECT_VERIFIED` | 残高拘束をStorageで再照合 |
| `CONSUMING` | `BUDGET_CONSUMING` | `RUNNING` | Provider実行中 |
| `PENDING_RECONCILIATION` | `BUDGET_RECONCILIATION_PENDING` | `EFFECT_IN_FLIGHT` | 保守額を保持 |
| `SETTLED` | `BUDGET_SETTLED`＋`EFFECT_RECEIPT_STORED` | `RECEIPT_DURABLE` | Cost ReceiptとJournalを参照 |
| `RELEASED` | `BUDGET_RELEASED` | 直前の共通Stateを維持 | 実行不存在または未使用額を確認 |
| `EXPIRED` | `BUDGET_RESERVATION_EXPIRED` | `BLOCKED_POLICY`または直前State | 新規有償実行不可 |
| `EFFECT_CONFLICT` | `EFFECT_CONFLICT_DETECTED`＋`ACTION_BLOCKED` | `BLOCKED_CONFLICT` | Store内`CONFLICT`名を使用しない |
| `STATUS_UNKNOWN` | `BUDGET_STATUS_UNKNOWN`＋`EFFECT_UNKNOWN` | `EFFECT_UNKNOWN` | 新規有償Action停止 |

不変条件：

* Phase Store StateをLedgerの正本Event名として記録しない。必ず上表の正規Ledger Eventへ写像する。
* `REMOTE_INVOCATION_UNCERTAIN`、`OUTBOX_STATUS_UNKNOWN`、`BUDGET_STATUS_UNKNOWN`は最終的に共通`EFFECT_UNKNOWN`へ写像する。
* Phase Storeだけが進みLedger Eventが欠落した場合、RecoveryはStore実体を照合しCompensating EventをAppendする。推測で成功状態へ進めない。
* Ledger Eventだけが存在しStore Stateが欠落する場合、同じEffectを再実行せず`REPAIR_REQUIRED`または`EFFECT_UNKNOWN`とする。
* `AT-EVENT-MAPPING-001`で全写像、欠落Event、順序違反、Unknown写像を機械検証する。

## 1.15 規範Schema Set

MVP0-A開始時点で`registries/schemas.yaml`が定める25 SchemaをJSON Schemaとして同梱する。

1. `EventEnvelope`
2. `Run`
3. `ActionIntent`
4. `ActionAttempt`
5. `ExecutionPlan`
6. `RuntimeEnvelopeSpec`
7. `InvocationManifest`
8. `TokenBudgetPolicy`
9. `TokenProfileSnapshot`
10. `ContextFragment`
11. `ContextBundle`
12. `ContextSelectionReceipt`
13. `PolicyDecision`
14. `ApprovalGrant`
15. `Lease`
16. `RuntimeAttestation`
17. `ArtifactManifest`
18. `EffectReceipt`
19. `OperationJournal`
20. `InputReadCapability`

各Schemaは`$id`、SemVer、必須項目、`additionalProperties=false`、制約、正常例、拒否例、前MajorからのUpcasterを持つ。`OperationJournal`と`InputReadCapability`はMVP0-A Core契約として固定する。MVP1以降の`ChangeSet`、`SessionResumeDescriptor`、`ProviderCandidateSet`、`BudgetReservation`、`OutboxRecord`、`WorkspaceCapability`等は該当フェーズで追加し、Execution PlanのSchema Set Hashへ含める。

## 1.16 Input Read Capability／Control-Data境界

### 1.16.1 Intake Read Protocol

Task Loader、Repository候補収集、Context候補収集、過去Artifact参照は、書込みActionと同様に明示的なRead境界を必要とする。Operatorが指定したWorkspace、Root、明示File以外を暗黙探索してはならない。

正規順序：

```text
OperatorがWorkspace／対象Root／明示Fileを指定
  → Workspace IdentityとLinux Root Directory Handleを確定
  → InputReadCapabilityを署名発行
  → INPUT_READ_CAPABILITY_ISSUED
  → Capability／Expiry／Scope／Policy Hashを検証
  → INPUT_READ_STARTED
  → Directory Handle相対でPath解決
  → File Identity／Mount Identity／File Typeを検証
  → Bytes読込み
  → Classification／Secret／PII Scan
  → 許可されたArtifactだけCAS保存
  → INPUT_ARTIFACT_CLASSIFIED
  → ContextFragment候補化
```

読み取りを開始した後にCapability Scope、Root Identity、Mount Identity、Policy Hashが変化した場合は候補収集を破棄し、新Capabilityで最初から実行する。読取りEvidence HashとCapability Set Hashを後続`ExecutionPlan`へ束縛する。

### 1.16.2 Linux Safe Read規則

MVP0-AからMVP1-EのLinux Runtimeでは、Root Directory Handleを基準に`openat2`相当の`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV`を使用する。利用不能なKernelでは、各Path Componentを`openat`、`O_NOFOLLOW`、`fstat`で検証する安全なDirectory WalkへFail-Closedで切り替える。

拒否対象：

* Symlink、Magic Link、Path Traversal、Root外参照
* Capabilityで許可されていないMount／Filesystem越境
* Block／Character Device、FIFO、Socket、Device Node、特殊File
* `/proc`、`/sys`、`/dev`、`/run`等のVirtual／Runtime Filesystem。ただし署名Policyで個別許可したRead-only Resourceを除く
* Network Filesystem、FUSE、Overlay境界。明示AllowlistとMount Identity固定がない場合
* Hardlink IdentityがWorkspace外の許可されない対象と衝突するFile
* Workspace外Path、絶対Path、空Path、NULを含むPath
* MVP2-A以前のUNC、Device Path、NT Namespace、Windows Network Path
* Size、File Count、Depth、Read Time上限を超える入力
* 読取り中にDevice／Inode／Generation／Mount IDが変化した対象

拒否時は`INPUT_READ_DENIED`を記録し、`ContextSelectionReceipt.rejected_input_resources`へCapability ID、要求Path、Canonical候補Path、File Identity、Reason Code、検証時刻を保存する。拒否PathのBytesはArtifact Storeへ保存しない。

#### 1.16.2.1 Input Read Event の発行層

`INPUT_READ_STARTED`、`INPUT_ARTIFACT_CLASSIFIED`、`INPUT_READ_DENIED`をLedgerへAppendする責務は**Application／Orchestration層だけが持つ**。`SafeInputReader`、Capability Broker、Masking Pipelineへこの責務を持たせない。判定を行う層と記録を残す層を分け、記録の有無で判定が変わらないようにする。

Readerは`ReadDecision`（`ALLOWED`／`DENIED`）と拒否理由だけを返す。Reader自身はLedger Portを受け取らず、Transactionも所有しない。Readerが記録まで負うと、Transaction境界の所有者が層をまたいで曖昧になり、不変条件#15（Repositoryは`commit()`しない）と整合しなくなる。

| 項目 | 規約 |
|---|---|
| Append位置 | Readerの判定確定直後、読取Bytesを後続Planへ束縛する前 |
| Transaction境界 | 単一SQLite Transaction。所有者はApplication層のUnit of Work |
| Ledger Head更新 | `expected_stream_sequence`でHeadを検証してからAppendする |
| Event順序 | `INPUT_READ_STARTED` → （分類を行った場合のみ`INPUT_ARTIFACT_CLASSIFIED`） → `INPUT_READ_DENIED`。許可時は`INPUT_READ_DENIED`を発行しない |
| 重複防止 | `expected_stream_sequence`の楽観検査。Headがずれていれば拒否し、再試行はTransactionごとやり直す |
| Effect開始前の線形化点 | Input Readは外部Effectを伴わないが、読取Evidence HashとCapability Set Hashを後続Planへ束縛する前をもって線形化点とする |
| Reader失敗時 | Readerが例外で終わりOrchestratorが判定を受け取れない場合、`INPUT_READ_DENIED`を推測でAppendしない。判定不能は`EFFECT_UNKNOWN`扱いで停止し、Event列は観測されたものだけを残す |

`INPUT_READ_STARTED`は読取を試みた事実の記録であり、Readerの判定より前にAppendする。したがって拒否Caseでも`INPUT_READ_STARTED`はLedgerに残る（§19.1.1 第6項）。

#### 1.16.2.2 Approval／Plan Event の発行層と Subject 境界

`APPROVAL_ISSUED`、`APPROVAL_CONSUMED`、`ACTION_BLOCKED`、`PLAN_RESOLVED`もApplication／Orchestration層がAppendする。Repository、Domain関数、PlannerはLedger責務を持たない。

**委任経路のEvent列を素の承認経路へ流用しない。** `resolve_delegation`と`replay_derived_approval`が組むEvent列は先頭が`DELEGATION_MATCHED`または`POLICY_DECIDED`であり、委任が成立した事実を含む。委任していない実行の記録として使うと、監査上は委任の記録になってしまう。同じEvent名を含むことは同じ列であることを意味しない（§19.1.1）。

Subjectの境界は次のとおりとする。

| Subject | 層 | 何を主張するか |
|---|---|---|
| `APPROVAL_GRANT` | Orchestration | 承認Grantが消費された事実。承認消費は実行の記録であり、Event列をLedgerへ残す |
| `PLAN_COMPARISON` | Unit／Planner | 同一Frozen InputからPlanを再構築した比較結果。**実行を主張しない。** Event列を要求せず、State と Error Code だけで主張が閉じる |

`PLAN_COMPARISON`はPlannerの内部比較であり、Ledgerに残る実行事象ではない。Planner結果をExecutor結果として扱わないため、この主体へEvent列を要求しない。

#### 1.16.2.3 Masking Event の発行層と Subject 境界（v1.17）

**Masking PipelineはLedgerへAppendしない。** Reader・Masker・Pipelineのいずれも
Ledger Portを持たない。Event発行は外側のOrchestration層が行う。
Input Read（§1.16.2.1）・Approval／Plan（§1.16.2.2）と同じ置き方である。

判定する層が記録も持つと、記録の都合で判定が変わる余地ができる。
Masking Pipelineは**判定結果を返すだけ**にする。

| Case が主張するもの | 発行層 | Event列 |
|---|---|---|
| Masking Pipeline内部の判定 | — | 要求しない。Unit Evidenceで観測する |
| Masking後のInput Read判定 | Orchestration | `INPUT_READ_*` |
| Scan#2による拒否 | Orchestration | `INPUT_MASKING_*` |

`REQUIRED_EMPTY`は「Ledgerを観測して0件だった」を意味する（§19.1.1）。
Ledger Portを持たない層の試験は観測する対象を持たないので、この宣言を使えない。
偽のProbeが返す0を根拠にしてはならない。**見ていないことを見て0件と書かない。**
Masking Pipelineを駆動するCaseは`NOT_APPLICABLE`とする。

Scan#2の拒否Event列はOrchestration Evidenceで観測する。
**Test側でEvent列を合成しない**（§19.1.1）。Masking Unit Caseの
Event観測Policyは`NOT_APPLICABLE`とし、`event_sequence`は`null`になる（§26.2.1）。

##### Scan#2拒否のEvent列（v1.18）

Scan#2が拒否した実行のEvent列は次の4件である。Registryの
`expected_event_sequence`が正本であり、本表はその写しである。

| # | Event | 出る条件 |
|---|---|---|
| 1 | `INPUT_MASKING_STARTED` | 実行開始時に無条件 |
| 2 | `INPUT_MASKING_SCAN1_CANDIDATES_READY` | Scan#1が`CLEAN`か`MASKABLE`のとき |
| 3 | `INPUT_MASKING_SPANS_PROPOSED` | Maskerを呼んだとき |
| 4 | `INPUT_MASKING_REJECTED` | 拒否で終わったとき |

**2を省いた3件の列は到達しない。** Maskerを呼ぶにはScan#1を通過している必要が
あり、通過していればScan#1の候補は揃っている。3が出る実行では2も必ず出る。
到達しない期待列は、実装をどう直しても満たせない。

##### 期待Event列は§1.4.1の必須先行Eventを含む（v1.19）

Caseの観測はStream全体を読む。したがって期待列を満たすには、その列が**そのまま
Stream全体**でなければならない。§1.4.1の必須先行EventはStream内の既出Eventを見るので、
列の途中に先行が要るのに列へ入っていなければ、その列は作れない。

先行は**推移的に**辿る。`POLICY_DECIDED`は`PLAN_RESOLVED`を、それは`INTENT_CREATED`を
要求する。期待列を書くときは、先頭のEventが要求する先行までさかのぼって含める。

Event を観測時に削除・並べ替えして列を合わせない。合わせてよいのは期待値の側である。

`INPUT_MASKING_SCAN1_CANDIDATES_READY`は「候補が揃ってMaskerへ進めた」ことを指す。
Scan#1が拒否した実行では候補が作られずMaskerへも進まないので出ない。
これを「Scan#1が走った」の意味で出すと、Event列を読んだ側が
「候補があったのに拒否された」と読み違える。

##### 同じ拒否でも層によってSubjectが変わる

Secret／特定個人情報の拒否は、**どの層から見るか**でSubjectとStateが変わる。
片方をもう片方へ暗黙変換しない。変換規則をここで正本化する。

| 層 | Subject | State | 意味 |
|---|---|---|---|
| Masking Pipeline内部 | `MASKING_RESULT` | `REJECTED` | Maskせず拒否した、というMaskingの判定 |
| Orchestration | `INPUT_READ_DECISION` | `DENIED` | その入力を読ませない、というInput Readの判定 |

同じ事象の2つの側面であり、どちらかが誤りではない。
**任意の`REJECTED`を`DENIED`へ読み替えることを禁止する。** 変換してよいのは、
Masking Policyの拒否をOrchestrationがInput Read判定として引き取る経路だけである。
Case側は自分がどちらの層を主張するのかをRegistryの`expected_subject_type`で宣言する。

`MASKING_RESULT`の`REJECTED`は`INPUT_READ_DECISION`のState Enumに存在せず、
`DENIED`は`MASKING_RESULT`のState Enumに存在しない（§19.1）。
Enumをまたぐ以上、変換には層の宣言が要る。宣言の無い変換は行わない。

### 1.16.3 Classification／Secret Scan順序とマスキング

Span順序と拒否理由の扱いは §1.16.3.1／§1.16.3.2 で定める。


Artifact StoreへのPut前に、次の順序で処理する。判定権限を持つのは決定論スキャナだけであり、
LLMはマスク範囲の提案しか行えない（ADR-007）。

```text
1. UCD 14.0割当済み符号位置Guard → 標準 unicodedata.normalize('NFC')
   Artifact欠落・Hash不一致・未割当符号位置は正規化前にReject
2. Deterministic Scan #1
     Media Type／Encoding／Size検証
     Secret／Credential Scan
     Data Classification判定
     PII／Regulated Data Scan
     Binary／Executable／Archive判定
     → MASKABLE候補Spanを生成
3. 判定（境界は masking-policy.yaml が正本）
     REJECT   : 保存・候補化を拒否。LLMへ渡さない
     CLEAN    : そのままCAS保存
     MASKABLE : 4へ
4. ローカルLLM Maskerへ正規化済みテキストを渡し、Spanだけを受け取る
5. Span検証（決定論）。Scan #1候補とUnionし重複排除
6. 決定論RewriterがMask Tokenへ置換
7. Deterministic Scan #2  ← 実質のGate
     合格   : MaskedArtifactとしてCAS保存
     不合格 : Reject。原本もマスク版も候補化しない
```

`RESTRICTED`以上、Secret／Credential検出、`NATIONAL_ID`、`SPECIAL_CATEGORY_DATA`、分類不能、
Scan不能はマスクせずRejectする。停止、Timeout、形式不正、Span不正、Hash不一致も全てRejectとし、
マスクなしでの通過を一切許さない。

Scan結果自体にSecret本文を含めず、Finding Type、位置の安全な要約、Rule ID、Scanner Version、
Evidence Hashだけを保存する。マスクによる`data_classification`の自動Downgradeを禁止する（§1.13）。

#### 1.16.3.1 Span順序は正規化せず拒否する（v1.17）

Maskerが返したSpanが開始位置で昇順に並んでいない場合、**Union前に拒否する**。

| 項目 | 値 |
|---|---|
| Subject | `MASKING_RESULT` |
| State | `REJECTED` |
| Error Code | `MASKING_SPAN_INVALID` |

**通常経路でsortして受理しない。** Unionのなかで並べ替えると、順序が壊れた出力を
返すMaskerを検出できなくなる。順序の検査は既にあるが、Unionが先に並べ替えると
到達しない。順序検査をUnionの**前**へ置く。

Error Codeは`errors.yaml`の既存語彙を使う。**この契約のためにCodeを新設しない。**

#### 1.16.3.2 Policy境界の拒否とScan#2の検証失敗を分ける（v1.17）

どちらも`MASKING_RESULT`の`REJECTED`だが、**原因が違う**ので Error Code で分ける。

| 原因 | Error Code | 意味 |
|---|---|---|
| Policy境界による拒否 | 無し（`null`） | Policyが「渡さない」と決めた。仕様どおりの動作であり、失敗ではない |
| Scan#2で残存を検出 | `MASKING_VERIFICATION_FAILED` | マスクしたはずの出力に残っていた。検証が失敗した |

Policy拒否に Error Code を持たせると、「仕様どおり止まった」と「検証が失敗した」が
同じ形で記録される。運用でこの2つを区別できなくなる。

**新しいError Codeを足さない。新しいCaseも足さない。** 既存の2 Caseが
それぞれの原因を担う。どちらがどちらかはRegistryの`expected_error_code`が示す。

### 1.16.4 Control／Data Message Role

Contextへ格納する各Fragmentは次の`message_role`を持つ。

```text
SYSTEM_CONTROL
DEVELOPER_CONTROL
USER_TASK
TOOL_DEFINITION
VERIFIED_REFERENCE_DATA
UNTRUSTED_ARTIFACT_DATA
UNTRUSTED_PROVIDER_DATA
```

`SYSTEM_CONTROL`と`DEVELOPER_CONTROL`へ昇格できるのは、Z0 Control Planeが生成し、署名、Policy Hash、Issuer、Expiryを検証できるArtifactだけとする。Repository、添付、過去Artifact、Provider出力、外部入力は、その本文に命令形式が含まれていてもControl Roleへ昇格しない。

`ContextFragment`は`message_role, control_authority, instruction_eligible, input_read_capability_id, source_file_identity, classification_scan_evidence_hash`を持つ。`InvocationManifest`は`message_role_manifest_hash`と`control_data_policy_hash`を持ち、Role順序とControl/Data分離をApproval対象に含める。Role変更は新Context Bundle、新Plan、新Approvalを要求する。

---


## 1.17 WSL2 Mount Boundary

起動時、Workspace登録時、InputReadCapability発行時にWorkspace RootのMountを`/proc/self/mountinfo`から最長Prefix一致で特定する。Mount ID、Parent Mount ID、Major:Minor、Root、Mount Point、Filesystem Type、Mount OptionsをRuntime Attestationへ格納する。

Policy Allowlist外のFilesystem、`/mnt/*`、Windows側Filesystem、Network FS、未知FUSE、未知Overlayは`WORKSPACE_ON_FOREIGN_FS_DENIED`で拒否する。文字列Prefixだけで`/mnt`判定せず、Mount情報とRoot Directory HandleのIdentityを併用する。

## 1.18 Artifact保持・GC・容量

| Artifact区分 | 既定保持 | 削除規則 |
|---|---:|---|
| 監査必須Evidence | 無期限 | Ledger／Receipt／Gateから参照される限り削除禁止 |
| Temp Artifact | 24時間 | Receipt Durable後、参照0件、Dry-run確認後 |
| 孤児Bytes | 7日 | DB Manifestなし、作成時刻経過、Hash再検証後 |
| Raw Provider Output Payload | 90日 | Payloadだけ削除可。Hash、Metadata、Classification、Deletion Eventは保持 |
| 再生成可能Cache | Policy値 | 正本参照がなく再生成可能な場合のみ |

### 1.18.1 GC Planner と GC Executor の境界

削除の**判断**と削除の**実行**を別の層へ置く。判断だけを行うPlannerの結果を、削除が成功した証拠として扱わない。

| 層 | 責務 | 責務でないもの |
|---|---|---|
| GC Planner（Domain） | 参照数と最小経過時間から削除**候補**を決める。Dry Runとして記録する | 実削除。Filesystemへの書込み。Ledgerへの記録 |
| GC Executor（Application） | 承認済み候補についてArtifact Storeの`delete_payload`を呼ぶ。削除結果を読み戻す。Manifest保持を確認する。`ARTIFACT_PAYLOAD_DELETED`をLedgerへAppendする | 削除可否の判断 |

PlannerはDomain層にあり`os`をimportしないため、そもそも削除できない。Plannerの`GC_RESULT`が`ACCEPTED`であることは「候補を正しく挙げた」ことの証拠であって、「Payloadが消えた」ことの証拠ではない。**PlannerのACCEPTEDだけで実削除Caseを合格にしない。**

GC Executorは次を同一Transactionの同一契約へ束縛して記録する。個別に観測して後から突き合わせると、削除したがEventを残せなかった場合に区別がつかない。

* `payload_deleted` — `delete_payload`の戻り値を読み戻して確認した結果
* `manifest_retained` — Manifest Recordが残っていることの確認結果
* `deletion_event_count` — Appendした`ARTIFACT_PAYLOAD_DELETED`の件数
* `observed_event_sequence` — Ledgerから読み出した実際のEvent列
* `ledger_head_before` / `ledger_head_after` — Append前後のLedger Head

#### 1.18.2 `ARTIFACT_PAYLOAD_DELETED` の契約

| 項目 | 定義 |
|---|---|
| Subject | `GC_RESULT` |
| State | `GC_RESULT`名前空間の`ACCEPTED`。Payload削除とManifest保持の両方を確認できた場合だけ`ACCEPTED`とする |
| 失敗時State | `REJECTED`。削除できなかった場合とManifestを失った場合の両方を含む |
| Error Code | 成功時は`null`。Storage書込みに失敗した場合は`STORAGE_WRITE_FAILED`、削除の成否を判定できない場合は`EFFECT_UNKNOWN`で停止する |
| Retry可否 | **可**。`delete_payload`は同じContent Hashに対して冪等であり、既に消えている場合も`ACCEPTED`へ収束する。ただしEventの重複Appendは`expected_stream_sequence`で防ぐ |
| 終端State | `ACCEPTED`と`REJECTED`。`EFFECT_UNKNOWN`は終端ではなくOperator判断を要する停止である |
| ActionAttempt State | 変更しない。Artifact保持はAttemptの実行状態ではない |

Payloadを消してもManifest Record、Content Hash、Metadata、Classification、および本Eventは保持する。何を消したかを後から示せなくなるためである。

`harness gc --dry-run`を必須とし、自動削除を既定で無効にする。Ledgerから参照される監査Artifactと、90日後にPayload削除できるRaw Provider Outputを同一Retention Classにしない。Artifact Store合計を`artifact_store_bytes`として記録し、既定10GBで警告する。

## 1.19 想定負荷と性能測定

| 指標 | 想定値 |
|---|---:|
| 通常Run | 20 runs/day |
| ピーク | 60 runs/day |
| Action | 8 actions/run |
| Event | 約60 events/run |
| Event追記 | 約1,200 events/day |
| Ledger成長 | 約2MB/day。実測で更新 |
| Artifact成長 | 約20MB/day。実測で更新 |

性能受入はWSL2 Linux側Filesystem、同一Hardware、Warm-up 5 Run、測定30 Runで実施し、Provider推論時間を除く制御処理をp50≤5秒、p95≤8秒とする。SQLite busy時間、fsync時間、最大値もEvidenceへ保存する。

# 2. フェーズ間依存関係

```text
MVP0-A
  └─ MVP0-B
       └─ MVP1-A
            └─ MVP0-C
                 └─ MVP1-D
```

実装対象外の参照仕様は次の依存を保持するが、Feature Flagを解禁しない。

```text
MVP1-BはMVP0-BまたはMVP0-Cの運用実績後
MVP1-CはMVP0-C後
MVP1-EはMVP0-CおよびApproval／Outbox設計の再評価後
MVP2-AはWindows Native Workspaceが要件化した場合
MVP2-Bは複数Worker／Hostが要件化した場合
Blind ReviewerはMVP0-A後にPilot可能
Enterprise Hardeningは組織利用前の横断Gate
```

並行開発は許可するがRuntime解禁は依存Gate順に行う。参照仕様のCode、Schema、Feature Flagを誤って有効化しないことをSpec LintとRuntime Policyで検査する。

# 15. 横断詳細設計

## 15.1 Policy Decision

すべてのPolicy Decisionは次を持つ。

* Decision ID
* Policy Package ID／Hash
* Rule Set ID／Version
* Policy Freshness=`CURRENT | LKG_WITHIN_TTL | STALE_TTL_EXCEEDED | REVOKED_OR_INVALID | UNKNOWN`
* LKG Age Seconds。該当時
* Input Hash
* Decision=`ALLOW | DENY | REQUIRE_APPROVAL | REQUIRE_HUMAN_REVIEW`
* Reason Code
* Human-readable Reason
* Required Approval Type
* Required Evidence
* Expiry
* Runtime Gate Expires At。Policy／Capability／Approval／Entitlement／Pricingの最短期限
* Stale Action Disposition
* Override可否
* Override Authority
* Created At

Overrideは元Decisionを書換えず、別の`PolicyOverrideGrant`として記録する。Security不変条件、Workspace Boundary、Secret平文保存禁止、Provider直接書込み禁止はOverride不可とする。

## 15.2 Schema Versioning

* JSON Schema Draft 2020-12を正本とする。
* `schema_name`＋SemVer＋`$id`＋Schema Content Hashで識別。
* 全Objectは原則`additionalProperties=false`。
* Required Field、Enum、Pattern、Length／Range、Format、相互制約をSchemaで表現する。
* State／Status依存FieldはJSON Schema Draft 2020-12の`if/then/else`、`dependentRequired`、またはState別`$defs`で機械強制する。
* Schemaで表現できない不変条件は`x-domain-invariants`へRule IDを列挙し、Domain Testへ接続する。
* Minor追加はOptionalかつ既定意味を持たない後方互換Fieldだけ。
* Required Field追加、意味変更、Enum削除、Hash対象変更はMajor。
* Patchは説明、Example、非規範Metadataの修正だけ。
* Readerは対応Major Versionを明示。
* Upcasterは元Recordを保持し、Projection時だけ新形式へ変換。入力Recordを破壊的に変更しない。
* Upcasterの結果は次を持つ。いずれも省略できない。
  * `from_version`／`to_version`：変換元と変換先のVersion。
  * `upcaster_code_hash`：Upcaster自身のCode Hash。どのCodeで変換したかを結果へ束縛する。
  * `conversion_reason`：なぜこのVersion変換が要るのか。
  * `lossless`：欠落なく運べたか。変換した場合はBoolean、変換しなかった場合は`null`。
* **`MIGRATED`はLosslessを意味しない。** 変換が成功したことと欠落なく運べたことは別であり、
  同一Fieldで表現しない。欠落の有無は`lossless`だけが述べる。
* 変換先Versionに行き先の無いFieldは、**監査・証跡Field／無害Field／未分類**の3つへ分類する。
  * **監査・証跡Fieldを落とす変換を禁止する。** 落とした場合は変換せず移行不能とする。
    監査・証跡Fieldとは、そのRecordが何を検証・除外・圧縮・マスクし、いつ測ったかを
    後から示すための値である。落とすと「行っていなかったのか、記録を落としたのか」を
    区別できないRecordが生まれる。区別できない状態を作らない。
  * **未分類Fieldを落とす変換を禁止する。** 分類できないものを捨てない（§6）。
  * 無害Fieldだけを落とす場合に限り変換してよく、そのとき`lossless`は`false`とする。
    無害Fieldの追加はOwner Decisionを要する。
* Upcasterは変換先Schema全体で出力を検証してから成功を返す。Required、Type、Enum、Const、
  Pattern、入れ子Object、`additionalProperties`のいずれか1つでも満たさない出力を成功としない。
* Upcaster結果の語彙はMigration Tool専用とし、Core Schema、Ledger Event、
  `errors.yaml`のRuntime Error Codeへ追加しない。
* Execution Planへ使用Schema ID／Version／HashのSchema Set Hashを含める。
* 各Schemaは正常Example、境界Example、拒否Exampleを最低1件ずつ持つ。
* Gate Corpusに旧Version読込み、Upcast、未知Major拒否、Hash差異試験を含める。
* Schema変更後は既存Approval、Plan、Receiptを再解釈せず、対応Readerまたは新Planを要求する。

## 15.3 Artifact Store

### Store規則

* BytesはSHA-256 Content-addressed、ManifestはSQLiteに保存する。
* PutはTemp write、File fsync、Atomic Rename、Parent Directory fsync、Manifest Transactionの順に固定する。
* 同一Hashが存在する場合はBytesを再Hashし、不一致なら`ARTIFACT_CONTENT_CONFLICT`。
* Ledger、Receipt、Gate Evidenceから参照される監査Artifactは削除しない。
* Raw Provider OutputはPayload Retentionを既定90日とし、削除後もHash、Metadata、Classification、Deletion Eventを保持する。
* TempはReceipt Durable後24時間、孤児Bytesは7日経過後にGC候補とする。
* GCは`harness gc --dry-run`結果に対するOperator明示実行だけで行う。
* Legal HoldはSchemaに保持するが単独利用の既定はなし。
* 合計容量を`artifact_store_bytes`へ記録し、既定10GBでWarning。

### Encryption

Secret、PII、Provider Raw Outputを保存する場合はPolicyで暗号化を要求する。暗号鍵そのものをArtifact Store、Ledger、Logへ保存しない。暗号化有無、Key Reference、Algorithm、Nonce、AAD HashをManifestへ記録する。

### Repair

ManifestありBytesなしは`BLOCKED_REPAIR_REQUIRED`。BytesありManifestなしは孤児であり、正本状態へ自動昇格しない。Repairは元Bytesの取得根拠とHashがある場合だけManifestを補完し、推測で復元しない。

## 15.4 Event Ledger Storage

実装対象フェーズでは全状態Storeを単一SQLite DBへ格納する。

必須設定：

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

Event StreamのAppendは`expected_stream_sequence`で楽観制御する。行単位のCASは`store_version`で行い、両概念を混同しない。

Ledger不変条件：

* Streamごとに`UNIQUE(stream_id, sequence_number)`。
* Event ID、Record IDは全体一意。
* Previous Event HashとCurrent Event HashでChainを構成。
* UPDATE／DELETE禁止。
* Append、Projection更新、同一Transactionで必要なJournal／Receipt更新を原子的に処理。
* Projectionは正本でなく、Ledgerから再構築可能。

多層強制：

1. Runtime接続のSQLite authorizer。
2. Ledger UPDATE／DELETE拒否Trigger。
3. Runtime／Migration Connection Factory分離。
4. `sqlite3.connect`直接利用と禁止SQLの静的検査。
5. `AT-LEDGER-TAMPER-001`と`AT-EVENT-ORDER-001`で実行時検査。

`BEGIN IMMEDIATE`を基本とし、RepositoryはTransactionをCommitしない。Application Unit of WorkがTransaction境界を所有する。`os._exit(137)`、ENOSPC、EIO、Ledger Append中断をFault Injectionで検証する。

## 15.5 Runtime Attestation

Runtime Attestationは「OS全体の形式保証」ではなく、Harnessが観測・適用したRuntime設定の証拠である。

含めるもの：

* Runtime ID
* RuntimeEnvelopeSpec ID／Hash
* InvocationManifest ID／Hash
* Launcher Version／Hash
* Executable／Image Absolute IdentityとHash
* Adapter Version／Hash
* Provider／Model／Model Digest
* Auth Route、Credential Type
* Account／Tenant／Billing Identity。Secret値なし
* Technical Capability Snapshot Hash
* Commercial Entitlement Snapshot Hash。External／Paid時
* Pricing Catalog Hash。External／Paid時
* Entitlement Evidence Artifact Hash。External／Paid時
* Budget Reservation ID／Hash、Cost Approval Hash。Paid時
* User／Identity
* Working Directory／Workspace Identity
* Filesystem Policy実効値
* Network Policy実効値
* Sandbox Implementation／Status／Assurance
* Environment Key一覧と非Secret Value Hash
* Config Root／Config Tree Hash
* Repository Instruction、Plugin、Hook、MCP、Session Cache実効状態
* Mounted Artifact
* Tool Allowlist
* CPU／Memory／Disk／PID／Time／Output上限
* Process／Container／Job ID
* Process Tree Root
* Started At
* Attestation Assurance Level
* Attestation Hash

起動前検証：

```text
RuntimeAttestation.runtime_envelope_spec_hash == ExecutionPlan.runtime_envelope_spec_hash
AND InvocationManifest Hash一致
AND Provider／Model／Auth Route／Account／Tenant／Billing Identity一致
AND External／Paid時はCapability／Entitlement／Pricing／Evidence Hash一致
AND 全必須実効値がSpec以内
AND Sandbox Status=ENFORCED
```

不一致は`plan_runtime_mismatch`と`runtime_hash_field_mismatch`を計上し、起動前に`BLOCKED_POLICY`。Runtime条件を観測できない環境、Sandboxが警告のみ、設定Driftがある環境では当該Actionを実行しない。

## 15.6 Security Test分類

* Unit：Canonicalization、Policy、State Machine
* Property：Path、Hash、Schema、Token Budget
* Integration：Ledger、CAS、Worktree、Adapter
* Fault Injection：Commit境界、Network、Process、DB
* Adversarial：Prompt Injection、Path Escape、Output Injection
* Concurrency：Lease、Fencing、Lock、Budget
* Recovery：各中間状態
* Golden／Hold-out：既知ケースと未公開ケース
* Operational：Backup、Restore、DR、Alert
* Penetration：Enterprise Gate

## 15.7 Logging

JSON Structured Logとし、最低限：

* Timestamp
* Level
* Run／Action／Attempt
* Correlation ID
* Event Type
* Component
* Outcome
* Error Classification
* Duration
* Provider／Model。許可される範囲
* Token／Cost
* Redaction Result

禁止：

* API Key
* Session Reference
* Authorization Header
* Provider Raw Prompt／Output本文
* PII
* Approval Secret
* Capability Token

## 15.8 Configuration

Git管理された設定Schemaを正本とし、未知Keyを拒否する。Secret値は設定に直接持たずSecret Referenceだけを記録する。

初期Feature Flag：

```text
external_provider_enabled         = false
paid_execution_enabled            = false
external_dispatch_enabled         = false
windows_wsl_write_enabled         = false
parallel_worker_enabled           = false
automatic_policy_approval_enabled = false
fault_injection_permitted         = false
plan_determinism_double_build     = true
automatic_gc_enabled              = false
```

RuntimeでFlag変更を行う管理APIは初期実装しない。変更はGit Commit、Schema検証、Policy Hash更新、新Plan／再承認を要求する。Emergency Recovery中のFlag変更は禁止する。

## 15.9 Core Schema Catalog v1

本節はCore Schemaの規範的な設計カタログである。件数の正本は`design-source/registries/schemas.yaml`であり本文へ書かない（不変条件#18）。実装成果物は`schemas/core/<schema-name>/<semver>.schema.json`（1 Version＝1 File）、正常例は`examples/valid`、拒否例は`examples/invalid`、Upcasterは`migrations/upcasters`へ配置する。

### Schema Version登録形式

1つの論理Schemaは複数のVersionを同時に登録できる。一意Keyは`schema_name`単独ではなく
`(schema_name, schema_version)`である。§15.2によりRequired Field追加・意味変更・Enum削除・
Hash対象変更はMajorであり、**既存Versionのファイルを上書きせず新しいVersionを追加する**。

`design-source/registries/schemas.yaml`の登録形式は次のとおりとする。

```yaml
- ordinal: 11
  schema_name: ContextBundle
  active_write_version: 2.0.0
  versions:
  - version: 1.0.0
    path: schemas/core/ContextBundle/1.0.0.schema.json
    read_only: true
  - version: 2.0.0
    path: schemas/core/ContextBundle/2.0.0.schema.json
    read_only: false
```

規則は次で固定する。

* 新規Recordの書込みは`active_write_version`へだけ行う。
* `read_only: false`は`active_write_version`にだけ付ける。1論理Schemaに書込み可能Versionが
  0個または2個以上ある登録はFail-Closedで拒否する。
* `read_only: true`のVersionは読取りとUpcastの入力にのみ使う。
* 件数は**論理Schema数とVersion数を分けて**数える。`registry-snapshot.json`は
  `totals.core_schema_count`（論理Schema数）と`totals.core_schema_version_count`（Version数）を
  別Fieldとして持つ。
* `schemas.yaml`が指すFileが存在しない場合、Registry Snapshotの生成をFail-Closedで停止する。
  Fileの無いVersionをCatalogへ載せない。

### Schema Catalog HashとSchema Set Hash

用途の違う2つのHashを持ち、混同しない。

| Hash | 対象 | 変わるとき | 置き場所 |
|---|---|---|---|
| `schema_catalog_hash` | 登録済み**全Version**のName／Version／Path／Content Hash／`read_only`／`active_write` | Registryに版が増減したとき、登録Fileの中身が変わったとき | `registry-snapshot.json` |
| `schema_set_hash` | そのPlan／Recordが**実際に使用した**Version集合 | そのPlanが使う版が変わったとき | Execution Plan（§15.2） |

分離しないと、旧Versionを読み取り可能なまま保持するだけで**全PlanのHashが動く**。
§15.2は「Execution Planへ**使用**Schema ID／Version／HashのSchema Set Hashを含める」と定めており、
Planへ束縛するのは使用集合の側である。

`schema_catalog_hash`の入力は`(schema_name, schema_version)`昇順へ整列し、Registryの記載順、
Filesystemの列挙順、`generated_at`のような生成時刻を含めない（§1.11）。

### 共通Envelope制約

全Recordは次を必須とする。

```json
{
  "schema_name": "EventEnvelope",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000001",
  "run_id": "018f0000-0000-7000-8000-000000000002",
  "correlation_id": "018f0000-0000-7000-8000-000000000003",
  "created_at": "2026-08-05T06:00:00Z",
  "producer": "harness-core/1.7.0",
  "content_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
}
```

* IDはUUIDv7または同等の衝突耐性を持つ形式。
* TimestampはRFC 3339 UTC。
* Hashは`sha256:`＋64桁lowercase hex。
* 未知Fieldは禁止。
* Secret値、Credential、Session Reference平文を禁止。

本カタログ内の`規範Fixture断片`はField意味を示す説明用断片であり、単独ではSchema Validを主張しない。Machine-validな完全Fixtureは`examples/valid/<schema-name>/*.json`へ共通Envelopeを含めて配置し、`AT-SCHEMA-COMPLETE-001`で`registries/schemas.yaml`のCore Schema全件を検証する。本文で`Schema完全Valid Fixture`と明記したJSONだけが、JSON Schema上の完全形を示す。署名検証、Content Hash再計算、Cross-reference、Event順序等のDomain Invariantは別試験で検証する。

### 1. EventEnvelope

必須Field：

`event_id, stream_id, stream_type, stream_sequence, global_sequence, event_type, event_version, causation_id, actor_id, payload_schema_id, payload_hash, previous_event_hash, event_hash`

制約：

* `(stream_id, stream_sequence)`、`global_sequence`、`event_id`は一意。
* `event_type`は§1.4.2と§1.8の正規Eventだけ。
* `event_hash`はHeader＋Payload Hash＋Previous HashをDomain-separated Hash。
* Payload本文は別Artifact参照可。

規範Fixture断片：`{"event_type":"PLAN_RESOLVED","stream_sequence":4}`
拒否例：`{"event_type":"ACTION_SUCCEEDED"}`
Upcaster：未知Event名を推測変換しない。明示Mappingがある旧EventのみProjection時に変換。

### 2. Run

共通必須Field：

`run_id, task_id, state, created_by, unresolved_action_count, risk_level, store_version`

状態依存Field：

`plan_version, current_plan_content_hash, current_execution_plan_hash, policy_snapshot_hash, release_decision_id, started_at, ended_at, terminal_reason_code, blocked_reason_code, unreconciled_effect_carried_to_run_id, related_run_id`

JSON SchemaはState別`oneOf`または`if/then`で次を強制する。

| State | 必須Field | 禁止／未設定可 |
|---|---|---|
| `CREATED` | 共通必須Field | Plan Hash、Policy Hash、Release、開始／終了時刻は未設定 |
| `PLANNING` | 共通必須Field | Plan Hash、Releaseは未設定。`started_at`は任意 |
| `WAITING_APPROVAL`／`READY`／`RUNNING`／`RECOVERING`／`WAITING_RELEASE` | 共通＋`plan_version, current_plan_content_hash, current_execution_plan_hash, policy_snapshot_hash` | `release_decision_id`は`WAITING_RELEASE`まで未設定 |
| `BLOCKED_REPAIR_REQUIRED` | 共通＋`blocked_reason_code` | `ended_at`、`terminal_reason_code`は禁止。Plan確定後なら両Plan Hashを保持 |
| `COMPLETED` | 共通＋全Plan／Policy Hash＋`release_decision_id, started_at, ended_at` | `unresolved_action_count=0`、Release Decision=`RELEASE` |
| `BLOCKED`／`FAILED`／`CANCELLED` | 共通＋`ended_at, terminal_reason_code` | Plan確定前の終端はPlan Hash未設定可。確定後なら両Plan Hashを保持 |

制約：

* Stateは§1.5のEnum。
* `BLOCKED_REPAIR_REQUIRED`は非終端で`ended_at=null`。
* `EFFECT_UNKNOWN`、`CANCEL_UNKNOWN`、未解決Manual Queue、欠損Artifactを持つRunは全終端状態へ遷移不可。
* `COMPLETED`では`release_decision_id`必須、`unresolved_action_count=0`。
* `current_plan_content_hash`と`current_execution_plan_hash`は片方だけ存在してはならない。
* Plan再発行では`plan_version`を単調増加し、旧HashをLedgerから削除しない。
* `unreconciled_effect_carried_to_run_id`の引継ぎ深度は1、自己参照・循環参照は禁止。
* Mutable更新は`store_version`のCASを必須とする。

Schema完全Valid Fixture（Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "Run",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000010",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:00:00Z",
  "producer": "harness-core/1.7.0",
  "content_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "task_id": "018f0000-0000-7000-8000-000000000013",
  "state": "WAITING_APPROVAL",
  "plan_version": 1,
  "current_plan_content_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "current_execution_plan_hash": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
  "policy_snapshot_hash": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
  "created_by": "operator:local",
  "unresolved_action_count": 1,
  "risk_level": "LOW",
  "store_version": 1
}
```

拒否Fixture：

* `state=COMPLETED`で`release_decision_id`欠落。
* `state=BLOCKED_REPAIR_REQUIRED`で`ended_at`存在。
* 未照合Effectがあるのに`state=FAILED`。
* `state=WAITING_APPROVAL`で両Plan Hashが片方だけ存在。

Upcaster：State追加は未知値として拒否し、Operator更新を要求。

### 3. ActionIntent

必須Field：

`action_id, run_id, action_type, requested_capability, input_artifact_hashes, requested_outputs, read_resources, write_resources, external_effect_resources, data_classification, trust_level, risk_level, maximum_attempts, timeout_seconds`

制約：

* `write_resources`または`external_effect_resources`が非空ならHuman Approval Policyを必須。
* Provider出力のTool Callは直接Actionにせず、新規IntentとしてSchema検証。
* Resource KeyはCanonical形式。
* `read_resources`は`InputReadCapability`参照を必須とし、CapabilityなしのPath文字列を受け付けない。

規範Fixture断片：`{"action_type":"MOCK_INFERENCE","write_resources":[]}`
拒否例：未分類のExternal Effect Intent。
Upcaster：Action Typeの意味変更はMajor。

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

### 5. ExecutionPlan

必須Field：

`execution_plan_id, plan_version, run_id, issued_at, expires_at, planner_identity, authority_scope, action_graph, context_bundle_hash, input_read_capability_set_hash, input_read_evidence_hash, control_data_policy_hash, token_budget_policy_hash, token_profile_snapshot_hash, runtime_envelope_spec_hash, invocation_manifest_hash, policy_snapshot_hash, schema_set_hash, cost_upper_bound, hash_profile_version, plan_content_hash, execution_plan_hash`

制約：

* Approval前に全Content HashとAuthority Fieldを確定する。
* `hash_profile_version=1`。
* `plan_content_hash`は§1.11.1の`PlanContentProjection`から計算する。
* `execution_plan_hash`は§1.11.1のAuthority Envelopeから計算する。
* `run_id, execution_plan_id, plan_version, issued_at, expires_at`を`plan_content_hash`へ含めない。
* External／PaidではExact Auth／Account／Entitlement／Pricing参照必須。
* Content変更時は両Hashが変わる。Authority再発行だけの場合はContent Hashを維持しExecution Hashを変更する。
* `expires_at > issued_at`。
* Plan ContentのCanonical再計算結果と保存`plan_content_hash`が一致しないRecordは拒否する。
* `action_graph`はランダムAction IDではなく`semantic_action_key`を使用し、Topological Order＋Keyで決定的に整列する。

Schema完全Valid Fixture（Hash関係のDomain検証は別試験）：

```json
{
  "schema_name": "ExecutionPlan",
  "schema_version": "1.0.0",
  "record_id": "018f0000-0000-7000-8000-000000000030",
  "run_id": "018f0000-0000-7000-8000-000000000011",
  "correlation_id": "018f0000-0000-7000-8000-000000000012",
  "created_at": "2026-08-05T06:02:00Z",
  "producer": "harness-core/1.7.0",
  "content_hash": "sha256:6666666666666666666666666666666666666666666666666666666666666666",
  "execution_plan_id": "018f0000-0000-7000-8000-000000000031",
  "plan_version": 1,
  "issued_at": "2026-08-05T06:02:00Z",
  "expires_at": "2026-08-05T06:17:00Z",
  "planner_identity": "harness-planner/1.7.0",
  "authority_scope": "ACTION_EXECUTION",
  "action_graph": {
    "nodes": ["action:sha256:4545454545454545454545454545454545454545454545454545454545454545"],
    "edges": []
  },
  "context_bundle_hash": "sha256:7777777777777777777777777777777777777777777777777777777777777777",
  "input_read_capability_set_hash": "sha256:8888888888888888888888888888888888888888888888888888888888888888",
  "input_read_evidence_hash": "sha256:9999999999999999999999999999999999999999999999999999999999999999",
  "control_data_policy_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "token_budget_policy_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "token_profile_snapshot_hash": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "runtime_envelope_spec_hash": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "invocation_manifest_hash": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "policy_snapshot_hash": "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
  "schema_set_hash": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "cost_upper_bound": {
    "currency": "JPY",
    "amount": "0"
  },
  "hash_profile_version": 1,
  "plan_content_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
  "execution_plan_hash": "sha256:3333333333333333333333333333333333333333333333333333333333333333"
}
```

拒否Fixture：

* `run_id`だけ変更したのに`execution_plan_hash`を再計算していない。
* 同じContent Projectionで`plan_content_hash`が異なる。
* `expires_at <= issued_at`。
* Runtime Spec Hashがnull。

Upcaster：Hash対象変更、Plan Content Projection変更はMajorで再Plan必須。

### 6. RuntimeEnvelopeSpec

必須Field：

`runtime_spec_id, runtime_type, launcher_version, executable_path, executable_sha256, argv, working_directory_identity, workspace_identity, os_boundary, provider_id, adapter_version, model_id, model_digest, auth_route, credential_type, provider_account_id, tenant_id, billing_identity, credential_scope_hash, config_tree_hash, instruction_policy_hash, plugin_hook_mcp_policy_hash, environment_allowlist_hash, filesystem_policy_hash, network_policy_hash, tool_policy_hash, process_policy_hash, output_schema_hash, resource_limits, sandbox_spec, runtime_envelope_spec_hash`

制約：

* `argv`は文字列配列。Shell文字列禁止。
* Executableは絶対Path＋SHA-256。
* Sandbox必須Actionでは`required=true`。
* Secret値禁止。

規範Fixture断片：`{"argv":["mock-provider","--input","/run/input.json"]}`
拒否例：`{"argv":"mock-provider < input"}`
Upcaster：実行意味に影響する追加はMajor。

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

### 8. TokenBudgetPolicy

必須Field：

`policy_id, maximum_input_tokens, reserved_output_tokens, reserved_tool_tokens, safety_margin_tokens, maximum_context_tokens, overflow_policy, required_fragment_types, compression_depth_limit, retry_reservation, policy_hash`

制約：

* 各値は0以上の整数。
* 合計がProvider Context Limit以下。
* Required Fragmentを除外不可。
* `overflow_policy=FAIL_CLOSED`を初期値。

規範Fixture断片：`{"maximum_input_tokens":4096,"reserved_output_tokens":1024}`
拒否例：負数、上限超過。
Upcaster：計算意味変更はMajor。

### 9. TokenProfileSnapshot

必須Field：

`token_profile_id, provider_id, model_id, tokenizer_name, tokenizer_version, vocabulary_hash, counting_adapter_version, overheads, reserved_output_tokens, reserved_tool_tokens, retry_fallback_reservation, context_limit, maximum_output_limit, estimate_assurance, created_at, expires_at, snapshot_hash`

制約：

* `estimate_assurance=UNKNOWN`ではRuntime GO不可。
* Model／Tokenizer組を一意に識別。
* Expiry切れ不可。

規範Fixture断片：`{"estimate_assurance":"CONSERVATIVE","context_limit":8192}`
拒否例：Tokenizer Versionなし。
Upcaster：Tokenizer差替えは新Snapshot。

### 10. ContextFragment

必須Field：

`fragment_id, source_artifact_hash, source_span, fragment_type, message_role, control_authority, instruction_eligible, input_read_capability_id, source_file_identity, classification_scan_evidence_hash, content_artifact_hash, estimated_tokens, classification_labels, trust_level, priority, mandatory, freshness, deduplication_key`

制約：

* Content本文はArtifact参照。
* Mandatory FragmentはSelectionから除外不可。
* Classificationは§1.13。
* `UNTRUSTED_ARTIFACT_DATA`または`UNTRUSTED_PROVIDER_DATA`は`instruction_eligible=false`。
* `SYSTEM_CONTROL`／`DEVELOPER_CONTROL`は検証済み`control_authority`必須。

規範Fixture断片：`{"fragment_type":"POLICY","mandatory":true}`
拒否例：Source Artifactなし。
Upcaster：Classification変換は自動Downgrade禁止。

### 11. ContextBundle

必須Field：

`context_bundle_id, selected_fragment_ids, ordered_fragment_ids, excluded_fragment_ids, total_estimated_tokens, input_read_capability_set_hash, input_read_evidence_hash, message_role_manifest_hash, token_profile_snapshot_hash, token_budget_policy_hash, compression_artifact_ids, selection_receipt_id, bundle_hash`

制約：

* Ordered IDsはSelected IDsと同集合。
* Token不変条件を満たす。
* Mandatory Fragmentを全て含む。

規範Fixture断片：`{"selected_fragment_ids":["f1"],"ordered_fragment_ids":["f1"]}`
拒否例：Mandatory欠落。
Upcaster：順序意味変更はMajor。

### 12. ContextSelectionReceipt

必須Field：

`selection_receipt_id, candidate_fragment_ids, selected_fragment_ids, excluded_fragments, rejected_input_resources, input_read_capability_set_hash, input_read_evidence_hash, deduplication_result, compression_result, estimated_token_total, token_profile_snapshot_hash, budget_policy_hash, algorithm_version, decision_hash`

制約：

* 各Excluded FragmentにReason Code必須。
* CandidateはSelected＋Excludedで完全に説明。
* Algorithm VersionとTie-breakerを記録。
* 各`rejected_input_resources`にCapability ID、要求Path、Reason Code、File／Mount Identity Evidenceを記録し、Bytes本文は保存しない。

規範Fixture断片：`{"excluded_fragments":[{"fragment_id":"f2","reason":"BUDGET"}]}`
拒否例：理由なし除外。
Upcaster：Algorithm差は再Selection。

### 13. PolicyDecision

必須Field：

`decision_id, policy_package_id, policy_package_hash, rule_set_id, rule_set_version, policy_freshness, lkg_age_seconds, input_hash, decision, reason_codes, human_reason, required_approval_type, required_evidence, expires_at, runtime_gate_expires_at, stale_action_disposition, override_allowed, created_at, decision_hash`

制約：

* Decision Enumは`ALLOW | DENY | REQUIRE_APPROVAL | REQUIRE_HUMAN_REVIEW`。
* Deny時はReason Code必須。
* Security不変条件はOverride不可。
* `STALE_TTL_EXCEEDED | REVOKED_OR_INVALID | UNKNOWN`では新規ActionのDecisionを`ALLOW`にできない。
* Runtime Gateは`runtime_gate_expires_at`超過後に§14.4.1を適用する。

規範Fixture断片：`{"decision":"REQUIRE_APPROVAL","reason_codes":["WRITE_EFFECT"],"policy_freshness":"CURRENT","stale_action_disposition":"NOT_APPLICABLE"}`
拒否例：ReasonなしDeny。
Upcaster：Rule Versionは変換せず当時値を保持。

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

### 15. Lease

必須Field：

`lease_id, resource_key, holder_id, attempt_id, fencing_token, issued_at, expires_at, renewed_at, status, store_version`

制約：

* ResourceごとにFencing Token単調増加。
* Stale Token更新拒否。
* Holder／Attempt変更不可。

規範Fixture断片：`{"fencing_token":42,"status":"ACTIVE"}`
拒否例：Token 41で更新。
Upcaster：Fencing意味変更不可。

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

### 17. ArtifactManifest

必須Field：

`artifact_id, content_hash, byte_size, media_type, encoding, classification_labels, trust_level, producer, source_artifact_ids, encryption_key_reference, created_at, retention_policy, legal_hold, compression, verification_status`

制約：

* PutはContent Hashで冪等。
* Restricted以上は暗号化Reference必須。
* Provider Raw OutputとNormalized OutputのArtifact Typeを分離。

規範Fixture断片：`{"media_type":"application/json","verification_status":"HASH_VERIFIED"}`
拒否例：Restrictedなのに暗号化なし。
Upcaster：Content Hashを変換しない。

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

### 20. InputReadCapability

共通必須Field：

`capability_id, workspace_id, os_boundary, broker_or_reader_id, root_directory_identity, filesystem_identity, allowed_operations, allowed_relative_path_patterns, file_type_policy, mount_policy, symlink_policy, special_file_policy, maximum_bytes, maximum_files, maximum_depth, classification_policy_hash, secret_scan_policy_hash, issued_to_subject, issued_at, not_before, expires_at, nonce, revocation_epoch, issuer_id, issuer_key_id, signature_algorithm, signature, capability_hash`

制約：

* `allowed_operations`はMVP0-Aでは`READ | ENUMERATE`のみ。
* Root Directory Handle／Device／Inode／Mount IDをEvidenceへ束縛する。
* Absolute Path、Root外、Symlink、Magic Link、許可外Mount、特殊File、Virtual FS、Network FSを既定拒否。
* Capability ID、Nonce一意。Expiry／Revocation／署名検証必須。
* Read後にRoot／File Identityが変化した場合、Bytesを採用しない。
* CapabilityのScope拡張は新Capability発行と候補再収集を要求。

規範Fixture断片：`{"os_boundary":"LINUX","allowed_operations":["READ","ENUMERATE"],"symlink_policy":"DENY"}`
拒否例：`{"allowed_relative_path_patterns":["/**"],"symlink_policy":"FOLLOW"}`。
Upcaster：Path解決意味、Mount Policy、署名対象変更はMajor。

### 21. DelegationGrant

`DelegationGrant`は、委任可能な承認範囲を署名・Audience・Predicate・Policy Snapshotへ束縛するCore Schemaである。MVP0-Aでは未実装だが、Registryが定義する22件のSchema Catalogから除外してはならない。

共通必須Field：

`delegation_grant_id, issuer_identity, delegate_identity, audience, scope_predicate, policy_snapshot_hash, issued_at, expires_at, signature, trust_anchor_id, delegation_hash`

制約：

* `audience`はDelegate IdentityおよびRuntime Audienceと一致しなければならない。
* `scope_predicate`は許可対象を拡張できず、破壊的Actionを委任してはならない。
* `policy_snapshot_hash`とTrust Anchorは発行時の値へ固定し、変更後の自動継続を許さない。
* RevocationはEffectの線形化点より前後を区別し、監査TrailをAppend-onlyで残す。
* 署名、Audience、Scope、Trust Anchorの不一致はFail-Closedで拒否する。

拒否Fixture：

* Audience不一致。
* Scopeを発行後に拡張。
* 失効済みTrust Anchor。
* Revocation後の新規Effect。

Upcaster：Audience、Scope Predicate、Trust Anchor、署名方式の意味変更はMajor。

### 22. MaskingReceipt

`MaskingReceipt`は、入力の正規化、検出、置換、二次検査の結果を監査可能に保存するCore Schemaである。Masking処理の結果は、実際に適用したPolicy SnapshotをHashで束縛し、正規化Profile ArtifactのHashをPolicy Snapshot Hashとして代用してはならない。

共通必須Field：

`masking_receipt_id, source_artifact_hash, masked_artifact_hash, policy_snapshot_hash, normalization_profile_artifact_hash, scanner_profile_hash, masking_result, created_at, content_hash`

制約：

* 永続化前に`CoreSchemaRegistry.validate()`でSchema検証を実行する。
* `policy_snapshot_hash`は適用Policy SnapshotのHashであり、`normalization_profile_artifact_hash`とは別の意味を持つ。
* `normalization_profile_artifact_hash`は再現可能なUnicode正規化Profileを束縛する。
* 二次検査でSecretまたは禁止PIIが残る場合は、後続Effectを生成せずFail-Closedで停止する。
* Receiptは原文やSecret値を含めず、必要最小限のHash・分類・統計だけを記録する。

拒否Fixture：

* `policy_snapshot_hash`が不正Hashまたは欠落。
* 正規化Profile HashをPolicy Snapshot Hash欄へ流用。
* 必須の二次検査結果が欠落。
* Schema検証前の永続化。

Upcaster：Policy束縛、正規化Profile、Secret非保持規約の意味変更はMajor。

### Schema Delivery Gate

* `registries/schemas.yaml`のCore Schema全てにValid／Invalid Exampleが存在。
* `Run`、`ActionIntent`、`ActionAttempt`、`ExecutionPlan`、`InvocationManifest`、`ApprovalGrant`、`RuntimeAttestation`、`EffectReceipt`、`OperationJournal`、`InputReadCapability`はProperty Testを必須化。
* `AT-SCHEMA-CONDITIONAL-001`でRun／ActionAttempt／ApprovalGrantのState／Status別`oneOf`または`if/then`を機械検証する。
* `AT-SCHEMA-COMPLETE-001`でCore SchemaのValid／Invalid Fixture、Hash Pattern、`additionalProperties=false`、Cross-referenceを機械検証する。
* 全Example Hashを`schema-example-manifest.json`へ固定。
* UpcasterはGolden RecordでLossless性を検証。
* Schema Set Hashを生成し、MVP0-A Execution Planへ含める。
* 実ファイル未作成またはTest未実行の状態は`UNVERIFIED`。

### 23. Conversation

`Conversation`は、1本の会話を識別するCore Schemaである。会話の本文は保持せず、識別子と内容Hashだけを持つ。**子Messageを列挙しない。**

共通必須Field：

`conversation_id, conversation_hash, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約：

* `message_ids`を持たない。Message追加のたびに親Recordを書き換えないためである。
* `message_count`を持たない。件数はMessage集合から導出する（不変条件#18）。
* Append-onlyとし、既存RecordをUPDATE／DELETEしない。訂正はCompensating EventのAppendで行う。

拒否Fixture：

* `conversation_hash`が不正Hashまたは欠落。
* `message_ids`または`message_count`を含む。

### 24. ConversationMessage

`ConversationMessage`は、会話中の1発話を表すCore Schemaである。`conversation_id`で親を指す**子→親の単方向参照**を持つ。

共通必須Field：

`message_id, conversation_id, role, sequence_number, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約：

* `role`は§1.16.4のControl／Data Message Roleと同じ語彙を使う。Chat専用のrole値を追加しない。
* Provider出力は`UNTRUSTED_PROVIDER_DATA`であり、本文に命令形式が含まれていてもControl Roleへ昇格しない。
* 順序は`sequence_number`で判定する。採番IDと時刻を順序キーにしない（不変条件#4）。
* `token_count`を持たない。Token会計は`TokenBudgetPolicy`と`TokenProfileSnapshot`が持つ。
* Append-onlyとする。

拒否Fixture：

* `role`が§1.16.4の語彙に無い値。
* `sequence_number`の欠落。
* `token_count`を含む。
* `content`／`text`／`body`をinlineで含む。

`2.0.0`（v1.22）：

`content_artifact_hash`をRequiredへ追加する。§15.2によりRequired追加はMajorであり、
`1.0.0`は上書きせずread_onlyで残す。追加Fieldは1つだけで、他のFieldは`1.0.0`と同じである。

共通必須Field（`2.0.0`）：

`message_id, conversation_id, role, sequence_number, content_artifact_hash, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約（`2.0.0`）：

* `content_artifact_hash`は`ContextFragment@2.0.0`と同名・同義であり、形式は`^sha256:[0-9a-f]{64}$`である。
* 参照先はArtifact CASであり、`ArtifactManifest`で解決する。参照先が存在しないMessageを受理しない。
* 本文をinlineで持たない。
* `content_hash`は本文Hash・`role`・`sequence_number`から導出する。

拒否Fixture（`2.0.0`）：

* `content_artifact_hash`の欠落。
* `content_artifact_hash`が不正Hash形式。
* 参照先ArtifactがManifestに無い。

### 25. ConversationSnapshot

`ConversationSnapshot`は、ある時点の会話Contextを再現するためのCore Schemaである。Message集合、Schema Set、設計正本の3つへ束縛する。

共通必須Field：

`snapshot_id, conversation_id, snapshot_hash, message_set_hash, schema_set_hash, design_sha256, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約：

* `message_set_hash`は各`ConversationMessage`の`content_hash`を`sequence_number`昇順で並べたもののHashである。
* `snapshot_hash`は自分自身を入力に取らない。
* ID、時刻、PID、Filesystem列挙順をHash入力へ入れない（不変条件#4）。
* `schema_set_hash`は**実際に使用したSchema Version**だけを含める。未使用Schemaの追加でSnapshotのHashを動かさない。
* `conversation_id`を必須とし、どの会話のSnapshotかをEvidence単体で検証できるようにする。
* Append-onlyとする。

拒否Fixture：

* `conversation_id`の欠落。
* `snapshot_hash`を自身の入力へ含めた値。
* `message_set_hash`が不正Hash。

### 26. ApprovalConsumeResult

Owner実回答 DCR-1-A と ACRC-1〜10 を反映した1.0.0。競合の敗者を1 Attempt単位で表す。通常のReplayや未参加の拒否をこのResultへ変換しない。

共通必須Field：

`consume_result_id, grant_id, concurrency_group, attempt_id, state, error_code, successful_consumes, failed_consumes, schema_set_hash, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約：

* `state=REJECTED`、`error_code=APPROVAL_REPLAY`、`successful_consumes=0`、`failed_consumes=1`。このErrorから他Stateへ推測で写さない。
* 呼出側の群IDを、GrantがISSUEDの間に登録する参加Attemptへ束縛する。参加登録はGrantを消費せず、1 Grantにつき1群。未登録・登録済みAttemptの再送はResultを生成せず拒否する。
* 消費CASの結果確定後にResultをAppend-onlyで永続化し、ApplicationがApproval専用StreamへAPPROVAL_REPLAY_DENIEDをAppendする。CAS・Result・Eventを単一SQLite Transactionへ束縛する。
* 群の成功は必ず1件であり、成功+敗者=Nを独立した2接続競合試験で検証する。個別敗者の値で群の保証を置換しない。
* actual_subject_idはconsume_result_id。content_hashは自身以外のResult全文から導出し、Schema Setへ束縛する。

拒否Fixture：必須Field欠落、敗者successful_consumes=1、異なるError/State、未知Field、再送の敗者誤分類、Event失敗時の部分Commit。

## 15.10 追加Runbook

以下を各フェーズのDelivery成果物へ追加する。

* Provider Sandbox失敗
* Provider Config Drift
* Credential／Auth Route／Billing Identity不一致
* Effect Unknown
* Ledger／Receipt／Journal不整合
* Windows Broker拒否
* Budget Reservation孤児
* Outbox Status Unknown／Manual Reconciliation
* Stale Worker／Fencing Reject
* Blindness Evidence不一致
* Remote Registry未永続化
* Context Read境界違反
* Schema Conditional失敗
* Policy Approval誤解禁
* Plan再現性不一致
* Policy期限切れ／Recovery-only移行
* Phase State／Ledger Event写像不整合
* Core Schema Suite失敗
* Test Manifest対象型不一致

各RunbookはOwner、Trigger Metric、Feature Flag縮退、証拠保全、禁止操作、Recovery Command、Escalation、復旧後の再Plan／再Approval条件を持つ。

---

## 15.11 Evidence Document Schema

Case Evidence、Gate Evidence、領域Evidenceは**Core Schema Catalogとは別系統**で版を持つ。
`schemas.yaml`の`core_schemas`はDomain Objectの正本であり、Evidence文書はそこに含まれない。
したがってEvidence文書へFieldを足しても`schema_catalog_hash`は動かない。混同しない。

Evidence文書の版の正本は`design-source/registries/evidence-schemas.yaml`である。
`core_schemas`と同じく`(schema_name, schema_version)`を一意Keyとし、書込みは
`active_write_version`へだけ行う。他の登録Versionは`read_only: true`とする。

### 15.11.1 2.0で足したField

`evidence_kind`と`event_observation`を**Required**で足した（§26.2.1）。
§15.2は「Required Field追加、意味変更、Enum削除、Hash対象変更はMajor」と定めている。
Case Evidenceの`evidence_hash`は`evidence_hash`自身を除く**Body全体**を対象にするため、
Fieldを1つ足すだけでHash対象が変わる。**Minorでは収まらない。**

`event_sequence`は2.0で`null`を取り得るようになった。これは既存Fieldの意味変更であり、
この一点だけでもMajorである。

v1.16で`event_observation_policy`を2.0のRequiredへ足した。**版を上げていない。**
2.0を名乗るEvidenceはこの時点で1件も存在しないためである（Release Evidenceは未生成、
Development Evidenceはすべて1.2）。無効化される既存Evidenceが無いので、締める側の
変更をMinorとして扱う余地はここにしかない。

**Evidenceが1件でも積み上がった後は同じことをしない。** そのときは3.0を発行する。
既にある2.0のEvidenceを、後から足したRequired Fieldで不適合にしてはならない。

### 15.11.2 1.2を上書きしない

1.2は`read_only`として保持する。Bytesの違う2つの文書が同じ版番号を名乗る状態を作らない
（§15.2）。

既存のDevelopment Evidenceは1.2のまま残す。**黙って2.0へ変換しない。** 変換すると、
1.2の時点では観測していなかった`event_observation`を、あたかも観測していたかのように
書き足すことになる。過去のEvidenceの意味を後から変えない。

Release Evidenceは本改訂の時点で1件も存在しない（Verifierは`BLOCKED_EVIDENCE_MISSING`）。
したがって2.0への引上げで**無効化されるRelease Evidenceは無い**。Release Evidenceが
生成された後に同じ引上げを行うと、既存Release Evidenceの再生成かUpcasterが要る。
版を上げるならEvidenceが積み上がる前に上げる。

Verifierは`evidence_schema_version`を`active_write_version`と完全一致で検査する。
1.2のEvidenceはRelease判定の入力として受理しない。**受理しないことと、消すことは別である。**
1.2のDevelopment Evidenceは履歴として残す。

---
