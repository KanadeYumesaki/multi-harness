<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

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
