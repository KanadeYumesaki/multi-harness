# マルチプロバイダーAI業務実行統制基盤

## フェーズ別詳細設計書 v1.25（Runtime GO判定実行正本・単独開発・WSL2限定・MIT公開版）

**作成日：2026年8月5日 JST**
**v1.8改訂日：2026年8月6日 JST**
**v1.9改訂日：2026年8月17日 JST**
**v1.10改訂日：2026年8月17日 JST**
**v1.11改訂日：2026年8月18日 JST**
**v1.12改訂日：2026年8月18日 JST**
**v1.13改訂日：2026年8月18日 JST**
**v1.14改訂日：2026年8月20日 JST**
**Verifier v1.2是正日：2026年8月5日 JST**
**上位仕様：`マルチプロバイダーAI業務実行統制基盤_統合要求仕様書_v3.2.md`**
**v1.14の改訂元：フェーズ別詳細設計書 v1.13 本文（SHA-256=`2c1138b4f623dfd872be7d079da0d34cebcea5aa28e6827f2561e2613f6a48f1`）**
**v1.13の改訂元：フェーズ別詳細設計書 v1.12 本文（SHA-256=`1ac24cc51cfea60dc0c0efc6a3b7d16dbc1fa54240ededeac2d0b02cf65c0c14`）**
**v1.12の改訂元：フェーズ別詳細設計書 v1.11 本文（SHA-256=`e5d16e688c5473a8aa849101c635f02a13570c361339c98f0519bc3a94cb7184`）**
**v1.11の改訂元：フェーズ別詳細設計書 v1.10 本文（SHA-256=`410ec17d0c834622a91dd9d579f439d3f71999fe4429395c237567c3d7d881a4`）**
**v1.10の改訂元：フェーズ別詳細設計書 v1.9 本文（SHA-256=`be906a69942c30ce5a9b9cb3d559179dab97d7cca768e1f7a9bc2beea6ce7e3f`）**
**v1.9の改訂元：フェーズ別詳細設計書 v1.8 本文（SHA-256=`03a0da32e8c621c4ccdd2c59a9cf1945124b5f779200fbb61f69ea33dbfda2e9`）**
**改訂元：フェーズ別詳細設計書 v1.6（SHA-256=`e44ace7606c4f78eb4c4dabf369c37e3e37680f8dcbad0712676e140f96f8f8e`）**
**統合元Hash：v1.3=`sha256:3fb548800a05156e557accca7d810eaf8653da485bf7674fa7dda1777ec2e40b`／v1.4=`sha256:1f07850e425ae0617465270a4192af627a91b5194f6dc353aff84c7b48345d36`／v1.5=`sha256:1c293bee956892e289ff71913509b9229822216161457e02822fcd83c8189703`**
**本書の性格：設計・実装・試験・Evidence・Runtime GO判定方法を単独完結させた実行正本。GO宣言は同梱Verifierの合格出力からのみ生成する**
**設計判定：v1.6レビュー反映済み。実装・試験開始GO**
**Runtime判定：`BLOCKED_EVIDENCE_MISSING`。実装Repositoryと実行Evidenceが未提供のため、本書作成時点ではRuntime GOを宣言しない**

**v1.8からv1.9への改訂：§23.4（Owner Decision B-1b／D-06／D-1a／E-1 適用。2026年8月16日〜8月17日）**
**v1.9からv1.10への改訂：§23.5（Owner Decision M-Q1〜M-Q4 適用。2026年8月17日）**
**v1.10からv1.11への改訂：§23.6（UI Scope／Denylist 決定 適用。2026年8月18日）**
**v1.11からv1.12への改訂：§23.7（Required Evidence AreaのRelease Scope別分割 適用。2026年8月18日）**
**v1.12からv1.13への改訂：§23.8（Event Sequence観測規約の明記。2026年8月18日）**
**v1.13からv1.14への改訂：§23.9（Event発行層・Subject境界・GC Executor境界の確定。2026年8月20日）**

**v1.6からの主要訂正（詳細は§23.3）**

1. Runtime GO判定をRelease Scope単位へ変更した。v1.6はMVP0-A判定に86 Case全PASSを要求していたが、うち16 Caseは参照仕様Phase専属であり達成不能だった。MVP0-Aの必要数は`registries/`から導出する（v1.8時点で110 Case／31 Test ID／45 Gate）。
2. Verifierをv1.1へ更新した。v1.0はEvidence Fileの内容を検証せず、`required_evidence_areas`の必須集合も検査していなかったため、ダミー1ファイルで`RUNTIME_GO`を取得できた。
3. 件数の正本を`design-source/registries/`へ一元化し、本文・Verifier・Schemaのハードコードを廃止した。
4. Plan決定性検証へ別プロセス三重Buildを追加した。同一プロセス内の二重Buildでは`PYTHONHASHSEED`依存の非決定性を検出できない。
5. CI定義（§16.5）と再認定階層（§26.6）を新設した。
6. Verifier v1.2でFixture／Raw Result／Runner／Commit／Environment／Schema／Migrationの束縛、Case／Gate／Area横断のEvidence一意性、耐久性Tier、Release Manifest生成を追加した。
7. `states.yaml`、`spec/`、`spec-manifest.json`、Shard Builder、新規スレッド開始契約を実ファイル化した。
8. Verifier Source期待HashのCI／Wrapper照合、Protected Branch運用、単一UIDでの限界を明文化した。
9. CIをStatic／Self-test専用とし、Runtime Evidence／Release Manifestの生成を機械的に拒否する境界Checkを追加した。
10. BLOCKED Record Schema、生成・一括検証ツール、復帰Runbookを追加し、同一Taskの無限再試行と仕様変更の未記録復帰を禁止した。

---

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

# 4. MVP0-B：Local Provider Read-only 詳細設計

## 4.1 目的とOperator Surfaceの範囲

MVP0-Aの実行統制を維持したまま、Ollama等のローカルProviderをRead-onlyで利用し、Provider Adapter、Runtime Envelope、Loopback Only、Provider出力隔離を検証する。

### 4.1.1 CLI／Control API／承認UIの関係

§1.2のZ4 Operator Surfaceは**3つの別Surface**である。互いの代替にしない。

| Surface | 実装Phase | 役割 |
|---|---|---|
| CLI | MVP0-A | Operatorが実行・照会・復旧を起動する |
| Control API | MVP1-A以降 | 外部からの制御 |
| **承認UI** | **MVP0-B** | 人がPlan差分を読み、承認・再承認を判断する |

承認UIをMVP0-Bへ置くのは、§0.3が同Phaseを「ローカルLLMで最初に実利用可能となる」
と定めるためである。**MVP0-Aには承認する実作業が無い。** Mock Executionに対する
承認画面の操作時間を測っても、実利用時の値にならない。

**CLIを承認UIとみなさない。** 3つを別Surfaceとして定義した以上、
一方の検証をもう一方の証跡にできない。Z4内の誤操作想定も、
Command入力と画面操作では同じではない。

### 4.1.2 UIが存在しない状態でUI測定値を生成しない

`UI_STATIC_CHECK`、`UI_RENDER`、`HUMAN_MEASUREMENT`をfault pointに持つCaseは、
**承認UIが実装されるまでEvidenceを生成しない。** 代替Surfaceで測った値を
そのCaseの実測値として記録しない。

特に`HUMAN_MEASUREMENT`は人間の操作時間である。実在しない画面に対する操作時間を
つくることは計測ではなく捏造であり、そのEvidenceを根拠にしたGO判定は成立しない。

該当Caseは`phase_scope`から`MVP0-A`を外し、UIが実装されるPhaseで測る（§23.6）。

## 4.2 追加コンポーネント

* Local Provider Adapter
* Provider Capability Discoverer
* Model Identity Resolver
* Loopback Network Enforcer
* Provider Process Supervisor
* Provider Usage Collector
* Output Normalizer
* Provider Health Monitor

## 4.3 Provider Adapter契約

Adapterは共通Portを実装する。

| Operation | 入力 | 出力 |
|---|---|---|
| `discover_capabilities` | Discovery Context | Technical Capability Snapshot |
| `resolve_runtime_spec` | Provider Request、Policy Snapshot | RuntimeEnvelopeSpec |
| `build_invocation_manifest` | Provider Request、Runtime Spec | InvocationManifest |
| `invoke` | Approved Invocation Manifest、Runtime Handle | Provider Raw Result |
| `request_cancel` | Invocation ID／Process Handle | Cancel Result |
| `poll_status` | Invocation ID | Provider Status |
| `collect_usage` | Invocation ID | Usage Record |
| `normalize_output` | Provider Raw Result | Normalized Provider Output |
| `classify_error` | Provider Error | Common Error Classification |
| `attest_effective_runtime` | Runtime Handle | Runtime Attestation |
| `attest_effective_config` | Runtime Handle | Effective Config Attestation |
| `verify_version` | Adapter／Provider Runtime | Adapter Version Verification |
| `enumerate_process_tree` | Runtime Handle | Process Tree Snapshot |

Provider固有構造をDomainへ漏らさない。Raw ResultはArtifact Storeへ隔離し、正規化済み出力だけを後続Actionへ渡す。

Adapterは便利な既定値を信用せず、Provider CLIの実効設定を構成・再取得・照合する。Providerが実効設定を報告できない場合は、SandboxとOS観測で保証できる範囲をAttestationに記録し、必要項目が観測不能なら実行しない。

### Provider Route Policy（v1.20）

Owner Decision `CHAT-3-A`により、経路の可否は`design-source/registries/route-policy.yaml`を正本とする。`masking-policy.yaml`と同じ位置づけであり、`registry_snapshot_hash`の対象である。境界を散文とコードへ直書きしない（不変条件#18）。

`LOCAL_ONLY`／`EXTERNAL_ALLOWED`は`docs/IMPLEMENTATION-ROADMAP-LLM-FIRST.md`にしか無かった。**文書の語はTrust Anchorを持たない。** v1.20でRegistryへ移し、`CONTRACTED_INTERNAL`を加えた3種を正式な語彙とする。

Route Classは「どこまで出てよいか」を決める。State名やProvider自身の申告では決めない。

| Route Class | 外部送信 | Masking Gate | 用途 |
|---|---|---|---|
| `LOCAL_ONLY` | しない | 不要 | Process内・同一Host内で完結する |
| `CONTRACTED_INTERNAL` | しない | **必須** | 契約済みの自社LLM。組織境界を越えない |
| `EXTERNAL_ALLOWED` | する | **必須** | 契約済みの外部Provider。組織境界を越える |

宣言が無いRouteは`LOCAL_ONLY`として扱う。**既定はもっとも狭い経路である。**

#### 許可の条件

allowlistに載っているだけでは送れない。Registryの`enablement_requirements`を**すべて**満たしたときにだけ送る。1つでも欠ければ拒否する。

| 要件 | 内容 |
|---|---|
| `LISTED_IN_ALLOWLIST` | Registryのprovidersに登録されている |
| `EXPLICITLY_ENABLED` | 設定で明示的に有効化されている。既定値による有効化を認めない |
| `CONTRACT_ACTIVE` | 契約状態がACTIVEである。期限切れは拒否 |
| `CAPABILITY_DECLARED` | 要求するCapabilityがそのProviderに載っている |
| `SECRET_RESOLVABLE` | `SecretRef`がKeyringから解決できる |
| `MASKING_GATE_PASSED` | 外部へ出るRouteならMasking Gateを通過している |

「前回送れたから」を根拠にしない。判定根拠は事前に登録された宣言といま解決された経路の照合であり、過去の実行履歴を参照しない。

Providerが「安全である」「契約済みである」と申告しても、それは入力であって判定ではない。判定はRegistryとこちら側の契約記録だけで行う。**Capability申告はallowlistとの照合対象であって、許可の根拠ではない。**

#### Fallback

**一時障害だけ**がFallbackの対象である。もう一度やれば結果が変わりうるものに限る。

| 区分 | 障害 |
|---|---|
| Fallback可能 | `PROVIDER_TIMEOUT`、`PROVIDER_RATE_LIMITED`、`PROVIDER_TEMPORARILY_UNAVAILABLE` |
| Fallback禁止 | `AUTHENTICATION_FAILED`、`POLICY_VIOLATION`、`MASKING_REJECTED`、`MALFORMED_INPUT`、`PROVIDER_NOT_ALLOWED`、`SECRET_UNRESOLVABLE` |

認証失敗、Policy違反、入力不備、Masking Gate拒否では別のProviderへ回さない。**回して隠さない。** 回しても同じ結果になるか、回すこと自体が制御の迂回である。

1回の実行の途中でProviderを差し替えない。判定根拠が混ざる。Fallback先も許可条件をすべて満たさなければならない。委任による委任を作らないのと同じ理由である。判定不能はFallbackではなく`EFFECT_UNKNOWN`で止める（不変条件#9）。

現行Registryに該当Error Codeが無い障害は`error_code: null`とした。**推測で採番しない。** Provider Adapterを実装するTaskで採番する。

### 外部Providerの許可境界（v1.20）

Owner Decision `CHAT-5-C`により、Masking Gate通過を条件に外部Providerを許す。**v1.20で反映するのは契約だけであり、接続実装を行わない。**

MVP0-Aの`ProviderPort`は`provider_id=mock`だけを受け付け、それ以外を`RUNTIME_SPEC_MISMATCH`で拒否する。これは文書上の約束ではなく、**いま効いているProductionの挙動**である。外部Providerを許すには、この拒否を動かす実装Taskが要る。v1.20では動かしていない。

外部Routeは必ずMasking Pipelineを通す（ADR-007）。`MASKING_RESULT`が`REJECTED`のとき送らない。マスクして送り直すこともしない。Secret／Credential／`NATIONAL_ID`／`SPECIAL_CATEGORY_DATA`はマスクせずRejectする（不変条件#21）。合否を決めるのは決定論スキャナであり、LLMはマスクを増やせるが通せない。

### Provider SecretのKeyring束縛（v1.20）

Owner Decision `CHAT-4-A`により、API Key本体を保存しない。OS Keyringへ置き、設定は`SecretRef`だけを持つ（不変条件#7）。

* DB、Repository、会話履歴、Artifact、Evidence、Logへ秘密値を置かない
* 例外とTest Evidenceへも出さない
* Keyringが利用できない場合は**Fail-Closed**で停止する
* **環境変数への暗黙のFallbackを実装しない。** 必要になったら別Decisionとして扱う

`SecretRef`を解決できないことは一時障害ではない。`SECRET_UNRESOLVABLE`はFallback禁止側に置く。

### Chat UIの境界（v1.20）

Owner Decision `CHAT-6-B`により、UIは標準ライブラリだけの最小HTTPと静的HTMLとする。**v1.20で記録するのは境界だけであり、UI実装を行わない。**

* `127.0.0.1`へだけBindする。外部InterfaceへBindしない
* 会話内容とAPI Keyを外部へ送らない
* **UIの表示だけで承認済みと判断しない。** 承認の判定はApproval Managerが単一SQLite TransactionのCASで行う（不変条件#10）。表示は判定ではない
* Chat UIとApproval UIを混同しない。Chat UIへ承認操作を置かない
* Approval Skip相当のFlagをUIへ作らない（不変条件#11）

## 4.4 RuntimeEnvelopeSpec／Runtime Attestation

### Approval前に確定するRuntimeEnvelopeSpec

必須項目：

* Provider Process Executable Absolute Path／SHA-256
* Provider Version、Adapter Version
* argv配列。Shell文字列は禁止
* Model ID、Model DigestまたはLocal File Hash
* Auth Route、Credential Type、Account、Tenant、Billing Identity
* Environment Allowlist
* Working DirectoryとIdentity
* Harness専用Config DirectoryとConfig Tree Hash
* Repository Instruction Policy
* User Config Policy
* Hook／Plugin／MCP AllowlistとConfig Hash
* Provider Session Cache Policy
* Shell Startup Policy
* Filesystem Policy
* Network Policy=`LOOPBACK_ONLY`
* Loopback Port Allowlist
* Sandbox Implementation／Required Assurance
* Process Tree Policy
* Timeout
* CPU／Memory／Disk／Process数上限
* Output Size上限
* Tool Policy=`NONE`または明示Allowlist
* Output Schema Hash
* Secret Reference。値は含めない

Runtime LauncherはEnvironmentを継承せず、空のEnvironmentからAllowlistで新規構成する。`HOME`、XDG Config、Provider Config、Shell RCはHarness専用の空Directoryへ向ける。

### 起動直前のRuntime Attestation

* Executable／Imageの実Hash
* 実argv
* 実cwd／Workspace Identity
* 実User／UID／GID
* 実Environment Keyと非Secret Value Hash
* 実Mount／Filesystem Policy
* 実Network Namespace／Loopback Policy
* 実Tool／Plugin／Hook／MCP状態
* Config Tree Hash
* Sandbox稼働状態
* Process／Container／Job Identity
* Parent／Child Process Policy
* Limit実効値
* Attestation Assurance Level

Specとの不一致、Sandbox未起動、Config Drift、Plugin／Hook／MCP混入、観測不能な必須項目がある場合は、プロセス起動前に`BLOCKED_POLICY`とする。SandboxなしのRetryやEscape Hatchは許可しない。

## 4.5 処理フロー

```text
Technical Capability Discovery
  → Executable／Adapter／Model Identity Resolution
  → Harness専用空Config構築
  → RuntimeEnvelopeSpec確定
  → InvocationManifest確定
  → Plan Content Hash／Execution Plan Hash確定
  → Policy Decision
  → Approval
  → Approval Consume／Claim／Lease
  → Strict Sandbox構築
  → Effective Config／Runtime Attestation
  → Spec完全一致検証
      ├─ 不一致／Sandbox unavailable → BLOCKED_POLICY
      └─ 一致 → Local Provider Invoke
  → Raw Output隔離保存
  → Output Normalize
  → Schema検証
  → Proposed Artifact保存
  → Deterministic Evaluation
  → Human Release
```

ProviderにはWorkspace RootをMountしない。入力が必要な場合は、選択済みContextBundleをRead-only ArtifactとしてRun専用一時領域へ配置する。Providerプロセス開始後にRuntime SpecやConfigを変更しない。

## 4.6 Model Identity

`model_name`だけではなく、可能な範囲で以下を記録する。

* Provider Product／Runtime Version
* Model ID
* Model Digest
* Quantization
* Local Model File Hash
* Template／System Prompt Version
* Context Limit
* Tool Capability
* Structured Output Capability

Digestを取得できない場合は`identity_assurance=PARTIAL`とし、同一結果再現を保証しない。Delivery GateではPartial Identityの許容範囲をPolicy化する。

## 4.7 Security

### Config／Instruction隔離

以下の暗黙読込みを禁止する。

* Repository Instructions、`CLAUDE.md`、`AGENTS.md`等
* User／Global Provider Config
* Hooks、Plugins、Extensions
* MCP Server／Tool定義
* Shell Startup Files
* Provider Session Cache
* Environment Proxy
* Credential Helper
* Editor／IDE連携設定

AdapterはProvider固有のStrict／Bare Modeを利用しつつ、Harness側でも実効設定を再照合する。CLI Optionだけを安全性の根拠にしない。

### Sandbox

* Sandbox unavailable時はFail-Closed
* Unsandboxed Retry／Escape Hatch禁止
* Loopback以外のBind／Connectを拒否
* Unix Domain Socket利用時はPath、Owner、Peer Identityを固定
* Providerプロセスは専用OS User、Container、Namespace等の隔離Identityで実行
* Workspace、Secret Store、Ledger DB、Approval Storeへアクセスさせない
* Context ArtifactだけRead-only
* Shell Tool、File Tool、Plugin、Hook、MCPは初期段階で無効
* Child Processは原則禁止。必要な場合はExecutable Hash Allowlist
* CPU、Memory、Disk、Process数、Output Size、Wall Clock上限
* Providerログは機密除外Filterを通す
* Providerが返すPath、Command、URLはUntrusted文字列として扱う

### Process Tree

Cancel／終了判定ではRoot PIDだけでなく、Process Group、cgroup、Container、Job等でProcess Tree全体を管理する。PID再利用対策としてStart Time、Process Handle、Runtime IDを照合する。

## 4.8 Cancellation

1. AdapterのCancel APIまたはRuntime Supervisorへ取消要求。
2. 新規Child Process生成を禁止。
3. 指定Grace PeriodでProcess Tree全体の終了を待つ。
4. 終了しない場合はSandbox境界単位で強制終了。
5. PID再利用対策としてStart Time／Handle／Runtime IDを照合。
6. Network Socket、Temp Artifact、Session Cache、Child Processを再走査。
7. Process TreeとProvider処理が終了確認できた場合だけ`CANCEL_CONFIRMED`。
8. 状態が確認不能なら`CANCEL_UNKNOWN`。

Local Processでも、生成済みArtifactや後続Effectの有無を確認せずにRunを再開しない。Sandbox自体が異常終了し、Process Treeを証明できない場合も`CANCEL_UNKNOWN`とする。

## 4.9 エラー処理

* Model未配置：`CAPABILITY_UNAVAILABLE`
* Digest不一致：`VALIDATION_ERROR`
* Loopback Policy適用不能：`POLICY_DENIED`
* Context Limit超過：Plan段階で停止
* Provider Timeout：停止確認後、新Attemptを許可
* Output Schema不正：再プロンプトは最大1回、同じAttempt内ではなく新Attempt
* Process Crash：Raw Outputを採用しない
* Output Size超過：Process停止、Artifact隔離、Action失敗

## 4.10 受入Gate

* MVP0-A Gateを維持
* `AT-SANDBOX-001`：Sandbox unavailable時に起動0件
* `AT-CONFIG-001`：Hook、Plugin、MCP、User Config、Repository Instruction混入を検出し停止
* RuntimeEnvelopeSpecがApproval前に固定
* Runtime Attestation不一致時のProvider起動0件
* Executable Hash変更時Approval再利用0件
* Loopback外通信0件
* Workspace直接書込み0件
* Provider ProcessからLedger／Secret／Approval Storeへのアクセス0件
* Harness専用空Config以外の読込み0件
* Model Identity記録率100%
* Output Schema不正時の後続Effect 0件
* Cancel確認不能Runの完了0件
* Provider停止後の孤児Process 0件
* Unsandboxed Retry 0件
* Adapter契約試験合格
* Mock AdapterとLocal AdapterのDomain差分0件


## 4.11 Chat基盤のCore Schema（v1.21）

Owner Decision `CHAT-1-A` と `CC-1`〜`CC-16` により、会話を正本の Core Schemaとして持つ。**Chatは MVP0-B に属する**（`CC-14-A`）。MVP0-A の Case数・Gate数・Runtime GO条件は動かさない（`CC-15-A`）。

| Schema | Version | Envelope に足すField |
|---|---|---|
| `Conversation` | `1.0.0` | `conversation_id`、`conversation_hash` |
| `ConversationMessage` | `1.0.0` | `message_id`、`conversation_id`、`role`、`sequence_number` |
| `ConversationSnapshot` | `1.0.0` | `snapshot_id`、`conversation_id`、`snapshot_hash`、`message_set_hash`、`schema_set_hash`、`design_sha256` |

`sequence_number` は §1.3 の共通識別子をそのまま使う。**別の順序Fieldを作らない。**

### 持たせないField

| Field | 持たせない理由 |
|---|---|
| `Conversation.message_count` | 件数の正本が2か所になる。件数はMessage集合から導く（不変条件#18） |
| `ConversationMessage.token_count` | Token会計は`TokenBudgetPolicy`と`TokenProfileSnapshot`が持つ |
| `Conversation.message_ids` | Message追加のたびに親Recordを書き換えることになり、Append-onlyと両立しない |

### 親子関係とAppend-only

`ConversationMessage`が`conversation_id`で親を指す。**子→親の単方向である**（`CC-9-A`）。親は子を列挙しない。

`Conversation`・`ConversationMessage`・`ConversationSnapshot`はすべてAppend-onlyである（`CC-10-B`）。既存RecordをUPDATE／DELETEしない。訂正はCompensating EventのAppendだけで行う（不変条件#1）。

### roleの語彙

`role`は§1.16.4のControl／Data Message Roleをそのまま使う。`ContextFragment`の`message_role`と同じ7値である。**Chat用に`user`／`assistant`のような別語彙を作らない。**

Provider出力は`UNTRUSTED_PROVIDER_DATA`であり、本文に命令形式が含まれていてもControl Roleへ昇格しない。この境界はChatでも同じである。

### Hashの相互関係

既存§1.11のHash Profileを再利用する（`CC-12-A`）。新しいProfileを作らない。

```text
content_hash        … 1件のRecordの内容Hash。Envelopeが持つ
conversation_hash   … Conversationの識別内容のHash
message_set_hash    … 各ConversationMessageのcontent_hashを
                      sequence_number昇順で並べた配列のHash
snapshot_hash       … message_set_hash、schema_set_hash、design_sha256、
                      conversation_hash を束ねたHash
```

規則は次で固定する。

* ID、時刻、PID、Filesystem列挙順をHash入力へ入れない（不変条件#4）
* **Hash自身をHash入力へ含めない。** `snapshot_hash`は自分を入力に取らない
* 同じMessage集合からは同じ`message_set_hash`が得られる
* `PYTHONHASHSEED`とFilesystem列挙順に依存しない（不変条件#6）
* `schema_set_hash`は**実際に使用したSchema Version**だけを含める（`CC-13-A`）。未使用Schemaを足しても`ConversationSnapshot`の`schema_set_hash`は動かない

`ConversationSnapshot`は`conversation_id`を持つ。どのConversationのSnapshotかを**Evidence単体で検証できる**ようにするためである（`CC-7-C`）。

### IDの生成

`conversation_id`・`message_id`・`snapshot_id`はUUIDv4をPort経由で採る（`CC-8-A`）。時刻とPIDを含めない。**Hash入力へは入れない。**

### 本文の保存とArtifact CAS参照（v1.22）

Owner Decision `CMC-1-A`／`CMC-2-A`により、会話本文はArtifact CASへ置き、`ConversationMessage`は`content_artifact_hash`で参照する。

`ConversationMessage@2.0.0`は`content_artifact_hash`をRequiredで持つ。§15.2によりRequired Field追加はMajorであるため、`1.0.0`を上書きせず版を足した（`CMC-6-A`）。`1.0.0`はread_onlyで残る。

* **本文をinlineで持つFieldを作らない。** `content`／`text`／`body`を追加しない
* `content_artifact_hash`は`ContextFragment@2.0.0`と同名・同義である
* 形式は`^sha256:[0-9a-f]{64}$`
* 参照先はArtifact CASであり、`ArtifactManifest`で解決する
* **参照先が存在しないMessageを受理しない。** 保存前に存在を確認する

Bytesの書込みはTemp write → File fsync → Atomic Rename → Directory fsync → Manifest登録の順にする（不変条件#13）。

#### Hashの役割分担（v1.22）

3つのHashを混同しない。

| Hash | 何のHashか | 動くとき |
|---|---|---|
| `content_artifact_hash` | 本文Bytesそのもの | 本文が変わったとき |
| `content_hash`（Message） | 本文Hash・`role`・`sequence_number`を束ねた値 | 本文・役割・順序のどれかが変わったとき |
| `message_set_hash` | 各Messageの`content_hash`をsequence_number昇順で並べた配列 | Message集合が変わったとき |

`CMC-3-A`により、Messageの`content_hash`は次の3つから導出する。

* 本文のArtifact CAS内容Hash（`content_artifact_hash`）
* `role`
* `sequence_number`

規則は次で固定する。

* `message_id`、`conversation_id`、時刻、PIDを`content_hash`へ含めない（不変条件#4）
* **Hash自身をHash入力へ含めない**
* 同じ本文・`role`・`sequence_number`から同じ`content_hash`が得られる
* `role`の差し替えを検出する
* `sequence_number`の差し替えを検出する
* `PYTHONHASHSEED`とFilesystem列挙順に依存しない（不変条件#6）

#### Conversation Hash（v1.22）

`CMC-4-A`により、`conversation_hash`は`conversation_id`と作成時の不変メタデータだけから導出する。

**Message追加でConversation Recordを書き換えない。** Message集合の完全性は`ConversationSnapshot`の`message_set_hash`で確認する。`conversation_hash`と`message_set_hash`は別物であり、片方でもう片方を代用しない。

#### 保存境界（v1.22）

`CMC-5-A`により、保存にも送信前と同じ境界を当てる。**これはProvider送信前のMasking Gateとは別の境界である。**

* 保存前に決定論的Scannerを実行する
* Secret／Credential／`NATIONAL_ID`／`SPECIAL_CATEGORY_DATA`は**保存を拒否**する
* Reject対象を**マスクして保存しない**（不変条件#21）
* Rejectされた本文をArtifact CASへ書かない
* Rejectされた本文をSQLite、Event Ledger、Evidence、Logへ書かない
* **Scanner未実行の本文を保存しない。** 判定不能はFail-Closedで止める

合否を決めるのは決定論スキャナである。LLMはマスクを増やせるが通せない（ADR-007）。

#### roleの制約（v1.22）

`role`は§1.16.4の7値だけを許す。正本に無いrole値を追加しない。

