<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

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
