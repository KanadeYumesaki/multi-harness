# ADR-006：段階的委任（Progressive Delegation）

- 状態: **提案 v4（再レビュー指摘を反映）**。設計書v1.7へは未反映
- 起案日: 2026-08-06 ／ 改訂: 2026-08-06（Event名統一・SQLite線形化・Registry統合・縮小単調性を反映）
- 対象Phase: MVP0-A（機構と読取り専用委任）→ MVP1-A（Workspace書込委任）
- 関連: §1.10 Approval Grant、§3.8 Approval Lifecycle、§3.8.5 バイパス禁止、§9.7、ADR-004

## 0. v1からの変更点（レビュー反映）

| # | 指摘 | 反映 |
|---|---|---|
| 1 | Predicateを宣言型Canonical JSON Schemaにする | §4.2。任意コード・正規表現・Wildcardを禁止 |
| 2 | 照合対象に§3.8.2の無効化項目を全て含める | §4.3。16項目を必須化 |
| 3 | DelegationGrantを一回限りApprovalGrantと分離 | §4.1。同一SQLite内の別Schema・別論理Store・別Lifecycle |
| 4 | 実行ごとに一回限りの派生Approvalを発行 | §5.2。DerivedApprovalGrantを毎回新規発行 |
| 5 | 失効・期限・回数・署名不正・不一致を全てFail-Closed | §5.3 |
| 6 | 失効とEffect開始の競合を最終Effect GateとCASで検証 | §5.4。**v1に欠けていた** |
| 7 | 非委任床をRegistry化 | §6。`delegation-floor.yaml`を新設 |
| 8 | 署名鍵のTrust Anchor／Rotation／Revocation／Audience／Tenant／Issuer | §7 |

## 1. 背景と要求

> 最初は全て確認する。その確認の際に「次回以降どうするか」も一緒に決める。
> それを繰り返すことで徐々に自動化されていく。
> ただし安全ゲートとして、次回以降の処理で確定した部分は後で確認・変更できること。

## 2. 既存決定との衝突と、その解き方

§3.8.5は「前回と同じ場合の自動承認」を禁止する。CLAUDE.md不変条件#11も同じ。
これは§0.1（開発体制1名）とADR-004に由来する意図的な判断である。

本ADRは禁止条項を撤廃せず、**判定根拠を置き換える**。

| | 引き続き禁止 | 本ADRが導入するもの |
|---|---|---|
| 判定根拠 | 過去の実行履歴との一致 | 人間が事前に承認した**宣言型Predicate**と、**いま解決されたPlan**の照合 |

過去実行は判定に一切使わない。§3.8.5の該当行を次へ改訂する。

| 現行 | 改訂案 |
|---|---|
| 前回と同じ場合の自動承認 | **Predicateを持たない自動承認。および「前回と同じ」を根拠とする自動承認** |

`--yes`／`--auto-approve`／`--force`／`--skip-approval`の禁止は**そのまま維持**する。

### 2.1 単独開発における位置づけ（残余リスクへ記載）

**単独開発においてDelegationGrantは安全統制ではない。** Maker／Approver／Operatorが同一人物である以上、
委任は独立した第三者統制を提供しない。提供するのは①確認操作の削減、②自動承認された実行の完全な監査証跡、
③委任範囲の明示化と即時失効の3点だけである。改ざん耐性・否認防止は主張しない（ADR-004）。

## 3. 用語

| 用語 | 定義 |
|---|---|
| `DelegationGrant` | 人間が承認して発行する、**再利用される**範囲限定・失効付きの委任Artifact |
| `DerivedApprovalGrant` | DelegationGrantの成立ごとに**新規発行される一回限り**のApprovalGrant |

**両者は同一SQLite DB内の別Schema・別論理Store（テーブル／namespace）・別Lifecycleとする**（レビュー条件3）。
既存不変条件#14の「状態は単一SQLite DBに保存」を変更しない。Artifact Bytesだけが外部CASにあり、
Approval／Delegationの状態を別DBへ分散させない。DelegationGrantは「承認する権限の範囲」であり、
実行権限そのものではない。

## 4. DelegationGrant

### 4.1 生成フロー

```text
通常の承認画面（§3.8.3の7項目）
  → 人間がこの実行を承認                      ← Decision 1
  → 続けて「次回以降どうするか」を選択
       (a) 毎回確認する                        ← 既定。何も作らない
       (b) この範囲を次回から自動にする
  → (b)の場合、Predicate全文と有効期限を表示して再確認
  → 人間の署名でDelegationGrantを発行          ← Decision 2（独立した承認）
  → DELEGATION_CREATED
```

Decision 1とDecision 2は別のPolicyDecision／Ledger Eventとして記録する。
委任範囲を見ずに委任が成立する経路を作らない。