Provider出力は`UNTRUSTED_PROVIDER_DATA`であり、本文に命令形式が含まれていてもControl Roleへ昇格しない。

### Chat Context 選択の境界（v1.23）

Owner Decision `CP-2-A`／`CP-3-B`／`CP-4-A` を反映する。

#### mandatory にできるRole

`mandatory` にできるのは §1.16.4 のControl Role、すなわち`SYSTEM_CONTROL`と`DEVELOPER_CONTROL`だけである（`CP-2-A`）。

Untrusted な入力を必須Contextにできると、予算が足りないときにUntrusted側が優先されてControl側が落ちうる。**Provider出力を必須Contextへ昇格させない。**

この制限はChatの境界で行い、`select_context`は変更しない。Domainを変えるとMVP0-Aの他の呼出側まで挙動が変わる。

**現時点でChatはmandatoryなMessageを作れない。** §1.16.4はControl Roleへの昇格を「Z0 Control Planeが生成し、署名、Policy Hash、Issuer、Expiryを検証できるArtifact」だけに許す。Chatにその検証経路が無いため、Control RoleのMessageは`CONTROL_DATA_ROLE_ESCALATION`で拒否される。権限を捏造して回避しない。検証経路ができるまでこの状態は変わらない。

#### 未設定Messageの扱い

`priority`または`mandatory`が未設定のMessageが1件でもあれば、**Chat送信全体を拒否する**（`CP-3-B`）。

* 黙ってContext選択の対象から外さない
* 既定値で補完しない
* 部分送信、Fallback、Queue投入を行わない

`priority`はRoleから解決する。写像の正本は`design-source/registries/chat-context-policy.yaml`である（次項）。`mandatory`は呼出側がMessageごとに明示する。**どちらも既定値を持たない。**

#### decision_hashの意味

`decision_hash`は**選択判断の再現性**を表す（`CP-4-A`）。`receipt_id`と`bundle_id`を入力に取らない。取ると同じ判断でもReceiptごとに値が変わり、「同じ入力から同じ判断が出たか」をHashで照合できなくなる。

Receipt個体の識別はRecordの`content_hash`が担う。§1.11.1の二層分離と同じ考え方である。

| Hash | 表すもの |
|---|---|
| `decision_hash` | 選択判断。Candidate、Selected、Excluded＋理由、Algorithm Version |
| Recordの`content_hash` | 保存Recordの個体。`record_id`・`created_at`・`producer`を含む |

Hash Profileと Canonicalization は変更しない。既存 §1.11 の規約をそのまま使う。

### Chat Context Policyの正本（v1.24）

Owner Decision `CP-1-A`／`CPM-1-A`／`CPM-2-A`／`CPM-3-A` を反映する。

#### 置き場所

Chat用のContext Policyは`design-source/registries/chat-context-policy.yaml`を正本とする（`CPM-1-A`）。`route-policy.yaml`と同じ位置づけであり、`registry_snapshot_hash`の対象である。Role別のpriorityと選択順序を散文とコードへ直書きしない（不変条件#18）。

`src/harness/application/_chat_context_policy_generated.py`はこのRegistryの生成物である。生成器は`tools/generate_chat_context_policy_code.py`であり、`--check`が再生成漏れを検出する。**Application層へ置く。** §3.6の`ContextFragment`は「Domainで推測して写像を作らない」と定めており、Chatの写像はChatという呼出側の関心である。MVP0-Aの他の呼出側と共有しない。

#### Roleとpriorityの写像

`CPM-2-A`により、priorityは**信頼境界の3段**で決める。区分は§1.16.4と`_CONTROL_ROLES`／`_UNTRUSTED_ROLES`からそのまま導ける。新しい語彙も区分も作らない。

| 段 | 属するRole | mandatoryの必要条件 |
|---|---|---|
| Control | §1.16.4のControl Role | 満たす |
| 中間 | Control でもUntrustedでもないRole | 満たさない |
| Untrusted | §3.6 手順2の昇格拒否対象 | 満たさない |

**具体値は本文へ書かない。** 3段が区別できることだけが要件であり（`CPM-2-A`）、値の正本はRegistryである。§3.6の9段階へRoleを個別に割り当てる案（`CPM-2-C`）は選ばれていないため、9段階との写像を作らない。

7 Roleを漏れなく1回ずつ写像する。正本に無いrole値はFail-Closedで拒否し、既定の段へ落とさない。

#### 段が同じMessageの順序

`CPM-3-A`により、段が同じMessageどうしは`sequence_number`降順で優先する。予算が足りないときに落ちるのは**古い発話**である。

段が第1キー、`sequence_number`が第2キーである。**新しいUntrustedな発話が、古い中間RoleのMessageを押しのけない。**

`fragment_id`昇順は決定論ではあるが会話の新しさと無関係であり、**会話の優先順位として使わない**（`CPM-3-C`は選ばれていない）。

`ContextFragment.priority`は整数1つであるため、段と`sequence_number`の2段階を1つの全順序へ写す。写した値はMessageごとに相異なり、`fragment_id`による同点崩しは起きない。この整数は`bundle_hash`にも`decision_hash`にも入らない（どちらの射影も`priority`を含まない）。順位だけが意味を持つ。

`select_context`は変更しない。DomainのTie-breakerとAlgorithm Versionは動かない。

#### 送信列は選択順ではない

選択は新しい順に行い、Providerへ渡す最終Message列は`sequence_number`**昇順**へ戻す（`final_message_order`）。降順のまま送ると会話が逆さになる。

`ContextBundle.ordered_fragment_ids`は**選択順**である。混同しない。

#### mandatoryは段だけでは決まらない

段は`mandatory`の**必要条件であって十分条件ではない**。§1.16.4はControl Roleへの昇格を「Z0 Control Planeが生成し、署名、Policy Hash、Issuer、Expiryを検証できるArtifact」だけに許す。この4件を検証したArtifactが無い限り、Control Roleであっても`mandatory=true`にしない。

Chat経路にその検証経路は無い（`control_authority_grant: NONE`）。**Roleだけを根拠にControl Authorityを付与しない。Role変換による権限昇格を禁止する。**Provider出力は常に`UNTRUSTED_PROVIDER_DATA`のままである。

#### 未解決の扱い

`priority`がRoleから解決できない、または`mandatory`が未指定のMessageが1件でもあれば、**Chat送信全体を拒否する**（`CP-3-B`）。Registryに既定値を置く案（`CP-3-C`）は選ばれていない。

判定はArtifactを読む**前**に全Messageへ通す。1件でも未解決なら本文を1件も読まずに止まる。Provider呼出しや外部送信より前である。

### この版で実装しないもの

本節は契約である。Conversationの保存、Context再構築、Provider接続、Router、Chat API／CLI、UIをv1.21では実装しない。Chat専用のRelease／Gate Evidence領域は正本に定義が無いため、必要になった時点で別Decisionとして起票する。

---

---

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

# 9. MVP1-D：Controlled Paid Execution 詳細設計

## 9.1 目的

Provider利用料、Token、Credit、日次・月次上限を事前予約し、実績を照合して、上限不明または超過状態での新規有償Actionを防止する。

## 9.2 追加コンポーネント

* Pricing Catalog
* Budget Manager
* Cost Estimator
* Budget Reservation Store
* Cost Approval Manager
* Usage Collector
* Cost Reconciler
* Budget Circuit Breaker

## 9.3 Budget階層

```text
Organization Budget
  └─ Project Budget
      └─ Daily / Monthly Budget
          └─ Run Budget
              └─ Action Budget Reservation
```

上位Budgetを下位が超えない。Currencyが異なる場合は換算時刻とRate Sourceを固定し、換算不明時は実行しない。

## 9.4 Cost Estimate

Estimate入力：

* Provider／Model
* Input Token
* Reserved Output Token
* Cached Input／Output条件
* Tool／Image／Audio等のUnit
* Request回数
* Retry／Fallback上限
* Pricing Catalog Version
* Tax／Currency取扱い
* Safety Margin

`estimated_upper_bound`は期待値ではなく、許可範囲内の最大消費を保守的に算出する。価格取得不能、段階課金不明、契約割引不明の場合は、明示された保守上限またはHuman設定額を使用する。

## 9.5 Reservation Protocol／Crash Recovery

`BudgetReservation`はEffect Protocolを適用する金銭拘束Actionとして扱う。

### 状態

```text
REQUESTED
  → PREPARED_DURABLE
  → RESERVED
  → CONSUMING
  → PENDING_RECONCILIATION
  → SETTLED
  → RELEASED

任意状態
  → EXPIRED
  → EFFECT_CONFLICT
  → STATUS_UNKNOWN
```

### 必須項目

* Reservation ID
* Effect ID
* Organization／Project／Period／Run／Action Scope
* Provider、Model
* Auth Route、Account、Tenant、Billing Identity
* Pricing Catalog Hash
* Entitlement Snapshot ID
* Currency
* Estimated Upper Bound
* Safety Margin
* Reserved Amount
* Consumed Amount
* Outstanding Amount
* Fencing Token
* Expires At
* State
* Ledger Event Sequence
* Provider Usage Reference
* Reconciliation Status

### Protocol

1. Budget Snapshotを取得。
2. Action Upper Boundを計算。
3. Organization、Project、Period、Run、Actionの全残高を検証。
4. Reservation `OperationJournal(operation_type=BUDGET_RESERVATION)`を`PREPARED_DURABLE`として保存し、`BUDGET_RESERVATION_PREPARED`と`ACTION_PREPARED`をAppendする。
5. 単一TransactionのCASで残高を拘束し、`RESERVED`へ遷移する。Storage観測後に`BUDGET_RESERVED`、`EFFECT_OBSERVED`、Reservation用`EffectReceipt`を保存する。
6. Reservation ID、Effect ID、Pricing／Entitlement／Auth情報をExecution PlanとAttemptへ束縛。
7. 実行開始前にReservation、Approval、Fencing Tokenを再検証。
8. 実行中はReservationを`CONSUMING`へ。
9. 実行完了後、Actual Usageで精算。
10. 未使用分を返却。
11. Usage不明分は`PENDING_RECONCILIATION`として保守額を保持。
12. Expiryまでに開始しないReservationは、実行不存在を確認して解放。

### Recovery

* Reservation Storeが成功、Ledger EventまたはEffectReceiptが欠落：Reservation、OperationJournal、Budget残高を照合しCompensating Event／ReceiptをAppend・補完。
* Ledger Eventが存在、Reservationが欠落：自動再予約せず`STATUS_UNKNOWN`、新規有償Action停止。
* Provider実行開始後にReservation状態不明：Upper Bound全額を保留。
* 二重Reservation要求：`UNIQUE(effect_id)`とCASで拒否。
* Stale Worker精算：Fencing Token不一致で拒否。
* 孤児Reservation：Remote Invocation Registry、Attempt状態、Provider Usageを照合し、解放・保留・人間確認を決定。
* 複数Worker段階ではBudget ScopeをResource Lock対象にし、ClaimとReservationを同一Scheduling Transactionへ接続。

## 9.6 Runtime Control

* Soft Limit到達：警告Event、Output Token削減または新規Attempt停止。
* Hard Limit接近：Provider Cancel Request。
* Hard Limit超過検知：Circuit Open、以降の有償Action停止。
* Providerが実時間Usageを返さない場合：事前Upper Boundを全額予約。
* Streamingでは受信Token／Elapsed Unitを監視。
* Provider Pricing変更時：Catalog Version差でApproval無効化。
* Auto Top-upが有効なAccountはPolicy Denied。

## 9.7 Cost Approval

Approvalに表示：

* Provider／Model
* Pricing Catalog Version
* Input／Output上限
* Expected Upper Bound
* Run／日次／月次残高
* Currency／Tax
* Fallback最大回数と総Upper Bound
* Additional Charge有無
* Expiry
* Budget Owner

Paid Executionは金額やRisk Levelにかかわらず、Enterprise SoD Gate合格までは`HUMAN_APPROVAL_ONLY`とする。Policy Approvalは同Gate合格後に限り、Budget OwnerとPolicy Administratorの分離、署名Rule Set、Provider／Model／Account／Currency／Upper Boundの固定、短いExpiry、Revocation、監査証跡、`AT-POLICY-APPROVAL-001`合格を条件に明示解禁できる。

## 9.8 Reconciliation

`CostReconciliationReceipt`：

* Reservation ID
* Effect ID
* Provider Usage Record ID
* Provider／Model
* Auth Route、Account、Tenant、Billing Identity
* Pricing Catalog Hash
* Local Estimate
* Provider Actual
* Currency
* Difference
* Reconciliation Status
* Retrieved At
* Source Assurance
* Outstanding Amount
* Finalized At
* Fencing Token
* Receipt Hash

差異がThresholdを超えた場合はFinding、Budget保留、次Action停止。Provider明細が遅延する場合は`PENDING_RECONCILIATION`とし、保守額を保持する。

Reconciliation Receipt保存とReservation State更新は同一Transaction、または回復可能なJournalで接続する。Receipt欠落、Pricing変更、Usage Source不明、Account Scope不一致は自動精算しない。

## 9.9 受入Gate

* Upper Bound不明実行0件
* Hard Limit超過状態の新規Action 0件
* Usage取得不能後の新規有償Action 0件
* Provider／Model／Auth Route／Account／価格変更時Approval再利用0件
* Auto Top-up有効Account実行0件
* Run／日次／月次上限の同時適用
* `AT-BUDGET-001`：Reservation二重消費、Usage不明、価格変更、孤児Reservationを検出
* Reservation Store／Ledger片側障害から復旧
* Stale Fencing Tokenによる精算0件
* Expired Reservationを実行不存在確認後に解放
* Fallback込み総上限を予約
* Provider実績との差異を監査可能
* `PENDING_RECONCILIATION`中の新規有償ActionをPolicy通り停止


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

# 16. 実装計画・バイブコーディング運用

## 16.1 実装順序

### Step 0：統合正本とSpec Linter

本書、Registry、生成specの参照整合を検査する`tools/lint_spec.py`を最初に実装する。

検査対象：

* State、Event、Error Code、Schema、Test ID、Case ID、Gate IDの一意性
* Gateから存在しないTestへの参照
* TestのSubject／State Enum不一致
* Error Code／Classification不一致
* Test件数、Case件数、README件数、Gate Report件数の一致
* Plan Content Field台帳外Field
* 参照仕様Feature Flagの誤解禁
* 生成specのSource Hash一致

### Step 1：Test Manifest Validator

Manifest Schema、Hash列、Subject／State整合、Error Registry、Evidence条件を検査する。

### Step 2：技術スパイク

| # | 対象 | 合格条件 |
|---|---|---|
| 1 | RFC 8785 JCS＋SHA-256 | 参照Vector一致。数値、NFC、重複Key、不正Surrogateを固定 |
| 2 | SQLite WAL＋FULL | `PREPARED_DURABLE`直後の`os._exit(137)`から一貫復旧 |
| 3 | `openat2`＋`RESOLVE_*` | Symlink、Mount、特殊Fileを拒否。Fallbackも同結果 |
| 4 | SQLite authorizer＋Trigger | 全Runtime接続でLedger UPDATE／DELETE拒否 |
| 5 | WSL2 Mount検査 | Linux側FSと`/mnt/c`をMount情報で識別 |
| 6 | Faultable I/O | ENOSPC／EIOをArtifact、SQLite境界へ実注入可能 |
| 7 | Ed25519／Key Permission | 署名、検証、0600検査、改変検知 |

成立しなかった前提を記録し、本書とADRを修正してから次へ進む。

### Step 3：Canonical／Schema／Migration

Canonical JSON、Hash、Clock／ID Port、Core Schema全件、Cross-reference Validator、SQLite Migration、Backup／Restoreを実装する。

### Step 4：最小垂直スライス

```text
TASK_LOAD → CONTEXT_BUILD → MOCK_INFERENCE
→ LOCAL_FILE_COMMIT → DETERMINISTIC_EVALUATION → HUMAN_RELEASE
```

最小契約Setは次の11 Schemaとする。

```text
EventEnvelope, Run, ActionAttempt, ExecutionPlan, ApprovalGrant,
Lease, OperationJournal, EffectReceipt, ArtifactManifest,
InputReadCapability, RuntimeAttestation
```

Fault Pointは`AFTER_ACTION_PREPARED`と`AFTER_EXECUTION_ATTEMPTED`から開始するが、MVP0-A Gate前に全10点とI/O Faultを網羅する。

### Step 5：MVP0-A全Gate

全Core Schema、Migration、Crash Matrix、Input Corpus、Approval UX、Backup／Restore、Drain、Performanceを実行する。

### Step 6以降

```text
MVP0-B → MVP1-A → MVP0-C → MVP1-D
```

## 16.2 AI支援実装の不変条件

AIには現在のTaskに必要なspec shardとRegistryだけを渡す。設計書全体を毎回Promptへ投入しない。ただし生成前後にSpec Lintを必ず実行する。

| 散文規約 | 機械強制 |
|---|---|
| Ledger UPDATE／DELETE禁止 | Authorizer＋Trigger＋静的検査 |
| Effect前に`PREPARED_DURABLE` | Effect Executor入口Assertion |
| Fence最終Storage再検証 | Storage Adapter必須引数 |
| Plan非決定値禁止 | Frozen Input＋二重Build＋Field Registry |
| Secret非記録 | `SecretRef`型＋allowlist serializer＋Canary Scan |
| `shell=True`禁止 | Launcher型＋AST検査 |
| Windows FS禁止 | Mount検査 |
| Approval bypass禁止 | CLI Schema＋AST／全文検索 |

## 16.3 反復ループ

* 1 Taskあたり8反復上限。
* 直近2反復で試験結果が改善しない場合は停止。
* 各反復前にCommitし、失敗時は直前Commitへ戻す。
* 試験を削除、Skip、期待値緩和してPASSさせる変更を禁止。
* AI生成Codeの採用条件は、Spec Lint、型検査、Unit、関連Integration、Security Checkの全合格。

## 16.4 推定作業量

MVP0-Aは15〜22週相当という推測を初期値とし、Step 2～4の実測後に再ベースラインする。Schema／CRUD／CLI雛形はAI短縮が効く一方、Filesystem、安全なI/O、クラッシュ整合、Fault Injection、並行CASの人間検証は短縮しにくい。

この見積りには次が含まれていなかった。v1.8で明示し、再ベースライン時に計上する。

| 未計上項目 | 内容 |
|---|---|
| Spec Foundation | Registry 6種、Spec Linter、Manifest Validator、Shard Builder、Registry Snapshot Builder |
| Schema Fixture | Core Schema（v1.8時点22件）× Valid／境界／拒否の各1件以上＝60件以上の手作りFixtureとUpcaster |
| Runbook | §15.10の19 Runbook。各Ownerと禁止操作を持つ |
| Verifier自己試験 | `AT-VERIFIER-*`とEvidence生成 |
| 耐久性Tier 2 | ループバックext4＋Cache Drop試験環境の構築 |

## 16.5 CI／継続的検証

CIは「AIが生成したCodeが不変条件を破っていないこと」を人間のレビュー前に落とす層である。§16.2の機械強制を実際に走らせる場所であり、定義がなければ§16.2は文章に留まる。

### Trigger別の実行内容

| Trigger | 実行内容 | 所要目安 |
|---|---|---|
| pre-commit（ローカル） | 整形、`ruff`、`mypy --strict`、Spec Lint、変更Package のUnit | 30秒以内 |
| Pull Request／全Push | 上記＋全Unit＋Property＋Schema Suite＋SQLite Integration＋Security静的検査＋Manifest Validator＋`AT-VERIFIER-*` | 10分以内 |
| main への統合後（nightly） | 上記＋Filesystem Corpus＋Fault Injection 10点＋I/O Fault＋Recovery＋Migration＋Backup／Restore＋Drain＋GC | 60分以内 |
| Release Gate（手動起動） | §26.3の全工程＋Performance 30計測＋Approval UX＋Secret Canary＋Evidence生成＋Runtime GO Verifier | 半日〜 |

Python Matrixは3.11／3.12。CI RunnerはWSL2ではないため、**CIのPASSはRuntime GOを構成しない。** §26.4のRuntime Environment要件を満たすのはWSL2上のRelease Gate実行だけである。CI結果とRuntime Evidenceを同一視しない。

CIとRelease Gateの責務を機械的に分離する。CIでは`tools/check_ci_runtime_boundary.py --ci`を必須Checkとして実行し、CI WorkspaceにRuntime Evidenceまたは`runtime-go-release-manifest.json`が生成された場合はFailとする。このCheckはWSL2適合性を証明するものではなく、CIの静的検証結果をRuntime Evidenceへ誤流用しないための境界である。Release Manifestを生成できるのは、§26.3のWSL2 Release Gateだけである。

Verifier Sourceは`ci/verifier-source.sha256`と照合し、CI／Wrapperで不一致を拒否する。Release CIでは保護変数`RUNTIME_GO_VERIFIER_SHA256`を`--trusted-hash`へ渡し、リポジトリ内Hashと外部Trust Anchorを二重照合する。期待Hash自体を同一UIDで変更できるため、これは暗号学的な信頼根ではない。Protected Branch、Required Check、変更者と承認者の分離、Tagまたは外部AttestationへのHash転記を運用上必須とし、それが無い場合は`UNTRUSTED_REVIEW_ONLY`としてRuntime GOを宣言しない。

### 必須Check

```text
spec-lint              : tools/lint_spec.py が終了Code 0
manifest-validate      : tools/validate_test_manifest.py が終了Code 0
registry-snapshot-sync : 再生成したsnapshotがCommit済みsnapshotと一致
design-generation      : レビュー済み正本、Snapshot、Spec ManifestのHash一致
verifier-source        : ci/verifier-source.sha256との照合が終了Code 0
runtime-go-boundary    : CIにRuntime Evidence／Release Manifestが存在しない
blocked-record-contract: OPEN／RESOLVED RecordのSchema・Hash・重複を検査
verifier-self-test     : AT-VERIFIER-* 全PASS
type-check             : mypy --strict が Domain／Application層で終了Code 0
forbidden-pattern      : shell=True、sqlite3.connect直接利用、Approval Skip Flag、
                         Planner内のset反復 の全文／AST検査で0件
license-and-sbom       : 依存Licenseのallowlist適合、SBOM生成、既知脆弱性0件（High以上）
```

### 依存物の保守

* Lock Fileを正本とし、Version Pinを必須とする。
* 依存更新は月次でまとめ、更新PRでは必ずnightly相当の試験を実行する。
* 依存更新は§26.6の`TIER_2`再認定を要求する。Lock File変更はRuntime GOを失効させる。
* `pip-audit`等でHigh以上の既知脆弱性が出た場合、Feature Flagを縮退させたうえで修正を優先する。

### 試験の扱い

* Coverage目標はDomain層100%、Application層90%、Infrastructure層は契約試験で代替する。
* Coverage低下を伴うPRはCIで拒否する。
* **試験の削除、Skip追加、期待値緩和によるGreen化を禁止する。** CIはSkip数とXFail数を出力し、増加をFailとする。

# 17. 段階別成果物一覧

| 段階 | 必須成果物 |
|---|---|
| Spec Foundation | 統合正本、Registry、Spec Linter、生成Manifest |
| Spike | 7スパイク結果、失敗前提一覧、ADR更新 |
| MVP0-A | Domain Model、Core Schema一式、Migration、CLI、Mock、Recovery、Fault Matrix、Golden Corpus、Backup／Restore、Drain Runbook、Gate Report |
| MVP0-B | Ollama等Local Adapter、Sandbox、Cancellation、Model Identity Evidence |
| MVP1-A | ChangeSet、Ephemeral Worktree、Test Runner、Commit／Rollback Evidence |
| MVP0-C | External Adapter、Data Gate、Remote Registry、Uncertain Queue |
| MVP1-D | Budget Reservation、Cost Receipt、Reconciliation |
| 公開 | MIT LICENSE、Security／Threat Model、検証状況表、Contribution Guide、Responsible Disclosure |

参照仕様フェーズの成果物は実装しない。実装を開始する場合はスコープADRを更新し、該当Schema、Gate、Runbookを実装対象へ戻す。

# 18. Gate Decision Template

各Delivery Gateで次を記録する。

```text
Gate ID:
Target Phase:
Target Version:
Git Commit:
Schema Set Hash:
Policy Version:
Test Environment:
Passed Tests:
Failed Tests:
Open Findings:
Residual Risks:
Rollback Procedure:
Operational Runbook:
Security Review:
Privacy / Legal Review:
Approvers:
Decision:
Valid Until:
Capabilities Enabled:
Capabilities Still Disabled:
```

`Decision=GO`でも、`Capabilities Still Disabled`を明示し、Runtime Policyで未解禁機能を拒否する。

---

# 19. 要求トレーサビリティ

| v3.2能力 | 本書設計 | 能力の扱い |
|---|---|---|
| Token上限内Context | 3.6 | 維持・詳細化 |
| ApprovalとPlan Content／Execution Authority Hash束縛 | 3.7、3.8 | 維持・詳細化 |
| Event Ledger／Recovery | 1.4、3.9、3.10、15.4 | 維持・詳細化 |
| Local Provider | 4 | 維持・詳細化 |
| External Provider | 5 | 維持・詳細化 |
| Provider生成変更 | 6 | 直接書込みを禁止し能力を維持 |
| Session Resume | 7 | 正本化せず能力を維持 |
| Routing／Fallback | 8 | 暗黙切替を禁止し能力を維持 |
| Paid Execution | 9 | 上限不明課金を禁止し能力を維持 |
| External Effect | 10 | 未承認送信を禁止し能力を維持 |
| Windows／WSL | 11 | Raw Path方式を禁止し能力を維持 |
| Multi-worker | 12 | 無調整並列を禁止し能力を維持 |
| Blind Reviewer | 13 | Release権限を禁止し能力を維持 |
| Enterprise | 14 | 本番統制を追加 |

上位仕様の能力は履歴・参照仕様として保持する。ただし本実装の初期スコープは5フェーズであり、参照仕様能力はFeature Flag OFFのままRuntime提供しない。

## 19.1 規範受入試験Manifest

`TestCaseManifest`は1行1決定結果を原則とし、Fault Point、観測条件、期待対象、期待状態が異なる場合は`case_id`を分ける。件数は`registries/tests.yaml`から自動生成し、本文へ手入力しない。

必須Field：

```text
test_id
case_id
scenario
expectation_descriptor_hash
input_fixture_hash
expected_event_sequence
expected_subject_type
expected_subject_id
expected_state
expected_error_code
trace_scope
assertions
auto_reexecution_prohibited
release_allowed
manual_queue_expected
fault_point
phase_scope
durability_tier
evidence_status
evidence_manifest_hash
```

`phase_scope`はv1.8で新設した。Case単位でどのRelease Scopeに属するかを列挙し、Runtime GO判定の必要集合はこの値から導出する。Test ID単位ではなくCase単位である理由は、1つのTest IDが複数Phaseの関心事を束ねる場合があるためである（例：`AT-EVENT-MAPPING-001`）。

`durability_tier`は§3.10.2のTierであり、Crash／I/O Fault Caseで必須とする。

### Subject型とState名前空間

`expected_state`は`expected_subject_type`のState Enumに対してだけ解釈する。`*_RESULT`型はTest Platform用Schemaであり、`schemas.yaml`が定義するCore Schema Catalogの件数には含めない。

本表は**完全なState Enum**であり、例示ではない。`expected_state`が該当Subject型のEnumに含まれない場合、Spec Linterが`EXPECTED_STATE_SUBJECT_MISMATCH`で拒否する。

v1.6では本表が「State例」と題された部分列挙であり、規範Manifestが使用する`BLOCKED_APPROVAL`、`FAILED_RETRYABLE`、`WAITING_APPROVAL`、`BLOCKED_CONFLICT`等を含んでいなかった。§19.1本文は「`expected_state`は`expected_subject_type`のState Enumに対してだけ解釈する」と規定しているにもかかわらず、照合先のEnumが本文に存在しないため、この検査は実装不能だった。v1.8で§1.4.2、§1.4.3、§1.5、§3.8.1、§15.9から完全Enumを導出して固定する。

