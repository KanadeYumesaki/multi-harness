<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 12. ContextSelectionReceipt

必須Field：

`selection_receipt_id, candidate_fragment_ids, selected_fragment_ids, excluded_fragments, rejected_input_resources, input_read_capability_set_hash, input_read_evidence_hash, deduplication_result, compression_result, estimated_token_total, token_profile_snapshot_hash, budget_policy_hash, algorithm_version, decision_hash`

制約：

* 各Excluded FragmentにReason Code必須。
* CandidateはSelected＋Excludedで完全に説明。
* Algorithm VersionとTie-breakerを記録。
* 各`rejected_input_resources`にCapability ID、要求Path、Reason Code、File／Mount Identity Evidenceを記録し、Bytes本文は保存しない。

規範Fixture断片：`{"excluded_fragments":[{"fragment_id":"f2","reason":"BUDGET"}]}`
拒否例：理由なし除外。
Upcaster：Algorithm差は再Selection。
