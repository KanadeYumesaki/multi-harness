<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

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