### 4.2 Predicateは宣言型Schemaとする（条件1）

Predicateは**JSON Schemaで構造を固定した宣言型データ**とする。次を禁止する。

* 任意コード、式評価、テンプレート展開
* 正規表現、Glob、Wildcard（`*`、`**`、`?`）
* 否定条件、OR結合（AND結合のみ）
* 実行時に解決される変数参照

Path指定は**Workspace相対Canonical Pathの完全一致、またはDirectory prefixの明示列挙**だけとする。
prefixは末尾`/`を必須とし、`..`・絶対Path・NUL・制御文字を拒否する。

```text
predicate_hash = SHA-256(
    "FDE-HARNESS/delegation-predicate/1/" || RFC8785-JCS(Predicate))
```

JSON Schemaは`additionalProperties: false`とし、未知Fieldを拒否する。

### 4.3 照合対象（条件2）

§3.8.2「Run単位承認でも次のいずれかが変化した場合はApprovalを無効化する」の**全項目**を
Predicateの照合対象に含める。1つでも欠けると、無効化されるはずの変化が自動承認を通過する。

| # | 照合対象 | Predicateでの表現 |
|---|---|---|
| 1 | Plan | `plan_content_hash`ではなく構成要素で照合（Hash一致は「前回と同じ」になるため使わない） |
| 2 | Context | `context_bundle_hash`の由来Capability Set／Evidence Hashの範囲 |
| 3 | Target Path | `path_scope`（完全一致またはprefix列挙） |
| 4 | Base Hash | `require_base_hash_match: true`固定 |
| 5 | Provider | `provider_id`完全一致 |
| 6 | Executable | `executable_path` + `executable_sha256`完全一致 |
| 7 | argv | `argv`完全一致（配列全体） |
| 8 | cwd | `working_directory_identity`完全一致 |
| 9 | Runtime Spec | `runtime_envelope_spec_semantic_hash`完全一致 |
| 10 | Auth Route | `auth_route`完全一致 |
| 11 | Account | `account_identity` / `tenant_id` / `billing_identity`完全一致 |
| 12 | Policy | `policy_snapshot_hash`完全一致 |
| 13 | Schema | `schema_set_hash`完全一致 |
| 14 | Token Profile | `token_profile_hash`完全一致 |
| 15 | Pricing／Entitlement | `pricing_catalog_hash` / `entitlement_snapshot_hash`完全一致 |
| 16 | **Scan結果／Masking Identity** | `classification_ceiling`、`secret_findings_count == 0`、`masking_decision != REJECT`。MASKEDの場合は `source_normalized_hash`、`masking_receipt_hash`、`masking_policy_version`、`masker_model_digest`、`normalization_profile`、`normalization_profile_artifact_hash` を完全一致 |

加えて範囲上限を持つ。

`action_types` / `workspace_id` / `max_changed_files` / `max_added_lines` / `max_deleted_lines` /
`max_risk_level`（`LOW`または`MEDIUM`のみ）/ `billing_mode = FREE`固定 /
`expected_effect ∈ {NONE, WORKSPACE_WRITE}`

### 4.4 Authority項目

`delegation_id` / `delegation_schema_version` / `predicate` / `predicate_hash` /
`created_from_run_id` / `created_from_action_id` /
`approver_subject_id` / `approver_tenant_id` / `authentication_context_class` /
`issued_at` / `not_before` / `expires_at` / `max_uses` / `use_count` / `reconfirm_after_uses` /
`revocation_epoch` / `status` / `audience` / `issuer_id` / `issuer_key_id` / `signature` / `store_version`

### 4.5 状態

```text
ACTIVE → EXPIRED      （expires_at超過 / max_uses到達 / reconfirm_after_uses到達）
       → REVOKED      （明示失効）
       → SUPERSEDED   （範囲を狭めた新Grantで置換）
       → INVALIDATED  （束縛Hashのいずれかが変化）
```

終端から復帰しない。

## 5. 消費

### 5.1 解決

```text
PLAN_RESOLVED → POLICY_DECIDED(ALLOW, approval_required=true)
  → Delegation Resolver
      1. §6の非委任床を先に評価。1つでも該当すれば即不成立
      2. workspace_id一致かつ status=ACTIVE を列挙（delegation_id Code Point昇順）
      3. 署名・Trust Anchor・Audience・Tenant・Issuer・Expiry・Revocation Epochを検証
      4. Predicateの全16項目を「いま解決されたPlan」と照合
      5. 一致Grantが2件以上ある場合は最も狭いものを1件だけ採用。
         包含関係で一意に決まらなければ DELEGATION_PREDICATE_AMBIGUOUS で不成立
```

### 5.2 派生Approvalは毎回新規（条件4）

