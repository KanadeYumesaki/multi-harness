<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 15. Lease

必須Field：

`lease_id, resource_key, holder_id, attempt_id, fencing_token, issued_at, expires_at, renewed_at, status, store_version`

制約：

* ResourceごとにFencing Token単調増加。
* Stale Token更新拒否。
* Holder／Attempt変更不可。

規範Fixture断片：`{"fencing_token":42,"status":"ACTIVE"}`
拒否例：Token 41で更新。
Upcaster：Fencing意味変更不可。
