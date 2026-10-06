<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 8. TokenBudgetPolicy

必須Field：

`policy_id, maximum_input_tokens, reserved_output_tokens, reserved_tool_tokens, safety_margin_tokens, maximum_context_tokens, overflow_policy, required_fragment_types, compression_depth_limit, retry_reservation, policy_hash`

制約：

* 各値は0以上の整数。
* 合計がProvider Context Limit以下。
* Required Fragmentを除外不可。
* `overflow_policy=FAIL_CLOSED`を初期値。

規範Fixture断片：`{"maximum_input_tokens":4096,"reserved_output_tokens":1024}`
拒否例：負数、上限超過。
Upcaster：計算意味変更はMajor。
