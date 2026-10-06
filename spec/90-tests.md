<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

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
