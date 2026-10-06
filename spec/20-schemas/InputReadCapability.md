<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 20. InputReadCapability

共通必須Field：

`capability_id, workspace_id, os_boundary, broker_or_reader_id, root_directory_identity, filesystem_identity, allowed_operations, allowed_relative_path_patterns, file_type_policy, mount_policy, symlink_policy, special_file_policy, maximum_bytes, maximum_files, maximum_depth, classification_policy_hash, secret_scan_policy_hash, issued_to_subject, issued_at, not_before, expires_at, nonce, revocation_epoch, issuer_id, issuer_key_id, signature_algorithm, signature, capability_hash`

制約：

* `allowed_operations`はMVP0-Aでは`READ | ENUMERATE`のみ。
* Root Directory Handle／Device／Inode／Mount IDをEvidenceへ束縛する。
* Absolute Path、Root外、Symlink、Magic Link、許可外Mount、特殊File、Virtual FS、Network FSを既定拒否。
* Capability ID、Nonce一意。Expiry／Revocation／署名検証必須。
* Read後にRoot／File Identityが変化した場合、Bytesを採用しない。
* CapabilityのScope拡張は新Capability発行と候補再収集を要求。

規範Fixture断片：`{"os_boundary":"LINUX","allowed_operations":["READ","ENUMERATE"],"symlink_policy":"DENY"}`
拒否例：`{"allowed_relative_path_patterns":["/**"],"symlink_policy":"FOLLOW"}`。
Upcaster：Path解決意味、Mount Policy、署名対象変更はMajor。