| Expected Subject Type | State Enum（完全） |
|---|---|
| `ACTION_ATTEMPT` | `PLANNING, WAITING_POLICY, WAITING_APPROVAL, READY, CLAIMED, LEASED, RUNTIME_VERIFIED, RUNNING, PREPARED_DURABLE, EFFECT_IN_FLIGHT, EFFECT_VERIFIED, RECEIPT_DURABLE, SUCCEEDED, FAILED_RETRYABLE, FAILED_PERMANENT, BLOCKED_POLICY, BLOCKED_APPROVAL, BLOCKED_CONFLICT, CANCELLED, CANCEL_UNKNOWN, EFFECT_UNKNOWN` |
| `RUN` | `CREATED, PLANNING, WAITING_APPROVAL, READY, RUNNING, RECOVERING, WAITING_RELEASE, COMPLETED, BLOCKED, BLOCKED_REPAIR_REQUIRED, CANCELLING, CANCELLED, FAILED` |
| `APPROVAL_GRANT` | `NOT_REQUIRED, REQUESTED, ISSUED, CONSUMED, EXPIRED, REVOKED, INVALIDATED, REPLAY_DENIED` |
| `APPROVAL_CONSUME_RESULT`／`SCHEMA_VALIDATION_RESULT`／`SCHEMA_SUITE_RESULT`／`MANIFEST_VALIDATION_RESULT`／`EVENT_APPEND_RESULT`／`LEDGER_CHAIN_VERIFICATION`／`APPROVAL_UI_RESULT`／`STATIC_ANALYSIS_RESULT`／`UX_MEASUREMENT_RESULT`／`EMERGENCY_RECOVERY_RESULT`／`PERFORMANCE_RESULT`／`STORAGE_IO_RESULT`／`MIGRATION_RESULT`／`BACKUP_RESTORE_RESULT`／`DEPLOYMENT_RESULT`／`GC_RESULT` | `ACCEPTED, REJECTED` |
| `REMOTE_INVOCATION_REGISTRY` | `PREPARED_DURABLE, REQUEST_DISPATCHING, REMOTE_ID_RECORDED, REMOTE_INVOCATION_UNCERTAIN, RECONCILED` |
| `OUTBOX_RECORD` | `PREPARED_DURABLE, DISPATCHING, RECONCILED, MANUAL_RECONCILIATION` |
| `BUDGET_RESERVATION` | `RESERVED, PENDING_RECONCILIATION, SETTLED, STATUS_UNKNOWN` |
| `INPUT_READ_DECISION` | `ALLOWED, DENIED` |
| `PLAN_COMPARISON` | `ASSERTIONS_SATISFIED, ASSERTIONS_FAILED` |
| `REPAIR_DECISION` | `REPAIR_REQUIRED, REPAIRED, EFFECT_UNKNOWN` |
| `DELEGATION_GRANT` | `ACTIVE, EXPIRED, REVOKED, SUPERSEDED, INVALIDATED` |
| `MASKING_RESULT` | `CLEAN, MASKED, REJECTED` |

型規則：

* `expectation_descriptor_hash`は常に必須。Domain Separationは`FDE-HARNESS/test-expectation/1/`。
* `input_fixture_hash`は実Fixture Bytes集合のHash。Fixture未作成時は`null`、`evidence_status=PASS`時は必須。Domain Separationは`FDE-HARNESS/test-input-fixture/1/`。
* `evidence_manifest_hash`は`evidence_status=PASS`時に必須。
* Hashは`sha256:`＋64桁lowercase hex。
* `evidence_status`は`UNVERIFIED | PASS | FAIL | BLOCKED`。
* `expected_event_sequence`は**実際に正本LedgerへAppendされたEvent**の順序付き配列である。`NONE`は空配列。詳細な規約は§19.1.1に置く。
* `expected_error_code`はRegistryの正規Codeまたは`null`。
* `expected_subject_type`とState Enum不一致は拒否する。
* `release_allowed=false`でRelease Eventが発生した場合は失敗。
* `auto_reexecution_prohibited=true`で新Effect Attemptが自動生成された場合は失敗。
* Manifest ValidatorはTest／Case一意性、Gate参照、Hash列、Error Classificationを検査する。

### 19.1.1 `expected_event_sequence`の観測規約

`expected_event_sequence`が指すのは**実際に正本LedgerへAppendされたEvent列**だけである。
Owner Decision `DEC-U-EVENT-SEQUENCE`（`docs/decision/EVENT-SEQUENCE-CONTRACT-V113.md`）
で確定した。

| # | 規約 |
|---:|---|
| 1 | 実際に正本LedgerへAppendされたEvent列である |
| 2 | Request Event列を含めない |
| 3 | Domain Verdictの予測列を含めない |
| 4 | Appendされなかった拒否Attemptを含めない |
| 5 | 拒否処理自身がAppendしたEventは含める |
| 6 | 拒否前に既にAppendされたEventは含める |
| 7 | 別の要求列が必要になった場合は別Fieldを設計し、本Fieldへ混在させない |

#### 区別の基準は「拒否されたか」ではなく「Ledgerに残ったか」

規約4と5は矛盾しない。拒否されたAppendはLedgerに何も残さないので列に入らない。
一方、拒否したという事実を記録するAppend（`ACTION_BLOCKED`等）は**成功したAppend**
であり、Ledgerに残るので入る。

**「拒否Caseだから空配列」と決めつけてはならない。** MVP0-Aで
`expected_event_sequence`が非空のCaseは54件あり、うち34件が
`REJECTED`／`DENIED`／`BLOCKED_*`である。空と決めつけると、
拒否処理がEventを残したことも、拒否前の状態を作ったEventも検証されなくなる。

#### Request列が偶然一致しても期待値の根拠にならない

`AT-EVENT-ORDER-001`の2 Caseがこの点を示す。どちらも`REJECTED`／
`EVENT_ORDER_VIOLATION`で`attempt_state_unchanged == true`である。

| Case | `attempt_state` | `expected_event_sequence` |
|---|---|---|
| `SEQUENCE_REGRESSION` | `WAITING_POLICY` | `['PLAN_RESOLVED']` |
| `ACTION_STARTED_MISSING` | `RUNTIME_VERIFIED` | `['RUNTIME_ATTESTED']` |

列にあるのは、そのAttemptを現在のStateにした**既にAppend済みのEvent**である。
拒否されたAppendは何も足していない。

#### 空のEvent列は2種類ある（v1.15）

`expected_event_sequence`が空であることには、意味の違う2つの状態が畳み込まれていた。

| Policy | 意味 | Ledger観測 |
|---|---|---|
| `REQUIRED_EMPTY` | Ledgerを観測したうえでEventが0件だったことを要求する | **必須** |

##### Ledger HeadをEvidence本体へ持つ（v1.19）

Case Evidence 3.0は`observed.ledger_head_before`と`observed.ledger_head_after`を
Requiredで持つ。拒否したときにHeadが動いていないことを、**Evidence単体から**
確かめられるようにする。

2.0でもHeadは観測していたが、Evidence本体へ載らずRelease判定へ渡らなかった。
観測しているだけでは、Evidenceを読む側から「見たのか見ていないのか」を区別できない。

Ledgerを観測しないUnit Caseは`null`である。**0で埋めない。** 埋めれば「見て0だった」と
偽ることになる。`event_sequence`が`NOT_APPLICABLE`のとき`null`になるのと同じ扱いである。

2.0はread_onlyとして残す。既存Evidenceを3.0へ変換しない。変換すると、2.0の時点では
Evidenceへ記録していなかったHeadを、記録していたかのように書き足すことになる。
| `NOT_APPLICABLE` | そのCaseはEvent観測自体を要求しない | 不要 |

**空配列をNot Applicableの代用にしない。** `[]`は「見て0件だった」であって
「見ていない」ではない。両者を同じ表現へ畳むと、Ledgerを一度も読んでいないCaseが
「0件を観測した」と主張するEvidenceを作れてしまう。

Policyの正本は`design-source/registries/tests.yaml`の`event_observation_policy`であり、
**Case単位で明示する**。`expected_event_sequence`が`[]`であることだけからPolicyを
推測しない。推測を許すと、後から`[]`のCaseを足したときにどちらの意味なのかが
Registryを読んでも決まらない。

Policyを持たないCaseは**未確定**である。未確定を既定値へ倒さない。
どちらでもないまま止める（不変条件#9）。

#### Unit層のCaseがNOT_APPLICABLEを名乗れる条件

`NOT_APPLICABLE`は「Ledgerを見なくてよい」という免除であり、濫用すると
Event検証を素通りさせる抜け道になる。次を満たすCaseだけが名乗れる。

| # | 条件 |
|---:|---|
| 1 | Registry正本で`event_observation_policy: NOT_APPLICABLE`が明示されている |
| 2 | Evidenceの`evidence_kind`が`UNIT`である |
| 3 | 主張がStateとError Codeで閉じており、Ledgerへの作用を主張しない |

Orchestration Evidenceは**常にLedger観測を要求する**。`NOT_APPLICABLE`を
Orchestration Caseへ与えない。与えれば、作用を主張するCaseがLedgerを見ないまま
PASSになる。

`SEQUENCE_REGRESSION`のRequestは`event_types=("PLAN_RESOLVED",)`であり、
期待値と一致する。**これは偶然である。** `ACTION_STARTED_MISSING`では
Request列と期待値が一致しない。Request列を期待値と読む解釈は、
2 Caseを並べた時点で成り立たない。

**偶然の一致を根拠にしない。** 一方のCaseだけを見て規約を決めると、
もう一方で破綻する。

#### 観測元の定義

`observed_event_sequence`の取得元は**正本Ledger、またはそれを代替する
Ledger Spy**だけとする。次から導出してはならない。

```text
Request Event列（積もうとした列）
Domain Verdictが返す予測列
Registryのexpected_event_sequence（期待値）
```

期待値から観測値を作ると、Evidenceは必ず一致し、
**何も検証していないのに全件PASSになる。**

Ledgerからの観測は、Append列と**Ledger Headを同時に**記録する。
拒否されたAppendではHeadが動かないことを併せて確かめる。
列だけを見ると、拒否されたはずのAppendが実は成功していた場合を見逃す。

#### 観測していない場合は空配列にしない

Event列を観測していないCaseの`observed_event_sequence`を`[]`にしてはならない。
`[]`は「Ledgerを見て0件だった」という積極的な主張であり、
「Ledgerを見ていない」とは意味が正反対である。

これは§26.2.1の`side_effects`が`null`と全ゼロを区別するのと同じ規則である。
未観測はEvidence生成を**拒否**し、`FAIL`とする。

下表の`expectation_descriptor_hash`は本書統合時に再生成した設計期待値Hashである。`input_fixture_hash`は実Fixture作成まで`null`とし、設計期待値HashをFixture Hashと呼ばない。

