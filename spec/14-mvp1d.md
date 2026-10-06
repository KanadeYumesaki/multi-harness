<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

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
