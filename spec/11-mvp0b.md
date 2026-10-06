<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

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