| Test ID | Case ID | Scenario | Expectation Descriptor Hash | Input Fixture Hash | Expected Event Sequence | Expected Subject Type | Expected Subject ID | Expected State | Expected Error Code | Trace Scope | Assertions | 自動再実行禁止 | Release可 | Manual Queue | Fault Point | Evidence Status | Evidence Manifest Hash |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `AT-PLAN-001` | `TAMPER` | `runtime_manifest_tamper` | `sha256:3ecd9b981c307e56af00f51b09ba0b6802a82f55cdba37e9a6ad84d64f68b925` | `null` | `PLAN_RESOLVED; POLICY_DECIDED; RUNTIME_SPEC_MISMATCH; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `RUNTIME_SPEC_MISMATCH` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `BEFORE_RUNTIME_LAUNCH` | `UNVERIFIED` | `null` |
| `AT-APPROVAL-001` | `REPLAY` | `approval_replay` | `sha256:a44e84d1d4486d1874e95fbe80f717fd082cd9b74c3ff1680110d9e2a0e15ceb` | `null` | `APPROVAL_ISSUED; APPROVAL_CONSUMED; APPROVAL_REPLAY_DENIED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_APPROVAL` | `APPROVAL_REPLAY` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `APPROVAL_CONSUME_CAS` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `AFTER_EXECUTION_EXPECTED_HASH` | `crash_after_execution_expected_hash_observed` | `sha256:d993d656f4b42f68dcd84be3f039171105335b01a2b9c4c4435e0f7360e6219a` | `null` | `EXECUTION_ATTEMPTED; RECOVERY_STARTED; RECOVERY_DECIDED; EFFECT_OBSERVED; EFFECT_RECEIPT_STORED; ACTION_COMMITTED` | `ACTION_ATTEMPT` | `$attempt_id` | `SUCCEEDED` | `null` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `true` | `false` | `AFTER_EXECUTION_ATTEMPTED` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `AFTER_EXECUTION_UNKNOWN` | `crash_after_execution_state_unknown` | `sha256:93f1bc5fe7bdbeddf236121412b272c15c8af169bf208921d13a9907251192e8` | `null` | `EXECUTION_ATTEMPTED; RECOVERY_STARTED; RECOVERY_DECIDED; EFFECT_UNKNOWN` | `ACTION_ATTEMPT` | `$attempt_id` | `EFFECT_UNKNOWN` | `EFFECT_UNKNOWN` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `true` | `AFTER_EXECUTION_ATTEMPTED` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `AFTER_OBSERVED` | `crash_after_effect_observed` | `sha256:e09b4c5549305b44a33c35717b92fbdd536f059890fc6bcba122a121f398a587` | `null` | `EFFECT_OBSERVED; RECOVERY_STARTED; RECOVERY_DECIDED; EFFECT_RECEIPT_STORED; ACTION_COMMITTED` | `ACTION_ATTEMPT` | `$attempt_id` | `SUCCEEDED` | `null` | `ACTION_ATTEMPT_STREAM` | `receipt_count == 1; duplicate_effects == 0` | `true` | `true` | `false` | `AFTER_EFFECT_OBSERVED` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `AFTER_PREPARED_ABSENCE_PROVEN` | `crash_after_prepared_target_unchanged` | `sha256:9137c43dd1f6682badfca762230097bce5cd301701327aeb61ce5397a0dd3ce6` | `null` | `ACTION_PREPARED; RECOVERY_STARTED; RECOVERY_DECIDED; EXECUTION_ATTEMPTED; EFFECT_OBSERVED; EFFECT_RECEIPT_STORED; ACTION_COMMITTED` | `ACTION_ATTEMPT` | `$attempt_id` | `SUCCEEDED` | `null` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `false` | `true` | `false` | `AFTER_ACTION_PREPARED` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `AFTER_RECEIPT` | `crash_after_receipt_before_commit` | `sha256:7546e0772bc341c7d127e2ea274ff0cd7710b1218a3604bfa1523c234073c74a` | `null` | `EFFECT_RECEIPT_STORED; RECOVERY_STARTED; RECOVERY_DECIDED; ACTION_COMMITTED` | `ACTION_ATTEMPT` | `$attempt_id` | `SUCCEEDED` | `null` | `ACTION_ATTEMPT_STREAM` | `receipt_count == 1; action_committed_count == 1` | `true` | `true` | `false` | `AFTER_RECEIPT_STORE_BEFORE_COMMIT` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `AFTER_REPLACE` | `crash_after_atomic_replace_before_observe` | `sha256:6a6db7ed0d9e2d358466c405115252dc29c53a31809067d2b277120009ff3013` | `null` | `EXECUTION_ATTEMPTED; RECOVERY_STARTED; RECOVERY_DECIDED; EFFECT_OBSERVED; EFFECT_RECEIPT_STORED; ACTION_COMMITTED` | `ACTION_ATTEMPT` | `$attempt_id` | `SUCCEEDED` | `null` | `ACTION_ATTEMPT_STREAM` | `observed_hash == expected_after_hash; duplicate_effects == 0` | `true` | `true` | `false` | `AFTER_ATOMIC_REPLACE_BEFORE_OBSERVE` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `BEFORE_EXECUTION` | `crash_before_execution_attempted` | `sha256:33afc74628a55797908755bf42fa94767e328152e1d4a420664c577fcf8084a7` | `null` | `ACTION_PREPARED; RECOVERY_STARTED; RECOVERY_DECIDED` | `ACTION_ATTEMPT` | `$attempt_id` | `PREPARED_DURABLE` | `null` | `ACTION_ATTEMPT_STREAM` | `target_hash == base_hash; effect_attempts == 0; operator_resume_required == true` | `true` | `false` | `false` | `BEFORE_EXECUTION_ATTEMPTED` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `BEFORE_PREPARED` | `crash_before_action_prepared` | `sha256:a6dbedafabe5a64398ceb138604f6bc9ac107cbd81ea4cae2808bef386ec8129` | `null` | `ACTION_STARTED; RECOVERY_STARTED; RECOVERY_DECIDED; ACTION_FAILED` | `ACTION_ATTEMPT` | `$attempt_id` | `FAILED_RETRYABLE` | `null` | `ACTION_ATTEMPT_STREAM` | `effect_journal_count == 0; target_hash == base_hash; effect_attempts == 0` | `false` | `false` | `false` | `BEFORE_ACTION_PREPARED` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `BEFORE_RECEIPT` | `crash_before_receipt_store` | `sha256:bd69678037b5dbbf79b9f163dfd2676d7a1a136af757a7fe014ff284b027cf6d` | `null` | `EFFECT_OBSERVED; RECOVERY_STARTED; RECOVERY_DECIDED; EFFECT_RECEIPT_STORED; ACTION_COMMITTED` | `ACTION_ATTEMPT` | `$attempt_id` | `SUCCEEDED` | `null` | `ACTION_ATTEMPT_STREAM` | `receipt_count == 1; receipt_hash_valid == true` | `true` | `true` | `false` | `BEFORE_RECEIPT_STORE` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `LEDGER_APPEND` | `crash_during_ledger_append` | `sha256:e5115da2001319349104362b4a15a677f235eae29582a23a90f75aa8e626edbe` | `null` | `RECOVERY_STARTED; RECOVERY_DECIDED` | `REPAIR_DECISION` | `$repair_decision_id` | `REPAIRED` | `null` | `LEDGER_RECOVERY_TRACE` | `partial_event_count == 0; chain_valid == true; duplicate_effects == 0` | `true` | `false` | `false` | `DURING_LEDGER_APPEND` | `UNVERIFIED` | `null` |
| `AT-CRASH-001` | `TEMP_BEFORE_FSYNC` | `crash_after_temp_write_before_fsync` | `sha256:7080748934b601985b96021f553970cb3d3a9e4d73279b979ff9bb2a7f34cca9` | `null` | `ACTION_STARTED; RECOVERY_STARTED; RECOVERY_DECIDED` | `REPAIR_DECISION` | `$repair_decision_id` | `REPAIRED` | `null` | `FILESYSTEM_RECOVERY_TRACE` | `journal_prepared == false; target_hash == base_hash; incomplete_temp_deleted_or_quarantined == true; duplicate_effects == 0` | `true` | `false` | `false` | `AFTER_TEMP_WRITE_BEFORE_FSYNC` | `UNVERIFIED` | `null` |
| `AT-PATH-001` | `LINUX_ESCAPE` | `linux_path_escape` | `sha256:122f863c0ac42eb66b5609858f7e939da41b5fdce02b9135569947e542d56c19` | `null` | `INPUT_READ_STARTED; INPUT_READ_DENIED` | `INPUT_READ_DECISION` | `$read_decision_id` | `DENIED` | `PATH_OUTSIDE_CAPABILITY` | `INPUT_READ_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `PATH_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-SANDBOX-001` | `UNAVAILABLE` | `sandbox_unavailable` | `sha256:f9a9d6043789c21fac2a8785fad8baaf1e2b902cf11c035efac0548b6dfdadfd` | `null` | `RUNTIME_ATTESTED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `SANDBOX_UNAVAILABLE` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `BEFORE_PROCESS_LAUNCH` | `UNVERIFIED` | `null` |
| `AT-CONFIG-001` | `DRIFT` | `provider_config_drift` | `sha256:71ed8d44da03eb64199f9c4866b27518fef48d5cb4ff65e799d6dadc11a04b96` | `null` | `RUNTIME_SPEC_RESOLVED; RUNTIME_ATTESTED; RUNTIME_SPEC_MISMATCH; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `RUNTIME_SPEC_MISMATCH` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `RUNTIME_ATTESTATION` | `UNVERIFIED` | `null` |
| `AT-AUTH-001` | `ROUTE_CHANGED` | `auth_route_change` | `sha256:28cb774a6ea221ff77049797f4ed07a71f4c79dec140b9ada9825e7b4a8d7cff` | `null` | `PLAN_RESOLVED; POLICY_DECIDED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_APPROVAL` | `APPROVAL_INVALIDATED` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `PLAN_HASH_VERIFY` | `UNVERIFIED` | `null` |
| `AT-FALLBACK-001` | `SCOPE_CHANGED` | `fallback_scope_change` | `sha256:88f460d14e21c34f4a67a531047a9de86732777a51f0a1ec26d51f6aa42a0c3a` | `null` | `ACTION_FAILED; PLAN_RESOLVED; POLICY_DECIDED` | `ACTION_ATTEMPT` | `$attempt_id` | `WAITING_APPROVAL` | `APPROVAL_REQUIRED` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `FALLBACK_DECISION` | `UNVERIFIED` | `null` |
| `AT-REMOTE-PREP-001` | `DISPATCH_STATE_MISSING` | `dispatch_without_request_dispatching_commit` | `sha256:e26a31d3d237b2b15e026840574c90020816fa5cff225b0d0e999556c3f7531d` | `null` | `ACTION_STARTED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `REMOTE_DISPATCH_STATE_NOT_DURABLE` | `REMOTE_INVOCATION_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `EGRESS_PRE_SEND_CHECK` | `UNVERIFIED` | `null` |
| `AT-REMOTE-PREP-001` | `REGISTRY_MISSING` | `dispatch_without_prepared_registry` | `sha256:07268c43d72b2a4a6b8b307dcc26405d9c2badc82d790f507ac2903ed66a2b14` | `null` | `ACTION_STARTED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `REMOTE_REGISTRY_NOT_DURABLE` | `REMOTE_INVOCATION_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `EGRESS_PRE_SEND_CHECK` | `UNVERIFIED` | `null` |
| `AT-INPUT-PATH-001` | `MOUNT_CROSSING` | `input_read_cross_mount` | `sha256:47d4ac815b8fb976ace05264d3ac7ff631b5699b1c4300621fae2632e2934ecb` | `null` | `INPUT_READ_STARTED; INPUT_READ_DENIED` | `INPUT_READ_DECISION` | `$read_decision_id` | `DENIED` | `MOUNT_CROSSING_DENIED` | `INPUT_READ_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `INPUT_MOUNT_CHECK` | `UNVERIFIED` | `null` |
| `AT-INPUT-PATH-001` | `SPECIAL_FILE` | `input_read_special_file` | `sha256:129bf23d1d9d5fa0c44f5f4af9c5bf5ff613f8255a102b8401951d34a5b67bb7` | `null` | `INPUT_READ_STARTED; INPUT_READ_DENIED` | `INPUT_READ_DECISION` | `$read_decision_id` | `DENIED` | `SPECIAL_FILE_DENIED` | `INPUT_READ_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `INPUT_FILE_TYPE_CHECK` | `UNVERIFIED` | `null` |
| `AT-INPUT-PATH-001` | `SYMLINK` | `input_read_via_symlink` | `sha256:a8261e49fb121a93510fc947a0cd97f9787c19f469b6c1a465e515a6cd275fb5` | `null` | `INPUT_READ_STARTED; INPUT_READ_DENIED` | `INPUT_READ_DECISION` | `$read_decision_id` | `DENIED` | `SYMLINK_DENIED` | `INPUT_READ_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `INPUT_PATH_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-SCHEMA-CONDITIONAL-001` | `CONSUMED_MISSING_ATTEMPT` | `approval_consumed_missing_attempt` | `sha256:0227eb1d8b211a80c74aba5382fe97a99730acfbf3900a1b922ad30372f32e26` | `null` | `NONE` | `SCHEMA_VALIDATION_RESULT` | `$schema_validation_id` | `REJECTED` | `SCHEMA_CONDITIONAL_VIOLATION` | `SCHEMA_VALIDATOR` | `schema_validation_result == expected_state` | `true` | `false` | `false` | `SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-SCHEMA-CONDITIONAL-001` | `READY_VALID` | `action_attempt_ready_without_lease` | `sha256:4add6ea8c4504b103d5866a3e251aea0577e41fb7eb13f118e1e09ba7d212617` | `null` | `NONE` | `SCHEMA_VALIDATION_RESULT` | `$schema_validation_id` | `ACCEPTED` | `null` | `SCHEMA_VALIDATOR` | `schema_validation_result == expected_state` | `false` | `false` | `false` | `SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-SCHEMA-CONDITIONAL-001` | `RUNNING_MISSING_ATTESTATION` | `action_attempt_running_missing_attestation` | `sha256:656321f6b2c496359e58e53894516d46659283518dc78053801d1ab2be0b95d2` | `null` | `NONE` | `SCHEMA_VALIDATION_RESULT` | `$schema_validation_id` | `REJECTED` | `SCHEMA_CONDITIONAL_VIOLATION` | `SCHEMA_VALIDATOR` | `schema_validation_result == expected_state` | `true` | `false` | `false` | `SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-SCHEMA-CONDITIONAL-001` | `SUCCEEDED_MISSING_RECEIPT` | `effect_attempt_succeeded_missing_receipt` | `sha256:9d5349697efc8a68ff1e2f2af8c95e79ad6e0afef523627a8709de83f1caadfa` | `null` | `NONE` | `SCHEMA_VALIDATION_RESULT` | `$schema_validation_id` | `REJECTED` | `SCHEMA_CONDITIONAL_VIOLATION` | `SCHEMA_VALIDATOR` | `schema_validation_result == expected_state` | `true` | `false` | `false` | `SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-EVENT-ORDER-001` | `ACTION_STARTED_MISSING` | `prepared_before_action_started` | `sha256:9690426c57cc70c790412a80dc34018c2b07290b853e3a86ad8155ce6410bbd4` | `null` | `RUNTIME_ATTESTED` | `EVENT_APPEND_RESULT` | `$append_result_id` | `REJECTED` | `EVENT_ORDER_VIOLATION` | `EVENT_APPEND_VALIDATOR` | `append_result == REJECTED; attempt_state_unchanged == true; attempt_state == RUNTIME_VERIFIED` | `true` | `false` | `false` | `EVENT_APPEND_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-EVENT-ORDER-001` | `SEQUENCE_REGRESSION` | `stream_sequence_regression` | `sha256:336e2423abf1a408e5f3d4e70857d3d63065e815d8ae9c94540a8b39359d776f` | `null` | `PLAN_RESOLVED` | `EVENT_APPEND_RESULT` | `$append_result_id` | `REJECTED` | `EVENT_ORDER_VIOLATION` | `EVENT_APPEND_VALIDATOR` | `append_result == REJECTED; attempt_state_unchanged == true; attempt_state == WAITING_POLICY` | `true` | `false` | `false` | `EVENT_APPEND_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-POLICY-APPROVAL-001` | `EFFECT_BEFORE_SOD` | `automatic_external_effect_approval_before_sod` | `sha256:14f895feddbd9dcd61e0c6927360868d870a48da93fd59437f478b6ad0e88ffd` | `null` | `POLICY_DECIDED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_APPROVAL` | `POLICY_APPROVAL_BEFORE_SOD` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `APPROVAL_ISSUANCE` | `UNVERIFIED` | `null` |
| `AT-POLICY-APPROVAL-001` | `PAID_BEFORE_SOD` | `automatic_paid_approval_before_sod` | `sha256:c4c8ced5d8d832b89a04d7f5bce0e01a06896d4b00b17069776e9e0ebc675f87` | `null` | `POLICY_DECIDED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_APPROVAL` | `POLICY_APPROVAL_BEFORE_SOD` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `APPROVAL_ISSUANCE` | `UNVERIFIED` | `null` |
| `AT-POLICY-APPROVAL-001` | `WRITE_BEFORE_SOD` | `automatic_workspace_write_approval_before_sod` | `sha256:928702de0e1fff8dcf13ddc5b67f1063df1ca6e77458e1a5d6e97f7ac3f5fa9c` | `null` | `POLICY_DECIDED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_APPROVAL` | `POLICY_APPROVAL_BEFORE_SOD` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `APPROVAL_ISSUANCE` | `UNVERIFIED` | `null` |
| `AT-OUTBOX-001` | `REMOTE_FOUND` | `receipt_loss_after_dispatch_remote_found` | `sha256:9ebcc4c87fd478e0462cdc495ef091eda70dbd1d4970f750119da2aeec362fed` | `null` | `OUTBOX_PREPARED; ACTION_PREPARED; OUTBOX_DISPATCHING; EXECUTION_ATTEMPTED; RECOVERY_STARTED; RECOVERY_DECIDED; OUTBOX_EFFECT_CONFIRMED; EFFECT_OBSERVED; EFFECT_RECEIPT_STORED; OUTBOX_RECONCILED; ACTION_COMMITTED` | `OUTBOX_RECORD` | `$outbox_id` | `RECONCILED` | `null` | `OUTBOX_STREAM` | `actual_state == expected_state` | `true` | `true` | `false` | `AFTER_REMOTE_ACCEPT` | `UNVERIFIED` | `null` |
| `AT-OUTBOX-001` | `REMOTE_UNKNOWN` | `receipt_loss_after_dispatch_remote_unknown` | `sha256:eb0593cf370973c78124efdbf02f209d6bed9c75c6a177fb56f2d830655e7b90` | `null` | `OUTBOX_PREPARED; ACTION_PREPARED; OUTBOX_DISPATCHING; EXECUTION_ATTEMPTED; RECOVERY_STARTED; OUTBOX_STATUS_UNKNOWN; RECOVERY_DECIDED; EFFECT_UNKNOWN; MANUAL_RECONCILIATION_ENQUEUED` | `OUTBOX_RECORD` | `$outbox_id` | `MANUAL_RECONCILIATION` | `REMOTE_STATUS_UNKNOWN` | `OUTBOX_STREAM` | `actual_state == expected_state` | `true` | `false` | `true` | `AFTER_REMOTE_ACCEPT` | `UNVERIFIED` | `null` |
| `AT-BUDGET-001` | `RESERVATION_FOUND` | `budget_reservation_store_found_ledger_missing` | `sha256:fda10090314a7b4f7bb0c29fe2204f5d76f805e62ba77389bdd16ecc369cd490` | `null` | `BUDGET_RESERVATION_PREPARED; ACTION_PREPARED; RECOVERY_STARTED; BUDGET_RESERVED; EFFECT_OBSERVED; EFFECT_RECEIPT_STORED; RECOVERY_DECIDED` | `BUDGET_RESERVATION` | `$reservation_id` | `RESERVED` | `null` | `BUDGET_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `AFTER_RESERVATION_COMMIT` | `UNVERIFIED` | `null` |
| `AT-BUDGET-001` | `RESERVATION_UNKNOWN` | `budget_reservation_state_unknown` | `sha256:6412c714645b0cb40140f1cf868a7336c5f0b7f61af46f8a72ee6daf47882b8e` | `null` | `RECOVERY_STARTED; BUDGET_STATUS_UNKNOWN; RECOVERY_DECIDED; EFFECT_UNKNOWN` | `BUDGET_RESERVATION` | `$reservation_id` | `STATUS_UNKNOWN` | `BUDGET_STATUS_UNKNOWN` | `BUDGET_STREAM` | `actual_state == expected_state` | `true` | `false` | `true` | `RESERVATION_RECONCILIATION` | `UNVERIFIED` | `null` |
| `AT-BLIND-001` | `CONTEXT_LEAK` | `blind_context_leak` | `sha256:319485377ea5ea3b46ac74f8b03f886b11d20e1046c71c6eafff2485cc1d0dc9` | `null` | `POLICY_DECIDED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `BLINDNESS_CONTEXT_LEAK` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `BLINDNESS_FILTER` | `UNVERIFIED` | `null` |
| `AT-FENCE-001` | `STALE_WORKER` | `stale_worker_commit` | `sha256:a66a47c61b866cc7fc7faae6c9b5b5038e8179f55ec5c43744ee25911d820310` | `null` | `FENCING_REJECTED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_CONFLICT` | `STALE_FENCING_TOKEN` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `FINAL_EFFECT_COMMIT` | `UNVERIFIED` | `null` |
| `AT-TEST-MANIFEST-001` | `AMBIGUOUS_EXPECTED` | `multiple_expected_states_in_one_case` | `sha256:ed7636d11b1183055263ddbc9c4d1209154c611b12392a4d70d8494048635e83` | `null` | `NONE` | `MANIFEST_VALIDATION_RESULT` | `$manifest_validation_id` | `REJECTED` | `AMBIGUOUS_EXPECTED_STATE` | `MANIFEST_VALIDATOR` | `manifest_validation_result == REJECTED` | `true` | `false` | `false` | `MANIFEST_SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-TEST-MANIFEST-001` | `HASH_COLUMN_MISSING` | `manifest_missing_expectation_or_input_hash_column` | `sha256:210b05447683cd4756b56b384f67514e2ccd99d2a502446de0b196b5f16acc0b` | `null` | `NONE` | `MANIFEST_VALIDATION_RESULT` | `$manifest_validation_id` | `REJECTED` | `HASH_COLUMN_MISSING` | `MANIFEST_VALIDATOR` | `manifest_rejected == true` | `true` | `false` | `false` | `MANIFEST_SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-TEST-MANIFEST-001` | `HASH_FORMAT` | `fixture_hash_without_prefix` | `sha256:4b605e4d7e0d2d6579b722d5e1d256ffbbdb99784d2c0e5ba509bfd45aa84650` | `null` | `NONE` | `MANIFEST_VALIDATION_RESULT` | `$manifest_validation_id` | `REJECTED` | `FIXTURE_HASH_FORMAT_INVALID` | `MANIFEST_VALIDATOR` | `manifest_validation_result == REJECTED` | `true` | `false` | `false` | `MANIFEST_SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-PLAN-DETERMINISM-001` | `NONDETERMINISTIC_OUTPUT` | `same_input_produces_different_plan_content` | `sha256:a22906fc23dd23e89f6a42374bc9ddcc28f158ff7aece83727d632f7ac286dfd` | `null` | `PLAN_NONDETERMINISTIC; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_CONFLICT` | `PLAN_NONDETERMINISTIC` | `PLAN_BUILDER` | `plan_content_hash_a != plan_content_hash_b; process_launches == 0` | `true` | `false` | `false` | `PLAN_BUILD_REPEAT` | `UNVERIFIED` | `null` |
| `AT-PLAN-DETERMINISM-001` | `SAME_INPUT` | `same_input_snapshot_plan_reproduction` | `sha256:226b00f27fd07282cd0df7862f9527ec74ac6c7d0e252148670ea31396cce63f` | `null` | `NONE` | `PLAN_COMPARISON` | `$comparison_id` | `ASSERTIONS_SATISFIED` | `null` | `PLAN_BUILDER` | `plan_content_hash_a == plan_content_hash_b; execution_plan_hash_a != execution_plan_hash_b; run_id_a != run_id_b` | `false` | `false` | `false` | `PLAN_BUILD_REPEAT` | `UNVERIFIED` | `null` |
| `AT-CONTROL-DATA-001` | `ARTIFACT_INSTRUCTION` | `untrusted_artifact_attempts_control_role` | `sha256:0ebe42161fffce51eca0f054abdd23de3dd9095952e0e8383bfec88e2e8b35ae` | `null` | `INPUT_ARTIFACT_CLASSIFIED; POLICY_DECIDED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `CONTROL_DATA_ROLE_ESCALATION` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `MESSAGE_ROLE_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-POLICY-STALE-001` | `INFLIGHT_ATTEMPTED` | `stale_policy_allows_reconciliation_only` | `sha256:45b7c7ae25b74fa104d9ec08a39d34a3c6ef445e8624bb179db65b4d057aaebc` | `null` | `POLICY_STALE_DETECTED; POLICY_STALE_RECOVERY_ONLY; RECOVERY_STARTED; RECOVERY_DECIDED; EFFECT_UNKNOWN` | `ACTION_ATTEMPT` | `$attempt_id` | `EFFECT_UNKNOWN` | `EFFECT_UNKNOWN` | `ACTION_ATTEMPT_STREAM` | `new_effect_attempts == 0; retry_count == 0; fallback_count == 0` | `true` | `false` | `true` | `AFTER_EXECUTION_ATTEMPTED` | `UNVERIFIED` | `null` |
| `AT-POLICY-STALE-001` | `NEW_EXTERNAL_EFFECT` | `stale_policy_blocks_external_dispatch` | `sha256:5551e02b8b4e297826c2037cac6114ab8d3bd1068a85c1658bea5fd41055a4e2` | `null` | `POLICY_STALE_DETECTED; POLICY_STALE_ACTION_BLOCKED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `POLICY_STALE_EXTERNAL_EFFECT_BLOCKED` | `ACTION_ATTEMPT_STREAM` | `network_bytes_sent == 0` | `true` | `false` | `false` | `EGRESS_PRE_SEND_CHECK` | `UNVERIFIED` | `null` |
| `AT-POLICY-STALE-001` | `NEW_LOCAL_READ` | `stale_policy_blocks_new_local_read` | `sha256:49ae6fa81408ee3ac3699ce797ed569dad4374eaaa046ec26de9f3bbe86ff619` | `null` | `POLICY_STALE_DETECTED; POLICY_STALE_ACTION_BLOCKED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `POLICY_STALE_NEW_ACTION_BLOCKED` | `ACTION_ATTEMPT_STREAM` | `network_calls == 0; process_launches == 0` | `true` | `false` | `false` | `RUNTIME_GO_POLICY_CHECK` | `UNVERIFIED` | `null` |
| `AT-POLICY-STALE-001` | `NEW_PAID` | `stale_policy_blocks_paid_execution` | `sha256:d06bb2b936df06038535f988fd1cf298237dcf6b64b027a075c62c037d196721` | `null` | `POLICY_STALE_DETECTED; POLICY_STALE_ACTION_BLOCKED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `POLICY_STALE_PAID_BLOCKED` | `ACTION_ATTEMPT_STREAM` | `budget_reservations_created == 0; provider_calls == 0` | `true` | `false` | `false` | `BEFORE_BUDGET_RESERVATION` | `UNVERIFIED` | `null` |
| `AT-POLICY-STALE-001` | `NEW_WORKSPACE_WRITE` | `stale_policy_blocks_workspace_write` | `sha256:fafdea454df05e52f70d953a3a57f029e6e281ff7fd4b65c97ed8218c52e4225` | `null` | `POLICY_STALE_DETECTED; POLICY_STALE_ACTION_BLOCKED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `POLICY_STALE_EFFECT_BLOCKED` | `ACTION_ATTEMPT_STREAM` | `workspace_commits == 0` | `true` | `false` | `false` | `BEFORE_ACTION_PREPARED` | `UNVERIFIED` | `null` |
| `AT-EVENT-MAPPING-001` | `BUDGET_UNKNOWN` | `budget_unknown_maps_to_effect_unknown` | `sha256:df32204fd3dd83acfa1ddaf4d053ab938d3ebcebf37e3e9d5a4b7afcc06b51c0` | `null` | `BUDGET_STATUS_UNKNOWN; EFFECT_UNKNOWN` | `BUDGET_RESERVATION` | `$reservation_id` | `STATUS_UNKNOWN` | `BUDGET_STATUS_UNKNOWN` | `BUDGET_STREAM` | `action_state == EFFECT_UNKNOWN; paid_actions_enabled == false` | `true` | `false` | `true` | `BUDGET_RECONCILIATION` | `UNVERIFIED` | `null` |
| `AT-EVENT-MAPPING-001` | `LEDGER_EVENT_MISSING` | `phase_store_advanced_without_ledger_event` | `sha256:7f7bab2ac8026691f9b4192243f333d071e449764172a47e1a7988c6d9ea7d5f` | `null` | `RECOVERY_STARTED; RECOVERY_DECIDED` | `REPAIR_DECISION` | `$repair_decision_id` | `REPAIR_REQUIRED` | `PHASE_LEDGER_EVENT_MISSING` | `CROSS_STORE_REPAIR_TRACE` | `duplicate_effect_attempts == 0; compensating_event_required == true` | `true` | `false` | `true` | `REPAIR_SCANNER` | `UNVERIFIED` | `null` |
| `AT-EVENT-MAPPING-001` | `OUTBOX_UNKNOWN` | `outbox_unknown_maps_to_effect_unknown` | `sha256:a1c741cea26cdc6b8bb53db2255cd5946f38de0269be6142c83844eb43bd8bf5` | `null` | `OUTBOX_STATUS_UNKNOWN; EFFECT_UNKNOWN; MANUAL_RECONCILIATION_ENQUEUED` | `OUTBOX_RECORD` | `$outbox_id` | `MANUAL_RECONCILIATION` | `REMOTE_STATUS_UNKNOWN` | `OUTBOX_STREAM` | `action_state == EFFECT_UNKNOWN; manual_queue_record_count == 1` | `true` | `false` | `true` | `OUTBOX_RECONCILIATION` | `UNVERIFIED` | `null` |
| `AT-EVENT-MAPPING-001` | `REMOTE_UNCERTAIN` | `remote_uncertain_maps_to_effect_unknown` | `sha256:21d5a2e7f48dcdb35f2ceafe7ed734cdcb0eb4fb4335e3908325cb3b82ab3dc2` | `null` | `REMOTE_INVOCATION_UNCERTAIN; EFFECT_UNKNOWN` | `ACTION_ATTEMPT` | `$attempt_id` | `EFFECT_UNKNOWN` | `REMOTE_INVOCATION_UNCERTAIN` | `REMOTE_INVOCATION_STREAM` | `registry_state == REMOTE_INVOCATION_UNCERTAIN; action_state == EFFECT_UNKNOWN` | `true` | `false` | `true` | `REMOTE_RESPONSE_LOST` | `UNVERIFIED` | `null` |
| `AT-SCHEMA-COMPLETE-001` | `ALL_VALID` | `all_core_schema_valid_fixtures` | `sha256:55bbdfd878c60bee2cd540ee48c69f9fe8a37a09dea3e644b174d4099fbf1fab` | `null` | `NONE` | `SCHEMA_SUITE_RESULT` | `$schema_suite_id` | `ACCEPTED` | `null` | `SCHEMA_VALIDATOR` | `valid_fixture_count == 26; validation_errors == 0` | `false` | `false` | `false` | `SCHEMA_SUITE_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-SCHEMA-COMPLETE-001` | `INVALID_HASH` | `invalid_hash_fixture_rejected` | `sha256:01a5f3f096aba3afe4f50c9926efb272052dcca1620bdf79d7f273b676c03451` | `null` | `NONE` | `SCHEMA_VALIDATION_RESULT` | `$schema_validation_id` | `REJECTED` | `HASH_PATTERN_INVALID` | `SCHEMA_VALIDATOR` | `invalid_fixture_rejected == true` | `true` | `false` | `false` | `SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-SCHEMA-COMPLETE-001` | `MISSING_CROSS_REFERENCE` | `effect_receipt_missing_journal_rejected` | `sha256:6b348e1027f4ad4c0ec4581b825c7017fb15c9e97af54cec75bda5dd23e2136c` | `null` | `NONE` | `SCHEMA_VALIDATION_RESULT` | `$schema_validation_id` | `REJECTED` | `JOURNAL_REFERENCE_MISSING` | `SCHEMA_VALIDATOR` | `cross_reference_validation == REJECTED` | `true` | `false` | `false` | `CROSS_REFERENCE_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-MANIFEST-SUBJECT-001` | `MIXED_SUBJECT_STATE` | `action_state_used_for_outbox_subject` | `sha256:774cf937c59d893ffd94e0b72b298c96f5e9b2b472b865048adf84565d7884f5` | `null` | `NONE` | `MANIFEST_VALIDATION_RESULT` | `$manifest_validation_id` | `REJECTED` | `EXPECTED_STATE_SUBJECT_MISMATCH` | `MANIFEST_VALIDATOR` | `manifest_rejected == true` | `true` | `false` | `false` | `MANIFEST_SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-MANIFEST-SUBJECT-001` | `VALID_TYPED_SUBJECT` | `typed_expected_state_subject` | `sha256:358726de2a24d7fd2f3e85fc4f46d5c4b251af875c9b15779ae392a8d5260ab6` | `null` | `NONE` | `MANIFEST_VALIDATION_RESULT` | `$manifest_validation_id` | `ACCEPTED` | `null` | `MANIFEST_VALIDATOR` | `expected_state_enum_matches_subject_type == true` | `false` | `false` | `false` | `MANIFEST_SCHEMA_VALIDATION` | `UNVERIFIED` | `null` |
| `AT-WSL-BOUNDARY-001` | `FOREIGN_FS` | `workspace_on_windows_filesystem` | `sha256:bcb35daf70748332d7f47cf503800bc3caf4b896c8d4b7cdc6a6ab03ff066267` | `null` | `ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `WORKSPACE_ON_FOREIGN_FS_DENIED` | `STARTUP_BOUNDARY_CHECK` | `process_launches == 0; workspace_writes == 0` | `true` | `false` | `false` | `MOUNT_BOUNDARY_CHECK` | `UNVERIFIED` | `null` |
| `AT-APPROVAL-002` | `CONCURRENT_LOSER` | `concurrent_consume_loser` | `sha256:87530e5d6a1ff810188d28a69c85e28b8894400bb54c2fcf7b9a0d6f237e24c4` | `null` | `APPROVAL_REPLAY_DENIED` | `APPROVAL_CONSUME_RESULT` | `$consume_result_id` | `REJECTED` | `APPROVAL_REPLAY` | `APPROVAL_STORE` | `concurrency_group == $group_id; failed_consumes == 1; successful_consumes == 0` | `true` | `false` | `false` | `APPROVAL_CONSUME_CAS` | `UNVERIFIED` | `null` |
| `AT-APPROVAL-002` | `CONCURRENT_WINNER` | `concurrent_consume_winner` | `sha256:41fa7fae842a7ae9f0029e9718967f9ffcc61992641c68fece260854bdff30d3` | `null` | `APPROVAL_ISSUED; APPROVAL_CONSUMED` | `APPROVAL_GRANT` | `$grant_id` | `CONSUMED` | `null` | `APPROVAL_STORE` | `concurrency_group == $group_id; successful_consumes == 1` | `true` | `false` | `false` | `APPROVAL_CONSUME_CAS` | `UNVERIFIED` | `null` |
| `AT-CLOCK-SKEW-001` | `EXCEEDED` | `approval_clock_skew_exceeded` | `sha256:94ef96984d156e7e3cb7ace0d668b1952cc2359bbd64f2a2dd34fa527ac2b735` | `null` | `APPROVAL_ISSUED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_APPROVAL` | `CLOCK_SKEW_EXCEEDED` | `APPROVAL_VALIDATOR` | `process_launches == 0; grant_consumed == false` | `true` | `false` | `false` | `APPROVAL_TIME_CHECK` | `UNVERIFIED` | `null` |
| `AT-LEDGER-TAMPER-001` | `ONE_BYTE` | `ledger_payload_one_byte_modified` | `sha256:617244f5362df8b0216d3a835be36a32157c869887bde320b415e31b5374dee6` | `null` | `NONE` | `LEDGER_CHAIN_VERIFICATION` | `$verification_id` | `REJECTED` | `LEDGER_CHAIN_TAMPERED` | `LEDGER_VERIFY_CHAIN` | `tamper_detected == true; chain_valid == false` | `true` | `false` | `false` | `LEDGER_TAMPER` | `UNVERIFIED` | `null` |
| `AT-RUN-TERMINAL-001` | `NO_RELEASE` | `completion_without_release_decision` | `sha256:2118e84b45a3d30f309d59bb116c811c26b73ab74f7af95013a725a752700761` | `null` | `EVALUATION_COMPLETED; ACTION_BLOCKED` | `RUN` | `$run_id` | `BLOCKED` | `RELEASE_DECISION_REQUIRED` | `RUN_TRANSITION_VALIDATOR` | `completed_event_count == 0; release_decision_id == null` | `true` | `false` | `false` | `RUN_TERMINAL_TRANSITION` | `UNVERIFIED` | `null` |
| `AT-RUN-TERMINAL-001` | `UNRECONCILED_TO_CANCELLED` | `unreconciled_effect_blocks_cancelled` | `sha256:d39058cc31acd50cd6e0c70ccdccd2dfcf538352e7ae8b61f4e9685ce9464c74` | `null` | `EFFECT_UNKNOWN; ACTION_BLOCKED` | `RUN` | `$run_id` | `BLOCKED_REPAIR_REQUIRED` | `UNRECONCILED_EFFECT_PRESENT` | `RUN_TRANSITION_VALIDATOR` | `requested_terminal_state == CANCELLED; ended_at == null` | `true` | `false` | `false` | `RUN_TERMINAL_TRANSITION` | `UNVERIFIED` | `null` |
| `AT-RUN-TERMINAL-001` | `UNRECONCILED_TO_COMPLETED` | `unreconciled_effect_blocks_completed` | `sha256:524bf0c172051650766ecfe7c42af03ff8359933611aed8aa6d80aba109f80ab` | `null` | `EFFECT_UNKNOWN; ACTION_BLOCKED` | `RUN` | `$run_id` | `BLOCKED_REPAIR_REQUIRED` | `UNRECONCILED_EFFECT_PRESENT` | `RUN_TRANSITION_VALIDATOR` | `requested_terminal_state == COMPLETED; ended_at == null` | `true` | `false` | `false` | `RUN_TERMINAL_TRANSITION` | `UNVERIFIED` | `null` |
| `AT-RUN-TERMINAL-001` | `UNRECONCILED_TO_FAILED` | `unreconciled_effect_blocks_failed` | `sha256:407c4cdfbedd7ea51e6896af707d5a379800547f84c8ed9bd26b2812408019fb` | `null` | `EFFECT_UNKNOWN; ACTION_BLOCKED` | `RUN` | `$run_id` | `BLOCKED_REPAIR_REQUIRED` | `UNRECONCILED_EFFECT_PRESENT` | `RUN_TRANSITION_VALIDATOR` | `requested_terminal_state == FAILED; ended_at == null` | `true` | `false` | `false` | `RUN_TERMINAL_TRANSITION` | `UNVERIFIED` | `null` |
| `AT-FAULT-GUARD-001` | `POLICY_DENIED` | `fault_injection_enabled_when_policy_forbids` | `sha256:c859a87a80466c2eed099a0033d9049917a0e0a33f63d9fac22a2383ae06cf02` | `null` | `ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$attempt_id` | `BLOCKED_POLICY` | `FAULT_INJECTION_NOT_PERMITTED` | `RUNTIME_GO_POLICY_CHECK` | `process_launches == 0; fault_injection_enabled == true` | `true` | `false` | `false` | `BEFORE_PROCESS_LAUNCH` | `UNVERIFIED` | `null` |
| `AT-APPROVAL-UX-001` | `DEFAULT_FIELDS` | `approval_default_view_has_seven_fields` | `sha256:4461975f0f2062bb8f1127b8f2349cee4fec782e0effdd3c6b7b09b053a15e5c` | `null` | `NONE` | `APPROVAL_UI_RESULT` | `$ui_result_id` | `ACCEPTED` | `null` | `APPROVAL_UI` | `default_field_count <= 7` | `false` | `false` | `false` | `UI_STATIC_CHECK` | `UNVERIFIED` | `null` |
| `AT-APPROVAL-UX-001` | `MEDIAN_TIME` | `approval_operation_time_median` | `sha256:e62880d20fb9cdabd332dddd6fd985b41f6e1d360870e4f33e9d59586b37b8a5` | `null` | `NONE` | `UX_MEASUREMENT_RESULT` | `$measurement_id` | `ACCEPTED` | `null` | `APPROVAL_UX_MEASUREMENT` | `sample_count == 10; median_seconds <= 30` | `false` | `false` | `false` | `HUMAN_MEASUREMENT` | `UNVERIFIED` | `null` |
| `AT-APPROVAL-UX-001` | `NO_BYPASS` | `approval_bypass_symbols_absent` | `sha256:093b73609a5cdb674ec56c01628225106c998168b1735489abf90d6cad7710d1` | `null` | `NONE` | `STATIC_ANALYSIS_RESULT` | `$analysis_id` | `ACCEPTED` | `null` | `SOURCE_STATIC_ANALYSIS` | `bypass_findings == 0` | `false` | `false` | `false` | `SOURCE_SCAN` | `UNVERIFIED` | `null` |
| `AT-APPROVAL-UX-001` | `REAPPROVAL_DIFF` | `reapproval_shows_changed_fields_and_reason` | `sha256:d056a84845e42348a597dfddad4dbe250a9535575383e1ca7ea9ff7d9976694b` | `null` | `NONE` | `APPROVAL_UI_RESULT` | `$ui_result_id` | `ACCEPTED` | `null` | `APPROVAL_UI` | `changed_fields_visible == true; invalidation_reason_visible == true` | `false` | `false` | `false` | `UI_RENDER` | `UNVERIFIED` | `null` |
| `AT-EMERGENCY-RECOVERY-001` | `ALLOWLIST` | `all_nine_recovery_operations_allowed` | `sha256:f682f1c39f35296addcd9b18fbf09abc08932a7b83dcc343c762606cabcc7915` | `null` | `POLICY_STALE_RECOVERY_ONLY; RECOVERY_STARTED; RECOVERY_DECIDED` | `EMERGENCY_RECOVERY_RESULT` | `$recovery_result_id` | `ACCEPTED` | `null` | `EMERGENCY_RECOVERY_POLICY` | `allowed_operation_pass_count == 9; new_effects == 0` | `true` | `false` | `false` | `EMERGENCY_POLICY` | `UNVERIFIED` | `null` |
| `AT-EMERGENCY-RECOVERY-001` | `BAD_SIGNATURE` | `invalid_emergency_profile_signature` | `sha256:ba4793a6a7ab019d4627e80740b7334204ecdfe76f9966b603721d55cbfd3bd4` | `null` | `NONE` | `EMERGENCY_RECOVERY_RESULT` | `$recovery_result_id` | `REJECTED` | `EMERGENCY_PROFILE_SIGNATURE_INVALID` | `EMERGENCY_PROFILE_VALIDATOR` | `recovery_started == false` | `true` | `false` | `false` | `EMERGENCY_PROFILE_VERIFY` | `UNVERIFIED` | `null` |
| `AT-EMERGENCY-RECOVERY-001` | `DENYLIST` | `all_nine_forbidden_operations_denied` | `sha256:2e20fa2fbf079d1641d64dfab616044cf5c26a941d894692ed96b073ce0818cb` | `null` | `POLICY_STALE_RECOVERY_ONLY` | `EMERGENCY_RECOVERY_RESULT` | `$recovery_result_id` | `REJECTED` | `EMERGENCY_OPERATION_NOT_ALLOWED` | `EMERGENCY_RECOVERY_POLICY` | `denied_operation_count == 9; workspace_writes == 0; provider_calls == 0` | `true` | `false` | `false` | `EMERGENCY_POLICY` | `UNVERIFIED` | `null` |
| `AT-PERF-001` | `CONTROL_OVERHEAD` | `eight_actions_sixty_events_performance` | `sha256:90c1196cf3118f889f2c276293dc1d9f452edc6f7fc8204f80c00e6e97b035be` | `null` | `NONE` | `PERFORMANCE_RESULT` | `$performance_result_id` | `ACCEPTED` | `null` | `PERFORMANCE_HARNESS` | `warmup_runs == 5; measured_runs == 30; p50_seconds <= 5; p95_seconds <= 8` | `false` | `false` | `false` | `PERFORMANCE_RUN` | `UNVERIFIED` | `null` |
| `AT-FAULT-IO-001` | `ARTIFACT_ENOSPC` | `artifact_write_disk_full` | `sha256:ddffef8f366fcfa2c5a427750117d4e31d0af37fef5e3903c01b6fde508bc284` | `null` | `NONE` | `STORAGE_IO_RESULT` | `$io_result_id` | `REJECTED` | `STORAGE_WRITE_FAILED` | `ARTIFACT_IO` | `manifest_created == false; journal_prepared == false; target_hash == base_hash` | `true` | `false` | `false` | `ARTIFACT_WRITE_ENOSPC` | `UNVERIFIED` | `null` |
| `AT-FAULT-IO-001` | `FSYNC_EIO` | `file_fsync_io_error` | `sha256:0c4c01c32d8e5f34eecc57177f24692e298ec61233b1209fc12f8581bc5ce1c3` | `null` | `NONE` | `STORAGE_IO_RESULT` | `$io_result_id` | `REJECTED` | `STORAGE_WRITE_FAILED` | `FILESYSTEM_IO` | `atomic_replace_count == 0; target_hash == base_hash` | `true` | `false` | `false` | `FILE_FSYNC_EIO` | `UNVERIFIED` | `null` |
| `AT-FAULT-IO-001` | `SQLITE_ENOSPC` | `sqlite_commit_disk_full` | `sha256:f3dd2b2dce993ec66b24783788aa2b7b9c153f53afdbf9630d5243d8367cbab8` | `null` | `NONE` | `STORAGE_IO_RESULT` | `$io_result_id` | `REJECTED` | `STORAGE_WRITE_FAILED` | `SQLITE_IO` | `partial_transaction_rows == 0; effect_attempts == 0` | `true` | `false` | `false` | `SQLITE_COMMIT_ENOSPC` | `UNVERIFIED` | `null` |
| `AT-MIGRATION-001` | `BACKUP_RESTORE` | `backup_restore_and_chain_verify` | `sha256:a7b1a7b17bcf703535cfd9113b75d6cbac6e4b36aff267d3005eccd50765bdb7` | `null` | `NONE` | `BACKUP_RESTORE_RESULT` | `$restore_id` | `ACCEPTED` | `null` | `BACKUP_RESTORE` | `restored_chain_head == original_chain_head; artifact_manifest_count_equal == true` | `false` | `false` | `false` | `BACKUP_RESTORE` | `UNVERIFIED` | `null` |
| `AT-MIGRATION-001` | `FRESH_INSTALL` | `migration_fresh_database` | `sha256:a76c9e25aa1cf5d6e7599177ad2392ee7a4a2981451289cef3ca04d8e3a857d4` | `null` | `NONE` | `MIGRATION_RESULT` | `$migration_id` | `ACCEPTED` | `null` | `MIGRATION_RUNNER` | `schema_version == expected_schema_version; integrity_check == ok` | `false` | `false` | `false` | `MIGRATION` | `UNVERIFIED` | `null` |
| `AT-DRAIN-001` | `ACTIVE_RUN_BLOCKS` | `deployment_with_active_run_is_blocked` | `sha256:0580229330e33cb52625d1ee74091fec12d5d26462817ff3f73bfd6082dee43b` | `null` | `ACTION_BLOCKED` | `DEPLOYMENT_RESULT` | `$deployment_id` | `REJECTED` | `DEPLOY_DRAIN_REQUIRED` | `DEPLOYMENT_DRAIN` | `active_run_count > 0; migration_started == false` | `true` | `false` | `false` | `DEPLOY_PRECHECK` | `UNVERIFIED` | `null` |
| `AT-DRAIN-001` | `DRAINED_DEPLOY` | `deployment_after_drain` | `sha256:ee03389b2ee29b718035f86f47cfb97f8afdee2b8417c47d13082bdd4c67b027` | `null` | `NONE` | `DEPLOYMENT_RESULT` | `$deployment_id` | `ACCEPTED` | `null` | `DEPLOYMENT_DRAIN` | `active_run_count == 0; pending_approval_count == 0` | `false` | `false` | `false` | `DEPLOY_PRECHECK` | `UNVERIFIED` | `null` |
| `AT-GC-001` | `ORPHAN_CANDIDATE` | `old_orphan_is_candidate_only` | `sha256:fa65c745de25797d9e72ed65aae61e38643c5cbb0c703612a55060e2ed086657` | `null` | `NONE` | `GC_RESULT` | `$gc_result_id` | `ACCEPTED` | `null` | `ARTIFACT_GC` | `orphan_candidate_count == 1; deleted_count == 0` | `false` | `false` | `false` | `GC_DRY_RUN` | `UNVERIFIED` | `null` |
| `AT-GC-001` | `RAW_RETENTION` | `raw_payload_retention_deletes_payload_keeps_metadata` | `sha256:99ebb06a634152acb1a2825f64a42613c98edbaedaa151c89c67beb331559ab1` | `null` | `ARTIFACT_PAYLOAD_DELETED` | `GC_RESULT` | `$gc_result_id` | `ACCEPTED` | `null` | `ARTIFACT_GC` | `payload_deleted == true; manifest_retained == true; deletion_event_count == 1` | `false` | `false` | `false` | `GC_EXECUTE_APPROVED` | `UNVERIFIED` | `null` |
| `AT-GC-001` | `REFERENCED_KEEP` | `gc_keeps_referenced_artifact` | `sha256:a93dab4e40f630bcb628cee648199e727e893bce746942a0a106ef08b4b4b0f4` | `null` | `NONE` | `GC_RESULT` | `$gc_result_id` | `ACCEPTED` | `null` | `ARTIFACT_GC` | `referenced_deleted_count == 0` | `false` | `false` | `false` | `GC_DRY_RUN` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `SCOPE_EXCEEDED` | `delegated_plan_outside_predicate_escalates_to_human` | `sha256:6a12693279e7e43a6c4d09a87f2775012a7dd4ab5d9e58d6d7508d3724bc6f52` | `null` | `PLAN_RESOLVED; POLICY_DECIDED; DELEGATION_REJECTED` | `ACTION_ATTEMPT` | `$action_attempt_id` | `WAITING_APPROVAL` | `APPROVAL_REQUIRED` | `ACTION_ATTEMPT_STREAM` | `auto_approved_count == 0; human_approval_required == true` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `SELF_MODIFICATION` | `delegation_cannot_create_or_widen_delegation` | `sha256:67b2b6906d1e96239e2f9fc2a3e971249905da58b85d4390c860a01b1b80d3cf` | `null` | `POLICY_DECIDED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$action_attempt_id` | `BLOCKED_POLICY` | `DELEGATION_SELF_MODIFICATION_DENIED` | `ACTION_ATTEMPT_STREAM` | `delegation_created_count == 0` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `WIDEN_DENIED` | `narrow_rejects_predicate_that_is_not_a_subset` | `sha256:eff2e9f2453e897449b9356775060b6de07d237376d13abdf4788f3902bce565` | `null` | `NONE` | `DELEGATION_GRANT` | `$delegation_grant_id` | `ACTIVE` | `DELEGATION_SCOPE_EXCEEDED` | `DELEGATION_GRANT` | `original_predicate_unchanged == true` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `REVOKE_IMMEDIATE` | `revoked_delegation_stops_auto_approval_at_once` | `sha256:5c562453487094d1e9bf37335a3b8013870bbee8cda0a9908a139a80c39483a4` | `null` | `DELEGATION_REVOKED; POLICY_DECIDED` | `ACTION_ATTEMPT` | `$action_attempt_id` | `WAITING_APPROVAL` | `APPROVAL_REQUIRED` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `POLICY_HASH_CHANGED` | `policy_change_invalidates_delegation` | `sha256:f7e0b6733efb66d63403b79bfc09c087d29091e916884811f5d29b16d1743996` | `null` | `DELEGATION_INVALIDATED; POLICY_DECIDED` | `DELEGATION_GRANT` | `$delegation_grant_id` | `INVALIDATED` | `null` | `DELEGATION_GRANT` | `actual_state == expected_state` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `PAID_NEVER_DELEGATED` | `paid_execution_ignores_delegation` | `sha256:4d750d4ca1f787352f1524b3fe1d79c608848a1ccbff81b049667e6fe4145995` | `null` | `NONE` | `ACTION_ATTEMPT` | `$action_attempt_id` | `WAITING_APPROVAL` | `DELEGATION_NOT_DELEGABLE_ACTION` | `ACTION_ATTEMPT_STREAM` | `auto_approved_count == 0` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `STALE_POLICY_NO_AUTO` | `delegation_inactive_when_policy_not_current` | `sha256:e38fb2e80c0b7abc80dbd45915acf91fcf568795bb3c70f0deff4ec9701be3f3` | `null` | `POLICY_STALE_DETECTED; DELEGATION_REJECTED` | `ACTION_ATTEMPT` | `$action_attempt_id` | `WAITING_APPROVAL` | `null` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `REVOKE_BEFORE_EFFECT_LINEARIZATION` | `revocation_wins_before_effect_linearization` | `sha256:fb3db61e27cfa0c00e35d6a740b4ed97cf92b28f6ea303125bc189ea9858a112` | `null` | `DELEGATION_MATCHED; APPROVAL_CONSUMED; DELEGATION_REVOKED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$action_attempt_id` | `BLOCKED_CONFLICT` | `DELEGATION_REVOKED_MID_FLIGHT` | `ACTION_ATTEMPT_STREAM` | `effect_attempts == 0; target_hash == base_hash; duplicate_effects == 0` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `REVOKE_AFTER_EFFECT_LINEARIZATION` | `effect_linearization_wins_before_revocation` | `sha256:443b319232c7eafef2e78ed0c8ef2ebf4c672cbbafe819362cfd71611167fe95` | `null` | `DELEGATION_MATCHED; APPROVAL_CONSUMED; DELEGATION_EFFECT_LINEARIZED; DELEGATION_REVOKED_AFTER_EFFECT_START` | `ACTION_ATTEMPT` | `$action_attempt_id` | `READY` | `DELEGATION_REVOKED_AFTER_EFFECT_START` | `ACTION_ATTEMPT_STREAM` | `effect_attempts == 1; reconciliation_required == true; duplicate_effects == 0` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `DERIVED_APPROVAL_IS_SINGLE_USE` | `derived_approval_cannot_be_replayed` | `sha256:ab4132d9f07c4532c5270910b8d992cf4052ea3877301b260476a39c460cb52f` | `null` | `DELEGATION_MATCHED; APPROVAL_CONSUMED; APPROVAL_REPLAY_DENIED; ACTION_BLOCKED` | `ACTION_ATTEMPT` | `$action_attempt_id` | `BLOCKED_APPROVAL` | `APPROVAL_REPLAY` | `ACTION_ATTEMPT_STREAM` | `delegation_grant_reused_as_execution_authority == false` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `PREDICATE_FIELD_CHANGED` | `any_invalidating_field_change_escalates_to_human` | `sha256:9a0a212e8ac71b91b9be564e85667c18c363fe200c4af8ac2144b8c4dd38498d` | `null` | `PLAN_RESOLVED; POLICY_DECIDED; DELEGATION_REJECTED` | `ACTION_ATTEMPT` | `$action_attempt_id` | `WAITING_APPROVAL` | `DELEGATION_SCOPE_EXCEEDED` | `ACTION_ATTEMPT_STREAM` | `checked_field_count == 16; auto_approved_count == 0` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `TRUST_ANCHOR_ROLLBACK` | `delegation_signed_by_rotated_or_rolled_back_key_is_rejected` | `sha256:b241444711e924dd803455e36173a34793db318575bb5c732af0e3edd15d0699` | `null` | `NONE` | `DELEGATION_GRANT` | `$delegation_grant_id` | `INVALIDATED` | `DELEGATION_TRUST_ANCHOR_INVALID` | `DELEGATION_GRANT` | `actual_state == expected_state` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `AUDIENCE_MISMATCH` | `delegation_from_another_workspace_is_rejected` | `sha256:5604bf960ab3cbab4dfb8afe3b3f74c0b47277e4b3de71b7542019e5f560bda5` | `null` | `NONE` | `ACTION_ATTEMPT` | `$action_attempt_id` | `WAITING_APPROVAL` | `DELEGATION_SUBJECT_MISMATCH` | `ACTION_ATTEMPT_STREAM` | `actual_state == expected_state` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `DESTRUCTIVE_NEVER_DELEGATED` | `gc_backup_migration_key_and_policy_ops_ignore_delegation` | `sha256:cc5d0531b4fe6ccb7175b7773c8dc7838c48fa13e27a77eea8b820a253d5eb4d` | `null` | `NONE` | `ACTION_ATTEMPT` | `$action_attempt_id` | `WAITING_APPROVAL` | `DELEGATION_NOT_DELEGABLE_ACTION` | `ACTION_ATTEMPT_STREAM` | `auto_approved_count == 0` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-DELEGATION-001` | `AUDIT_TRAIL_COMPLETE` | `auto_approved_run_is_fully_traceable_to_delegation` | `sha256:2b72dcad3f92fe0a6c11ed4c76eaf8a335294f207e496d1c24b3b8d9ef1d2673` | `null` | `DELEGATION_MATCHED; APPROVAL_ISSUED; APPROVAL_CONSUMED` | `ACTION_ATTEMPT` | `$action_attempt_id` | `READY` | `null` | `ACTION_ATTEMPT_STREAM` | `approval_mode == POLICY_DELEGATED; delegation_id_present == true; created_from_run_id_resolvable == true` | `true` | `false` | `false` | `DELEGATION_RESOLUTION` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `SECRET_REJECTED_NOT_MASKED` | `credentials_are_rejected_never_sent_to_masker` | `sha256:01308dedfff5d701820d85c4db1f579fdc80554b4897fac0c353beaa4f8fb151` | `null` | `INPUT_READ_STARTED; INPUT_ARTIFACT_CLASSIFIED; INPUT_READ_DENIED` | `INPUT_READ_DECISION` | `$input_read_decision_id` | `DENIED` | `null` | `INPUT_READ_DECISION` | `masker_invocation_count == 0; artifact_persisted_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `SECOND_SCAN_IS_THE_GATE` | `under_masked_output_is_rejected_by_scan2` | `sha256:5f8f90624f319ae668a21eb4e9d8e4754ce215e02e1c81cff5545463dab2af3a` | `null` | `INPUT_MASKING_STARTED; INPUT_MASKING_SPANS_PROPOSED; INPUT_MASKING_REJECTED` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_VERIFICATION_FAILED` | `MASKING_RESULT` | `context_fragment_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `SPAN_OUT_OF_RANGE` | `span_beyond_text_length_or_negative_is_rejected` | `sha256:5f61ed97c6a33eb7dbeb735eca6c0b7e4b338511f1fd9ec33634f379b7a16b88` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_SPAN_INVALID` | `MASKING_RESULT` | `rewriter_invocation_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `SPAN_OVERLAP_OR_NESTED` | `unsupported_overlap_or_nesting_outside_single_candidate_containment_is_rejected` | `sha256:7644dae7c8d9676ec8fdbc4db93d7f229208748f1890f0d9f9d19c8873baa582` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_SPAN_INVALID` | `MASKING_RESULT` | `actual_state == expected_state` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `SPAN_UNSORTED` | `spans_not_sorted_by_start_are_rejected` | `sha256:3fe567e3fc410e393ed70ac5bf3e00ee5e078307a95c3850b087cefe9f63a2a8` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_SPAN_INVALID` | `MASKING_RESULT` | `actual_state == expected_state` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `SPAN_CATEGORY_UNKNOWN` | `unregistered_category_is_rejected` | `sha256:80e29564f92192cb8fdea99e33129627f01b83d38306320b94e4a7add3d9b898` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_CATEGORY_UNKNOWN` | `MASKING_RESULT` | `actual_state == expected_state` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `MASK_RATIO_EXCEEDED` | `whole_document_masked_as_single_span_is_rejected` | `sha256:f9b37015a440046d16e008c9c0f8e93c2042d74fb57c92419117badd4cf109ae` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_RATIO_EXCEEDED` | `MASKING_RESULT` | `actual_state == expected_state` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `MASKER_OUTPUT_MALFORMED` | `non_json_unknown_field_or_hash_mismatch_is_rejected` | `sha256:599cb2e2f4a4beb8fe5fd9b9f175b03ff2c0f34993b49df299c7b50a87b3b44e` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKER_OUTPUT_MALFORMED` | `MASKING_RESULT` | `rewriter_invocation_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `MASKER_UNAVAILABLE_FAIL_CLOSED` | `masker_timeout_or_down_does_not_pass_unmasked_content` | `sha256:dea9be4e1083abdc5086acd134a19ed4d0e11ebf24dd0aaa56e70432f2199dce` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKER_UNAVAILABLE` | `MASKING_RESULT` | `unmasked_fragment_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `MASKER_ISOLATION_INCOMPLETE` | `incomplete_masker_isolation_rejects_raw_pii_path` | `sha256:472d5c42b9d61df2099b40b169041d48a2457b3e1134c27bb6f0da63a2b70ae6` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKER_ISOLATION_INCOMPLETE` | `MASKING_RESULT` | `masker_invocation_count == 0; unmasked_fragment_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `UNICODE_NORMALIZATION` | `span_offsets_stable_across_nfc_with_combining_and_non_bmp` | `sha256:553915ec9d50573c8f126186bad0d195e3e83b31645365060bcf60c9ca277731` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `MASKED` | `null` | `MASKING_RESULT` | `masked_text_matches_expected == true; no_codepoint_split == true` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `NORMALIZATION_PROFILE_MISMATCH` | `masker_profile_or_unicode_data_version_mismatch_is_rejected` | `sha256:195dc67abaec679b28463144a0e0a7919b4290baac6bd2f1d05912e088c00cb0` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_NORMALIZATION_PROFILE_MISMATCH` | `MASKING_RESULT` | `actual_state == expected_state` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `UNICODE_PROFILE_CROSS_PYTHON` | `python_311_and_312_produce_same_ucd14_profile_hash` | `sha256:d8260768d338e76e7c4889bb87d34bc494b59ccd25a6c139fe20fde257688f14` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `MASKED` | `null` | `MASKING_RESULT` | `artifact_format_equal == true; normalization_profile_artifact_hash_equal == true; source_normalized_hash_equal == true` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `UNICODE_PROFILE_ARTIFACT_MISSING` | `missing_or_hash_mismatched_ucd14_assigned_bitmap_fails_closed` | `sha256:ea6afc5c9070e4855690d107461538fc4aed7c15c9d763ae21968719a5ab0d63` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_NORMALIZATION_ARTIFACT_MISSING` | `MASKING_RESULT` | `artifact_format_valid == false; normalization_invocation_count == 0; masker_invocation_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `UNICODE_UCD14_ASSIGNED_GUARD` | `codepoint_unassigned_in_ucd14_is_rejected_before_normalization` | `sha256:98f3ff10a2e397edae6b2c42072ce812281a3faf3b5a28529ac9db72bf380dee` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_UNSUPPORTED_CODEPOINT` | `MASKING_RESULT` | `normalization_invocation_count == 0; masker_invocation_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `GRAPHEME_SPLIT` | `risky_grapheme_codepoint_span_is_rejected_without_runtime_dependency` | `sha256:bc737aa1ee99f86ac64d46e1d00cb1dde359e843d1c9ed1bd070f5a8322e10fc` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_GRAPHEME_SPLIT` | `MASKING_RESULT` | `rewriter_invocation_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `MASK_RATIO_BOUNDARY` | `llm_additional_union_mask_ratio_boundary_is_deterministic` | `sha256:ac15df8a567f805efbcfcc6682c7fd4445643c3c50030e1c4e50dd9d0bdb6f22` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `MASKED` | `null` | `MASKING_RESULT` | `ratio_0_59_passes == true; ratio_0_60_passes == true; ratio_0_61_rejects == true; deterministic_scan1_over_0_60_passes == true` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `SCAN1_CANDIDATE_UNION` | `deterministic_scan_candidates_are_union_with_llm_additions` | `sha256:0f6585d02eff521ecdb2134d52cb1a80d0ed8eec44ffd9c848f4d5345c04cc77` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `MASKED` | `null` | `MASKING_RESULT` | `deterministic_candidate_spans_preserved == true; llm_only_spans_are_additive == true; deterministic_scan1_over_0_60_is_not_ratio_rejected == true` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `SPAN_CONTAINMENT_EXPANSION` | `same_category_llm_span_containing_one_scan1_candidate_is_canonicalized` | `sha256:862ed5d52afee7a10a5e9e3d1f1aad998d536736bb8822d940911515329f3f7c` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `MASKED` | `null` | `MASKING_RESULT` | `canonical_span_is_containing_span == true; masked_content_is_deterministic == true` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `SPAN_CONFLICT_PARTIAL` | `partial_or_cross_category_overlap_is_rejected` | `sha256:747c2d46f6d3ce12174e82a208f721bf8f897c02b2c0e2adf192acb7506296d1` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `MASKING_SPAN_CONFLICT` | `MASKING_RESULT` | `rewriter_invocation_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `NATIONAL_ID_REJECTED_NOT_MASKED` | `national_id_is_rejected_before_masker_invocation` | `sha256:52380063bc9afa26662c1f465f8fe0dad70797b5752fd918bb287c01faa59803` | `null` | `NONE` | `INPUT_READ_DECISION` | `$input_read_decision_id` | `DENIED` | `null` | `INPUT_READ_DECISION` | `masker_invocation_count == 0; artifact_persisted_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `REWRITER_DETERMINISM` | `same_source_and_spans_always_produce_same_output` | `sha256:46e94a7fd82dc3033dd416771a1c970efa99740730ea5da29c9bddcd519600a3` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `MASKED` | `null` | `MASKING_RESULT` | `masked_content_hash_a == masked_content_hash_b` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `NO_LLM_PATH` | `clean_and_reject_paths_never_invoke_masker` | `sha256:8678080c01ff66ba099740ca26e8f2b870215f7d4465030c8cac231d1bf536c8` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `CLEAN` | `null` | `MASKING_RESULT` | `masker_invocation_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `PROMPT_INJECTION` | `instructions_inside_input_cannot_change_masking_policy` | `sha256:f0b5b20f9f1d7633d323047bc8a589888957868347a2d763dc569be487f72481` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `null` | `MASKING_RESULT` | `masking_policy_version_unchanged == true; mask_tokens_unchanged == true; reject_categories_unchanged == true` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `NO_SECRET_IN_RECEIPT` | `masking_receipt_contains_no_secret_value` | `sha256:7b6846ce50547433500403c3e29724f5c718e276a0c9515de97f9765bccc952f` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `MASKED` | `null` | `MASKING_RESULT` | `canary_occurrences_in_receipt == 0; canary_occurrences_in_ledger == 0; canary_occurrences_in_logs == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `NO_SECRET_IN_RECEIPT_REJECTED` | `secret_input_is_rejected_and_receipt_contains_no_secret_value` | `sha256:aa98be2071cf3e040c6e7bd9d6683db9d5ef8be912fac5eb60508aa3cf8a399b` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `REJECTED` | `null` | `MASKING_RESULT` | `canary_occurrences_in_receipt == 0; canary_occurrences_in_ledger == 0; canary_occurrences_in_logs == 0; masker_invocation_count == 0; artifact_persisted_count == 0` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |
| `AT-MASKING-001` | `NO_AUTO_DOWNGRADE` | `masking_does_not_lower_data_classification` | `sha256:ba0a9155c6192eeef5fd7ec69a40b5e154abab39330582fb5ee87d4e6f8831aa` | `null` | `NONE` | `MASKING_RESULT` | `$masking_result_id` | `MASKED` | `null` | `MASKING_RESULT` | `classification_after == classification_before` | `true` | `false` | `false` | `MASKING_PIPELINE` | `UNVERIFIED` | `null` |

