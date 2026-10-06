<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 13. PolicyDecision

必須Field：

`decision_id, policy_package_id, policy_package_hash, rule_set_id, rule_set_version, policy_freshness, lkg_age_seconds, input_hash, decision, reason_codes, human_reason, required_approval_type, required_evidence, expires_at, runtime_gate_expires_at, stale_action_disposition, override_allowed, created_at, decision_hash`

制約：

* Decision Enumは`ALLOW | DENY | REQUIRE_APPROVAL | REQUIRE_HUMAN_REVIEW`。
* Deny時はReason Code必須。
* Security不変条件はOverride不可。
* `STALE_TTL_EXCEEDED | REVOKED_OR_INVALID | UNKNOWN`では新規ActionのDecisionを`ALLOW`にできない。
* Runtime Gateは`runtime_gate_expires_at`超過後に§14.4.1を適用する。

規範Fixture断片：`{"decision":"REQUIRE_APPROVAL","reason_codes":["WRITE_EFFECT"],"policy_freshness":"CURRENT","stale_action_disposition":"NOT_APPLICABLE"}`
拒否例：ReasonなしDeny。
Upcaster：Rule Versionは変換せず当時値を保持。