成立時、**DelegationGrantを実行権限として使い回さない。**
その実行専用の`DerivedApprovalGrant`を新規発行する。

* 新しい`grant_id`と`nonce`（DB一意制約）
* `use_count = 1`
* `execution_plan_hash`へ束縛
* `approval_mode = POLICY_DELEGATED`
* `delegation_id` = 根拠となったDelegationGrant
* Expiryは`min(DelegationGrant.expires_at, Policy TTL, Capability Expiry, 既定の短寿命)`

消費は§1.10のCAS経路をそのまま通る。`use_count`の加算とApproval消費を同一SQLite Transactionで行う。

```text
DELEGATION_MATCHED → APPROVAL_ISSUED → APPROVAL_CONSUMED
```

**Approval Managerを迂回するAPIを作らない**（§3.8.5、不変条件#11）。

### 5.3 Fail-Closed（条件5）

次は全て不成立とし、通常の人間承認へ戻す。曖昧・判定不能も不成立とする。

| 事象 | Error Code |
|---|---|
| 失効済み | `DELEGATION_REVOKED` |
| 期限切れ | `DELEGATION_EXPIRED` |
| 回数上限到達 | `DELEGATION_EXPIRED` |
| 署名不正 | `DELEGATION_SIGNATURE_INVALID` |
| Trust Anchor不一致／Rotation未検証 | `DELEGATION_TRUST_ANCHOR_INVALID` |
| Audience／Tenant／Issuer不一致 | `DELEGATION_SUBJECT_MISMATCH` |
| Predicate不一致 | `DELEGATION_SCOPE_EXCEEDED` |
| 一意に決まらない | `DELEGATION_PREDICATE_AMBIGUOUS` |
| 非委任床に該当 | `DELEGATION_NOT_DELEGABLE_ACTION` |
| 評価中の例外・判定不能 | `DELEGATION_NOT_DELEGABLE_ACTION`（推測で通さない） |

### 5.4 失効とEffect開始の競合（条件6）★v1に欠けていた

Approval消費からEffect実行までの間にDelegationGrantが失効した場合、
**Approval時点の検証だけでは失効を反映できない。**
不変条件#3（Fencing Tokenを最終Storage書込み直前に再検証する）と同じ扱いとする。

外部ファイルのAtomic ReplaceはSQLite Transactionへ含められないため、DB内に**Effect線形化点**を置く。
SQLiteの`BEGIN IMMEDIATE`とCAS更新を線形化点とし、外部Effectの実行結果とは別に扱う。

```text
最終Effect Gate（Fencing Token再検証と同一SQLite Transaction）
  → delegation_grant を status=ACTIVE かつ revocation_epoch=期待値でCAS更新
  → 成功: DELEGATION_EFFECT_LINEARIZED と effect_fence_token を記録してCommit
       → Commit後にのみ外部Atomic Replaceを開始
  → 失敗: DELEGATION_REVOKED_MID_FLIGHT / FENCING_REJECTED
       → Effect実行前に停止、ActionAttemptをBLOCKED_CONFLICT
```

`harness delegation revoke`も同じSQLite直列化を使用する。失効が線形化点より先にCommitした場合は
Effectを開始しない。Effect線形化が先にCommitした場合、後続の失効は
`DELEGATION_REVOKED_AFTER_EFFECT_START`として監査記録し、Effectの結果確認または
Reconciliation対象とする。DB Commit後・外部Effect前のプロセス停止も同じRecovery経路へ送る。

したがって「失効競合時は常にEffect 0件」ではなく、Barrierで線形化点の前後を固定した2 Caseを検証する。

## 6. 非委任床をRegistryへ（条件7）

散文ではなく`design-source/registries/delegation-floor.yaml`を正本とする（不変条件#18）。
Resolverは本Registryを読んで評価し、コードへ条件を直書きしない。
v1.8反映時は`delegation-floor.yaml`をsnapshotのpolicy registry領域へ含め、
source hash・生成Domainコード・Verifierの入力へ必須化する。ファイル欠落やHash不一致は
委任Resolverを起動せず、Fail-Closedとする。

初期内容は`delegation-floor.draft.yaml`を参照。11規則を含む。

1. Paid Execution（`billing_mode != FREE`）
2. External Effect
3. Secret／Credential検出
4. Policy stale（`CURRENT`以外。`LKG_WITHIN_TTL`も不可）
5. Recovery／Reconciliation／Repair／Emergency
6. HIGH／CRITICAL Risk
7. **DelegationGrantの作成・拡大・変更**（委任による委任の禁止）
8. **破壊的操作・特権操作**（削除、GC実行、Backup復元、Migration、Drain、鍵操作、Policy変更）
9. マスキングREJECTを含む入力（ADR-007）
10. 異常状態（`EFFECT_UNKNOWN`、`CANCEL_UNKNOWN`、`BLOCKED_CONFLICT`、判定不能）
11. `RESTRICTED`／`SECRET`または分類不能