`PASS`時は次を含む`TestEvidenceManifest`を生成し、`evidence_manifest_hash`へ保存する。

* Test ID、Case ID
* Expectation Descriptor Hash、Input Fixture Hash
* Git Commit
* Schema Set Hash
* Policy Hash
* Plan Content Hash
* Execution Plan Hash
* Runtime Envelope Spec／Attestation Hash
* Input Read Capability Set／Evidence Hash。該当時
* Fault Point
* Expected／Actual Event Sequence
* Expected Subject Type／ID
* Expected／Actual State
* Expected／Actual Error Code
* Trace Scope
* AssertionごとのExpected／Actual／Result
* 自動再実行の実績
* Release Decision有無
* Manual Queue Record有無
* Event Chain Head Hash
* Receipt／Journal／Remote Evidence Hash
* Log Artifact Hash
* Started／Completed At
* Test Runner Identity
* Result=`PASS | FAIL | BLOCKED`

`AT-PLAN-DETERMINISM-001`では、異なる`run_id`とAuthority Envelopeを持つ2回のBuildを行い、最低限次を直接検証する。

```text
plan_content_hash_a == plan_content_hash_b
execution_plan_hash_a != execution_plan_hash_b
run_id_a != run_id_b
semantic_diff(plan_content_projection_a, plan_content_projection_b) == empty
```

## 19.2 Phase Gateへの試験割当

| Test | 必須Phase |
|---|---|
| `AT-PLAN-001` | MVP0-A、MVP0-B、MVP0-C |
| `AT-APPROVAL-001`／`AT-APPROVAL-002`／`AT-CLOCK-SKEW-001` | MVP0-A以降のApproval Phase |
| `AT-CRASH-001`／`AT-FAULT-IO-001`／`AT-FAULT-GUARD-001` | MVP0-A、MVP1-A、MVP1-D、MVP1-E |
| `AT-PATH-001`／`AT-INPUT-PATH-001`／`AT-WSL-BOUNDARY-001` | MVP0-A |
| `AT-SANDBOX-001` | MVP0-B、MVP1-A |
| `AT-CONFIG-001` | MVP0-B、MVP0-C |
| `AT-AUTH-001` | MVP0-C、MVP1-B、MVP1-C、MVP1-D、MVP1-E |
| `AT-FALLBACK-001` | MVP1-C |
| `AT-REMOTE-PREP-001` | MVP0-C |
| `AT-SCHEMA-CONDITIONAL-001`／`AT-SCHEMA-COMPLETE-001` | MVP0-A以降 |
| `AT-EVENT-ORDER-001`／`AT-LEDGER-TAMPER-001` | MVP0-A以降 |
| `AT-POLICY-APPROVAL-001` | MVP1-A、MVP1-D、MVP1-E、Enterprise |
| `AT-OUTBOX-001` | MVP1-E |
| `AT-BUDGET-001` | MVP1-D |
| `AT-BLIND-001` | Blind Reviewer |
| `AT-FENCE-001` | MVP0-A単一Worker復帰、MVP2-B |
| `AT-TEST-MANIFEST-001`／`AT-MANIFEST-SUBJECT-001` | 全Phase |
| `AT-PLAN-DETERMINISM-001` | MVP0-A以降 |
| `AT-CONTROL-DATA-001` | MVP0-A以降 |
| `AT-POLICY-STALE-001`／`AT-EMERGENCY-RECOVERY-001` | MVP0-A、MVP0-C、MVP1-A、MVP1-D、MVP1-E、Enterprise |
| `AT-EVENT-MAPPING-001` | MVP0-C、MVP1-D、MVP1-E |
| `AT-RUN-TERMINAL-001` | MVP0-A以降 |
| `AT-APPROVAL-UX-001` | MVP0-A以降のHuman Approval Phase |
| `AT-PERF-001` | MVP0-A、各Release Gate |
| `AT-MIGRATION-001`／`AT-DRAIN-001`／`AT-GC-001` | MVP0-A以降 |

本表はTest ID単位の概要であり、**判定に使う正本はCase単位の`registries/tests.yaml`の`phase_scope`である。** 上表と`phase_scope`が矛盾する場合はRegistryを正とし、Spec Lintが検出する。

規範Manifest全体は**39 Test ID／127 Case**である。ただしこれは全Phaseの合計であり、**個々のRelease Scopeの必要数ではない。**

| Release Scope | 必要Case | 必要Test ID | 必要Gate |
|---|---:|---:|---:|
| MVP0-A | 108 | 31 | 45 |
| MVP0-B | 69 | 22 | 別途定義 |
| MVP1-A | 80 | 26 | 別途定義 |
| MVP0-C | 53 | 24 | 別途定義 |
| MVP1-D | 71 | 27 | 別途定義 |

v1.6では§26.1が「初回GO対象はMVP0-Aのみ」としながら、§0.6とVerifierが86 Case全PASSを要求していた。86 Caseには`AT-FALLBACK-001`（MVP1-C）、`AT-OUTBOX-001`（MVP1-E）、`AT-BLIND-001`（Blind Reviewer）という**§0.3が「実装しない」と宣言した参照仕様Phase専属のCase**が含まれるため、MVP0-AのRuntime GOは原理的に到達不能だった。v1.8はScope単位判定でこれを解消する。

参照仕様PhaseはVerifierの`--release-scope`として指定できない。指定した場合は終了Code 4で拒否する（`AT-VERIFIER-017`）。

各Phaseは本文Gateと本表を機械結合する。`UNVERIFIED`、Hash欠落、Expected Event Sequence不一致、未注入Fault PointはPASSに数えない。

# 20. 実装開始時のDecision

## Decision

**技術スパイクおよびSpec Foundation：GO**
**MVP0-A本実装：Spec Lintと§16.1スパイク合格を条件とするGO**
**MVP0-C外部実行、Paid Execution、External Effect、Windows Write、Parallel Worker：Feature Flag OFF**

MVP0-AのRuntime GOは§3.13の全GateとEvidenceが揃うまで出さない。設計書の完成、静的レビュー、AI生成Codeの存在をRuntime PASSとして扱わない。

## Why

本書はv1.3とv1.4を統合し、単一SQLite、WSL2 Linux側FS、Python、改ざん検知、同一ホストPlan決定性という実装前提を1つの正本へ固定した。一方、実装、Schema Fixture、Migration、Crash Evidence、Filesystem Corpusは未作成であり、実測を伴わない。

## Next 3 Actions

1. Registry、Spec Linter、Registry Snapshot Builderが本書内参照と件数を機械検査できる状態を維持する（v1.8で同梱済み。以後は編集のたびにCIで検証する）。
2. §16.1の7技術スパイクを破棄前提で実行する。特にスパイク3（`openat2`）とスパイク6（Faultable I/O）は、成立しない場合にADR-002／§3.10.2の書き換えが必要となる。
3. 11 Schemaの最小垂直スライスを実装し、Crash 2点から開始する。

Runtime GO Verifier v1.2と`AT-VERIFIER-*`は実装着手前に完成している必要がある。**判定器が先、被判定物が後である。** v1.6ではこの順序が逆転しており、無検証の判定器が唯一の合否根拠になっていた。

# 21. 承認・自己チェックが必要な時点

単独開発ではProduct Owner、Security Reviewer、Budget Owner、Legalが同一人物になり得るため、組織的SoDを主張しない。次の時点では、署名Approvalまたは記録済み自己チェックを要求する。

* Workspace Write前
* Paid ExecutionのBudget予約前
* External ProviderへRestricted以上を送信する前
* Compensating Decisionで未照合Effectを引き渡す前
* Emergency RecoveryでCompensating EventをAppendする前
* Raw Provider Output PayloadをRetention期限前に削除する前
* Migration／Schema Set Hash変更をDeployする前
* Feature FlagをOFFからONへ変更する前
* Threat Model、Error Classification、Plan Content Projectionを変更する前

組織利用へ移行する場合は、承認主体を別Identity／別UID／別Serviceへ分離し、Enterprise Hardening Gateを実装対象へ戻す。

# 22. 残余リスク

