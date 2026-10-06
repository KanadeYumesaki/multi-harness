<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 6. RuntimeEnvelopeSpec

必須Field：

`runtime_spec_id, runtime_type, launcher_version, executable_path, executable_sha256, argv, working_directory_identity, workspace_identity, os_boundary, provider_id, adapter_version, model_id, model_digest, auth_route, credential_type, provider_account_id, tenant_id, billing_identity, credential_scope_hash, config_tree_hash, instruction_policy_hash, plugin_hook_mcp_policy_hash, environment_allowlist_hash, filesystem_policy_hash, network_policy_hash, tool_policy_hash, process_policy_hash, output_schema_hash, resource_limits, sandbox_spec, runtime_envelope_spec_hash`

制約：

* `argv`は文字列配列。Shell文字列禁止。
* Executableは絶対Path＋SHA-256。
* Sandbox必須Actionでは`required=true`。
* Secret値禁止。

規範Fixture断片：`{"argv":["mock-provider","--input","/run/input.json"]}`
拒否例：`{"argv":"mock-provider < input"}`
Upcaster：実行意味に影響する追加はMajor。
