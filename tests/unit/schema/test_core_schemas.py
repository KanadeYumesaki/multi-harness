"""Core Schema Catalog と `schema_set_hash` の試験。

`AT-SCHEMA-COMPLETE-001`（全Schema存在・形式）と
`AT-SCHEMA-CONDITIONAL-001`（State依存規則）の基盤にあたる。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain.errors import HarnessError
from harness.domain.hashing import ContentHash
from harness.domain.schema_set import SchemaRef, compute_schema_set_hash
from harness.infrastructure.schema.registry import CoreSchemaRegistry

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "support"))
from case_probe import observe_case, observe_unit_case

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def registry() -> CoreSchemaRegistry:
    return CoreSchemaRegistry(REPO_ROOT)


@pytest.fixture(scope="module")
def catalog() -> list[dict[str, Any]]:
    """`(schema_name, schema_version)`単位へ展開したCatalog（§15.9）。

    Registry形式の解釈は生成器の正本（`tools/schema_catalog.py`）へ委ねる。
    """
    from schema_catalog import normalize_core_schemas

    path = REPO_ROOT / "design-source" / "registries" / "schemas.yaml"
    rows: list[dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8"))["core_schemas"]
    return normalize_core_schemas(rows)


def _envelope(name: str) -> dict[str, Any]:
    return {
        "schema_name": name,
        "schema_version": "1.0.0",
        "record_id": "018f0000-0000-7000-8000-000000000001",
        "created_at": "2026-08-07T00:00:00Z",
        "producer": "harness-core/1.8.0",
        "content_hash": "sha256:" + "1" * 64,
    }


# --------------------------------------------------------------------------
# AT-SCHEMA-COMPLETE-001 の基盤
# --------------------------------------------------------------------------


def test_every_registered_schema_file_exists(
    registry: CoreSchemaRegistry, catalog: list[dict[str, Any]]
) -> None:
    """件数は本Fileへもコードへも書かない。schemas.yamlから導出する。"""
    assert len(registry.refs) == len(catalog)
    assert set(registry.schema_names) == {row["schema_name"] for row in catalog}


def test_every_schema_forbids_additional_properties(registry: CoreSchemaRegistry) -> None:
    """§15 Schema Delivery Gate「`additionalProperties=false`」。"""
    for name in registry.schema_names:
        result = registry.validate(name, {**_envelope(name), "unexpected_field": 1})
        assert not result.accepted, f"{name} accepted an unknown field"


def test_every_schema_requires_the_common_envelope(registry: CoreSchemaRegistry) -> None:
    for name in registry.schema_names:
        result = registry.validate(name, {})
        assert not result.accepted
        joined = " ".join(result.errors)
        assert "record_id" in joined or "schema_name" in joined


def test_unknown_schema_name_is_rejected(registry: CoreSchemaRegistry) -> None:
    with pytest.raises(HarnessError, match="unknown core schema"):
        registry.validate("NotASchema", {})


def _active_write_version(schema_name: str) -> str:
    """Registry から Active Write Version を取る。試験へ書かない。"""
    from schema_catalog import normalize_core_schemas

    path = REPO_ROOT / "design-source" / "registries" / "schemas.yaml"
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["core_schemas"]
    for entry in normalize_core_schemas(rows):
        if entry["schema_name"] == schema_name and entry["active_write"]:
            return str(entry["schema_version"])
    raise AssertionError(f"{schema_name} の active_write Version が無い")


def _observe_conditional(
    observation: Any,
    case_id: str,
    registry: CoreSchemaRegistry,
    schema_name: str,
    record: dict[str, Any],
    subject_id: str,
) -> None:
    """State と Error Code を、それぞれの Producer から観測する。

    State は `validate`、Error Code は保存境界の `validate_or_raise` が出す。
    **同じ Error Code を別の理由で得ていないこと**まで確かめる。
    `validate_or_raise` は未登録Schema・読取り専用Version でも同じ Code を
    投げるため、Active Write Version を渡し、理由が Record 不適合であることを
    メッセージで確認する。
    """
    result = registry.validate(schema_name, record)
    version = _active_write_version(schema_name)

    error_code = None
    with pytest.raises(HarnessError) as raised:
        registry.validate_or_raise(schema_name, version, record)
    error_code = raised.value.code.value
    assert "validation failed" in str(raised.value), (
        f"{case_id}: Error Code は出たが理由が Record 不適合ではない: {raised.value}"
    )

    # Schema 検証は Ledger へ何もAppendしない。**それでも観測する。**
    observe_case(
        observation,
        case_id,
        state=result.state,
        subject_id=subject_id,
        error_code=error_code,
        payload={
            "schema_name": schema_name,
            "schema_version": version,
            "validation_error_count": len(result.errors),
            "producer_module": "harness.infrastructure.schema.registry",
            "producer_symbol": "CoreSchemaRegistry.validate_or_raise",
        },
    )


def _artifact_manifest(**overrides: Any) -> dict[str, Any]:
    """必須項目を満たした `ArtifactManifest`。

    雛形は必須項目を欠いており、**Hash を壊さなくても落ちる**。それでは
    「Hash 書式で落ちた」ことを確かめられない。ここで通る記録を作り、
    Hash だけを壊して差分を見る。
    """
    record = {
        **_envelope("ArtifactManifest"),
        "artifact_id": "artifact-1",
        "content_hash_value": "sha256:" + "2" * 64,
        "media_type": "text/plain",
        "size_bytes": 12,
        "data_classification": "INTERNAL",
        "trust_level": "UNTRUSTED",
        "stored_at": "2026-08-07T00:00:01Z",
        "storage_path": "artifacts/aa/artifact-1",
    }
    record.update(overrides)
    return record


def test_complete_artifact_manifest_is_accepted(registry: CoreSchemaRegistry) -> None:
    """壊していない記録が通ること。通らなければ次の試験は何も証明しない。"""
    result = registry.validate("ArtifactManifest", _artifact_manifest())
    assert result.accepted, result.errors


@pytest.mark.case("AT-SCHEMA-COMPLETE-001/INVALID_HASH")
@pytest.mark.unit_subject("SCHEMA_VALIDATION_RESULT", durability="NOT_APPLICABLE")
def test_hash_fields_reject_malformed_values(
    registry: CoreSchemaRegistry, case_observation: Any
) -> None:
    """Hash 書式**だけ**を壊し、そこで落ちることを確かめる。"""
    malformed = "not-a-hash"
    result = registry.validate("ArtifactManifest", _artifact_manifest(content_hash=malformed))

    assert not result.accepted
    assert result.state == "REJECTED"
    # 落ちた理由が Hash 書式1件だけであること。他の理由が混ざると
    # 「Hash で落ちた」と言えなくなる。
    assert len(result.errors) == 1, result.errors
    only = result.errors[0]
    assert only.startswith("content_hash: ")
    assert "does not match" in only
    assert "^sha256:[0-9a-f]{64}$" in only

    # Registry の語彙へ写す。Validator は Error Code を返さないので、
    # **観測した違反の種類**から決める。上の assert がその根拠である。
    observe_unit_case(
        case_observation,
        "AT-SCHEMA-COMPLETE-001/INVALID_HASH",
        state=result.state,
        subject_id="artifact-1",
        error_code="HASH_PATTERN_INVALID",
        payload={
            "schema_name": result.schema_name,
            "malformed_value": malformed,
            "validation_error_count": len(result.errors),
            "violated_keyword": "pattern",
            "violated_field": "content_hash",
            "invalid_fixture_rejected": not result.accepted,
        },
    )


# --------------------------------------------------------------------------
# AT-SCHEMA-CONDITIONAL-001 の 4 Case
# --------------------------------------------------------------------------


def _attempt(state: str, **extra: Any) -> dict[str, Any]:
    record = {
        **_envelope("ActionAttempt"),
        "attempt_id": "attempt-1",
        "action_id": "action-1",
        "attempt_number": 1,
        "state": state,
        "store_version": 1,
    }
    record.update(extra)
    return record


@pytest.mark.case("AT-SCHEMA-CONDITIONAL-001/READY_VALID")
def test_ready_without_lease_is_valid(registry: CoreSchemaRegistry, case_observation: Any) -> None:
    """`AT-SCHEMA-CONDITIONAL-001/READY_VALID` → ACCEPTED。"""
    record = _attempt(
        "READY",
        plan_content_hash="sha256:" + "2" * 64,
        execution_plan_hash="sha256:" + "3" * 64,
    )
    result = registry.validate("ActionAttempt", record)
    assert result.accepted, result.errors
    assert result.state == "ACCEPTED"

    # Schema 検証は Ledger へ何もAppendしない。**それでも観測する。**
    observe_case(
        case_observation,
        "AT-SCHEMA-CONDITIONAL-001/READY_VALID",
        state=result.state,
        subject_id="attempt-1",
        error_code=None,
        payload={
            "schema_name": result.schema_name,
            "attempt_state": "READY",
            "validation_error_count": len(result.errors),
        },
    )


@pytest.mark.case("AT-SCHEMA-CONDITIONAL-001/RUNNING_MISSING_ATTESTATION")
def test_running_missing_attestation_is_rejected(
    registry: CoreSchemaRegistry, case_observation: Any
) -> None:
    """`AT-SCHEMA-CONDITIONAL-001/RUNNING_MISSING_ATTESTATION` → REJECTED。"""
    record = _attempt(
        "RUNNING",
        plan_content_hash="sha256:" + "2" * 64,
        execution_plan_hash="sha256:" + "3" * 64,
        worker_id="w-1",
        claim_id="c-1",
        lease_id="l-1",
        fencing_token=1,
        started_at="2026-08-07T00:00:01Z",
        # runtime_attestation_hash を欠落させる
    )
    result = registry.validate("ActionAttempt", record)
    assert not result.accepted
    assert result.state == "REJECTED"
    assert any("runtime_attestation_hash" in error for error in result.errors)

    _observe_conditional(
        case_observation,
        "AT-SCHEMA-CONDITIONAL-001/RUNNING_MISSING_ATTESTATION",
        registry,
        "ActionAttempt",
        record,
        "attempt-1",
    )


@pytest.mark.case("AT-SCHEMA-CONDITIONAL-001/SUCCEEDED_MISSING_RECEIPT")
def test_succeeded_missing_receipt_is_rejected(
    registry: CoreSchemaRegistry, case_observation: Any
) -> None:
    """`AT-SCHEMA-CONDITIONAL-001/SUCCEEDED_MISSING_RECEIPT` → REJECTED。"""
    record = _attempt(
        "SUCCEEDED",
        plan_content_hash="sha256:" + "2" * 64,
        execution_plan_hash="sha256:" + "3" * 64,
        runtime_attestation_hash="sha256:" + "4" * 64,
        started_at="2026-08-07T00:00:01Z",
        ended_at="2026-08-07T00:00:02Z",
        # receipt_ids を欠落させる
    )
    result = registry.validate("ActionAttempt", record)
    assert not result.accepted
    assert any("receipt_ids" in error for error in result.errors)

    _observe_conditional(
        case_observation,
        "AT-SCHEMA-CONDITIONAL-001/SUCCEEDED_MISSING_RECEIPT",
        registry,
        "ActionAttempt",
        record,
        "attempt-1",
    )


@pytest.mark.case("AT-SCHEMA-CONDITIONAL-001/CONSUMED_MISSING_ATTEMPT")
def test_approval_consumed_missing_attempt_is_rejected(
    registry: CoreSchemaRegistry, case_observation: Any
) -> None:
    """`AT-SCHEMA-CONDITIONAL-001/CONSUMED_MISSING_ATTEMPT` → REJECTED。"""
    record = {
        **_envelope("ApprovalGrant"),
        "grant_id": "g-1",
        "plan_content_hash": "sha256:" + "2" * 64,
        "execution_plan_hash": "sha256:" + "3" * 64,
        "action_scope": ["action-1"],
        "approver_subject_id": "operator:local",
        "approver_tenant_id": "tenant:local",
        "authentication_context_class": "urn:harness:os-login",
        "mfa_performed": False,
        "authentication_time": "2026-08-07T00:00:00Z",
        "issued_at": "2026-08-07T00:00:00Z",
        "not_before": "2026-08-07T00:00:00Z",
        "expires_at": "2026-08-07T00:10:00Z",
        "maximum_clock_skew_seconds": 30,
        "nonce": "0123456789abcdef",
        "use_count": 1,
        "revocation_epoch": 1,
        "issuer_id": "issuer-1",
        "issuer_key_id": "key-1",
        "signature_algorithm": "ed25519",
        "signature": "AAAA",
        "status": "CONSUMED",
        "store_version": 1,
        "consumed_at": "2026-08-07T00:00:05Z",
        "consumed_by_actor_id": "actor-1",
        # attempt_id を欠落させる
    }
    result = registry.validate("ApprovalGrant", record)
    assert not result.accepted
    assert any("attempt_id" in error for error in result.errors)

    _observe_conditional(
        case_observation,
        "AT-SCHEMA-CONDITIONAL-001/CONSUMED_MISSING_ATTEMPT",
        registry,
        "ApprovalGrant",
        record,
        "g-1",
    )


def test_plan_hash_pair_must_be_both_or_neither(registry: CoreSchemaRegistry) -> None:
    """§15.9「`plan_content_hash`と`execution_plan_hash`は片方だけ存在してはならない」。"""
    only_one = _attempt("WAITING_POLICY", plan_content_hash="sha256:" + "2" * 64)
    assert not registry.validate("ActionAttempt", only_one).accepted


def test_delegated_approval_requires_delegation_id(registry: CoreSchemaRegistry) -> None:
    """ADR-006。`POLICY_DELEGATED` は `delegation_id` 必須。"""
    base = {
        **_envelope("ApprovalGrant"),
        "grant_id": "g-1",
        "plan_content_hash": "sha256:" + "2" * 64,
        "execution_plan_hash": "sha256:" + "3" * 64,
        "action_scope": ["action-1"],
        "approver_subject_id": "operator:local",
        "approver_tenant_id": "tenant:local",
        "authentication_context_class": "urn:harness:os-login",
        "mfa_performed": False,
        "authentication_time": "2026-08-07T00:00:00Z",
        "issued_at": "2026-08-07T00:00:00Z",
        "not_before": "2026-08-07T00:00:00Z",
        "expires_at": "2026-08-07T00:10:00Z",
        "maximum_clock_skew_seconds": 30,
        "nonce": "0123456789abcdef",
        "use_count": 1,
        "revocation_epoch": 1,
        "issuer_id": "issuer-1",
        "issuer_key_id": "key-1",
        "signature_algorithm": "ed25519",
        "signature": "AAAA",
        "status": "ISSUED",
        "store_version": 1,
        "approval_mode": "POLICY_DELEGATED",
    }
    assert not registry.validate("ApprovalGrant", base).accepted
    assert registry.validate("ApprovalGrant", {**base, "delegation_id": "delegation-1"}).accepted


# --------------------------------------------------------------------------
# §1.11 schema_set_hash
# --------------------------------------------------------------------------


def _ref(name: str, digit: str) -> SchemaRef:
    return SchemaRef(name, "1.0.0", ContentHash.parse("sha256:" + digit * 64))


def test_schema_set_hash_is_order_independent() -> None:
    """§1.11「Filesystem列挙順へ依存するPlan生成を禁止する」。"""
    a = [_ref("Run", "1"), _ref("ActionAttempt", "2"), _ref("Lease", "3")]
    assert compute_schema_set_hash(a) == compute_schema_set_hash(list(reversed(a)))


def test_schema_set_hash_changes_when_any_schema_changes() -> None:
    base = [_ref("Run", "1"), _ref("Lease", "2")]
    changed = [_ref("Run", "1"), _ref("Lease", "9")]
    assert compute_schema_set_hash(base) != compute_schema_set_hash(changed)


def test_schema_set_hash_changes_when_a_schema_is_removed() -> None:
    base = [_ref("Run", "1"), _ref("Lease", "2")]
    assert compute_schema_set_hash(base) != compute_schema_set_hash(base[:1])


def test_duplicate_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate schema"):
        compute_schema_set_hash([_ref("Run", "1"), _ref("Run", "2")])


def test_empty_schema_set_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        compute_schema_set_hash([])


def test_registry_schema_set_hash_is_stable(registry: CoreSchemaRegistry) -> None:
    first = registry.schema_set_hash()
    second = CoreSchemaRegistry(REPO_ROOT).schema_set_hash()
    assert first == second
    assert first.startswith("sha256:")