| リスク | 状態／対応 |
|---|---|
| 同一UIDがHarnessと鍵を改変可能 | 改ざん検知のみ。改ざん耐性を主張しない |
| WSL2／Storageの電源断耐久性 | fsync呼出までを検証。Device同期はUNVERIFIED |
| `openat2`／Mount情報の環境差 | スパイクとFilesystem Corpusで検証 |
| SQLite単一DB破損 | Backup／Restore、Integrity Check、Evidence ExportをGate化 |
| Raw Provider OutputのPrivacy | Retention ClassとPayload削除を分離 |
| Secret Scanの完全性 | Canary Corpusで測定。任意Secretの完全検出は主張しない |
| AI生成による仕様逸脱 | Spec Lint、実行時不変条件、反復上限、Code Review |
| 参照仕様の誤解禁 | Feature Flag OFF、Spec Lint、Runtime Policy |
| Emergency ProfileのRollback | Version Floor、Trust Anchor、Attestationで防止 |
| 単独開発のレビュー限界 | Runtime Evidenceと公開Issueで補完。SoDは未達 |
| 19本のRunbook作成負荷 | 各Feature Delivery前に必要分を作成し、Owner／Trigger／禁止操作を持たせる |
| 外部依存ライブラリの変更 | Lock File、SBOM、License Check、Version Pin、月次更新＋`TIER_2`再認定（§16.5、§26.6） |
| Plan決定性のHash順序依存 | 別プロセス三重Build＋`set`反復のAST禁止（§1.11.1）。ただし網羅性は主張しない |
| fsync境界の未検証 | `T2_CACHE_DROP`で主要Caseを検証。`T3`デバイス電源断は`UNVERIFIED`（§3.10.2） |
| Runtime GO Verifier自身の欠陥 | `AT-VERIFIER-*`否定系試験、`verifier_self_test`のEvidence必須化、Verifier Source HashのRelease Manifest記録 |
| 保守時の再認定コスト | §26.6の3階層。人手計測EvidenceはSubtree Hashへ束縛し再利用可能とする |


## 22.1 v1.8で追加した残余リスク

| # | 残余リスク | 内容 |
|---|---|---|
| R-06 | 単独開発における委任は安全統制ではない | Maker／Approver／Operatorが同一人物のため、`DelegationGrant`は独立した第三者統制を提供しない。提供するのは確認操作の削減、監査証跡、範囲の明示化と即時失効の3点だけである（ADR-004、ADR-006） |
| R-07 | 委任失効とEffectの競合は完全には防げない | DB Commitを線形化点とするため、線形化点より後の失効は進行中の外部Effectを止められない。`DELEGATION_REVOKED_AFTER_EFFECT_START`として監査しReconciliation対象とする |
| R-08 | LLMマスキングは決定論スキャナを代替しない | 多層防御の一層である。日本語PII検出精度は未測定であり、Scan #1／#2のRule Set品質が上限を決める |
| R-09 | ローカルMaskerのSwap／メモリダンプ対策は不完全 | 単一UIDでは`/proc/<pid>/mem`の読出しを防げない。`mlockall`はBest Effortであり保証ではない |
| R-10 | 正規化決定性はUnicodeの外部保証へ依存する | Unicode Normalization Stability Policyが「割当済み文字の正規化形は将来Versionでも不変」と保証することに依拠する。この保証が覆る場合、UCD 14.0 Guardだけでは処理系差異を吸収できない |

# 23. v1.5統合対応状況

## 23.1 統合で解消した事項

* v1.3／v1.4の二重正本を廃止し、本書を単独正本化。
* Snapshot IDをPlan Contentから除去。
* Plan BuilderをFrozen Input＋二重Buildへ変更。
* 単一SQLiteとArtifact Directory fsyncを明文化。
* WSL2 Linux側Mount BoundaryをRuntime強制。
* Runへ`BLOCKED_REPAIR_REQUIRED`を追加し、未照合Effectの全終端を禁止。
* Approval UX、Fault Injection、Emergency RecoveryをMVP0-A本文へ統合。
* `expected_stream_sequence`と`store_version`を概念分離。
* Test ManifestのExpectation HashとInput Fixture Hashを分離。
* Gate、Test、Case件数をRegistry自動生成対象へ変更。
* 10 Fault Point、I/O Fault、Migration、Backup、Drain、GC試験を追加。
* 最小垂直スライスを11 Schemaへ修正。

## 23.2 未検証

* 26 JSON Schema実ファイルとFixture
* SQLite Migration、Backup／Restore
* Python `openat2`実装とFallback
* Mount Boundary判定
* Faultable I/OとCrash Evidence
* Ed25519鍵管理
* Approval UX実測
* Performance実測
* Local／External Provider Adapter

全項目は証跡作成まで`UNVERIFIED`とする。

## 23.3 v1.6→v1.8の訂正内容

| # | 分類 | v1.6の状態 | v1.8の是正 |
|---|---|---|---|
| 1 | Blocker | Verifier v1.0がEvidence内容を検証せず、`required_evidence_areas`空配列を許容。ダミー1ファイルで`RUNTIME_GO`が成立した | Verifier v1.1。領域必須集合、Evidence一意性、Evidence内容突合、escape検査統一、Python下限比較を追加。`AT-VERIFIER-*`22 Caseで実証 |
| 2 | Blocker | MVP0-A GOに86 Case全PASSを要求。うち16 Caseが参照仕様Phase専属で達成不能 | Release Scope単位判定。必要数はRegistryから導出する（v1.8時点70 Case／29 Test ID、v1.8時点110 Case／31 Test ID） |
| 3 | High | 件数がVerifier・Schema・本文の3箇所に重複 | Registry一元化。VerifierはRegistry Snapshotから導出 |
| 4 | High | `test_manifest_hash`／`schema_set_hash`が形式検査のみで何とも突合されない | `test_manifest_hash`をRegistry導出のScope別値と突合 |
| 5 | High | Plan決定性を同一プロセス二重Buildで検証。`PYTHONHASHSEED`依存を検出不能 | 別プロセス三重Build＋AST検査＋Locale／TZ固定（§1.11.1） |
| 6 | Medium | Crash試験がfsync境界に到達しないままGate文言が耐久性を主張 | 耐久性Tier 3段階を定義。`durability_tier`をEvidence必須項目化（§3.10.2） |
| 7 | Medium | Commit毎にGO失効するが再認定手順が未定義。人手計測Approval UXが毎回必要 | §26.6の再認定3階層。人手計測EvidenceをSubtree Hashへ束縛し再利用可能化 |
| 8 | Medium | CI定義が存在しない | §16.5でTrigger別実行内容、必須Check、依存保守、Coverage方針を定義 |
| 9 | Medium | 生成spec・Registry・CLAUDE.md／AGENTS.mdが実ファイルとして存在せず、新規スレッドで同品質を再現できない | Registry、Linter、Snapshot Builder、`CLAUDE.md`、`AGENTS.md`を実ファイルとして同梱 |
| 10 | Low | Appendix Cが旧版のファイル名を指し、規範Fixtureの`producer`が旧版のVersionを名乗っていた | Version表記を1.7へ統一。Spec Linterが版数ドリフトを検出 |
| 13 | Low | §19.1のSubject／State表が「State例」の部分列挙で、規範Manifestが使う`BLOCKED_APPROVAL`等を含まず、State照合が実装不能だった | 完全State Enumへ置換（§19.1） |
| 14 | Low | §3.13 Gate 2が機械可読なTest参照を持たず、Gateの根拠Caseを検証できなかった | `test_refs_mode=ALL_IN_SCOPE`を導入し、Scope内全Caseの列挙を必須化 |
| 11 | Low | Gate 4／13がScope外Testを参照 | Case単位`phase_scope`で解消。Spec Lintが再発を検出 |
| 12 | Low | `run_verification.sh`の出力先が§28と不一致。Evidence Tree内部へ書き戻していた | Evidence Root外へ出力。§28と一致 |


## 23.4 v1.8→v1.9の改訂記録（Owner Decision適用）

本節はv1.8として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/LLM-FIRST-CONTEXT-DECISIONS.md`のOwner Decision（2026-08-16確定）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-16 | §1.7.1 | 正本File名の是正。`error-codes-v1.json`はRepositoryに実在せず、実際の正本は`design-source/registries/errors.yaml`だった | B-1b |
| 2 | 2026-08-16 | §1.7.1 | Error Code表を`errors.yaml`の全数へ一致させた。ADR-006（委任）とADR-007（マスキング）が追加したCodeが本表に取り込まれていなかった | B-1b |
| 3 | 2026-08-16 | §1.7.1 | `CONTEXT_BUDGET_EXCEEDED`を追加し、Classification・Retry可否・終端Stateを定義した | B-1b |
| 4 | 2026-08-16 | §15.9 | Core Schemaの複数Version登録形式、`active_write_version`、`schema_catalog_hash`と`schema_set_hash`の分離を規定した | D-06 |
| 5 | 2026-08-17 | §15.2／§15.9 | Context／Token系5 SchemaへRequired Fieldを追加する`2.0.0`を登録した。`1.0.0`は`read_only`として保持し、Upcasterと Gate Corpus を同梱した | D-1a |
| 6 | 2026-08-17 | §19.1 | `AT-SCHEMA-COMPLETE-001/ALL_VALID`の`expectation_descriptor_hash`を導出値へ是正した。Case本文（`scenario`・`assertions`）を改めた際に再生成していなかった | E-1 |

### 版番号をv1.9へ上げた（Owner Decision E-1）

上記1〜6は**規範的な改訂**である。v1.6→v1.8の前例（§23.3）に従い、規範改訂には
新しい版番号を与える。2026-08-16のOwner Decision E-1により**v1.9へ改訂**した。

版番号の変更は次を同時に伴う。本改訂で1〜3を実施した。

1. 正本Fileのリネーム（`design-v1.8-runtime-go.md` → `design-v1.9-runtime-go.md`）と、
   README・CI・`run_verification.sh`・Task Brief・spec-manifestの`source_version`／
   `source_file`の追随。
2. 日本語名Fileの扱いの確定。**英語名を正本、日本語名を同期ミラー**とし、
   両者のBytes一致を`tools/check_design_mirror.py`が機械検査する。
   同内容2Fileは、片方だけが更新された瞬間に「正本を名乗るものが2つある」状態になる。
   命名規則を決めるだけでは防げないため、一致を検査する側を置く。
3. 既存BLOCKED Recordの`design_sha256`束縛の更新。`design_sha256`は不変Fieldであり、
   `BLOCKED-RECOVERY.md`の復帰条件3に従って後継Recordを新規発行する。
   論理Taskごとに`OPEN`は1件に保ち、旧Recordは`SUPERSEDED_BY_DESIGN_HASH`で閉じる。

未実施は次の1点であり、**Owner Decision E-1第5項により本改訂の範囲外**とした。

* Release Trust Anchorの再束縛。設計書Hashが動いた時点で旧Trust Anchorと
  旧Release BindingをRelease根拠に使えない。**再利用しない。**
  Release準備時の別Taskとする。

**Releaseは作成しない。** Runtime判定は`BLOCKED_EVIDENCE_MISSING`のままである。
本文Hashが動いた事実は、`registry-snapshot.json`の`design_sha256`、
`spec/spec-manifest.json`の`source_hash`、READMEのHash行、後継BLOCKED Recordが同時に記録する。

## 23.5 v1.9→v1.10の改訂記録（Owner Decision M-Q1〜M-Q4適用）

本節はv1.9として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/MIGRATION-LOSS-POLICY.md`のOwner Decision（2026-08-17確定）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-17 | §15.2 | Upcaster結果が持つFieldを`from_version`／`to_version`／`upcaster_code_hash`／`conversion_reason`／`lossless`として明記した。v1.9は「Code Hash、変換理由、Lossless可否」と述べるだけでField名を定めておらず、実装との対応が取れなかった | M-Q2 |
| 2 | 2026-08-17 | §15.2 | **`MIGRATED`はLosslessを意味しない**ことを明記した | M-Q1 |
| 3 | 2026-08-17 | §15.2 | 行き先の無いFieldを監査・証跡／無害／未分類の3分類とし、**監査・証跡Fieldと未分類Fieldを落とす変換を禁止**した | M-Q1 |
| 4 | 2026-08-17 | §15.2 | 無害Fieldの追加にOwner Decisionを要すると定めた | M-Q4 |
| 5 | 2026-08-17 | §15.2 | Upcasterが変換先Schema全体（Required／Type／Enum／Const／Pattern／入れ子Object／`additionalProperties`）で検証してから成功を返すと定めた | M-Q2 |
| 6 | 2026-08-17 | §15.2 | Upcaster結果の語彙をMigration Tool専用とし、Core Schema／Ledger Event／Runtime Error Codeへ追加しないと定めた | M-Q2 |
| 7 | 2026-08-17 | §23.4 | v1.9改訂時に混入したMarkdown不備（表を分断する空行、§24.1直前の空行欠落）を是正した | — |

### 設計と実装の不一致を解消した改訂である

改訂1〜6は、`TASK-DESIGN-MIGRATION-HARDENING-001`で**実装側が先行して**満たしていた
規範である。当時の§15.2はそこまで求めておらず、実装のほうが厳しい状態だった。
その差分は`docs/decision/MIGRATION-LOSS-POLICY.md`へ記録し、設計書は無断で変更しなかった。
本改訂でOwner決裁を経て設計側へ反映し、不一致を解消した。

### 版番号をv1.10へ上げた（Owner Decision M-Q2）

改訂1〜6は**規範的な改訂**である。v1.6→v1.8（§23.3）、v1.8→v1.9（§23.4）の前例に従い、
規範改訂には新しい版番号を与える。M-Q2は「現在のv1.9を黙って書き換えず、
次版v1.10として反映する」と定めており、本改訂はこれに従う。

版番号の変更は次を同時に伴う。本改訂で1〜3を実施した。

1. 正本Fileのリネーム（`design-v1.9-runtime-go.md` → `design-v1.10-runtime-go.md`）と、
   README・CI・`run_verification.sh`・Task Brief・spec-manifestの`source_version`／
   `source_file`の追随。旧名は`tools/check_design_reference_currency.py`が
   歴史参照として固定し、運用Pathへ残らないことを機械検査する。
2. 日本語名Fileの同期ミラー維持。両者のBytes一致を`tools/check_design_mirror.py`が検査する。
3. 既存BLOCKED Recordの`design_sha256`束縛の更新。後継Recordを新規発行し、
   論理Taskごとに`OPEN`は1件に保ち、旧Recordは`SUPERSEDED_BY_DESIGN_HASH`で閉じる。

### 本改訂で行っていないこと

Owner Decision M-Q3により、`ContextSelectionReceipt`と`TokenProfileSnapshot`が
現行`2.0.0`では実Recordが常に移行不能になることを**許容**した。
この問題だけを理由に`3.0.0`を起票しない。旧Recordは`1.0.0`のまま読み取り可能である。

Release Trust Anchorの再束縛は本改訂の範囲外である。設計書Hashが動いた時点で
旧Trust Anchorと旧Release BindingをRelease根拠に使えない。**再利用しない。**

**Releaseは作成しない。** Runtime判定は`BLOCKED_EVIDENCE_MISSING`のままである。

## 23.6 v1.10→v1.11の改訂記録（Owner Decision適用）

本節はv1.10として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/UI-SCOPE-AND-DENYLIST-V111.md`のOwner Decision（2026-08-18確定）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-18 | §3.10.1 | Emergency Recoveryの禁止操作を**9操作の名称付き表**として明記した。v1.10本文は読点で8区切りに書かれており、`AT-EMERGENCY-RECOVERY-001/DENYLIST`が要求する`denied_operation_count == 9`と本文から突き合わせられなかった | Denylist決定 |
| 2 | 2026-08-18 | §3.10.1 | `NEW_EFFECT_PREPARE`と`NEW_EFFECT_EXECUTE`を別操作として数えると定めた。不変条件#2が規定する境界に一致させた | Denylist決定 |
| 3 | 2026-08-18 | §3.10.1 | 拒否したすべての操作で`new_effects == 0`を測定すると定めた | Denylist決定 |
| 4 | 2026-08-18 | §4.1 | CLI／Control API／承認UIを3つの別Surfaceとして明記し、承認UIの実装PhaseをMVP0-Bと定めた | UI Scope決定 |
| 5 | 2026-08-18 | §4.1 | UIが存在しない状態でUI測定値を生成しないと定めた | UI Scope決定 |
| 6 | 2026-08-18 | §19.1 Registry | `AT-APPROVAL-UX-001`の`DEFAULT_FIELDS`／`REAPPROVAL_DIFF`／`MEDIAN_TIME`を`MVP0-A`の`phase_scope`から除外した。`NO_BYPASS`は`SOURCE_SCAN`でUIに依存しないためMVP0-Aへ残した | UI Scope決定 |

### なぜ除外が必要だったか

`AT-APPROVAL-UX-001`の3 Caseは`fault_point`が`UI_STATIC_CHECK`／`UI_RENDER`／
`HUMAN_MEASUREMENT`であり、承認UIの実在を前提とする。しかしMVP0-Aの実装範囲に
承認UIは無く、`src/harness/presentation/`はCLIだけである。

MVP0-AのRuntime GOがこの3 Caseを要求し続ける限り、判定は**原理的に到達不能**である。
v1.6→v1.8で解消した「参照仕様Phase専属Caseを要求して到達不能になる」構図（§23.3）と
同じ形であり、同じ理由で解消する。

**UIを実装せずにCaseだけを通す道は採らない。** CLIを承認UIとみなす、
あるいは人手計測の数値をつくる、のいずれも「検証していないものを検証したことにする」
経路であり、Runtime GO判定の根拠を毀損する。

### 版番号をv1.11へ上げた

改訂1〜6は**規範的な改訂**である。§23.3〜§23.5の前例に従い、規範改訂には
新しい版番号を与える。**v1.10本文は書き換えていない。** 新しい名前で書き出してから
旧名を削除しており、v1.10は「UIのScope不整合とDenylistの曖昧さが残っていた版」として
Git履歴とBlock Recordの`design_sha256`束縛の中に残る。

MVP0-AのCase数はRegistryから導出する。本改訂で110から107へ変わった。
件数を本文へ手入力しない（不変条件#18）。

**Releaseは作成しない。** Runtime判定は`BLOCKED_EVIDENCE_MISSING`のままである。
Release Trust Anchorの再束縛は本改訂の範囲外であり、旧Trust Anchorを再利用しない。

## 23.7 v1.11→v1.12の改訂記録（Owner Decision適用）

本節はv1.11として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/EVIDENCE-SCOPE-V12.md`のOwner Decision（`DEC-U-EVIDENCE-SCOPE`。
2026-08-18確定。Option B採用）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-18 | §0.2 | Registry正本へ`evidence-areas.yaml`を追加した。Evidence領域の定義と`phase_scope`の正本である | Evidence Scope決定 |
| 2 | 2026-08-18 | §0.2 | `registry-snapshot.json`がScopeごとの必要Evidence領域集合を持つと定めた。領域の定義Catalogは`evidence_areas`が持ち、**定義と要求を分ける**と明記した | Evidence Scope決定 |
| 3 | 2026-08-18 | §0.6 | 「13 Evidence領域が全て存在」を「当該ScopeのEvidence領域が全て存在」へ改めた。Scope外領域をManifestへ足せないことも明記した | Evidence Scope決定 |
| 4 | 2026-08-18 | §0.6 | 偽装手口表へ、Scope別必要集合を空にする手口とScope外領域を足す手口の2行を追加した | Evidence Scope決定 |
| 5 | 2026-08-18 | §3.13 | MVP0-Aの必要Evidence領域を13から12へ改めた。件数はRegistry導出であり本文は転記である | Evidence Scope決定 |
| 6 | 2026-08-18 | §26.2 | 必須領域がScopeごとに異なることを明記し、`approval_ux`をMVP0-B以降と定めた。`NO_BYPASS`を`approval_ux`の充足根拠にしないと明記した | Evidence Scope決定 |
| 7 | 2026-08-18 | §26.6 | `human_measured`は再利用可否判定にだけ使い、必須性には関与しないと明記した。過去Releaseが無い時点で`reused_from`が成立しないことも明記した | Evidence Scope決定 |
| 8 | 2026-08-18 | §27 | `required_evidence_areas`がScope依存であり、過不足なく一致させると明記した | Evidence Scope決定 |
| 9 | 2026-08-18 | §28 | Verifier v1.3が必要Evidence領域をScopeから導出し、未知Scopeと空集合を`INPUT_INVALID`で拒否すると明記した | Evidence Scope決定 |

### なぜ分割が必要だったか

§26.1は**v1.8から**「Release Scopeは判定の入力である」と定め、必要Case集合・
必要Gate集合・`test_manifest_hash`をRegistryからScope別に導出していた。
しかしEvidence領域だけは`tools/build_registry_snapshot.py`の
「Scopeによらず必須」という定数のままで、その外に取り残されていた。

v1.11（§23.6）は`AT-APPROVAL-UX-001`の3 CaseをMVP0-Aの`phase_scope`から外した。
**Caseは外れたが、領域は外れていなかった。** 結果、MVP0-Aには
「対応するCaseが1件も無いのに充足を要求される領域」が残った。

`approval_ux`を満たす経路は2つしかなく、どちらも成立しない。承認UIは未実装で
（§4.1.1がMVP0-Bと定める）、過去Releaseが無いので`reused_from`も使えない。
MVP0-AのRuntime GOは、この一点だけで**原理的に到達不能**だった。

v1.6→v1.8（§23.3）とv1.10→v1.11（§23.6）で解消したのと同じ構図である。
層がCaseからEvidence領域へ移っただけであり、同じ理由で解消する。

### 通そうと思えば通せた

Verifierは`human_measured`を`reused_from`の可否判定にしか使っていない。
新規Evidenceなら`status=PASS`と`evidence_manifest_hash`があれば受理する。
`approval-ux-report.json`を機械生成して`PASS`と書けば**検査は通った**。

**通していない。** MVP0-Aに残った承認UX Caseは`SOURCE_SCAN`の`NO_BYPASS`だけであり、
それだけを根拠に領域をPASSにすると、実施していないUX検証を実施したと主張することになる。
Verifierが受理することと、その主張が正しいことは別である。

この性質は本改訂の後も残る。`human_measured`の意味を増やさないと決めたためである。
だからこそ**領域を要求しないScopeを正しく定義する**ことが対処になる。

### 要求を減らす改訂なので、減らし方を固定する

Scope別化は必須集合を小さくする変更である。次を同時に固定した。

| # | 固定した事項 | 実装 |
|---:|---|---|
| 1 | Scope未指定・未知Scopeで全領域へフォールバックしない | `--release-scope`必須。`release_enabled_phases`外は`INPUT_INVALID` |
| 2 | Scope別必要集合が空のSnapshotを拒否する | Verifier `INPUT_INVALID`。生成側も`required_areas_for`で停止 |
| 3 | Manifest側でScopeの必要集合を削減できない | 不足は`missing`、Scope外の追加は`out of release scope`でFAIL |
| 4 | MVP0-Bで`approval_ux`が無ければ拒否する | `phase_scope`にMVP0-Bを含めた |
| 5 | 領域名を変えない・消さない | `approval_ux`はCatalogに残り、MVP0-AのRequiredから外れるだけ |

### Block Recordの解決Outcomeを1つ足した

これまでの解決Outcomeは`SUPERSEDED_BY_RENUMBERING`と`SUPERSEDED_BY_DESIGN_HASH`の
2つだけだった。どちらも「別Recordへ引き継いだ」を意味し、
`resolution.successor_blocker_id`を必ず持つ。

**塞がりが実際に解消した場合の閉じ方が無かった。** これまで解消した例が無く、
版が上がるたびに引き継ぐだけだったためである。

v1.12は`approval_ux`の充足不能を実際に解消する。引き継ぎ先が無いので、
既存の2つで閉じるとどちらも事実に反する。`SUPERSEDED_BY_DESIGN_HASH`で
閉じれば「まだどこかで塞がっている」と読め、存在しない後継を指すことになる。

| Outcome | 意味 | `successor_blocker_id` |
|---|---|---|
| `SUPERSEDED_BY_RENUMBERING` | Task改番により別Recordへ引き継いだ | **必須** |
| `SUPERSEDED_BY_DESIGN_HASH` | 設計書Hash変更により新Hashへ束縛し直した | **必須** |
| `RESOLVED_BY_FIX` | 塞がりそのものが解消した | **持たない** |

`RESOLVED_BY_FIX`は`resolution.verified_by`を必須とする。何をもって解消と
判断したかを記録しないと、「直したことにする」経路になる。
`verified_by`には解消を確認したCommand・Check・Evidenceを書く。

**`SUPERSEDED_BY_DESIGN_HASH`を解消の意味で使わない。** 設計書Hashが動いた
という事実と、塞がりが解けたという事実は別である。前者で後者を代用すると、
Block Recordの履歴から「何が実際に直ったのか」が読めなくなる。

### 版番号をv1.12へ上げた

改訂1〜9は**規範的な改訂**である。§23.3〜§23.6の前例に従い、規範改訂には
新しい版番号を与える。**v1.11本文は書き換えていない。** 新しい名前で書き出してから
旧名を削除しており、v1.11は「Evidence領域がScopeの外に取り残されていた版」として
Git履歴とBlock Recordの`design_sha256`束縛の中に残る。

Case ID、Test ID、期待値、Expectation Hash、`phase_scope`は**変更していない。**
本改訂はEvidence領域の話であり、Caseの`phase_scope`の話ではない。
v1.11が`phase_scope`を動かし、v1.12がEvidence領域を動かす。2つを混同しない。

**Releaseは作成しない。** Runtime判定は`BLOCKED_EVIDENCE_MISSING`のままである。
Case Evidenceが未生成であることは本改訂では解消しない。
Release Trust Anchorの再束縛は本改訂の範囲外であり、旧Trust Anchorを再利用しない。

## 23.8 v1.12→v1.13の改訂記録（Owner Decision適用）

本節はv1.12として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/EVENT-SEQUENCE-CONTRACT-V113.md`のOwner Decision
（`DEC-U-EVENT-SEQUENCE`。2026-08-18確定）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-18 | §19.1 | `expected_event_sequence`が「実際に正本LedgerへAppendされたEvent」の列であると明記した | Event Sequence決定 |
| 2 | 2026-08-18 | §19.1.1 | 観測規約7項目を新設した。Request列・Verdict予測列・期待値からの導出を禁じ、観測元をLedger（またはSpy）に限定した | Event Sequence決定 |
| 3 | 2026-08-18 | §19.1.1 | 区別の基準が「拒否されたか」ではなく「Ledgerに残ったか」であると明記した | Event Sequence決定 |
| 4 | 2026-08-18 | §19.1.1 | Request列が偶然一致しても期待値の根拠にならないことを、`AT-EVENT-ORDER-001`の2 Caseを並べて示した | Event Sequence決定 |
| 5 | 2026-08-18 | §19.1.1 | Append列とLedger Headを同時に記録し、拒否時のHead不変を確かめると定めた | Event Sequence決定 |
| 6 | 2026-08-18 | §19.1.1／§26.2.1 | 未観測を`[]`で記録しないと定めた。未観測はEvidence生成を拒否する | Event Sequence決定 |

## 23.9 v1.13→v1.14の改訂記録（Owner Decision適用）

本節はv1.13として発行した後に本文へ加えた改訂を記録する。根拠は`docs/decision/OWNER-DECISION-A3-8CASES.md`のOwner Decision（Decision 1〜4。2026-08-20確定）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-20 | §1.16.2.1 | Input Read系Eventの発行層をApplication／Orchestration層と定めた。Reader・Broker・Masking PipelineへLedger責務を持たせない | Decision 1-A |
| 2 | 2026-08-20 | §1.16.2.1 | Append位置・Transaction境界・Ledger Head更新・Event順序・重複防止・線形化点・Reader失敗時の扱いを表で定めた | Decision 1-A |
| 3 | 2026-08-20 | §1.16.2.2 | Approval／Plan系Eventの発行層をApplication／Orchestration層と定めた。委任経路のEvent列を素の承認経路へ流用しないと明記した | Decision 2-A |
| 4 | 2026-08-20 | §1.16.2.2 | `PLAN_COMPARISON`はPlannerの比較結果であり実行を主張しないため、Event列を要求しないと定めた | Decision 2-B |
| 5 | 2026-08-20 | §19.1 | `AT-PLAN-DETERMINISM-001/SAME_INPUT`の期待Event列を`NONE`へ改めた | Decision 2-B |
| 6 | 2026-08-20 | §1.18.1 | GC PlannerとGC Executorの境界を定めた。Plannerの`ACCEPTED`を実削除成功として扱わないと明記した | Decision 3-A |
| 7 | 2026-08-20 | §1.18.1 | 実削除Caseが束縛すべき6項目を同一契約として列挙した | Decision 3-A |
| 8 | 2026-08-20 | §1.8／§1.18.2 | `ARTIFACT_PAYLOAD_DELETED`をEvent Type正本へ追加し、State・失敗時State・Error Code・Retry可否・終端Stateを定めた | Decision 3-A |
| 9 | 2026-08-20 | §19.1 | `AT-GC-001/RAW_RETENTION`の期待Event列を`ARTIFACT_PAYLOAD_DELETED`へ改めた。`deletion_event_count == 1`と期待Event列が空である矛盾を解消した | Decision 3-A |

### 版番号をv1.14へ上げた

本文Bytesが変わるためDesign Hashが変わる。`DESIGN-VERSION-V19`§0の先例に従い、本文改訂に新しい版番号を与える。Bytesが違う2文書が同じ版番号を名乗る状態を作らない。

旧Design Hashへ束縛されたOPEN Block Recordは`SUPERSEDED_BY_DESIGN_HASH`で閉じ、論理Taskごとに後継Recordを1件発行する。**過去Recordの不変Fieldは書き換えない。**

### 本改訂で実装していないもの

本改訂は設計正本とRegistryの整合だけを行う。Orchestration Event Appendの実装、Approval／Plan Subject生成、GC Executorの実装、削除Event処理、Adapterの再配線はいずれも別Taskである。設計が先に確定していないと、実装が設計を追い越して「実装がそうなっているから正しい」という循環に落ちる。

### なぜ明記が必要だったか

規約はRegistryの値からは読み取れたが、**どこにも書かれていなかった。**
読み取れることと書いてあることは別である。

## 23.10 v1.14→v1.15の改訂記録（Owner Decision適用）

本節はv1.14として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/OWNER-ANSWERS-UNIT-EVIDENCE-DCR.md`のOwner Decision
（DCR-1〜DCR-5。2026-08-21確定）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-21 | §26.2 | Unit層のCaseを数えるEvidence領域を新設し、Orchestration層とは別領域として数えると定めた。領域名・Path・Scopeの正本は`evidence-areas.yaml`とした | A1-UNIT-CASE-SUITE-ALL-PHASES |
| 2 | 2026-08-21 | §26.2.1 | Case Evidenceへ`evidence_kind`を必須Fieldとして足した。層のラベルであって領域所属ではないと明記した | B2-EVIDENCE-KIND-REQUIRED |
| 3 | 2026-08-21 | §26.2.1 | Case Evidenceへ`event_observation`を必須Fieldとして足し、`OBSERVED`／`OBSERVED_EMPTY`／`NOT_APPLICABLE`と`event_sequence`の対応を一意に定めた | C3-ENUM-AND-NULL |
| 4 | 2026-08-21 | §19.1.1 | 空のEvent列を`REQUIRED_EMPTY`と`NOT_APPLICABLE`へ分けた。Policyの正本を`tests.yaml`の`event_observation_policy`とし、`[]`から推測しないと定めた | D1-ADOPT-AND-REGISTRY |
| 5 | 2026-08-21 | §19.1.1 | `NOT_APPLICABLE`を名乗れる条件を3項目で定めた。Orchestration Evidenceは常にLedger観測を要求すると明記した | E1-NA-ONLY-EXEMPT |
| 6 | 2026-08-21 | §15.11 | Evidence文書のSchema CatalogをCore Schema Catalogと別系統として新設した。2.0を発行し、1.2を`read_only`で保持すると定めた | B2-EVIDENCE-KIND-REQUIRED／C3-ENUM-AND-NULL |

