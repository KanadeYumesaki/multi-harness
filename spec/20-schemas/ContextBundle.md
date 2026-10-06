<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 11. ContextBundle

必須Field：

`context_bundle_id, selected_fragment_ids, ordered_fragment_ids, excluded_fragment_ids, total_estimated_tokens, input_read_capability_set_hash, input_read_evidence_hash, message_role_manifest_hash, token_profile_snapshot_hash, token_budget_policy_hash, compression_artifact_ids, selection_receipt_id, bundle_hash`

制約：

* Ordered IDsはSelected IDsと同集合。
* Token不変条件を満たす。
* Mandatory Fragmentを全て含む。

規範Fixture断片：`{"selected_fragment_ids":["f1"],"ordered_fragment_ids":["f1"]}`
拒否例：Mandatory欠落。
Upcaster：順序意味変更はMajor。
