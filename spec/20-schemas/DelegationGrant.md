<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 21. DelegationGrant

`DelegationGrant`は、委任可能な承認範囲を署名・Audience・Predicate・Policy Snapshotへ束縛するCore Schemaである。MVP0-Aでは未実装だが、Registryが定義する22件のSchema Catalogから除外してはならない。

共通必須Field：

`delegation_grant_id, issuer_identity, delegate_identity, audience, scope_predicate, policy_snapshot_hash, issued_at, expires_at, signature, trust_anchor_id, delegation_hash`

制約：

* `audience`はDelegate IdentityおよびRuntime Audienceと一致しなければならない。
* `scope_predicate`は許可対象を拡張できず、破壊的Actionを委任してはならない。
* `policy_snapshot_hash`とTrust Anchorは発行時の値へ固定し、変更後の自動継続を許さない。
* RevocationはEffectの線形化点より前後を区別し、監査TrailをAppend-onlyで残す。
* 署名、Audience、Scope、Trust Anchorの不一致はFail-Closedで拒否する。

拒否Fixture：

* Audience不一致。
* Scopeを発行後に拡張。
* 失効済みTrust Anchor。
* Revocation後の新規Effect。

Upcaster：Audience、Scope Predicate、Trust Anchor、署名方式の意味変更はMajor。