### 版番号をv1.15へ上げた

本文Bytesが変わるためDesign Hashが変わる。v1.13→v1.14と同じ扱いで、本文改訂に
新しい版番号を与える。Bytesが違う2文書が同じ版番号を名乗る状態を作らない。

旧Design Hashへ束縛されたOPEN Block Recordは`SUPERSEDED_BY_DESIGN_HASH`で閉じ、
論理Taskごとに後継Recordを1件発行する。**過去Recordの不変Fieldは書き換えない。**

### 本改訂で実装していないもの

本改訂は設計正本・Registry・Evidence Schema・Verifier契約の整合だけを行う。
Unit CaseのAdapter配線、Unit Evidenceの生成、Gate Evidenceの準備はいずれも別Taskである。
設計が先に確定していないと、実装が設計を追い越して「実装がそうなっているから正しい」
という循環に落ちる。

### 未確定のまま残したもの

`event_observation_policy`を持たないCaseが残っている。いずれもMaskingのOPEN Block Record
で塞がっており、Registryの期待値と実装の食い違いがOwner Decision待ちである。
**塞がっているものを既定値で埋めない。** 埋めれば、決まっていないことが決まったように見える。
件数と対象はRegistryとBlock Recordが正本であり、本文へ書かない（不変条件#18）。

## 23.20 v1.24→v1.25の改訂記録（Owner Decision適用）

2026-09-07。根拠はdocs/decision/OWNER-DECISION-APPROVAL-COUNTER-20260907.jsonと、参照先のOwner実回答。ACRC-1〜10の回答は不変。§15.9へApprovalConsumeResultを追加し、§19.1の敗者成功数をDCR-1-Aの個別値へ訂正した。群の成功1回保証は別途維持する。既存Schemaは変更せず新規Schemaを登録。旧Evidenceは変換・再利用しない。旧Reportと回答PackageはGit履歴のHash束縛のまま保存する。

## 23.19 v1.23→v1.24の改訂記録（Owner Decision適用）

本節はv1.23として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/DCR-CHAT-PRIORITY-MAPPING.md`のOwner Decision（`CPM-1`〜`CPM-3`）と、
それによって実装可能になった`docs/decision/DCR-CHAT-CONTEXT-POLICY.md`の`CP-1-A`である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-30 | §4.11 | Chat Context Policyの正本を`design-source/registries/chat-context-policy.yaml`と定めた | CP-1-A／CPM-1-A |
| 2 | 2026-08-30 | §4.11 | Role別priorityを信頼境界の3段で決めると定めた。具体値は本文へ書かずRegistryを正本とした | CPM-2-A |
| 3 | 2026-08-30 | §4.11 | 段が同じMessageは`sequence_number`降順で優先し、最終送信列は昇順へ戻すと定めた | CPM-3-A |
| 4 | 2026-08-30 | §4.11 | `mandatory`は段が必要条件であり、§1.16.4の4検証を満たすArtifactが十分条件であると明記した | CP-2-A |

### §23.18との関係

§23.18は「`CP-1-A`は反映していない」と記録している。**v1.23発行時点では真である。**その後`DCR-CHAT-PRIORITY-MAPPING`の3設問へ回答があり、置き場所・段の決め方・段内の順序が確定したため、本版で反映した。過去の改訂記録は書き換えない。

### この版の影響

Registryは`chat-context-policy.yaml`を新規に加えた。`registry_snapshot_hash`が動く。
Core Schemaを変更していないため`schema_catalog_hash`は動かない。
Case・Event・Error Code・Stateの追加と削除は行っていない。
MVP0-Aの必須Case数とGate数は動いていない。
既存Evidenceを新Versionへ変換していない。

## 23.18 v1.22→v1.23の改訂記録（Owner Decision適用）

本節はv1.22として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/DCR-CHAT-CONTEXT-POLICY.md`のOwner Decision（`CP-1`〜`CP-4`）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-29 | §4.11 | `mandatory`にできるRoleを`SYSTEM_CONTROL`と`DEVELOPER_CONTROL`へ限定した | CP-2-A |
| 2 | 2026-08-29 | §4.11 | 未設定Messageが1件でもあればChat送信全体を拒否すると定めた | CP-3-B |
| 3 | 2026-08-29 | §4.11 | `decision_hash`から`receipt_id`／`bundle_id`を外し、選択判断の再現性を表すと定めた | CP-4-A |

### 反映していないOwner Decision

`CP-1-A`はChat用MappingをRegistryへ置くと決めたが、置くFile名も、Roleと§3.6の9段階優先順位の写像も、priority数値も正本に無い。Phase 1の実測で3件が導出不可だった。`docs/decision/DCR-CHAT-PRIORITY-MAPPING.md`（設問3件・未回答）で止めており、Registryへ何も足していない。

Core Schemaを変更していないため`schema_catalog_hash`は動かない。
Registryを変更していないため`registry_snapshot_hash`も動かない。
Case・Event・Error Code・Stateの追加と削除は行っていない。
MVP0-Aの必須Case数とGate数は動いていない。

## 23.17 v1.21→v1.22の改訂記録（Owner Decision適用）

本節はv1.21として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/DCR-CHAT-MESSAGE-CONTENT.md`のOwner Decision（`CMC-1`〜`CMC-6`）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-28 | §4.11／§15.9 | 会話本文をArtifact CASへ置き、`ConversationMessage@2.0.0`が`content_artifact_hash`で参照すると定めた | CMC-1-A / CMC-2-A |
| 2 | 2026-08-28 | §4.11 | Messageの`content_hash`を本文Hash・`role`・`sequence_number`から導出すると定め、3つのHashの役割を分けた | CMC-3-A |
| 3 | 2026-08-28 | §4.11 | `conversation_hash`を`conversation_id`と作成時の不変メタだけから導出すると定めた | CMC-4-A |
| 4 | 2026-08-28 | §4.11 | 保存前に決定論的Scannerを実行し、Reject対象はマスクせず保存も拒否すると定めた | CMC-5-A |
| 5 | 2026-08-28 | §15.9 | `ConversationMessage@2.0.0`を新設し、`1.0.0`をread_onlyで残した | CMC-6-A |

`ConversationMessage@1.0.0`のSchema ID・Hash・Bytesは変更していない。
Core Schemaの論理件数は25件のまま、版が30件から31件になった。
`schema_catalog_hash`と`registry_snapshot_hash`が動く。
Case・Event・Error Code・Stateの追加と削除は行っていない。
MVP0-Aの必須Case数とGate数は動いていない。

本文をinlineで持つFieldは追加していない。`role`の語彙も増やしていない。

## 23.16 v1.20→v1.21の改訂記録（Owner Decision適用）

本節はv1.20として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/DCR-CHAT-CONTRACT.md`のOwner Decision（`CC-1`〜`CC-16`）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-28 | §4.11 | `Conversation`／`ConversationMessage`／`ConversationSnapshot`を`1.0.0`でCore Schemaへ追加した | CC-1-A / CC-2-A / CC-3-C / CC-4-A |
| 2 | 2026-08-28 | §4.11 | 必須Fieldを定めた。`message_count`と`token_count`は持たせない | CC-5-A / CC-6-A / CC-7-C |
| 3 | 2026-08-28 | §4.11 | 親子を子→親の単方向とし、3 SchemaすべてをAppend-onlyと定めた | CC-9-A / CC-10-B |
| 4 | 2026-08-28 | §4.11 | Hashの相互関係と、既存§1.11 Profileの再利用を定めた | CC-11-A / CC-12-A / CC-13-A |
| 5 | 2026-08-28 | §4.11 | IDをUUIDv4のPort経由と定め、Hash入力へ入れないとした | CC-8-A |
| 6 | 2026-08-28 | §4.11 | ChatをMVP0-Bへ所属させ、MVP0-Aの判定条件を動かさないと定めた | CC-14-A / CC-15-A / CC-16-A |

Core Schemaは22件から25件になった。`schema_catalog_hash`と`registry_snapshot_hash`が動く。既存22 Schemaの定義は変えていない。
Case・Event・Error Code・Stateの追加と削除は行っていない。
MVP0-Aの必須Case数とGate数は動いていない。

`role`は§1.16.4の既存語彙を使い、新しいrole値を追加していない。

## 23.15 v1.19→v1.20の改訂記録（Owner Decision適用）

本節はv1.19として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/OWNER-DECISION-CHAT-FOUNDATION.md`のOwner Decision（`CHAT-1`〜`CHAT-7`）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-27 | §1.11 | 会話SnapshotへPlan Contentと同じ決定論規則を当て、Message集合Hash・Schema Hash・Design Hashへ束縛すると定めた | CHAT-2-A |
| 2 | 2026-08-27 | §4.3 | Provider Route PolicyをRegistry正本へ登録し、Route Class・許可条件・Fallback可否を定めた | CHAT-3-A |
| 3 | 2026-08-27 | §4.3 | Provider SecretをOS Keyringへ置き`SecretRef`だけを保持すると定めた。環境変数への暗黙Fallbackを禁止した | CHAT-4-A |
| 4 | 2026-08-27 | §4.3 | 外部ProviderをMasking Gate通過条件つきで許可する契約を定めた。接続実装は行っていない | CHAT-5-C |
| 5 | 2026-08-27 | §4.3 | Chat UIを`127.0.0.1`限定とし、表示を承認判定にしないと定めた | CHAT-6-B |

Registryは`route-policy.yaml`を新規に加えた。`registry_snapshot_hash`が動く。
Case・Event・Error Code・State・Core Schemaの追加と削除は行っていない。
`schema_catalog_hash`は動かない。

### 反映していないOwner Decision

`CHAT-1-A`と`CHAT-7-B`は方針を決めたが、契約を書くための値を与えていない。

| Decision | 欠けている値 |
|---|---|
| `CHAT-1-A` | 会話SnapshotのSchema名、`Conversation`と`Message`の必須Field、相互参照 |
| `CHAT-7-B` | 別Phase／別Scopeの正式名称 |

`Conversation`は本設計書に一度も現れず、`Message`のSchemaもRegistryも無い。
名前とField列を推測で決めれば、正本が推測になる。
`docs/decision/DCR-CHAT-CONTRACT.md`（設問5件・未回答）で止めており、
Core Schemaを作っていない。`schemas.yaml`も`scopes`も変えていない。

## 23.14 v1.18→v1.19の改訂記録（Owner Decision適用）

本節はv1.18として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/OWNER-ANSWERS-EVENT-PREDECESSOR-EVIDENCE-SCHEMA.md`のOwner Decision
（`DEC-EVT-PRED-A`／`DEC-EVT-PRED-B`）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-25 | §1.16.2.3 | 期待Event列は§1.4.1の必須先行Eventを推移的に含めると定めた。Caseの観測はStream全体を読むため、先行を欠いた列はどの実行でも作れない | DEC-EVT-PRED-A / A-1 |
| 2 | 2026-08-25 | §19.1.1 | Case Evidence 3.0へ`ledger_head_before`／`ledger_head_after`をRequiredで足した。Unit Caseは`null`とし0で埋めない。2.0はread_onlyとして残す | DEC-EVT-PRED-B / B-1 |

Registryの変更は2件のCaseの`expected_event_sequence`だけである。どちらも先行Eventを
足して5件になった。State・Error Code・Subject・Assertionsは変えていない。Case・Event・
Error Code・Stateの追加と削除は行っていない。

Evidence Schemaは3.0を新規発行し、2.0をread_onlyへ移した。2.0のFileは上書きしていない。

## 23.13 v1.17→v1.18の改訂記録（Owner Decision適用）

