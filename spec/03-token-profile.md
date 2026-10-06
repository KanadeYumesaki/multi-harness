<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

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