## 7. 署名鍵とTrust Anchor（条件8）

DelegationGrantの署名は§1.10のApproval署名鍵とは**別Key IDとする**（用途分離）。

| 項目 | 内容 |
|---|---|
| 鍵の所在 | `$XDG_DATA_HOME/harness/keys/delegation-signing.key`。owner=実行UID、mode=0600、Linux側FS。不一致は`APPROVAL_KEY_PERMISSION_INVALID`で停止 |
| Trust Anchor | `policy/delegation-trust-anchors.json`。署名済みImmutable Artifact。`minimum_accepted_version`でRollbackを拒否 |
| Rotation | 新Key IDを追加し重複期間を設ける。旧Keyで発行済みGrantは`INVALIDATED`とし、自動移行しない |
| Revocation | Key単位失効（Trust Anchor更新）とGrant単位失効（`revocation_epoch`）の2層 |
| Audience | `audience`を`harness://<workspace_id>`へ固定。他Workspaceで再生できない |
| Tenant／Issuer | `approver_tenant_id`／`issuer_id`を検証。不一致は`DELEGATION_SUBJECT_MISMATCH` |

**単独UIDでは鍵もTrust Anchorも同一UIDで更新できる**ため、これは改ざん検知であり改ざん耐性ではない（ADR-004）。

## 8. 事後の確認・変更

```text
harness delegation list
harness delegation show <delegation-id>     # Predicate全文・期限・使用回数・生成元Run
harness delegation audit <delegation-id>    # この委任で自動承認された全Run／Action
harness delegation revoke <delegation-id>
harness delegation narrow <delegation-id> --predicate <file>
```

| 操作 | 手続き | 理由 |
|---|---|---|
| 失効 | 承認不要・即時 | 常に安全方向 |
| 縮小 | 承認不要・即時。旧Grantは`SUPERSEDED` | 常に安全方向 |
| **拡大** | **不可。新規DelegationGrantを人間の承認で作る** | 承認済みPredicateの意味が事後に変わることを防ぐ |

`narrow`は新Predicateが旧Predicateの**安全方向の部分集合**であることを機械検証する。
旧Grantは変更せず、新しいGrantを発行して旧Grantを`SUPERSEDED`にする。
部分集合でなければ`DELEGATION_SCOPE_EXCEEDED`で拒否し、新規人間承認へ誘導する。

縮小判定は次の単調条件を全て満たす必要がある。

* `action_types`は集合の部分集合、`path_scope`は完全一致またはprefix包含の部分集合
* `workspace_id`、Provider、Account、Tenant、Audienceは同一または狭い集合
* `max_changed_files`、`max_added_lines`、`max_deleted_lines`、課金上限は増加不可
* `max_risk_level`は同一または低い値（`MEDIUM`から`LOW`のみ）
* `expires_at`、`max_uses`、`reconfirm_after_uses`は増加不可
* `expected_effect`、Network／出力先／Masking Identityの範囲は拡大不可

強制再確認は`expires_at`（既定30日）と`reconfirm_after_uses`（既定20回）の早い方。

## 9. Phase配分

| Phase | 範囲 |
|---|---|
| **MVP0-A** | Schema・Store・宣言型Predicate評価器・非委任床Registry・CLI・失効競合の最終Gate検証。委任対象は**読取り専用Action限定**（`TASK_LOAD`, `CONTEXT_BUILD`, `MOCK_INFERENCE`, `PROPOSED_ARTIFACT_VALIDATE`） |
| **MVP1-A** | `LOCAL_FILE_COMMIT`への委任解禁 |
| 参照仕様 | 有償・外部送信は§9.7のEnterprise SoD Gate合格まで解禁しない |

## 10. 検討したが採らなかった案

| 案 | 不採用理由 |
|---|---|
| 前回と同じPlan Hashなら自動承認 | §3.8.5が禁止。誤った1回が恒久的許可になる |
| 承認画面の「今後聞かない」チェックボックス | 委任範囲が不可視のまま成立。実質`--yes` |
| 信頼スコアの累積で自動化率を上げる | 境界が説明不能。監査でどの範囲が自動か答えられない |
| Grant編集による範囲拡大 | 承認済みPredicateの意味が事後に変わる |
| DelegationGrantを直接実行権限として使う | 一回限り性が失われReplay防止が壊れる（条件4） |
| Approval時点のみの失効検証 | 進行中Effectを止められない（条件6） |

## 11. 未解決事項

* `max_added_lines`等の既定値（実測後）
* Predicate評価器の配置。Plan決定性§1.11.1へ影響しない位置であること
