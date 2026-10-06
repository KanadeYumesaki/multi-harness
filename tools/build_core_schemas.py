#!/usr/bin/env python3
"""§15.9 Core Schema Catalog の JSON Schema を生成する。

生成先は `design-source/registries/schemas.yaml` の `path` 列に従う。
22 Schema が共通Envelope（`record_id`／`content_hash`／`producer` 等）を持つため、
各Fileへ手で写すと必ずずれる。共通部分を1箇所で定義して展開する。

## 生成物の性質

* `additionalProperties: false` を全Schemaへ付ける（§15 Schema Delivery Gate）
* Hash Fieldは `sha256:[0-9a-f]{64}` を強制する
* State依存の必須Fieldは `allOf` の `if/then` で表す
* 件数を本Script内へ手入力しない。対象はschemas.yamlから読む（不変条件#18）

## 状態依存規則の範囲

`AT-SCHEMA-CONDITIONAL-001` が検証する 3 Schema（`ActionAttempt`、`ApprovalGrant`、
`EffectReceipt`）は §15.9 の表を全て実装する。他Schemaは共通Envelopeと必須Field、
型・Hash形式までとし、状態依存規則は当該Componentの実装Taskで追加する。
Schema変更は`schema_set_hash`を変えるため、Approvalが無効化される正しい挙動になる。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

import yaml

# `schemas.yaml` の正規化は4 Tool共有の `tools/schema_catalog.py` が正本である
# （Step 3-b でTool内の複製を削除した）。Script実行時は sys.path[0] が tools/ に
# なるが、Testが importlib で読み込む経路では解決されないため明示的に足す。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema_catalog import logical_schema_count, normalize_core_schemas

__all__ = ["build_schema", "logical_schema_count", "main", "normalize_core_schemas"]

HASH_PATTERN: Final[str] = "^sha256:[0-9a-f]{64}$"
UUID_PATTERN: Final[str] = "^[0-9a-fA-F-]{36}$"
TIMESTAMP_PATTERN: Final[str] = "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"

# §1.3 共通識別子。全Recordが持つEnvelope。
ENVELOPE_REQUIRED: Final[tuple[str, ...]] = (
    "schema_name",
    "schema_version",
    "record_id",
    "created_at",
    "producer",
    "content_hash",
)


def _hash() -> dict[str, Any]:
    return {"type": "string", "pattern": HASH_PATTERN}


def _hash_or_null() -> dict[str, Any]:
    return {"type": ["string", "null"], "pattern": HASH_PATTERN}


def _id() -> dict[str, Any]:
    return {"type": "string", "minLength": 1}


def _ts() -> dict[str, Any]:
    return {"type": "string", "pattern": TIMESTAMP_PATTERN}


def _envelope(schema_name: str, schema_version: str) -> dict[str, Any]:
    return {
        "schema_name": {"const": schema_name},
        "schema_version": {"const": schema_version},
        "record_id": _id(),
        "run_id": _id(),
        "correlation_id": _id(),
        "causation_id": _id(),
        "sequence_number": {"type": "integer", "minimum": 1},
        "created_at": _ts(),
        "producer": {"type": "string", "minLength": 1},
        "content_hash": _hash(),
    }


def _string_enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _array_of(item: dict[str, Any], *, min_items: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": item, "minItems": min_items}


def _absent(*fields: str) -> dict[str, Any]:
    """該当Fieldが存在しないことを要求する（`未設定`の表現）。"""
    return {"not": {"anyOf": [{"required": [field]} for field in fields]}}


# ---------------------------------------------------------------------------
# 各Schemaの型別定義
# ---------------------------------------------------------------------------

_ACTION_ATTEMPT_STATES = (
    "PLANNING",
    "WAITING_POLICY",
    "WAITING_APPROVAL",
    "READY",
    "CLAIMED",
    "LEASED",
    "RUNTIME_VERIFIED",
    "RUNNING",
    "PREPARED_DURABLE",
    "EFFECT_IN_FLIGHT",
    "EFFECT_VERIFIED",
    "RECEIPT_DURABLE",
    "SUCCEEDED",
    "FAILED_RETRYABLE",
    "FAILED_PERMANENT",
    "BLOCKED_POLICY",
    "BLOCKED_APPROVAL",
    "BLOCKED_CONFLICT",
    "CANCELLED",
    "CANCEL_UNKNOWN",
    "EFFECT_UNKNOWN",
)

_PLAN_STATES = ("WAITING_POLICY", "WAITING_APPROVAL", "READY")
_TERMINAL_FAILURE_STATES = (
    "FAILED_RETRYABLE",
    "FAILED_PERMANENT",
    "BLOCKED_POLICY",
    "BLOCKED_APPROVAL",
    "BLOCKED_CONFLICT",
    "CANCELLED",
    "CANCEL_UNKNOWN",
    "EFFECT_UNKNOWN",
)


def _action_attempt() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """§15.9 4. ActionAttempt。State別必須Fieldを`if/then`で強制する。"""
    properties = {
        "attempt_id": _id(),
        "action_id": _id(),
        "attempt_number": {"type": "integer", "minimum": 1},
        "state": _string_enum(*_ACTION_ATTEMPT_STATES),
        "store_version": {"type": "integer", "minimum": 1},
        "plan_content_hash": _hash(),
        "execution_plan_hash": _hash(),
        "worker_id": _id(),
        "claim_id": _id(),
        "lease_id": _id(),
        "fencing_token": {"type": "integer", "minimum": 0},
        "runtime_attestation_hash": _hash(),
        "operation_journal_id": _id(),
        "started_at": _ts(),
        "ended_at": _ts(),
        "receipt_ids": _array_of(_id()),
        "error_classification": {"type": "string"},
    }
    required = ["attempt_id", "action_id", "attempt_number", "state", "store_version"]

    def when(states: tuple[str, ...], then: dict[str, Any]) -> dict[str, Any]:
        return {
            "if": {"properties": {"state": {"enum": list(states)}}, "required": ["state"]},
            "then": then,
        }

    conditionals = [
        # PLANNING: Plan Hash・Worker・Lease・Runtime・時刻・Receiptは未設定
        when(
            ("PLANNING",),
            _absent(
                "plan_content_hash",
                "execution_plan_hash",
                "worker_id",
                "claim_id",
                "lease_id",
                "fencing_token",
                "runtime_attestation_hash",
                "started_at",
                "ended_at",
                "receipt_ids",
            ),
        ),
        when(_PLAN_STATES, {"required": ["plan_content_hash", "execution_plan_hash"]}),
        when(
            ("CLAIMED",),
            {"required": ["plan_content_hash", "execution_plan_hash", "worker_id", "claim_id"]},
        ),
        when(
            ("LEASED",),
            {
                "required": [
                    "plan_content_hash",
                    "execution_plan_hash",
                    "worker_id",
                    "claim_id",
                    "lease_id",
                    "fencing_token",
                ]
            },
        ),
        when(
            ("RUNTIME_VERIFIED",),
            {
                "required": [
                    "plan_content_hash",
                    "execution_plan_hash",
                    "worker_id",
                    "claim_id",
                    "lease_id",
                    "fencing_token",
                    "runtime_attestation_hash",
                ]
            },
        ),
        # RUNNING は RUNTIME_VERIFIED の必須 + started_at。
        # AT-SCHEMA-CONDITIONAL-001/RUNNING_MISSING_ATTESTATION が
        # runtime_attestation_hash 欠落の拒否を要求する。
        when(
            ("RUNNING",),
            {
                "required": [
                    "plan_content_hash",
                    "execution_plan_hash",
                    "worker_id",
                    "claim_id",
                    "lease_id",
                    "fencing_token",
                    "runtime_attestation_hash",
                    "started_at",
                ]
            },
        ),
        when(
            ("PREPARED_DURABLE", "EFFECT_IN_FLIGHT", "EFFECT_VERIFIED"),
            {
                "required": [
                    "runtime_attestation_hash",
                    "started_at",
                    "fencing_token",
                    "operation_journal_id",
                ]
            },
        ),
        when(
            ("RECEIPT_DURABLE",),
            {
                "required": [
                    "runtime_attestation_hash",
                    "started_at",
                    "fencing_token",
                    "operation_journal_id",
                    "receipt_ids",
                ],
                "properties": {"receipt_ids": {"minItems": 1}},
            },
        ),
        # SUCCEEDED: AT-SCHEMA-CONDITIONAL-001/SUCCEEDED_MISSING_RECEIPT
        when(
            ("SUCCEEDED",),
            {
                "required": [
                    "plan_content_hash",
                    "execution_plan_hash",
                    "runtime_attestation_hash",
                    "started_at",
                    "ended_at",
                    "receipt_ids",
                ],
                "properties": {"receipt_ids": {"minItems": 1}},
            },
        ),
        when(_TERMINAL_FAILURE_STATES, {"required": ["ended_at", "error_classification"]}),
        # plan_content_hash と execution_plan_hash は片方だけ存在してはならない
        {
            "if": {"required": ["plan_content_hash"]},
            "then": {"required": ["execution_plan_hash"]},
        },
        {
            "if": {"required": ["execution_plan_hash"]},
            "then": {"required": ["plan_content_hash"]},
        },
    ]
    return properties, required, conditionals


def _approval_grant() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """§15.9 14. ApprovalGrant。Status別規則。"""
    properties = {
        "grant_id": _id(),
        "plan_content_hash": _hash(),
        "execution_plan_hash": _hash(),
        "action_scope": _array_of(_id(), min_items=1),
        "approver_subject_id": _id(),
        "approver_tenant_id": _id(),
        "authentication_context_class": {"type": "string"},
        "mfa_performed": {"type": "boolean"},
        "authentication_time": _ts(),
        "issued_at": _ts(),
        "not_before": _ts(),
        "expires_at": _ts(),
        "maximum_clock_skew_seconds": {"type": "integer", "minimum": 0},
        "nonce": {"type": "string", "minLength": 8},
        "use_count": {"type": "integer", "const": 1},
        "revocation_epoch": {"type": "integer", "minimum": 0},
        "issuer_id": _id(),
        "issuer_key_id": _id(),
        "signature_algorithm": {"type": "string"},
        "signature": {"type": "string", "minLength": 1},
        "status": _string_enum(
            "NOT_REQUIRED",
            "REQUESTED",
            "ISSUED",
            "CONSUMED",
            "EXPIRED",
            "REVOKED",
            "INVALIDATED",
            "REPLAY_DENIED",
        ),
        "store_version": {"type": "integer", "minimum": 1},
        # ADR-006。委任由来のApprovalを識別する。
        "approval_mode": _string_enum("HUMAN", "POLICY_DELEGATED"),
        "delegation_id": _id(),
        "consumed_at": _ts(),
        "consumed_by_actor_id": _id(),
        "attempt_id": _id(),
        "revoked_at": _ts(),
        "revoked_by_actor_id": _id(),
        "revocation_reason": {"type": "string"},
        "invalidated_at": _ts(),
        "invalidation_reason": {"type": "string"},
    }
    required = [
        "grant_id",
        "plan_content_hash",
        "execution_plan_hash",
        "action_scope",
        "approver_subject_id",
        "approver_tenant_id",
        "authentication_context_class",
        "mfa_performed",
        "authentication_time",
        "issued_at",
        "not_before",
        "expires_at",
        "maximum_clock_skew_seconds",
        "nonce",
        "use_count",
        "revocation_epoch",
        "issuer_id",
        "issuer_key_id",
        "signature_algorithm",
        "signature",
        "status",
        "store_version",
    ]

    def when_status(status: str, then: dict[str, Any]) -> dict[str, Any]:
        return {
            "if": {"properties": {"status": {"const": status}}, "required": ["status"]},
            "then": then,
        }

    conditionals = [
        when_status(
            "ISSUED",
            _absent(
                "consumed_at", "consumed_by_actor_id", "attempt_id", "revoked_at", "invalidated_at"
            ),
        ),
        # AT-SCHEMA-CONDITIONAL-001/CONSUMED_MISSING_ATTEMPT
        when_status(
            "CONSUMED", {"required": ["consumed_at", "consumed_by_actor_id", "attempt_id"]}
        ),
        when_status(
            "REVOKED",
            {
                "required": ["revoked_at", "revoked_by_actor_id", "revocation_reason"],
                **_absent("consumed_at", "consumed_by_actor_id"),
            },
        ),
        when_status("INVALIDATED", {"required": ["invalidated_at", "invalidation_reason"]}),
        when_status("EXPIRED", _absent("consumed_at", "consumed_by_actor_id")),
        # ADR-006: POLICY_DELEGATED は delegation_id 必須、HUMAN は禁止
        {
            "if": {
                "properties": {"approval_mode": {"const": "POLICY_DELEGATED"}},
                "required": ["approval_mode"],
            },
            "then": {"required": ["delegation_id"]},
        },
        {
            "if": {
                "properties": {"approval_mode": {"const": "HUMAN"}},
                "required": ["approval_mode"],
            },
            "then": _absent("delegation_id"),
        },
    ]
    return properties, required, conditionals


def _effect_receipt() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """§15.9 18. EffectReceipt。effect_type別の必須参照。"""
    properties = {
        "receipt_id": _id(),
        "effect_id": _id(),
        "operation_journal_id": _id(),
        "action_id": _id(),
        "attempt_id": _id(),
        "effect_type": _string_enum(
            "WORKSPACE_WRITE", "REMOTE_INVOCATION", "EXTERNAL_DISPATCH", "BUDGET_RESERVATION"
        ),
        "effect_subject_type": {"type": "string"},
        "effect_subject_id": _id(),
        "target_resource_identity": {"type": "string", "minLength": 1},
        "before_hash": _hash(),
        "expected_after_hash": _hash(),
        "observed_hash": _hash(),
        "observation_method": {"type": "string"},
        "confirmation_level": {"type": "string"},
        "fencing_token": {"type": "integer", "minimum": 0},
        "prepared_event_id": _id(),
        "execution_attempted_event_id": _id(),
        "observed_at": _ts(),
        "durability_level": {"type": "string"},
        "receipt_hash": _hash(),
        "remote_invocation_registry_id": _id(),
        "outbox_id": _id(),
        "budget_reservation_id": _id(),
        "remote_object_reference_hash": _hash(),
        "provider_receipt_hash": _hash(),
        "usage_evidence_hash": _hash(),
    }
    required = [
        "receipt_id",
        "effect_id",
        "operation_journal_id",
        "action_id",
        "attempt_id",
        "effect_type",
        "effect_subject_type",
        "effect_subject_id",
        "target_resource_identity",
        "before_hash",
        "expected_after_hash",
        "observed_hash",
        "observation_method",
        "confirmation_level",
        "fencing_token",
        "prepared_event_id",
        "execution_attempted_event_id",
        "observed_at",
        "durability_level",
    ]

    def when_type(effect_type: str, field: str) -> dict[str, Any]:
        return {
            "if": {
                "properties": {"effect_type": {"const": effect_type}},
                "required": ["effect_type"],
            },
            "then": {"required": [field]},
        }

    conditionals = [
        when_type("REMOTE_INVOCATION", "remote_invocation_registry_id"),
        when_type("EXTERNAL_DISPATCH", "outbox_id"),
        when_type("BUDGET_RESERVATION", "budget_reservation_id"),
    ]
    return properties, required, conditionals


# 共通Envelopeと必須Fieldだけを持つSchema。状態依存規則は各Component実装時に追加する。
# 設計書 §1.16.4 のControl／Data Message Role。`ContextFragment` の `message_role`
# と同じ語彙である。**Chat用に別のroleを作らない。** 一致は試験が見張る。
_MESSAGE_ROLES: Final[tuple[str, ...]] = (
    "SYSTEM_CONTROL",
    "DEVELOPER_CONTROL",
    "USER_TASK",
    "TOOL_DEFINITION",
    "VERIFIED_REFERENCE_DATA",
    "UNTRUSTED_ARTIFACT_DATA",
    "UNTRUSTED_PROVIDER_DATA",
)


_SIMPLE_SCHEMAS: Final[dict[str, tuple[dict[str, Any], list[str]]]] = {
    # -----------------------------------------------------------------
    # Chat 基盤（Owner Decision CC-1〜CC-16／設計書 §4.6）
    #
    # 親子は子→親の単方向である（CC-9-A）。Conversation は `message_ids` を
    # 持たない。持てばMessage追加のたびに親を書き換えることになり、
    # Append-only（CC-10-B）と両立しない。
    # -----------------------------------------------------------------
    "Conversation": (
        {
            "conversation_id": _id(),
            "conversation_hash": _hash(),
        },
        [
            "conversation_id",
            "conversation_hash",
        ],
    ),
    "ConversationMessage": (
        {
            "message_id": _id(),
            "conversation_id": _id(),
            "role": _string_enum(*_MESSAGE_ROLES),
        },
        [
            "message_id",
            "conversation_id",
            "role",
            # 順序キー。Envelope が持つ §1.3 の `sequence_number` をそのまま使う。
            # 採番IDと時刻は順序キーにできない（不変条件#4）。
            "sequence_number",
        ],
    ),
    "ConversationSnapshot": (
        {
            "snapshot_id": _id(),
            "conversation_id": _id(),
            "snapshot_hash": _hash(),
            "message_set_hash": _hash(),
            "schema_set_hash": _hash(),
            "design_sha256": _hash(),
        },
        [
            "snapshot_id",
            "conversation_id",
            "snapshot_hash",
            "message_set_hash",
            "schema_set_hash",
            "design_sha256",
        ],
    ),
    "EventEnvelope": (
        {
            "event_id": _id(),
            "event_type": {"type": "string", "minLength": 1},
            "stream_id": _id(),
            "payload_hash": _hash(),
            "previous_event_hash": _hash_or_null(),
            "event_hash": _hash(),
            "payload_schema_version": {"type": "string"},
            "recorded_at": _ts(),
        },
        [
            "event_id",
            "event_type",
            "stream_id",
            "payload_hash",
            "event_hash",
            "payload_schema_version",
            "recorded_at",
        ],
    ),
    "Run": (
        {
            "task_id": _id(),
            "state": _string_enum(
                "CREATED",
                "PLANNING",
                "WAITING_APPROVAL",
                "READY",
                "RUNNING",
                "RECOVERING",
                "WAITING_RELEASE",
                "COMPLETED",
                "BLOCKED",
                "BLOCKED_REPAIR_REQUIRED",
                "CANCELLING",
                "CANCELLED",
                "FAILED",
            ),
            "created_by": _id(),
            "unresolved_action_count": {"type": "integer", "minimum": 0},
            "risk_level": _string_enum("LOW", "MEDIUM", "HIGH", "CRITICAL"),
            "store_version": {"type": "integer", "minimum": 1},
            "ended_at": _ts(),
            "unreconciled_effect_carried_to_run_id": _id(),
        },
        [
            "task_id",
            "state",
            "created_by",
            "unresolved_action_count",
            "risk_level",
            "store_version",
        ],
    ),
    "ActionIntent": (
        {
            "action_id": _id(),
            "action_type": {"type": "string", "minLength": 1},
            "requested_capability": _array_of({"type": "string"}),
            "input_artifact_hashes": _array_of(_hash()),
            "requested_outputs": _array_of({"type": "string"}),
            "read_resources": _array_of({"type": "string"}),
            "write_resources": _array_of({"type": "string"}),
            "external_effect_resources": _array_of({"type": "string"}),
            "data_classification": {"type": "string"},
            "trust_level": {"type": "string"},
            "risk_level": _string_enum("LOW", "MEDIUM", "HIGH", "CRITICAL"),
            "maximum_attempts": {"type": "integer", "minimum": 1},
            "timeout_seconds": {"type": "integer", "minimum": 1},
        },
        [
            "action_id",
            "action_type",
            "data_classification",
            "trust_level",
            "risk_level",
            "maximum_attempts",
            "timeout_seconds",
        ],
    ),
    "ExecutionPlan": (
        {
            "execution_plan_id": _id(),
            "plan_version": {"type": "integer", "minimum": 1},
            "plan_content_hash": _hash(),
            "execution_plan_hash": _hash(),
            "planner_identity": {"type": "string"},
            "planner_algorithm_version": {"type": "string"},
            "authority_scope": {"type": "string"},
            "hash_profile_version": {"type": "integer", "const": 1},
            "issued_at": _ts(),
            "expires_at": _ts(),
            "schema_set_hash": _hash(),
            "policy_snapshot_hash": _hash(),
            "token_profile_hash": _hash(),
            "context_bundle_hash": _hash(),
        },
        [
            "execution_plan_id",
            "plan_version",
            "plan_content_hash",
            "execution_plan_hash",
            "planner_identity",
            "planner_algorithm_version",
            "authority_scope",
            "hash_profile_version",
            "issued_at",
            "expires_at",
            "schema_set_hash",
        ],
    ),
    "RuntimeEnvelopeSpec": (
        {
            "runtime_spec_version": {"type": "string"},
            "runtime_type": {"type": "string"},
            "launcher_version": {"type": "string"},
            "executable_path": {"type": "string"},
            "executable_sha256": _hash(),
            "argv": _array_of({"type": "string"}, min_items=1),
            "working_directory": {"type": "string"},
            "workspace_id": _id(),
            "os_boundary": _string_enum("LINUX"),
            "provider": {"type": "string"},
            "adapter": {"type": "string"},
            "model": {"type": "string"},
            "model_digest": {"type": "string"},
            "auth_route": {"type": "string"},
            "environment_allowlist_hash": _hash(),
            "output_schema_hash": _hash(),
            "semantic_hash": _hash(),
            "masker_provider": {"type": "string"},
            "masker_model": {"type": "string"},
            "masker_model_digest": {"type": "string"},
            "masker_instruction_hash": _hash(),
        },
        [
            "runtime_spec_version",
            "runtime_type",
            "launcher_version",
            "executable_path",
            "executable_sha256",
            "argv",
            "working_directory",
            "workspace_id",
            "os_boundary",
            "provider",
            "adapter",
            "auth_route",
            "semantic_hash",
        ],
    ),
    "InvocationManifest": (
        {
            "invocation_id": _id(),
            "invocation_mode": _string_enum("MOCK", "LOCAL", "EXTERNAL"),
            "provider_operation": {"type": "string"},
            "provider": {"type": "string"},
            "model": {"type": "string"},
            "billing_mode": _string_enum("FREE", "PAID", "UNKNOWN"),
            "request_artifact_hash": _hash(),
            "context_bundle_hash": _hash(),
            "instruction_hash": _hash(),
            "message_role_manifest_hash": _hash(),
            "control_data_policy_hash": _hash(),
            "output_schema_hash": _hash(),
            "token_profile_snapshot_hash": _hash(),
            "expected_effect": _string_enum(
                "NONE", "REMOTE_INVOCATION", "WORKSPACE_WRITE", "EXTERNAL_EFFECT"
            ),
            "semantic_hash": _hash(),
        },
        [
            "invocation_id",
            "invocation_mode",
            "provider",
            "billing_mode",
            "context_bundle_hash",
            "message_role_manifest_hash",
            "control_data_policy_hash",
            "token_profile_snapshot_hash",
            "expected_effect",
            "semantic_hash",
        ],
    ),
    "TokenBudgetPolicy": (
        {
            "policy_id": _id(),
            "policy_hash": _hash(),
            "reserved_output_tokens": {"type": "integer", "minimum": 0},
            "reserved_tool_tokens": {"type": "integer", "minimum": 0},
            "compression_max_depth": {"type": "integer", "minimum": 0, "maximum": 2},
        },
        [
            "policy_id",
            "policy_hash",
            "reserved_output_tokens",
            "reserved_tool_tokens",
            "compression_max_depth",
        ],
    ),
    "TokenProfileSnapshot": (
        {
            "snapshot_id": _id(),
            "provider": {"type": "string"},
            "model": {"type": "string"},
            "tokenizer_name": {"type": "string"},
            "tokenizer_version": {"type": "string"},
            "vocabulary_hash": _hash(),
            "context_limit": {"type": "integer", "minimum": 1},
            "maximum_output_limit": {"type": "integer", "minimum": 1},
            "estimate_assurance": _string_enum("EXACT", "CONSERVATIVE", "UNKNOWN"),
            "retrieved_at": _ts(),
            "expires_at": _ts(),
            "snapshot_hash": _hash(),
        },
        [
            "snapshot_id",
            "provider",
            "model",
            "tokenizer_name",
            "tokenizer_version",
            "context_limit",
            "maximum_output_limit",
            "estimate_assurance",
            "retrieved_at",
            "expires_at",
            "snapshot_hash",
        ],
    ),
    "ContextFragment": (
        {
            "fragment_id": _id(),
            "message_role": _string_enum(
                "SYSTEM_CONTROL",
                "DEVELOPER_CONTROL",
                "USER_TASK",
                "TOOL_DEFINITION",
                "VERIFIED_REFERENCE_DATA",
                "UNTRUSTED_ARTIFACT_DATA",
                "UNTRUSTED_PROVIDER_DATA",
            ),
            "control_authority": {"type": "boolean"},
            "instruction_eligible": {"type": "boolean"},
            "input_read_capability_id": _id(),
            "source_file_identity": {"type": "string"},
            "classification_scan_evidence_hash": _hash(),
            "fragment_content_hash": _hash(),
            "token_count": {"type": "integer", "minimum": 0},
            "masking_receipt_id": _id(),
        },
        [
            "fragment_id",
            "message_role",
            "control_authority",
            "instruction_eligible",
            "fragment_content_hash",
            "token_count",
        ],
    ),
    "ContextBundle": (
        {
            "bundle_id": _id(),
            "bundle_hash": _hash(),
            "fragment_ids": _array_of(_id()),
            "total_token_count": {"type": "integer", "minimum": 0},
            "message_role_manifest_hash": _hash(),
        },
        [
            "bundle_id",
            "bundle_hash",
            "fragment_ids",
            "total_token_count",
            "message_role_manifest_hash",
        ],
    ),
    "ContextSelectionReceipt": (
        {
            "receipt_id": _id(),
            "bundle_id": _id(),
            "selected_fragment_ids": _array_of(_id()),
            "excluded_fragment_ids": _array_of(_id()),
            "compressed_fragment_ids": _array_of(_id()),
            "rejected_input_resources": _array_of({"type": "object"}),
            "masking_receipt_ids": _array_of(_id()),
        },
        [
            "receipt_id",
            "bundle_id",
            "selected_fragment_ids",
            "excluded_fragment_ids",
            "rejected_input_resources",
        ],
    ),
    "PolicyDecision": (
        {
            "decision_id": _id(),
            "decision": _string_enum("ALLOW", "DENY"),
            "approval_required": {"type": "boolean"},
            "policy_snapshot_hash": _hash(),
            "policy_freshness": _string_enum(
                "CURRENT", "LKG_WITHIN_TTL", "STALE_TTL_EXCEEDED", "REVOKED_OR_INVALID", "UNKNOWN"
            ),
            "reason_code": {"type": "string"},
            "expires_at": _ts(),
        },
        [
            "decision_id",
            "decision",
            "approval_required",
            "policy_snapshot_hash",
            "policy_freshness",
            "expires_at",
        ],
    ),
    "Lease": (
        {
            "lease_id": _id(),
            "resource_key": {"type": "string", "minLength": 1},
            "holder_id": _id(),
            "fencing_token": {"type": "integer", "minimum": 0},
            "acquired_at": _ts(),
            "expires_at": _ts(),
            "released_at": _ts(),
        },
        ["lease_id", "resource_key", "holder_id", "fencing_token", "acquired_at", "expires_at"],
    ),
    "RuntimeAttestation": (
        {
            "attestation_id": _id(),
            "runtime_spec_semantic_hash": _hash(),
            "observed_executable_sha256": _hash(),
            "observed_argv": _array_of({"type": "string"}),
            "observed_working_directory": {"type": "string"},
            "mount_id": {"type": "string"},
            "filesystem_type": {"type": "string"},
            "fault_injection_enabled": {"type": "boolean"},
            "attestation_hash": _hash(),
            "plan_build_environment_hash": _hash(),
        },
        [
            "attestation_id",
            "runtime_spec_semantic_hash",
            "observed_executable_sha256",
            "observed_argv",
            "observed_working_directory",
            "fault_injection_enabled",
            "attestation_hash",
        ],
    ),
    "ArtifactManifest": (
        {
            "artifact_id": _id(),
            "content_hash_value": _hash(),
            "media_type": {"type": "string"},
            "size_bytes": {"type": "integer", "minimum": 0},
            "data_classification": {"type": "string"},
            "trust_level": {"type": "string"},
            "stored_at": _ts(),
            "storage_path": {"type": "string"},
        },
        [
            "artifact_id",
            "content_hash_value",
            "media_type",
            "size_bytes",
            "data_classification",
            "trust_level",
            "stored_at",
            "storage_path",
        ],
    ),
    "OperationJournal": (
        {
            "operation_journal_id": _id(),
            "effect_id": _id(),
            "attempt_id": _id(),
            "workspace_id": _id(),
            "target_relative_path": {"type": "string"},
            "filesystem_id": {"type": "string"},
            "mount_id": {"type": "string"},
            "parent_file_identity": {"type": "string"},
            "target_file_identity_before": {"type": "string"},
            "base_object_hash": _hash(),
            "expected_after_hash": _hash(),
            "temp_object_identity": {"type": "string"},
            "fencing_token": {"type": "integer", "minimum": 0},
            "durability_level": {"type": "string"},
            "state": {"type": "string"},
            "prepared_at": _ts(),
            "execution_attempted_at": _ts(),
            "observed_at": _ts(),
            "receipt_id": _id(),
            "store_version": {"type": "integer", "minimum": 1},
        },
        [
            "operation_journal_id",
            "effect_id",
            "attempt_id",
            "workspace_id",
            "target_relative_path",
            "base_object_hash",
            "expected_after_hash",
            "fencing_token",
            "durability_level",
            "state",
            "store_version",
        ],
    ),
    "InputReadCapability": (
        {
            "capability_id": _id(),
            "workspace_id": _id(),
            "root_directory_identity": {"type": "string"},
            "allowed_scope": _array_of({"type": "string"}),
            "policy_hash": _hash(),
            "nonce": {"type": "string", "minLength": 8},
            "issued_at": _ts(),
            "expires_at": _ts(),
            "revocation_epoch": {"type": "integer", "minimum": 0},
            "issuer_key_id": _id(),
            "signature": {"type": "string", "minLength": 1},
        },
        [
            "capability_id",
            "workspace_id",
            "root_directory_identity",
            "allowed_scope",
            "policy_hash",
            "nonce",
            "issued_at",
            "expires_at",
            "revocation_epoch",
            "issuer_key_id",
            "signature",
        ],
    ),
    # ---- ADR-006 / ADR-007 で追加 ----
    "DelegationGrant": (
        {
            "delegation_id": _id(),
            "delegation_schema_version": {"type": "string"},
            "predicate": {"type": "object"},
            "predicate_hash": _hash(),
            "created_from_run_id": _id(),
            "created_from_action_id": _id(),
            "approver_subject_id": _id(),
            "approver_tenant_id": _id(),
            "authentication_context_class": {"type": "string"},
            "issued_at": _ts(),
            "not_before": _ts(),
            "expires_at": _ts(),
            "max_uses": {"type": "integer", "minimum": 1},
            "use_count": {"type": "integer", "minimum": 0},
            "reconfirm_after_uses": {"type": "integer", "minimum": 1},
            "revocation_epoch": {"type": "integer", "minimum": 0},
            "status": _string_enum("ACTIVE", "EXPIRED", "REVOKED", "SUPERSEDED", "INVALIDATED"),
            "audience": {"type": "string", "minLength": 1},
            "issuer_id": _id(),
            "issuer_key_id": _id(),
            "signature": {"type": "string", "minLength": 1},
            "store_version": {"type": "integer", "minimum": 1},
        },
        [
            "delegation_id",
            "delegation_schema_version",
            "predicate",
            "predicate_hash",
            "created_from_run_id",
            "created_from_action_id",
            "approver_subject_id",
            "approver_tenant_id",
            "issued_at",
            "not_before",
            "expires_at",
            "use_count",
            "reconfirm_after_uses",
            "revocation_epoch",
            "status",
            "audience",
            "issuer_id",
            "issuer_key_id",
            "signature",
            "store_version",
        ],
    ),
    "MaskingReceipt": (
        {
            "masking_receipt_id": _id(),
            "stream_id": _id(),
            "attempt_id": _id(),
            "lease_id": _id(),
            "masking_result": {"type": "string"},
            "rejected_categories": _array_of({"type": "string"}),
            "error_code": {"type": "string"},
            "source_content_hash": _hash(),
            "source_normalized_hash": _hash(),
            "masked_content_hash": _hash(),
            "scan1_decision": _string_enum("CLEAN", "MASKABLE", "REJECT"),
            "scan1_findings": _array_of({"type": "object"}),
            "spans": _array_of(
                {
                    "type": "object",
                    "properties": {
                        "start": {"type": "integer", "minimum": 0},
                        "end": {"type": "integer", "minimum": 1},
                        "category": {"type": "string", "minLength": 1},
                    },
                    "required": ["start", "end", "category"],
                    "additionalProperties": False,
                }
            ),
            "span_validation_result": _string_enum("ACCEPTED", "REJECTED"),
            "masker_provider": {"type": "string"},
            "masker_model": {"type": "string"},
            "masker_model_digest": {"type": "string"},
            "masker_instruction_hash": _hash(),
            "masker_invocation_count": {"type": "integer", "minimum": 0},
            "normalization_profile": {"type": "string"},
            "normalization_profile_artifact_hash": _hash(),
            "rewriter_version": {"type": "string"},
            "scan2_result": _string_enum("ACCEPTED", "REJECTED"),
            "masking_policy_version": {"type": "integer", "minimum": 1},
            "policy_snapshot_hash": _hash(),
            "store_version": {"type": "integer", "minimum": 1},
        },
        [
            "masking_receipt_id",
            "stream_id",
            "source_content_hash",
            "source_normalized_hash",
            "scan1_decision",
            "scan1_findings",
            "normalization_profile",
            "normalization_profile_artifact_hash",
            "policy_snapshot_hash",
            "masking_policy_version",
            "store_version",
        ],
    ),
}


def _approval_consume_result() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    # ACRC-3-A: only an admitted concurrent loser has this result subject.
    properties = {
        "consume_result_id": _id(),
        "grant_id": _id(),
        "concurrency_group": _id(),
        "attempt_id": _id(),
        "schema_set_hash": _hash(),
        "state": {"const": "REJECTED"},
        "error_code": {"const": "APPROVAL_REPLAY"},
        "successful_consumes": {"type": "integer", "const": 0},
        "failed_consumes": {"type": "integer", "const": 1},
    }
    return properties, list(properties), []


_COMPLEX_BUILDERS: Final[dict[str, Any]] = {
    "ApprovalConsumeResult": _approval_consume_result,
    "ActionAttempt": _action_attempt,
    "ApprovalGrant": _approval_grant,
    "EffectReceipt": _effect_receipt,
}


_V1_0_0: Final[str] = "1.0.0"

# --------------------------------------------------------------------------
# 2.0.0 定義（Owner Decision D-1a）
#
# 対象は Context/Token 系5 Schema だけである。Required Field追加は §15.2 で
# **Major変更**であり、`1.0.0` を上書きせず新Versionとして足す。
#
# Field名は **Schema本文（`spec/20-schemas/*.md`）側へ寄せる**。D-1a は
# 「設計本文を正本としてJSON Schemaを拡張する」案であり、乖離0を達成するには
# 本文が必須と宣言した名前をJSON Schema側が持つ必要があるためである。
# したがって `1.0.0` の `bundle_id` / `total_token_count` などは
# `2.0.0` で `context_bundle_id` / `total_estimated_tokens` になる。
# **これは`1.0.0`の改名ではない。** `1.0.0`はそのまま読み取り可能なまま残る。
# --------------------------------------------------------------------------


def _context_bundle_v2() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    properties = {
        "context_bundle_id": _id(),
        "bundle_hash": _hash(),
        "selection_receipt_id": _id(),
        "selected_fragment_ids": _array_of(_id()),
        "ordered_fragment_ids": _array_of(_id()),
        "excluded_fragment_ids": _array_of(_id()),
        "compression_artifact_ids": _array_of(_id()),
        "total_estimated_tokens": {"type": "integer", "minimum": 0},
        "message_role_manifest_hash": _hash(),
        "token_profile_snapshot_hash": _hash(),
        "token_budget_policy_hash": _hash(),
        "input_read_capability_set_hash": _hash(),
        "input_read_evidence_hash": _hash(),
    }
    return properties, list(properties), []


def _context_selection_receipt_v2() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    properties = {
        "selection_receipt_id": _id(),
        "decision_hash": _hash(),
        "candidate_fragment_ids": _array_of(_id()),
        "selected_fragment_ids": _array_of(_id()),
        # `ContextSelectionReceipt`制約「各Excluded FragmentにReason Code必須」。
        # 1.0.0 の `excluded_fragment_ids`（ID配列）では理由を保存できなかった。
        "excluded_fragments": _array_of(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "fragment_id": _id(),
                    "reason": _string_enum("BUDGET", "DUPLICATE"),
                },
                "required": ["fragment_id", "reason"],
            }
        ),
        "rejected_input_resources": _array_of({"type": "object"}),
        "deduplication_result": _array_of(_id()),
        "compression_result": _array_of(_id()),
        "estimated_token_total": {"type": "integer", "minimum": 0},
        "token_profile_snapshot_hash": _hash(),
        "budget_policy_hash": _hash(),
        "input_read_capability_set_hash": _hash(),
        "input_read_evidence_hash": _hash(),
        "algorithm_version": {"type": "string", "minLength": 1},
    }
    return properties, list(properties), []


def _conversation_message_v2() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """Chat 本文を Artifact CAS 参照で持つ版（Owner Decision CMC-1-A / CMC-2-A）。

    `1.0.0` との差は `content_artifact_hash` の Required 追加だけである。
    §15.2 により Required 追加は Major なので、`1.0.0` を上書きせず版を足す。

    **本文を inline で持たない。** `content`／`text`／`body` を作らない。
    `content_artifact_hash` は `ContextFragment@2.0.0` と同名・同義である。
    """
    properties = {
        "message_id": _id(),
        "conversation_id": _id(),
        "role": _string_enum(*_MESSAGE_ROLES),
        "content_artifact_hash": _hash(),
    }
    required = [
        "message_id",
        "conversation_id",
        "role",
        # 順序キー。Envelope が持つ §1.3 の `sequence_number` をそのまま使う。
        "sequence_number",
        "content_artifact_hash",
    ]
    return properties, required, []


def _context_fragment_v2() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    properties = {
        "fragment_id": _id(),
        "source_artifact_hash": _hash(),
        "content_artifact_hash": _hash(),
        "source_span": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 0},
            },
            "required": ["start", "end"],
        },
        "fragment_type": {"type": "string", "minLength": 1},
        "message_role": _string_enum(
            "SYSTEM_CONTROL",
            "DEVELOPER_CONTROL",
            "USER_TASK",
            "TOOL_DEFINITION",
            "VERIFIED_REFERENCE_DATA",
            "UNTRUSTED_ARTIFACT_DATA",
            "UNTRUSTED_PROVIDER_DATA",
        ),
        "control_authority": {"type": "boolean"},
        "instruction_eligible": {"type": "boolean"},
        "input_read_capability_id": _id(),
        "source_file_identity": {"type": "string", "minLength": 1},
        "classification_scan_evidence_hash": _hash(),
        "estimated_tokens": {"type": "integer", "minimum": 0},
        # §1.13 Data Classification。複数Labelを許す。
        "classification_labels": _array_of({"type": "string", "minLength": 1}),
        "trust_level": _string_enum(
            "TRUSTED_CONTROL",
            "VERIFIED_INTERNAL",
            "AUTHENTICATED_EXTERNAL",
            "UNTRUSTED_PROVIDER_OUTPUT",
            "UNTRUSTED_EXTERNAL_INPUT",
            "UNKNOWN",
        ),
        "priority": {"type": "integer"},
        "mandatory": {"type": "boolean"},
        "freshness": _ts(),
        "deduplication_key": _hash(),
        "masking_receipt_id": _id(),
    }
    required = [name for name in properties if name != "masking_receipt_id"]
    return properties, required, []


def _token_profile_snapshot_v2() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    properties = {
        "token_profile_id": _id(),
        "snapshot_hash": _hash(),
        "provider_id": _id(),
        "model_id": _id(),
        "tokenizer_name": {"type": "string", "minLength": 1},
        "tokenizer_version": {"type": "string", "minLength": 1},
        "vocabulary_hash": _hash(),
        # §1.12「Counting Library／Adapter Version」。
        "counting_adapter_version": {"type": "string", "minLength": 1},
        # §1.12 のOverhead群。算定不能を0と区別するため全項目を必須にする。
        "overheads": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "system_message_overhead": {"type": "integer", "minimum": 0},
                "developer_message_overhead": {"type": "integer", "minimum": 0},
                "tool_definition_overhead": {"type": "integer", "minimum": 0},
                "per_message_overhead": {"type": "integer", "minimum": 0},
                "structured_output_overhead": {"type": "integer", "minimum": 0},
                "streaming_frame_overhead": {"type": "integer", "minimum": 0},
            },
            "required": [
                "system_message_overhead",
                "developer_message_overhead",
                "tool_definition_overhead",
                "per_message_overhead",
                "structured_output_overhead",
                "streaming_frame_overhead",
            ],
        },
        "reserved_output_tokens": {"type": "integer", "minimum": 0},
        "reserved_tool_tokens": {"type": "integer", "minimum": 0},
        "retry_fallback_reservation": {"type": "integer", "minimum": 0},
        "context_limit": {"type": "integer", "minimum": 1},
        "maximum_output_limit": {"type": "integer", "minimum": 1},
        "estimate_assurance": _string_enum("EXACT", "CONSERVATIVE", "UNKNOWN"),
        "expires_at": _ts(),
    }
    # `created_at` は共通Envelopeが供給する。Schema本文の必須列にも含まれる。
    return properties, list(properties), []


def _token_budget_policy_v2() -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    properties = {
        "policy_id": _id(),
        "policy_hash": _hash(),
        "maximum_input_tokens": {"type": "integer", "minimum": 0},
        "maximum_context_tokens": {"type": "integer", "minimum": 0},
        "reserved_output_tokens": {"type": "integer", "minimum": 0},
        "reserved_tool_tokens": {"type": "integer", "minimum": 0},
        "safety_margin_tokens": {"type": "integer", "minimum": 0},
        "retry_reservation": {"type": "integer", "minimum": 0},
        # §3.6 圧縮規則「圧縮深度は初期値1、最大2」。
        "compression_depth_limit": {"type": "integer", "minimum": 0, "maximum": 2},
        # `TokenBudgetPolicy`制約「`overflow_policy=FAIL_CLOSED`を初期値」。
        "overflow_policy": _string_enum("FAIL_CLOSED"),
        "required_fragment_types": _array_of({"type": "string", "minLength": 1}),
    }
    return properties, list(properties), []


# `(schema_name, schema_version)` -> 定義Builder。
#
# **現在は空である。** Step 4 で `2.0.0` を追加するときにここへ入れる。
#
# 空のまま schemas.yaml へ `2.0.0` の行を足すと `KeyError` で落ちる。これは意図した
# Fail-Closed である。以前は定義Tableが `schema_name` だけをKeyにしており、
# `build_schema(name, version)` の `version` は Envelope の const にしか使われていなかった。
# その状態で `2.0.0` を登録すると、**`1.0.0` も新しい定義で再生成され上書きされる**。
# 旧Recordを読むReaderとUpcasterの土台が静かに消えるため、
# 名前だけで引く経路を残さない（§15.2、Owner Decision D-Q6）。
_VERSIONED_DEFINITIONS: Final[dict[tuple[str, str], Any]] = {
    ("ContextBundle", "2.0.0"): _context_bundle_v2,
    ("ContextSelectionReceipt", "2.0.0"): _context_selection_receipt_v2,
    ("ContextFragment", "2.0.0"): _context_fragment_v2,
    ("TokenProfileSnapshot", "2.0.0"): _token_profile_snapshot_v2,
    ("TokenBudgetPolicy", "2.0.0"): _token_budget_policy_v2,
    ("ConversationMessage", "2.0.0"): _conversation_message_v2,
}


def _definition(name: str, version: str) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """`(name, version)` 単位で生成定義を解決する。

    解決順は次のとおりで、名前だけへのFallbackを持たない。

    1. `_VERSIONED_DEFINITIONS[(name, version)]`
    2. `version == "1.0.0"` のときだけ、現行の名前KeyのTable
    3. それ以外は `KeyError`
    """
    override = _VERSIONED_DEFINITIONS.get((name, version))
    if override is not None:
        properties, required, conditionals = override()
        return properties, required, conditionals

    if version != _V1_0_0:
        raise KeyError(
            f"no definition for core schema {name}@{version}; "
            "register it in _VERSIONED_DEFINITIONS before adding the version to schemas.yaml"
        )

    if name in _COMPLEX_BUILDERS:
        properties, required, conditionals = _COMPLEX_BUILDERS[name]()
        return properties, required, conditionals
    if name in _SIMPLE_SCHEMAS:
        properties, required = _SIMPLE_SCHEMAS[name]
        return properties, required, []
    raise KeyError(f"no definition for core schema: {name}")


def build_schema(name: str, version: str) -> dict[str, Any]:
    properties, required, conditionals = _definition(name, version)

    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://fde-harness.local/schemas/core/{name}/{version}.schema.json",
        "title": name,
        "type": "object",
        "properties": {**_envelope(name, version), **properties},
        "required": [*ENVELOPE_REQUIRED, *required],
        "additionalProperties": False,
    }
    if conditionals:
        schema["allOf"] = conditionals
    return schema


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registries", type=Path, default=Path("design-source/registries"))
    parser.add_argument("--out", type=Path, default=Path("."))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    catalog = yaml.safe_load((args.registries / "schemas.yaml").read_text(encoding="utf-8"))[
        "core_schemas"
    ]

    entries = normalize_core_schemas(catalog)

    stale: list[str] = []
    for entry in entries:
        name = entry["schema_name"]
        version = entry["schema_version"]
        relative = entry["path"]
        rendered = (
            json.dumps(build_schema(name, version), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        target = args.out / relative
        if args.check:
            if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
                stale.append(relative)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")

    if args.check:
        if stale:
            print("core schema files are stale:", file=__import__("sys").stderr)
            for item in stale:
                print(f"  {item}", file=__import__("sys").stderr)
            print("run: python tools/build_core_schemas.py", file=__import__("sys").stderr)
            return 1
        # 1 Version形式では entries と catalog の件数が一致する。その場合は
        # 従来と同じ文言を保つ（現行Registryに対する出力を変えないため）。
        if len(entries) == len(catalog):
            print(f"core schemas: all {len(catalog)} up to date")
        else:
            print(
                f"core schemas: all {len(entries)} versions of "
                f"{logical_schema_count(entries)} schemas up to date"
            )
        return 0

    if len(entries) == len(catalog):
        print(f"wrote {len(catalog)} core schemas under {args.out}")
    else:
        print(
            f"wrote {len(entries)} versions of {logical_schema_count(entries)} "
            f"core schemas under {args.out}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
