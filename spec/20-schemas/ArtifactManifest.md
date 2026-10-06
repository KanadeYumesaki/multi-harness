<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 17. ArtifactManifest

必須Field：

`artifact_id, content_hash, byte_size, media_type, encoding, classification_labels, trust_level, producer, source_artifact_ids, encryption_key_reference, created_at, retention_policy, legal_hold, compression, verification_status`

制約：

* PutはContent Hashで冪等。
* Restricted以上は暗号化Reference必須。
* Provider Raw OutputとNormalized OutputのArtifact Typeを分離。

規範Fixture断片：`{"media_type":"application/json","verification_status":"HASH_VERIFIED"}`
拒否例：Restrictedなのに暗号化なし。
Upcaster：Content Hashを変換しない。