本節はv1.17として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/OWNER-ANSWERS-MASKING-EVENT-POLICY.md`のOwner Decision
（`MASK-EVT-1`／`MASK-EVT-2`）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-24 | §1.16.2.3 | Scan#2拒否のEvent列を4件と定めた。`INPUT_MASKING_SCAN1_CANDIDATES_READY`を含む。2を省いた3件の列は構成上到達しないことを明記した。Event Registryへの新規追加は行っていない | MASK-EVT-1 |
| 2 | 2026-08-24 | §1.16.2.3 | `REQUIRED_EMPTY`はLedger Portを持たない層では使えないと定めた。偽Probeが返す0を根拠にしない。Masking Pipelineを駆動するCaseは`NOT_APPLICABLE`とする | MASK-EVT-2 |

Registryの変更は次の2件である。`AT-MASKING-001/SECOND_SCAN_IS_THE_GATE`の
`expected_event_sequence`を3件から4件へ、Masking Pipelineを駆動する19件の
`event_observation_policy`を`REQUIRED_EMPTY`から`NOT_APPLICABLE`へ改めた。
Case・Event・Error Code・Stateの追加と削除は行っていない。

## 23.12 v1.16→v1.17の改訂記録（Owner Decision適用）

本節はv1.16として発行した後に本文へ加えた改訂を記録する。根拠は
`docs/decision/OWNER-ANSWERS-MASKING-CASE-ID.md`のOwner Decision
（`DEC-U-MASKING-CONTRACT`／`DEC-U-MASKING-CASE-ID`）である。

| # | 日付 | 節 | 改訂内容 | 根拠 |
|---|---|---|---|---|
| 1 | 2026-08-24 | §1.16.2.3 | Masking PipelineへLedger責務を持たせないと定めた。Event発行は外側のOrchestration層とし、Masking Unit CaseのEvent観測Policyを`NOT_APPLICABLE`とした | MASK-1-C |
| 2 | 2026-08-24 | §1.16.2.3 | 同じ拒否でも層によってSubjectとStateが変わることを表で定めた。Masking内部は`MASKING_RESULT`／`REJECTED`、Orchestrationは`INPUT_READ_DECISION`／`DENIED`とし、任意の`REJECTED`→`DENIED`変換を禁止した | MASK-2-C |
| 3 | 2026-08-24 | §1.16.3.1 | 未ソートSpanをUnion前に拒否すると定めた。通常経路でsortして受理しない。Error Codeは既存語彙を使い新設しない | MASK-3-A |
| 4 | 2026-08-24 | §1.16.3.2 | Policy境界の拒否は Error Code を持たず、Scan#2の検証失敗は`MASKING_VERIFICATION_FAILED`を持つと分けた。新しいError CodeもCaseも足さない | MASK-5-C |
| 5 | 2026-08-24 | §19.1 | Receipt非漏洩のCaseを入力で2系統へ分けた。PII入力は`MASKED`、Secret入力は`REJECTED`で、どちらもReceiptへ原文が残らないことを主張する | MASK-4-C |

### 版番号をv1.17へ上げた

本文Bytesが変わるためDesign Hashが変わる。Registryへ Case を1件足し、
既存 Case の期待値も変えるため`registry_snapshot_hash`も動く。

Block Recordの`bound_to_current`はDesign HashとRegistry Snapshot Hashの**両方**の
一致を要求するので、旧OPEN Recordは`SUPERSEDED_BY_DESIGN_HASH`で閉じ、
後継を1件ずつ発行する。**過去Recordの不変Fieldは書き換えない。**

### Maskingの塞がりは解消していない

本改訂は設計正本とRegistryの整合だけを行う。Masking CaseのAdapter配線と
Evidence生成は別Taskである。**6件のBlock RecordはOPENのまま残す。**
契約が決まったことと、実装が契約どおりに動くことは別である。

## 23.11 v1.15→v1.16の改訂記録

本節はv1.15として発行した後に本文へ加えた改訂を記録する。
`TASK-UNIT-EVIDENCE-POLICY-BINDING-001`による。

| # | 日付 | 節 | 改訂内容 |
|---|---|---|---|
| 1 | 2026-08-23 | §26.2.1 | Case Evidenceへ`event_observation_policy`を必須Fieldとして足した |
| 2 | 2026-08-23 | §26.2.1 | VerifierはローカルRegistryを読まず、Release束縛のSnapshotだけを免除の根拠にすると定めた |
| 3 | 2026-08-23 | §26.2.1 | Policy・層・観測有無の対応表と9つの拒否条件を定めた |
| 4 | 2026-08-23 | §26.2.1 | 期待列が空でPolicy欠落の組合せを、Evidence生成・Release判定とも拒否すると定めた |
| 5 | 2026-08-23 | §15.11.1 | 2.0を名乗るEvidenceが1件も存在しないうちに限り、締める変更を版を上げずに行えると定めた。Evidenceが積み上がった後は3.0を発行する |

### 版番号をv1.16へ上げた

本文Bytesが変わるためDesign Hashが変わる。v1.14→v1.15と同じ扱いで、本文改訂に
新しい版番号を与える。

Registry Snapshotへ`event_observation_policy`を足したため`registry_snapshot_hash`も動く。
Block Recordの`bound_to_current`はDesign HashとRegistry Snapshot Hashの**両方**の一致を
要求するので、どちらが動いても後継Recordが要る。旧Recordは`SUPERSEDED_BY_DESIGN_HASH`で
閉じ、論理Taskごとに後継を1件発行する。**過去Recordの不変Fieldは書き換えない。**
**塞がりの内容は変わっていない。**

### 本改訂で実装していないもの

Unit CaseのAdapter配線とUnit Evidenceの生成は別Taskである。Maskingの未確定Caseの
分類もOwner Decision待ちのまま残す。

実際に事故が起きた。`AT-EVENT-ORDER-001/SEQUENCE_REGRESSION`のCase Adapterが
Ledgerを見ずに`events=[]`と記録し、Development Evidenceが
`mismatched_fields: ['event_sequence']`でFAILした。

`[]`は「Ledgerを見て0件だった」ではなく「Ledgerを見ていない」だった。
§26.2.1が`side_effects`について禁じている「未測定を0として扱う」誤りを、
**Event列の次元で犯していた。** 同じ規則がEvent列にも要ることが分かった。

### Registryは変更していない

MVP0-Aで`expected_event_sequence`が非空の54 Caseを全件監査した。
**Registryの期待値に誤りは1件も無く**、54件すべてが上記規約1〜6で説明できる。

| 説明 | 例 |
|---|---|
| 拒否前に既にAppend済み | `AT-EVENT-ORDER-001`の2件 |
| 拒否処理自身のAppend | `AT-FAULT-GUARD-001/POLICY_DENIED` = `['ACTION_BLOCKED']` |
| 正常系の一連のAppend | `AT-CRASH-001/*` |
| 前段Event＋拒否Event | `AT-DELEGATION-001/SCOPE_EXCEEDED` |

**Case ID、Test ID、Assertion、Error Code、State、`expected_event_sequence`、
Expectation Descriptor Hashを1件も変更していない。** 本改訂は本文への明記だけである。
Schema Catalog Hashも変わらない。

直したのは観測側である。`SEQUENCE_REGRESSION`の統合試験は
`PLAN_RESOLVED`を実際にAppendして状態を作り、順序違反のAppendを試み、
Headが動かないことを数え、`ledger.appended`を観測値として記録する形へ改めた。

### 版番号をv1.13へ上げた

改訂1〜6は**規範的な改訂**である。§23.3〜§23.7の前例に従い、規範改訂には
新しい版番号を与える。**v1.12本文は書き換えていない。** 新しい名前で書き出してから
旧名を削除しており、v1.12は「Event Sequenceの観測規約が本文に無かった版」として
Git履歴とBlock Recordの`design_sha256`束縛の中に残る。

**Releaseは作成しない。** Runtime判定は`BLOCKED_EVIDENCE_MISSING`のままである。
Case Evidenceは0/107であり、本改訂では解消しない。
Release Trust Anchorの再束縛は本改訂の範囲外であり、旧Trust Anchorを再利用しない。

## 24.1 静的整合条件

* Markdown Code Fenceが閉じている。
* Test ID／Case IDが一意。
* ManifestのSubject／State、Error Code、Gate参照がRegistryと一致。
* 廃止したVersion列名とSnapshot ID／Hash表記が実装対象節に残っていない。
* v1.3／v1.4を実装正本として参照する記述がない。
* 参照仕様Feature FlagがOFF。
* 実行していない試験は全て`UNVERIFIED`。

## 24.2 Runtime判定

静的整合は実装やRuntime安全性を証明しない。Schema、Migration、Fault Injection、Filesystem Corpus、Runtime Evidenceがないため、Runtimeは全フェーズ`UNVERIFIED`である。

# 25. 最終状態

本書はv1.3とv1.4を統合し、Python／単独開発／WSL2限定の実装正本として再構成した。設計上の実装順序、状態機械、永続化境界、承認、Fault Injection、Emergency Recovery、Gate、Test Manifestを1文書内で整合させた。

次の着手はCode生成ではなく、Registry、Spec Linter、7技術スパイクである。これらが合格するまでMVP0-A本実装のRuntime GOを出さない。

**設計：統合完了**
**Spec／Schema／Migration／Runtime：UNVERIFIED**
**外部Provider／Paid／External Effect／Windows Write／Parallel Worker：DISABLED**

---

# Appendix A. MIT公開要件

READMEへ次を必須記載する。

* 個人利用向けであり、Enterprise RBAC／SoD／IdP／監査Export／DRは未実装。
* 単一UID構成は改ざん検知であり、改ざん耐性・否認防止を提供しない。
* WSL2 Linux側Filesystem限定。`/mnt/*`とWindows Native Workspaceは非対応。
* 未検証機能をPASSと表示しない。
* Feature Flagの既定OFF一覧。
* Security Issueの報告方法。
* 依存ライブラリのLicense、SBOM、既知脆弱性確認方法。

検証状況表はRegistryとEvidenceから自動生成する。

```text
Design                         = INTEGRATED
Spec Lint                      = UNVERIFIED
20 Core JSON Schema            = UNVERIFIED
SQLite Migration               = UNVERIFIED
Linux Safe Reader              = UNVERIFIED
Runtime Attestation            = UNVERIFIED
Fault Injection / Recovery     = UNVERIFIED
Acceptance Test Manifest       = UNVERIFIED
```

# Appendix B. CLAUDE.md／AGENTS.md共通不変条件

**本Appendixは要約である。正本は同梱の実ファイル`CLAUDE.md`および`AGENTS.md`とする。**

v1.6ではこの内容が設計書内のCode Fenceにしか存在せず、AIコーディングエージェントが実際に読み込む`CLAUDE.md`／`AGENTS.md`が成果物として存在しなかった。そのため新規スレッドで同等の品質を再現できなかった。v1.8では実ファイルとして同梱し、本文はその要約に留める。

不変条件の一覧：

1. Event LedgerをUPDATE／DELETEしない。訂正はCompensating EventのAppendだけで行う。
2. Effect実行前にOperationJournalを`PREPARED_DURABLE`へCommitする。
3. Fencing Tokenを最終Storage書込み直前に再検証する。
4. Plan ContentへランダムID、採番ID、時刻、PID、列挙順を含めない。SnapshotはHashだけを含める。
5. Planは1回凍結した`PlanBuildInput`から2回Buildし、さらに別`PYTHONHASHSEED`の子プロセスで3回目をBuildしてHash一致を検証する。
6. Planner Packageで`set`／`frozenset`を反復しない。Code Point昇順で`sorted`してから使う。
7. Secret値をLog、Event、Attestation、例外、Test Evidenceへ出さない。`SecretRef`だけを扱う。
8. Process起動は`list[str]`だけ。`shell=True`とShell文字列を禁止する。
9. 例外を握り潰さない。判定不能は`EFFECT_UNKNOWN`または`BLOCKED_REPAIR_REQUIRED`で停止する。
10. Approval消費は単一SQLite TransactionのCASで行う。
11. Approval Skip Flag／環境変数／直接実行APIを作らない。
12. WorkspaceをWindows側Filesystem、`/mnt/*`、未知Network／FUSE／Overlayへ置かない。
13. ArtifactはTemp write→File fsync→Atomic Rename→Directory fsync→Manifest登録の順にする。
14. 全状態Storeを単一SQLite DBへ置き、物理分離しない。
15. DB接続は`ConnectionFactory`だけから生成し、Repositoryは`commit()`しない。
16. 実行していないTestをPASSと書かない。証跡なしは`UNVERIFIED`。
17. Testを削除、Skip、期待値緩和してGateを通さない。
18. 実装前後にSpec Lintを実行する。件数を本文へ手入力せずRegistryから取る。
19. CIのPASSをRuntime Evidenceへ流用しない。CIはStatic／Self-test専用で、Release Manifestを生成しない。
20. 同一TaskにOPENのBLOCKED Recordがある場合は反復しない。`BLOCKED-RECOVERY.md`の復帰条件とOwnerのDecisionを先に処理する。

# Appendix C. 生成spec構造

```text
repo/
  design-v1.14-runtime-go.md
  CLAUDE.md                      # AIエージェント共通不変条件（実ファイル）
  AGENTS.md                      # 同上
  THREAD-START.md                # 新規スレッド読み込み順序
  TASK-BRIEF.template.md         # 1 Task単位の入出力・Gate契約
  design-source/
    registries/
      states.yaml  events.yaml  errors.yaml
      schemas.yaml tests.yaml   gates.yaml
      evidence-areas.yaml
  tools/
    lint_spec.py                 # 参照整合・件数・Scope整合の機械検査
    validate_test_manifest.py    # Test Manifest契約の検査
    build_registry_snapshot.py   # Registry → registry-snapshot.json
    build_manifest_template.py   # Registry → Manifest Template
    build_spec_shards.py         # Registry＋正本 → spec/*.md
    check_verifier_integrity.py  # Verifier Source期待Hashの照合
    check_ci_runtime_boundary.py # CIとRuntime GOの責務境界
    check_blocked_records.py     # BLOCKED Record一括検証
    check_design_generation_contract.py # レビュー済み正本の巻戻り検出
  ci/verifier-source.sha256      # Branch Protectionと併用する期待Hash
  BLOCKED-RECOVERY.md             # BLOCKED復帰Runbook
  blocked-record.schema.json      # BLOCKED Record Schema
  registry-snapshot.json         # 生成物。Verifier入力
  verify_runtime_go.py           # Runtime GO Verifier v1.2
  tests/test_verify_runtime_go.py  # AT-VERIFIER-*
  spec/
    00-common.md
    01-canonical-hash.md
    02-input-read.md
    03-token-profile.md
    10-mvp0a.md
    11-mvp0b.md
    12-mvp1a.md
    13-mvp0c.md
    14-mvp1d.md
    20-schemas/
    30-policy-freshness.md
    90-tests.md
    99-reference/                # 参照仕様Phase。実装Taskへは渡さない
    spec-manifest.json
```

AIには現在Taskに対応するshard、関連Registry、`CLAUDE.md`だけを渡す。`spec-manifest.json`のSource Hashが本書と一致しない場合はCode生成を停止する。

### 新規スレッドで同一品質を再現するための手順

本書は約82,000 tokensあり、うち参照仕様Phase（§7、§8、§10〜§14）が約26%を占める。全文投入は文脈を圧迫し、参照仕様のCodeを誤って生成する誘因にもなる。次の順序で読み込む。

```text
1. CLAUDE.md                    … 不変条件。必ず最初
2. spec/00-common.md            … 共通アーキテクチャ
3. 対象Taskのshard              … 例: spec/10-mvp0a.md
4. 関連Registry                 … tests.yaml / gates.yaml / errors.yaml の該当部分
5. spec/20-schemas/<name>.md    … 触るSchemaだけ
```

`spec/99-reference/`を実装Taskの文脈へ入れてはならない。参照仕様のCode、Schema、Feature Flagを生成した場合はSpec Lintが検出する。

# 26. Runtime GO判定プロトコル

## 26.1 判定単位

初回のRuntime GO対象は**MVP0-Aのみ**とする。MVP0-B、MVP1-A、MVP0-C、MVP1-Dは、それぞれのPhase固有GateとEvidenceが揃った時点で別Release Decisionを発行する。MVP0-AのGOを後続PhaseのGOへ読み替えない。

**Release Scopeは判定の入力である。** Verifierへ`--release-scope`を必ず与え、必要Case集合・必要Gate集合・`test_manifest_hash`をRegistryから導出する。Scopeを与えない判定、および全Phase合計のCase数を1回の判定へ要求する運用を禁止する。

Scopeとして指定できるのは実装対象Phaseだけである。

```text
指定可能 : MVP0-A, MVP0-B, MVP1-A, MVP0-C, MVP1-D
指定不可 : MVP1-B, MVP1-C, MVP1-E, MVP2-A, MVP2-B,
           BLIND_REVIEWER, ENTERPRISE（いずれも参照仕様）
```

判定対象は次のTupleで一意に識別する。

```text
(release_scope, design_version, design_sha256, registry_snapshot_hash,
 implementation_commit_sha, runtime_environment_hash, schema_set_hash,
 migration_head, test_manifest_hash, gate_report_hash,
 verifier_source_sha256)
```

Tupleの1項目でも変化した場合、既存Runtime GOは新しい実装へ継承されない。**ただし全項目の変化が同じ重さを持つわけではない。** 再試験の範囲は§26.6の再認定階層で決定する。無条件の全再実行を要求すると、人手計測を含むRelease Gateが毎Commit必要になり、保守が成立しない。

## 26.2 必須成果物

```text
runtime-evidence/<release-id>/
  environment/runtime-environment.json          # 領域: environment
  environment/mountinfo.txt
  environment/python-packages.lock
  source/spec-lint-report.json                  # 領域: spec_lint
  source/static-analysis.json                   # 領域: source_static_analysis
  source/source-tree.json
  schema/schema-suite-report.json               # 領域: core_schema_suite
  migration/migration-report.json               # 領域: sqlite_migration
  migration/backup-restore-report.json          # 領域: backup_restore
  filesystem/safe-reader-corpus-report.json     # 領域: linux_safe_reader
  runtime/runtime-attestation.json              # 領域: runtime_attestation
  fault/fault-matrix-report.json                # 領域: fault_injection_recovery
  performance/performance-report.json           # 領域: performance
  approval/approval-ux-report.json              # 領域: approval_ux（人手計測。MVP0-B以降）
  security/secret-canary-report.json            # 領域: secret_canary
  tooling/verifier-self-test-report.json        # 領域: verifier_self_test
  cases/unit-case-suite-report.json             # 領域: unit_case_suite
  tests/test-run-summary.json
  tests/cases/<test-id>/<case-id>/evidence.json
  recovery/recovery-report.json
  gates/<gate-id>.json
  release/runtime-go-release-manifest.json
```

**必須領域はRelease Scopeごとに異なる。** 上のTreeは全領域を並べたものであり、「全Scopeで全部要る」という意味ではない。どの領域がどのScopeで必須かは`design-source/registries/evidence-areas.yaml`の`phase_scope`が正本であり、`registry-snapshot.json`の`scopes[<scope>].required_evidence_areas`へ導出される。

`approval_ux`はMVP0-B以降で必須とする。承認UIの実装PhaseはMVP0-Bであり（§4.1.1）、MVP0-Aには人が承認する実作業も承認UIも存在しない。**MVP0-Aで`approval_ux`のEvidenceを作らない。** UIが無い状態でUI測定値を生成しない（§4.1.2）。`AT-APPROVAL-UX-001/NO_BYPASS`はMVP0-AのCaseとして残るが、あれは`SOURCE_SCAN`でありUX計測ではない。**`NO_BYPASS`を`approval_ux`領域の充足根拠にしない。**

Scopeが要求しない領域をManifestへ足すことも拒否する。要求されない領域を足せるなら、Scope別化は「出しても出さなくてもよい」になり、MVP0-Bで`approval_ux`を省く抜け道と対称の穴になる。

`unit_case_suite`はUnit層で検証したCaseの領域である（v1.15）。Orchestration層のCaseとは
**別領域として数える**。同じ領域へ混ぜると、Ledgerを観測して作用を確かめたCaseと、
StateとError Codeだけで閉じたCaseが同じ重みで充足に数えられる。何を検証したEvidenceなのかを
後から区別できる形にしておく。

領域の正本は`design-source/registries/evidence-areas.yaml`であり、`area`名・`evidence_path`・
`human_measured`・`phase_scope`をそこで定める。**本文へ領域名や件数を書かない**（不変条件#18）。
Unit Caseは実装Phaseのいずれにも現れるため、`phase_scope`は実装Phase全体とする。Scopeを
絞ると、後続PhaseでUnit Caseが増えたときに足し忘れが黙って「数えない」へ倒れる。

領域が重ならないことは`evidence_kind`が保証する。1件のCase Evidenceは`UNIT`と
`ORCHESTRATION`のどちらか一方だけを名乗る。

Evidence FileのHashは**保存Bytesに対するSHA-256**であり、Canonical化を経由しない。Manifestには`evidence_root`からの相対Pathを記録する。絶対Path、`..`を含むPath、Symlink経由のPathは拒否する。Evidence自身にSecret、Token、Provider生本文、個人情報を含めない。

### 26.2.1 Evidence JSONの必須契約

VerifierはEvidence FileをHashするだけでなく**内容を検証する**。v1.6ではHashしか見ていなかったため、内容が`{}`のFileでもPASS証跡として成立していた。

`observed_event_sequence`は**実際に正本LedgerへAppendされたEvent列**である（§19.1.1）。Request列・Verdict予測列・期待値から導出しない。観測していない場合に`[]`を書かない。`[]`は「見て0件だった」であり、「見ていない」とは意味が正反対である。`side_effects`の`null`と全ゼロを区別するのと同じ規則であり、未観測はEvidence生成を拒否する。

Case Evidence（`tests/cases/<test-id>/<case-id>/evidence.json`）：

```json
{
  "evidence_schema_version": "1.2",
  "release_scope": "MVP0-A",
  "test_id": "AT-CRASH-001",
  "case_id": "BEFORE_PREPARED",
  "status": "PASS",
  "expectation_descriptor_hash": "sha256:...",
  "input_fixture_hash": "sha256:...",
  "input_fixture_path": "fixtures/AT-CRASH-001/BEFORE_PREPARED.json",
  "raw_result_path": "raw/AT-CRASH-001/BEFORE_PREPARED.json",
  "raw_result_hash": "sha256:...",
  "runner_source_hash": "sha256:...",
  "command": ["python", "-m", "pytest", "..."],
  "exit_code": 0,
  "observed_subject_type": "ACTION_ATTEMPT",
  "observed_state": "PREPARED_DURABLE",
  "observed_error_code": null,
  "observed_event_sequence": ["ACTION_PREPARED", "RECOVERY_STARTED", "RECOVERY_DECIDED"],
  "assertions": [{"expression": "target_hash == base_hash", "result": true}],
  "durability_tier": "T2_CACHE_DROP",
  "implementation_commit_sha": "0123456789abcdef0123456789abcdef01234567",
  "runtime_environment_hash": "sha256:...",
  "schema_set_hash": "sha256:...",
  "migration_head": "0001_initial",
  "producer": "harness-test-runner/1.0",
  "test_run_id": "...",
  "started_at": "2026-08-05T00:00:00Z",
  "recorded_at": "2026-08-05T00:00:00Z"
}
```

Verifierの検査：

* `release_scope`、`test_id`、`case_id`、`status`、`input_fixture_hash`がManifest行と一致する。
* `expectation_descriptor_hash`がRegistryの値と一致する。
* `observed_subject_type`／`observed_state`／`observed_error_code`／`observed_event_sequence`がRegistryの期待値と一致する。
* `assertions`が非空で、全要素の`result`が`true`である。
* `test_run_id`が存在する。
* `input_fixture_path`と`raw_result_path`がEvidence Root内の実Fileであり、Bytes Hashが一致する。
* `runner_source_hash`、`command=list[str]`、`exit_code=0`、実行時刻が存在する。
* Commit／Environment／Schema Set／Migration Headが判定Manifestと一致する。
* Crash／I/O Fault Caseの`durability_tier`がRegistry導出値と一致する。

Gate Evidence（`gates/<gate-id>.json`）は`gate_id`、`release_scope`、`status`、`case_refs`を持つ。`case_refs`の各要素はManifest上で`PASS`のCaseでなければならない。**Gateは自分の根拠となったCaseを名指しする義務を負う。**

領域Evidence（`<area>`）は`area`、`release_scope`、`status`、`summary`、`test_run_id`を持つ。

#### Case Evidenceは層と観測有無を自分で名乗る（v1.15）

Case Evidenceは次の2 Fieldを**必須**で持つ。どちらもEvidence単体から読めなければならない。
外部の対応表へ預けると、表を失った時点で「何を検証したEvidenceなのか」が判らなくなる。

| Field | 値 | 意味 |
|---|---|---|
| `evidence_kind` | `UNIT` | Unit層で検証した。Ledgerへの作用を主張しない |
| | `ORCHESTRATION` | Orchestration層で検証した。Ledger観測を伴う |
| `event_observation` | `OBSERVED` | Ledgerを観測し、Event列を取得した |
| | `OBSERVED_EMPTY` | Ledgerを観測し、Event列が空だった |
| | `NOT_APPLICABLE` | Event観測を要求しないUnit Caseである |

`evidence_kind`は**層のラベルであって領域所属ではない**。どの領域へ数えるかは
`evidence-areas.yaml`とRegistryが決める。層と領域を同じFieldへ持たせると、
領域を増やすたびに層の意味が動く。

`event_sequence`との対応は次のとおりで、他の組合せを許さない。

| `event_observation` | `event_sequence` |
|---|---|
| `OBSERVED` | 観測したEvent列（非空） |
| `OBSERVED_EMPTY` | `[]`（**必須**。`null`にしない） |
| `NOT_APPLICABLE` | `null`（**必須**。`[]`にしない） |

`NOT_APPLICABLE`でだけ`null`を許す。`[]`を残すと、`event_observation`を読まない
消費側が「観測して0件」と読む。真実源を1つに保つため、非該当では列そのものを持たせない。

`evidence_kind`が`ORCHESTRATION`のとき`event_observation`に`NOT_APPLICABLE`を
書けない。Orchestration Evidenceは常にLedger観測を要求する。

#### 免除の根拠はRelease束縛のSnapshotだけである（v1.16）

Case Evidenceは`event_observation_policy`も持つ。Registryが宣言したPolicyを
Evidence自身へ写し、Verifierが照合できるようにするためである。

**VerifierはローカルのRegistryを読まない。** 読むのはRelease Manifestが束縛した
`registry-snapshot.json`だけである。ローカルのRegistryを直接信用すると、
Registryを書き換えた木でVerifierを走らせるだけで免除を作れる。判定の入力は
Release時点で固定されたSnapshotでなければならない。

Verifierは次を拒否する。いずれも「そのCaseは何を検証したのか」が決まらない状態である。

| # | 拒否条件 |
|---:|---|
| 1 | 期待Event列が空なのにSnapshotへPolicyが無い |
| 2 | Evidenceの`event_observation_policy`がSnapshotの値と一致しない |
| 3 | `REQUIRED_EMPTY`のCaseが`NOT_APPLICABLE`を名乗る |
| 4 | `NOT_APPLICABLE`のCaseが`OBSERVED_EMPTY`を名乗る |
| 5 | `evidence_kind`が`ORCHESTRATION`で`NOT_APPLICABLE`を名乗る |
| 6 | `evidence_kind`が`UNIT`で`REQUIRED_EMPTY`を名乗る |
| 7 | Snapshotに無いCaseのEvidenceである |
| 8 | Policyが語彙の外である |
| 9 | 現行Release判定へ`1.2`のEvidenceを出している |

Policyと層と観測有無は1対1に対応する。**ずれた組合せを1つも許さない。**

| Policy | `evidence_kind` | `event_observation` | `event_sequence` |
|---|---|---|---|
| `REQUIRED_EMPTY` | `ORCHESTRATION` | `OBSERVED_EMPTY` | `[]` |
| `NOT_APPLICABLE` | `UNIT` | `NOT_APPLICABLE` | `null` |
| 宣言なし（期待列が非空） | `ORCHESTRATION` | `OBSERVED` | 期待列と完全一致 |
| 宣言なし（期待列が空） | — | — | **Evidence生成もRelease判定も拒否** |

最後の行が要点である。**空の期待列とPolicy欠落の組合せを既定値で埋めない。**
埋めた側へ倒すと、Ledgerを一度も読んでいないCaseが「0件を観測した」と主張するか、
免除されるべきCaseが永久に落ちる。どちらも「決まっていない」を「決まった」に
すり替える。決まっていないなら止める（不変条件#9）。

Snapshotの`expectations`はPolicyの**欠落も含めて**Release Hashへ束縛される。
後からPolicyだけを足しても`registry_snapshot_hash`と`expectation_descriptor_hash`が
動くため、束縛済みのReleaseに対しては通らない。

いずれのEvidence Fileも、**同一Bytes Hashを複数の判定単位へ流用できない。** Verifierは`evidence_manifest_hash`の一意性を検査する。

## 26.3 実行順序

```text
Release Scope決定
→ Registry Snapshot再生成・Commit済みsnapshotとの一致確認
→ Verifier自己試験（AT-VERIFIER-*）
→ Source Tree固定・Clean確認・Subtree Hash採取
→ Dependency Lock／Environment採取
→ Spec Lint
→ Schema Suite
→ Fresh Migration
→ Unit／Property／Integration Test
→ Filesystem Corpus
→ Fault Injection 10点＋I/O Fault（T1／T2）
→ Recovery／Backup／Restore／Drain／GC
→ Performance 30計測
→ Approval UX 10計測（§26.6で再利用可能な場合は省略可）
→ Secret Canary全文検査
→ Scope内Case Evidence生成
→ Scope内Gate評価
→ Runtime GO Verifier（--release-scope 付き）
→ Release Manifest生成
```

**Verifier自己試験を最初期に置く。** 判定器が壊れている状態で生成したEvidenceは、後段が全て成功しても意味を持たない。

前段が失敗した場合でも後段をPASSとして生成しない。試験Harness障害とSUT障害を区別し、Harness障害は`BLOCKED`、SUT不適合は`FAIL`とする。

## 26.4 Runtime Environment固定

最低限、次を記録する。

* Windows Build、WSL Version、Kernel、Distribution
* `/proc/version`、`uname -a`、`/proc/self/mountinfo`
* Workspace Root、Mount Point、Filesystem Type、Mount ID
* CPU、Memory、Disk、Filesystem空き容量
* Python Version、SQLite Version、OpenSSL Version
* `pip freeze`またはLock File Hash
* Locale、Timezone、umask
* Harness Config Hash、Policy Hash、Schema Set Hash
* Fault Injection設定が通常Runでは無効であること

Runtime EnvironmentがWSL2 Linux側Filesystem要件を満たさない場合、他の試験結果にかかわらずGOを拒否する。

Environment Evidenceは`tools/collect_runtime_environment.py`が`/proc/version`と`/proc/self/mountinfo`を実読して生成する。`is_wsl2`、Filesystem Type、Mount ID、Workspace Rootを人手で入力しない。Collectorが`/mnt/*`、Network FS、FUSE／Overlayを検出した場合はEvidenceをPASSとして生成しない。

### 26.4.1 CIとの境界

GitHub Actions等のCI Runnerは、Spec／Static／Verifier自己試験だけを実行する。CIで作成されたテスト結果、Coverage、SBOM、Verifier自己試験ログをRuntime Evidenceの代替にしてはならない。CIではRelease Manifestの生成を禁止し、`tools/check_ci_runtime_boundary.py`でその不在を検査する。WSL2 Linux側Filesystem、Kernel、Mount情報、電源断耐久性を取得できる手動Release GateだけがRuntime Evidenceを発行できる。

## 26.5 PASS／FAIL規則

| 状態 | 意味 | Runtime GOへの算入 |
|---|---|---|
| `PASS` | 期待値とEvidenceが一致 | 算入する |
| `FAIL` | SUTが期待値を満たさない | GO拒否 |
| `BLOCKED` | Harness、環境、依存物不足で実行不能 | GO拒否 |
| `UNVERIFIED` | 未実行またはEvidenceなし | GO拒否 |
| `SKIPPED` | 条件分岐等による未実行 | GO拒否 |
| `XFAIL` | 既知不具合を許容 | GO拒否 |

Runtime GO対象Manifestでは`PASS`以外を許可しない。

### 26.5.1 BLOCKEDからの復帰

`BLOCKED_SPEC_CLARIFICATION`、`BLOCKED_EVIDENCE_MISSING`、Harness障害、環境不一致で停止したTaskは、`blocked/records/BLK-*.json`を作成してから停止する。同一Taskに`OPEN` Recordがある間は、AIエージェントが同じ実装・試験を反復してはならない。Recordには観測したCommand／終了Code、対象Release Scope、設計書Hash、Registry Snapshot Hash、Owner、次Action、受入条件を束縛する。

Ownerが設計書・Registry・環境・依存物のいずれかを変更した場合は、変更後Hashを持つ新しいRecordまたは既存Recordの明示的な`RESOLVED`更新を行う。復帰時は`BLOCKED-RECOVERY.md`の順序（Decision／環境修復→Spec／Snapshot再生成→受入条件→Verifier自己試験→元Task）を守り、未解決Recordを残したままGOを宣言してはならない。Recordの検証は`tools/check_blocked_records.py`と`tools/validate_blocked_record.py`で自動化する。

## 26.6 再認定階層

§26.1のTupleは1項目の変化でGOを失効させる。`implementation_commit_sha`がTupleに含まれる以上、**素直に読むとバグ修正1件ごとに§26.3の全工程が必要**になる。Approval UXは人手10計測であり、Commit毎の再実行は物理的に成立しない。保守を成立させるため、変更の性質に応じた3階層を定義する。

| Tier | Trigger | 再実行範囲 | 人手計測Evidence |
|---|---|---|---|
| `TIER_1_SMOKE` | 文書・コメント・非実行Asset のみの変更 | Spec Lint、Manifest Validator、Verifier自己試験、Registry Snapshot一致 | 再利用可 |
| `TIER_2_AFFECTED` | 実装変更で、影響分析により関連Gateが特定できる場合。依存Lock更新を含む | `TIER_1`＋全Unit／Property／Schema／Integration＋影響Gateに紐づく全Case＋Fault／Recovery／Migration | Subtree Hash不変なら再利用可 |
| `TIER_3_FULL` | Schema Set変更、Migration Head変更、Plan Content Projection変更、Canonical／Hash規約変更、Threat Model変更、Phase Scope変更、Verifier変更、Registry構造変更 | §26.3の全工程 | 再取得必須 |

### 影響分析の記録

`TIER_2`を選ぶ場合、次をEvidenceへ残す。推測で範囲を狭めてはならない。

```text
changed_paths        : 変更File一覧
affected_gate_ids    : 影響Gate（根拠付き）
excluded_gate_ids    : 除外Gateと除外根拠
tier_decision_by     : 判定者
tier_decision_at     : 判定時刻
```

### 人手計測Evidenceの再利用

`approval_ux`のような人手計測領域は、Manifestで次を宣言することで再利用できる。

```json
{
  "area": "approval_ux",
  "status": "PASS",
  "evidence_path": "approval/approval-ux-report.json",
  "evidence_manifest_hash": "sha256:...",
  "reused_from": {
    "release_id": "r-2026-08-01",
    "bound_subtree_hash": "sha256:..."
  }
}
```

Verifierは`bound_subtree_hash`が現在の`source_subtree_hashes["src/harness/presentation/cli"]`と一致することを検査する。**承認UIのSourceが1バイトでも変われば再利用は無効化され、再計測を要求する。** 機械計測領域への`reused_from`指定は拒否する。

`human_measured`は**この再利用可否判定にだけ**使う。領域が必須かどうかは`phase_scope`が決めるのであって`human_measured`は関与しない。2つを結び付けると、人手計測領域を機械計測へ変えるだけで必須性が変わる。

過去Releaseが存在しない時点で`reused_from`は成立しない。**「最初のRelease」は人手計測領域を再利用で埋められない。**

不確かな場合は上位Tierを選ぶ。Tierの選択自体がPolicy判断であり、記録対象である。

# 27. Runtime GO Evidence Manifest契約

同梱Templateを実行結果から更新する。人手でHashを転記せず、試験HarnessがEvidence作成直後に計算する。Templateは`tools/build_manifest_template.py`がRegistryから生成する。

必須トップレベル項目（`manifest_version=1.2`）：

* `manifest_version="1.1"`
* `design_version="1.7"`
* `design_sha256`
* `release_scope`
* `implementation_repository`
* `implementation_commit_sha`
* `source_tree_clean=true`
* `source_subtree_hashes`：再認定でEvidence再利用を宣言する場合に必須
* `runtime_environment`：`environment_manifest_path`／`environment_manifest_hash`／`is_wsl2`／`workspace_on_linux_native_fs`／`python_version`
* `schema_set_hash`
* `migration_head`
* `test_manifest_hash`
* `expected_gate_count`／`expected_test_id_count`／`expected_case_count`：**Scope依存。固定値ではない**
* `test_cases`／`gates`／`required_evidence_areas`：いずれも**Scope依存**。`required_evidence_areas`はScopeの必要領域と過不足なく一致させる。多くても少なくても`FAIL`とする
* `skipped_count=0`／`xfail_count=0`
* `release_decision`

`expected_*_count`はManifest作成者の自己申告であり、Verifierはこれを信用せずRegistry導出値と突合する。不一致は`FAIL`とする。この項目は「Manifest作成者が何件だと思っていたか」を記録し、認識のずれを検出するために残す。

Verifierの検査対象：

1. 件数と集合の両方。Case／Gate／領域それぞれで過不足を検出する。
2. Test ID／Case IDの重複。
3. Hash形式。
4. Evidence Fileの存在、evidence-root配下であること、実Bytes Hash一致。
5. **Evidence Fileの内容とRegistry期待値の一致**（§26.2.1）。
6. **Evidence Fileの一意性**。同一Hashの流用を拒否する。
7. 全Status。
8. `test_manifest_hash`のRegistry導出値との一致。
9. `python_version`のADR-003適合。
10. Registry Snapshotの自己Hash整合と、その`design_sha256`の実File一致。

# 28. 自動Runtime GO Verifier

```bash
python verify_runtime_go.py \
  --design design-v1.14-runtime-go.md \
  --registry registry-snapshot.json \
  --release-scope MVP0-A \
  --manifest runtime-evidence/<release-id>/runtime-go-manifest.json \
  --evidence-root runtime-evidence/<release-id> \
  --emit-report release/runtime-go-verification-report.json \
  --emit-release-manifest release/runtime-go-release-manifest.json
```

`--registry`と`--release-scope`はv1.1で必須化した。件数と必要集合をコードへ持たせないためである。

Verifier v1.3は必要Evidence領域も`--release-scope`から導出する。Scope未指定は元より受け付けず、**未知Scopeや必要領域が空のSnapshotは`INPUT_INVALID`で停止する。** 全領域へフォールバックしない。

`--emit-report`の出力先は**evidence-root外**とする。Evidence Tree内部へ書き戻すと、判定のたびにTreeが変化する。

終了Code：

| Code | 意味 | Decision |
|---|---|---|
| `0` | 全条件合格。Release Manifest生成可 | `RUNTIME_GO` |
| `2` | Evidence不足／未実行 | `BLOCKED_EVIDENCE_MISSING` |
| `3` | FAIL／Hash不一致／件数不一致／Scope不一致／Evidence流用／内容不一致 | `RUNTIME_NO_GO` |
| `4` | Manifest／Registry／Scope指定が入力として不正 | `INPUT_INVALID` |

VerifierはGOを付与するEvidence整合性境界である。単一UID内の改ざん耐性は主張せず、次の束縛と否定系試験を担保する。

* Verifier自身のSource Hash（`verifier_source_sha256`）をReportとRelease Manifestへ記録する。
* `verification_report_hash`は`manifest_sha256`、`registry_snapshot_hash`、`verifier_source_sha256`、`implementation_commit_sha`、`release_scope`を含む本体全体を覆う。v1.0のreport hashはdecisionと件数しか覆わず、**全く内容の異なるManifest 2件が同一Hashを出していた**（`AT-VERIFIER-015`が再発を検出する）。
* `tests/test_verify_runtime_go.py`の`AT-VERIFIER-*`全Caseで、既知の不整合・流用・未束縛証跡がFAILすることを機械検証する。件数は本文へ書かず、Verifierの`SELF_TEST_MIN_TESTS`が実件数と一致することを同Fileの試験が固定する（不変条件#18）。
* `verifier_self_test`をEvidence必須領域とし、自己試験の結果なしにGOを出せなくする。

Verifierは stdlib のみに依存する。GO判定器へ第三者依存を持ち込まない。

# 29. v1.6作成時点のEvidence状態

| 領域 | 状態 | 理由 |
|---|---|---|
| 実装Repository | `BLOCKED` | 本会話へ実装Sourceが提供されていない |
| Spec Lint | `UNVERIFIED` | 実行可能なRegistry／Linterが未提供 |
| Core Schema（v1.6作成時点は20件。現行はschemas.yamlが正本） | `UNVERIFIED` | Schema実ファイルとFixtureが未提供 |
| SQLite Migration | `UNVERIFIED` | Migration Sourceと実行DBが未提供 |
| Linux Safe Reader | `UNVERIFIED` | 実装とFilesystem Corpusが未提供 |
| Runtime Attestation | `UNVERIFIED` | 対象Runtimeが未提供 |
| Fault Injection／Recovery | `UNVERIFIED` | 実装と試験Harnessが未提供 |
| MVP0-A Scope 107 Cases | `UNVERIFIED` | Fixture／Evidenceが未提供 |
| MVP0-A 45 Gates | `UNVERIFIED` | Case Evidenceが未提供 |
| Registry／Spec Linter | `PASS` | v1.8で同梱し、本書に対して実行済み |
| Runtime GO Verifier自己試験 | `PASS` | `AT-VERIFIER-*`実行済み |
| Runtime GO | **`BLOCKED_EVIDENCE_MISSING`** | 実装Repositoryと実行Evidenceが未提供 |

この表は設計上の不足ではなく、実装・実行物が入力されていないという事実を表す。文書生成だけでこの状態を`PASS`へ変更してはならない。

# 30. Runtime GO Release Decision

## 現在のDecision

```text
Release Scope        : MVP0-A
Design Version       : 1.7
Input v1.6 Hash      : e44ace7606c4f78eb4c4dabf369c37e3e37680f8dcbad0712676e140f96f8f8e
Design Artifact Hash : CALCULATED_BY_VERIFIER
Registry Snapshot    : PROVIDED
Verifier Self Test   : PASS
Implementation SHA   : NOT_PROVIDED
Evidence Manifest    : NOT_PROVIDED
Gate Result          : 0 / 36 PASS
Acceptance Cases     : 0 / 70 PASS
Evidence Areas       : 0 / 13 PASS
Decision             : BLOCKED_EVIDENCE_MISSING
```

## GO宣言の生成規則

Verifierが合格した場合だけ、次の形式のRelease Manifestを生成する。

```text
Decision             : RUNTIME_GO
Release Scope        : MVP0-A
Design SHA-256       : sha256:<64-hex>
Registry Snapshot    : sha256:<64-hex>
Verifier Source      : sha256:<64-hex>
Manifest SHA-256     : sha256:<64-hex>
Implementation SHA   : <40-hex Git SHA>
Environment Hash     : sha256:<64-hex>
Schema Set Hash      : sha256:<64-hex>
Test Manifest Hash   : sha256:<64-hex>
Gate Report Hash     : sha256:<64-hex>
Recertification Tier : TIER_3_FULL
Passed Gates         : 36 / 36
Passed Cases         : 70 / 70
Passed Areas         : 13 / 13
Skipped/XFail        : 0 / 0
Reused Evidence      : <領域名一覧、なければ NONE>
Durability Tier      : T1/T2 verified, T3 NOT VERIFIED
Residual Limitation  : Device-level power-loss durability is not proven
Release Manifest Hash: sha256:<64-hex>
```

このGOは記載されたRelease Scope、Commit、Environment、Schema Set、Migration Head、Registry Snapshot、Verifier Sourceへだけ有効である。**他PhaseのGOへ読み替えてはならない。**
