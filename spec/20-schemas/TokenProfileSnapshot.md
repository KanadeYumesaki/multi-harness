<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

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
