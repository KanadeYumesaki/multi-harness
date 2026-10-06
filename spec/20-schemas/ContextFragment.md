<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 10. ContextFragment

必須Field：

`fragment_id, source_artifact_hash, source_span, fragment_type, message_role, control_authority, instruction_eligible, input_read_capability_id, source_file_identity, classification_scan_evidence_hash, content_artifact_hash, estimated_tokens, classification_labels, trust_level, priority, mandatory, freshness, deduplication_key`

制約：

* Content本文はArtifact参照。
* Mandatory FragmentはSelectionから除外不可。
* Classificationは§1.13。
* `UNTRUSTED_ARTIFACT_DATA`または`UNTRUSTED_PROVIDER_DATA`は`instruction_eligible=false`。
* `SYSTEM_CONTROL`／`DEVELOPER_CONTROL`は検証済み`control_authority`必須。

規範Fixture断片：`{"fragment_type":"POLICY","mandatory":true}`
拒否例：Source Artifactなし。
Upcaster：Classification変換は自動Downgrade禁止。
