# チャット機能の安全境界

**安全制御を緩めない。** 本書は既存の制御とチャット機能の接続点を並べたものである。

## 動かしてはならない既存制御

| 制御 | 実体 | チャットでの扱い |
|---|---|---|
| Masking Pipeline（ADR-007） | `src/harness/infrastructure/masking/` | 外部 Provider へ渡す本文は必ず通す |
| Scan#2 | `MaskingPipeline` | 省略しない。マスク結果が新たな一致を作る場合を捕まえる |
| Secret / Credential / `NATIONAL_ID` / `SPECIAL_CATEGORY_DATA` | `masking-policy.yaml` | マスクせず Reject。LLM へ渡さない |
| Input Read の Policy 判定 | `InputReadOrchestrator` | 会話入力も同じ経路を通す |
| Event Ledger の Append-only | `EventLedgerPort` + SQLite Trigger | 履歴訂正は Compensating Event だけ |
| Approval / Effect 境界 | `ApprovalPlanOrchestrator` | チャットから Effect を起こさない |
| Delegation Floor | `delegation-floor.yaml` | 委任による委任を作らない |

## Provider 接続の現行境界

`ProviderPort` の docstring は次のように書いている。

> MVP0-Aでは `provider_id=mock` だけを許可し、実Network・認証・請求を伴わない。

`MockProvider.propose` は `provider_id != "mock"` を `RUNTIME_SPEC_MISMATCH` で拒否する。
**これは文書上の約束ではなく、Production で効いている拒否である。**

### 外部 LLM 接続を許可する最小境界

実 Provider へ繋ぐには、少なくとも次が要る。**いずれも未定である。**

1. `provider_id` の許可集合をどこが持つか（現在は Adapter 内の直書き）
2. 契約状態（契約済み / 未契約 / 停止）を表す語彙
3. Route Policy（`LOCAL_ONLY` / `EXTERNAL_ALLOWED` に相当するもの）
4. 外部送信の直前に Masking Gate を通したことの証跡
5. `network_used=true` を許す条件と、その記録先

**3 は Registry にも Production にも無い。** ロードマップ文書にしか書かれていない。
推測で足さず、Decision Package の設問にしてある。

## Provider 秘密情報

| 規則 | 根拠 |
|---|---|
| API Key を保存・表示・Log 出力しない | 不変条件#7（`SecretRef` 型だけを扱う） |
| Secret 値を Event / Attestation / 例外 / Evidence へ出さない | 同上 |
| 会話本文を Ledger / Event / Evidence へ直接入れない | ロードマップ §2.3 |

既存 Production は `SecretRef` を持つ。チャットの Provider 設定でも同じ型を使い、
値そのものを持たない。**保管方式（OS Keyring / 環境変数 / 暗号化 Artifact）は未定であり、
Decision Package の設問にしてある。**

## Fallback の可否

ロードマップ §3 L1 が定める区別を、そのまま境界にする。

| 事象 | Fallback |
|---|---|
| Timeout | 可（Policy が許す場合） |
| 一時的 Rate Limit | 可（同上） |
| 認証失敗 | **不可** |
| Policy 違反 | **不可** |
| 入力不備 | **不可** |
| Effect 実行中 | **不可**（Provider を差し替えない） |

`LOCAL_ONLY` 相当の会話を外部 Provider へ渡す場合は、切替前に Policy を再評価する。
**評価せずに渡さない。**

## UI が守ること

| 規則 | 理由 |
|---|---|
| 承認 UI が無い間は「承認済み」と表示しない | 不変条件#11。Approval Skip を作らない |
| `DEMO_ONLY` を明示する | Runtime GO と誤認させない |
| Provider 切替の理由を表示する | 切替が黙って起きたと見えないようにする |
| 安全制御で止まったことを表示する | Fail-Closed を「故障」と誤読させない |
| API Key を表示しない | 不変条件#7 |

**UI だけで承認済みとして扱わない。** 表示は判定ではない。
