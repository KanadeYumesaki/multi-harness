"""CLI Workbench Preview の Application Service。

## 2 つの承認を混ぜない

    依頼 → 送信 Plan → **送信承認** → 単一 Transaction で消費・claim・Journal
      → CLI を 1 回だけ起動 → 応答検査
      → 適用 Plan → **適用承認** → 最終 Fence → Atomic Rename → Receipt

送信の承認は書込みの承認ではない。送信承認 ID を適用へ流用しても通らないように、
**別 Grant・別 Plan・別 scope** にしてある。

## 送信 Journal と適用 Journal を分ける

外部生成には送信前に既知の `expected_after_hash` が無い。既存 `OperationJournal`
（`LOCAL_FILE_COMMIT`）へ仮の期待値を入れると、実測と期待の意味が壊れる。だから
送信側は `CliInvocationJournal`（Preview 版付き契約）を使い、ローカル適用側だけが
従来の Effect Protocol（`OperationJournal` + Storage Fence + `EffectReceipt`）を使う。

## Transaction を持ったまま CLI を起動しない

`claim_send` が単一 Transaction で Approval 消費・Lease・Journal を確定し、
`dispatch_send` は **Transaction の外** で起動する。UI はその間も状態を読める。

## 「送っていない」と決めつけない

Process の終了を観測できた場合だけ決定的に扱う。Timeout・出力上限・起動不能・不明は
すべて `EFFECT_UNKNOWN` へ倒し、自動再送・自動 Provider 切替を一切しない。
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from harness.domain.artifact import ArtifactMetadata
from harness.domain.attempt import ActionAttempt
from harness.domain.canonical import canonicalize
from harness.domain.chatgpt_request import logical_request, responses_request
from harness.domain.cli_invocation import CliInvocationJournal, InvocationState
from harness.domain.code_proposal import CodeProposal
from harness.domain.effect import EffectState, OperationJournal
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.hashing import ContentHash, hash_bytes, hash_canonical
from harness.domain.history_selection import (
    MAX_VERIFIED_HISTORY_BYTES,
    select_history,
    validate_selection,
)
from harness.domain.lease import Lease, LeaseStatus
from harness.domain.plan import PlanAction, PlanAuthority, PlanBuildInput
from harness.domain.plan_build_payload import freeze_plan_request
from harness.domain.timestamps import timestamp_from_seconds, timestamp_seconds
from harness.domain.workbench import (
    CONTINUABLE_STATES,
    OUTPUT_CONTRACT_VERSION,
    REQUEST_CONTRACT_VERSION,
    SESSION_CONTRACT_VERSION,
    WorkbenchLimits,
    WorkbenchPreference,
    WorkbenchState,
    build_request_document,
    classify_target,
    history_hash_projection,
    instruction_hash,
    parse_state,
    request_payload_bytes,
    request_projection_hash,
    require_transition,
    validate_instruction,
)
from harness.domain.workspace_task import (
    TEXT_OUTPUT_CONTRACT,
    TEXT_REQUEST_CONTRACT,
    TEXT_SESSION_CONTRACT,
    TextArtifact,
    build_text_request,
    task_catalog,
    validate_task,
)
from harness.ports.approval import ApprovalConsumeRequest, ApprovalGrantPort
from harness.ports.approval_consume import ApprovalConsumerPort
from harness.ports.artifact_store import ArtifactStorePort
from harness.ports.chatgpt import ChatGptGenerationPort, HttpGenerationSpec
from harness.ports.cli_workbench import (
    CliLaunchSpec,
    CliOutcome,
    CliProfilePort,
    CliRunnerPort,
    ConversationHistoryPort,
    IdSourcePort,
    InvocationJournalStorePort,
    WorkbenchPreferenceStorePort,
    WorkbenchRecord,
    WorkbenchStorePort,
)
from harness.ports.effect import EffectJournalPort
from harness.ports.effect_execution import ClockPort
from harness.ports.event_ledger import EventLedgerPort, NewEvent
from harness.ports.lease import LeaseAcquireRequest, LeasePort
from harness.ports.local_workflow import AttemptStorePort, LocalAuthorityPort, LocalWorkspacePort
from harness.ports.masking import MaskingPipelinePort
from harness.ports.operation_control import OperationAdmissionPort
from harness.ports.plan_builder import PlanBuilderPort
from harness.ports.storage_commit import StorageCommitGuardPort
from harness.ports.unit_of_work import UnitOfWorkPort

__all__ = ["WorkbenchService"]

_PLANNER_VERSION = "harness-cli-workbench-planner/1.0"
_PRODUCER = "harness-cli-workbench/1"
_MAX_LISTED_TARGETS = 512

#: 承認消費時に許す時刻の後戻り（秒）。
#:
#: WSL2 の Wall Clock は負荷時に数秒単位で後戻りすることを実測した
#: （`docs/development/cli-workbench-e2e-20260908/clock-skew-observation.json`）。
#: 許容量が 0 だと、承認した直後の消費が理由なく `CLOCK_SKEW_EXCEEDED` で落ちる。
#: `ApprovalGrant.maximum_clock_skew_seconds` は元から契約にある Field なので、
#: **勝手に時刻を書き換えるのではなく、この Field へ根拠付きで載せる。**
#: 値は Plan の Policy Snapshot へ束縛され、承認の対象に含まれる。
_CLOCK_SKEW_TOLERANCE_SECONDS = 30


class WorkbenchService:
    """依頼から適用までを 1 本にする。**Repository も SQLite も UI へ出さない。**"""

    def __init__(
        self,
        *,
        sessions: WorkbenchStorePort,
        invocations: InvocationJournalStorePort,
        artifacts: ArtifactStorePort,
        ledger: EventLedgerPort,
        uow: UnitOfWorkPort,
        clock: ClockPort,
        ids: IdSourcePort,
        workspace: LocalWorkspacePort,
        workspace_label: str,
        authority: LocalAuthorityPort,
        grants: ApprovalGrantPort,
        approval_consumer: ApprovalConsumerPort,
        leases: LeasePort,
        journals: EffectJournalPort,
        attempts: AttemptStorePort,
        builder: PlanBuilderPort,
        profiles: CliProfilePort,
        runner: CliRunnerPort,
        masking: MaskingPipelinePort,
        response_parser: Callable[[str, bytes, int], CodeProposal],
        text_response_parser: Callable[[str, bytes, int], TextArtifact],
        guard_factory: Callable[[Lease], StorageCommitGuardPort],
        runtime_identity: Callable[[], str],
        validate_record: Callable[[str, dict[str, Any]], None],
        policy_snapshot: Callable[[], dict[str, Any]],
        schema_set_hash: ContentHash,
        limits: WorkbenchLimits | None = None,
        default_ttl_seconds: int = 3600,
        operation_gate: OperationAdmissionPort | None = None,
        preferences: WorkbenchPreferenceStorePort | None = None,
        preference_scope: ContentHash | None = None,
        conversations: ConversationHistoryPort | None = None,
        chatgpt: ChatGptGenerationPort | None = None,
    ) -> None:
        self.chatgpt = chatgpt
        self.operation_gate = operation_gate
        self.conversations = conversations
        self.preferences = preferences
        self.preference_scope = preference_scope
        self.sessions, self.invocations, self.artifacts = sessions, invocations, artifacts
        self.ledger, self.uow, self.clock, self.ids = ledger, uow, clock, ids
        self.workspace, self.workspace_label = workspace, workspace_label
        self.authority, self.grants = authority, grants
        self.approval_consumer, self.leases = approval_consumer, leases
        self.journals, self.attempts, self.builder = journals, attempts, builder
        self.profiles, self.runner, self.masking = profiles, runner, masking
        self.response_parser = response_parser
        self.text_response_parser = text_response_parser
        self.guard_factory, self.runtime_identity = guard_factory, runtime_identity
        self.validate_record, self.policy_snapshot = validate_record, policy_snapshot
        self.schema_set_hash = schema_set_hash
        self.limits = limits or WorkbenchLimits()
        self.default_ttl_seconds = default_ttl_seconds

    # -- 読み取り -----------------------------------------------------------

    def workspace_view(self) -> dict[str, Any]:
        """画面の見出し。**任意 Path を開ける口を作らない。**"""
        return {
            "workspace_label": self.workspace_label,
            "workspace_identity": self.workspace.identity(),
            "scope": "起動時に登録した隔離 Git Worktree 内の既存 UTF-8 File 1 件の完全置換",
            "limits": self.limits.projection(),
            "request_contract": REQUEST_CONTRACT_VERSION,
            "output_contract": OUTPUT_CONTRACT_VERSION,
            "session_contract": SESSION_CONTRACT_VERSION,
        }

    def provider_status(self) -> list[dict[str, Any]]:
        return [
            {
                "provider_id": status.provider_id,
                "display_name": status.display_name,
                "installed": status.installed,
                "package_version": status.package_version,
                "profile_verified": status.profile_verified,
                "restriction_summary": list(status.restriction_summary),
                "residual_risks": list(status.residual_risks),
                "login_state": status.login_state,
                "login_hint": status.login_hint,
                "models": list(status.models),
                "model_efforts": {
                    model: list(levels) for model, levels in status.model_efforts.items()
                },
                "blocking_reason": status.blocking_reason,
                "model_labels": dict(status.model_labels),
            }
            for status in (
                *self.profiles.statuses(),
                *(self.chatgpt.statuses() if self.chatgpt else ()),
            )
        ]

    def _preference_scope(self) -> ContentHash:
        if self.preference_scope is None:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "preference workspace scope is unavailable"
            )
        return self.preference_scope

    def preference_view(self) -> dict[str, Any]:
        preference = (
            None
            if self.preferences is None
            else self.preferences.get_preference(self._preference_scope())
        )
        return {
            "available": self.preferences is not None,
            "configured": preference is not None,
            "version": 0 if preference is None else preference.version,
            "selection": None if preference is None else preference.projection(),
            "grants_send_approval": False,
            "grants_apply_approval": False,
        }

    def save_preference(
        self,
        *,
        provider_id: str,
        model_id: str,
        reasoning_effort: str | None,
        expected_version: int,
        approved: bool,
    ) -> dict[str, Any]:
        """Save explicit form choices only. Resolve validates values, without running a CLI."""
        if approved is not True:
            raise HarnessError(ErrorCode.APPROVAL_REQUIRED, "confirm saving these settings first")
        if type(expected_version) is not int or not 0 <= expected_version < 2147483647:
            raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "invalid preference version")
        if self.preferences is None:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "preference storage is unavailable")
        preference = WorkbenchPreference(
            self._preference_scope(), provider_id, model_id, reasoning_effort, expected_version + 1
        )
        # No attest, runner, approval grant or execution journal here. Every new Plan
        # still resolves and attests its own immutable settings before send approval.
        self._require_no_rejected_content(
            json.dumps(
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "reasoning_effort": reasoning_effort,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            "WORKBENCH_PREFERENCE",
        )
        self._resolve_launch(provider_id, model_id, reasoning_effort)
        with (
            self.operation_gate.admission("WorkbenchService.save_preference", kind="intake")
            if self.operation_gate is not None
            else nullcontext()
        ):
            with self.uow.begin_immediate():
                self.preferences.get_preference(preference.scope_hash)
                self.preferences.save_preference(preference, expected_version=expected_version)
        return self.preference_view()

    def list_targets(self) -> list[dict[str, Any]]:
        """Worktree の実体から候補を作る。**deny 理由も一緒に返して隠さない。**"""
        snapshot = self.workspace.snapshot()
        rows: list[dict[str, Any]] = []
        for relative_path in sorted(snapshot)[:_MAX_LISTED_TARGETS]:
            decision = classify_target(relative_path)
            rows.append(
                {
                    "relative_path": relative_path,
                    "eligible": decision.eligible,
                    "reason": decision.reason,
                    "content_hash": snapshot[relative_path],
                }
            )
        return rows

    def preview_target(self, relative_path: str) -> dict[str, Any]:
        """選んだ 1 件の本文を返す。**deny 規則をここでも強制する。**"""
        payload, _ = self._read_eligible(relative_path)
        text = self._decode(payload)
        return {
            "relative_path": relative_path,
            "size_bytes": len(payload),
            "content_hash": str(hash_bytes(payload)),
            "text": text,
        }

    # -- 送信 Plan ----------------------------------------------------------

    def create_send_plan(
        self,
        *,
        provider_id: str,
        model_id: str,
        relative_path: str | None,
        instruction: str,
        ttl_seconds: int | None = None,
        reasoning_effort: str | None = None,
        parent_session_id: str | None = None,
        conversation_id: str | None = None,
        history_selection: dict[str, Any] | None = None,
        important_notes: str = "",
        task_kind: str = "file_edit",
        reference_text: str = "",
        output_format: str = "markdown",
    ) -> dict[str, Any]:
        """送信 Plan を凍結し、承認待ちにする。**まだ Process は起動しない。**"""
        with (
            self.operation_gate.admission("WorkbenchService.create_send_plan", kind="intake")
            if self.operation_gate is not None
            else nullcontext()
        ):
            ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
            if not 60 <= ttl <= 86400:
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                    "ttl must be between 60 and 86400 seconds",
                )
            reference_bytes = validate_task(task_kind, output_format, reference_text, self.limits)
            text_task = task_kind != "file_edit"
            if text_task:
                if relative_path is not None:
                    raise HarnessError(
                        ErrorCode.PATH_OUTSIDE_CAPABILITY, "text tasks cannot name a file target"
                    )
                source_bytes, snapshot = reference_bytes, self.workspace.snapshot()
                source_text = reference_text
                before_hash = None
            else:
                if relative_path is None or reference_text:
                    raise HarnessError(
                        ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                        "file tasks require a target and no reference_text",
                    )
                source_bytes, snapshot = self._read_eligible(relative_path)
                source_text = self._decode(source_bytes)
                before_hash = snapshot[relative_path]
            output_contract = TEXT_OUTPUT_CONTRACT if text_task else OUTPUT_CONTRACT_VERSION
            instruction_bytes = validate_instruction(instruction, self.limits)
            spec = self._resolve_launch(provider_id, model_id, reasoning_effort)
            self._verify_launch(spec)
            action_kind = (
                (
                    "CHATGPT_TEXT_ARTIFACT_REQUEST"
                    if isinstance(spec, HttpGenerationSpec)
                    else "CLI_TEXT_ARTIFACT_REQUEST"
                )
                if text_task
                else (
                    "CHATGPT_CODE_PROPOSAL_REQUEST"
                    if isinstance(spec, HttpGenerationSpec)
                    else "CLI_CODE_PROPOSAL_REQUEST"
                )
            )
            # 実行前制限が効いていることを **測ってから** Plan へ束縛する。
            attestation = dict(self._attest_launch(spec))

            selection = validate_selection(history_selection)
            validate_instruction(important_notes or "none", self.limits)
            history, handoff = self._collect_history(
                parent_session_id, conversation_id, selection=selection
            )
            document_request = (
                build_request_document(
                    provider_id=provider_id,
                    model_id=model_id,
                    relative_path=relative_path or "",
                    source_text=source_text,
                    instruction=instruction,
                    limits=self.limits,
                    conversation_history=history,
                )
                if not text_task
                else build_text_request(
                    provider_id=provider_id,
                    model_id=model_id,
                    task_kind=task_kind,
                    instruction=instruction,
                    reference_text=reference_text,
                    output_format=output_format,
                    limits=self.limits,
                    conversation_history=history,
                )
            )
            if important_notes:
                document_request["important_notes"] = important_notes
            if reasoning_effort is not None:
                document_request["reasoning_effort"] = reasoning_effort
            wire_request = (
                responses_request(model_id, document_request)
                if isinstance(spec, HttpGenerationSpec)
                else document_request
            )
            payload = request_payload_bytes(wire_request, self.limits)
            self._require_no_rejected_content(payload, "OUTBOUND_REQUEST")

            session_id = self.ids.new_id()
            now = self.clock.now()
            expires_at = _plus_seconds(now, ttl)
            policy = self.policy_snapshot()
            policy_hash = hash_canonical(
                {
                    "workbench_policy": policy,
                    "limits": self.limits.projection(),
                    "request_contract": document_request["contract"],
                    "output_contract": output_contract,
                    "runtime_identity": self.runtime_identity(),
                },
                artifact_type="cli-workbench-policy",
                schema_major=1,
            )
            # 課金・利用枠は観測していない。**FREE や 0 で埋めず、UNVERIFIED として束縛する。**
            commercial = {
                "network_used": True,
                "billing_mode": "UNKNOWN",
                "entitlement_state": "UNVERIFIED",
                "usage_quota_state": "UNVERIFIED",
                "credential_route": spec.runtime_projection.get("credential_route", "CLI_OWNED"),
            }
            build = PlanBuildInput(
                intent_hash=instruction_hash(instruction_bytes),
                workspace_snapshot_hash=hash_canonical(
                    snapshot, artifact_type="local-worktree-snapshot", schema_major=1
                ),
                input_read_evidence_hash=hash_canonical(
                    (
                        {
                            "task_kind": task_kind,
                            "reference_hash": str(hash_bytes(reference_bytes)),
                            "reference_size": len(reference_bytes),
                            "output_format": output_format,
                        }
                        if text_task
                        else {
                            "target_relative_path": relative_path,
                            "before_hash": before_hash,
                            "before_size": len(source_bytes),
                        }
                    ),
                    artifact_type="cli-workbench-read-evidence",
                    schema_major=1,
                ),
                context_bundle_hash=hash_bytes(payload),
                policy_snapshot_hash=policy_hash,
                token_profile_hash=hash_canonical(
                    self.limits.projection(), artifact_type="cli-workbench-limits", schema_major=1
                ),
                schema_set_hash=self.schema_set_hash,
                capability_snapshot_hash=hash_canonical(
                    {
                        "runtime": dict(spec.runtime_projection),
                        "restriction_attestation": attestation,
                    },
                    artifact_type="cli-workbench-capability",
                    schema_major=1,
                ),
                entitlement_snapshot_hash=hash_canonical(
                    commercial, artifact_type="cli-workbench-commercial", schema_major=1
                ),
                normalized_actions=(
                    PlanAction(
                        action_kind,
                        {
                            "provider_id": provider_id,
                            "model_id": model_id,
                            "request_payload_hash": str(hash_bytes(payload)),
                            "request_projection_hash": str(request_projection_hash(wire_request)),
                            "target_relative_path": relative_path,
                            "expected_before_hash": before_hash,
                            "runtime_hash": str(spec.runtime_hash),
                            "output_contract": output_contract,
                        },
                        {"proposal_contract": output_contract},
                    ),
                ),
                runtime_envelope_spec_hash=spec.runtime_hash,
                invocation_manifest_hash=hash_canonical(
                    {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "output_contract": output_contract,
                        "runtime_hash": str(spec.runtime_hash),
                    },
                    artifact_type="cli-workbench-invocation",
                    schema_major=1,
                ),
                token_budget_policy_hash=hash_canonical(
                    {"limits": self.limits.projection(), "budget_reservation": "NOT_IMPLEMENTED"},
                    artifact_type="cli-workbench-budget",
                    schema_major=1,
                ),
                control_data_policy_hash=hash_canonical(
                    {"masking": policy.get("masking", {}), "stdin_only_payload": True},
                    artifact_type="cli-workbench-control-data",
                    schema_major=1,
                ),
                planner_algorithm_version=_PLANNER_VERSION,
            )
            authority = PlanAuthority(
                run_id=session_id,
                execution_plan_id=session_id + ":send-plan",
                plan_version=1,
                issued_at=now,
                expires_at=expires_at,
                planner_identity=_PRODUCER,
                authority_scope=action_kind,
            )
            frozen = freeze_plan_request(build, authority)
            plan = self.builder.build(frozen)

            document: dict[str, Any] = {
                "contract": TEXT_SESSION_CONTRACT if text_task else SESSION_CONTRACT_VERSION,
                "session_id": session_id,
                "state": WorkbenchState.DRAFTED.value,
                "version": 0,
                "created_at": now,
                "expires_at": expires_at,
                "provider_id": provider_id,
                "model_id": model_id,
                "reasoning_effort": reasoning_effort,
                "target": relative_path,
                "before_hash": before_hash,
                "before_size": len(source_bytes),
                "snapshot": snapshot,
                "workspace_identity": self.workspace.identity(),
                "workspace_label": self.workspace_label,
                "runtime_identity": self.runtime_identity(),
                "runtime_hash": str(spec.runtime_hash),
                "runtime_projection": dict(spec.runtime_projection),
                "restriction_attestation": attestation,
                "commercial_disclosure": commercial,
                "limits": self.limits.projection(),
                "instruction": instruction,
                "instruction_hash": str(instruction_hash(instruction_bytes)),
                "request_payload_hash": str(hash_bytes(payload)),
                "frozen_request": json.loads(frozen),
                "plan_content_hash": str(plan.plan_content_hash),
                "execution_plan_hash": str(plan.execution_plan_hash),
                "send_events": [],
                "failure": None,
            }
            if text_task:
                document.update(
                    task_kind=task_kind,
                    output_format=output_format,
                    reference_hash=str(hash_bytes(reference_bytes)),
                    reference_size=len(reference_bytes),
                )
            if handoff is not None:
                document["handoff"] = handoff
            with self.uow.begin_immediate():
                self._require_handoff_current(document)
                document["request_artifact_hash"] = str(self._put(payload, "application/json"))
                self._save(document, WorkbenchState.DRAFTED, EventType.RUN_CREATED)
                self._save(document, WorkbenchState.DRAFTED, EventType.INTENT_CREATED)
                self._save(document, WorkbenchState.DRAFTED, EventType.PLAN_RESOLVED)
                document["policy_decision"] = {
                    "outcome": "ALLOW",
                    "approval_required": True,
                    "network_egress": True,
                }
                self._save(document, WorkbenchState.DRAFTED, EventType.POLICY_DECIDED)
            return document

    def confirmation(self, session_id: str) -> dict[str, Any]:
        """「送信内容を確認」に出す完全な payload。**画面が作った文字列を正本にしない。**"""
        document = self.inspect(session_id)
        payload = self.artifacts.get(ContentHash.parse(document["request_artifact_hash"]))
        if str(hash_bytes(payload)) != document["request_payload_hash"]:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "stored request payload changed"
            )
        return {
            "session_id": session_id,
            "state": document["state"],
            "provider_id": document["provider_id"],
            "model_id": document["model_id"],
            "reasoning_effort": document.get("reasoning_effort"),
            "target": document["target"],
            "before_hash": document["before_hash"],
            "execution_plan_hash": document["execution_plan_hash"],
            "expires_at": document["expires_at"],
            "limits": document["limits"],
            "commercial_disclosure": document["commercial_disclosure"],
            "runtime_projection": document["runtime_projection"],
            "request_payload_hash": document["request_payload_hash"],
            "request_payload_text": payload.decode("utf-8"),
            "handoff": document.get("handoff"),
            "context_budget": {
                "request_bytes": len(payload),
                "maximum_request_bytes": self.limits.max_request_bytes,
                "token_count": None,
                "token_count_assurance": "UNKNOWN",
                "provider_context_limit": "UNVERIFIED",
                "note": "容量はUTF-8のBytesで実測。モデル固有のトークン数・上限ではありません。",
            },
            "transparency_note": (
                "ここに出るJSON全体を公式Responses APIへ送ります。認証トークンは含みません。"
                "出力トークン上限は指定できません。利用枠・クレジット設定はChatGPT側で確認してください。"
            )
            if document["provider_id"] == "chatgpt"
            else (
                "ここに出るのは Harness が stdin へ渡す完全な payload である。"
                "各 CLI が内部で付ける system prompt や独自の前後処理までは可視化していない。"
            ),
        }

    # -- 送信承認と実行 -----------------------------------------------------

    def approve_send(
        self, session_id: str, *, execution_plan_hash: str, auth_session: str
    ) -> dict[str, Any]:
        with (
            self.operation_gate.admission("WorkbenchService.approve_send", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            subject = self.authority.subject(auth_session)
            document = self.inspect(session_id)
            self._require_state(document, WorkbenchState.DRAFTED)
            if document["execution_plan_hash"] != execution_plan_hash:
                raise HarnessError(
                    ErrorCode.APPROVAL_INVALIDATED, "approval names a different execution plan"
                )
            self._require_send_preconditions(document)
            grant, public_key = self.authority.issue(
                run_id=session_id + ":send",
                plan_content_hash=ContentHash.parse(document["plan_content_hash"]),
                execution_plan_hash=ContentHash.parse(execution_plan_hash),
                scope=(self._send_scope(document),),
                subject=subject,
                now=self.clock.now(),
                expires_at=document["expires_at"],
                maximum_clock_skew_seconds=_CLOCK_SKEW_TOLERANCE_SECONDS,
            )
            with self.uow.begin_immediate():
                self._require_handoff_current(document)
                self.grants.issue(grant)
                document.update(
                    send_grant_id=grant.grant_id,
                    send_approver=subject,
                    send_public_key=public_key,
                )
                self._save(document, WorkbenchState.SEND_APPROVED, EventType.APPROVAL_ISSUED)
            return document

    def claim_send(self, session_id: str) -> dict[str, Any]:
        """承認消費・claim・Lease・Journal を **単一 Transaction** で確定する。"""
        with (
            self.operation_gate.admission("WorkbenchService.claim_send", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            document = self.inspect(session_id)
            self._require_state(document, WorkbenchState.SEND_APPROVED)
            self._require_send_preconditions(document)
            grant = self.grants.get(document["send_grant_id"])
            if grant is None or grant.action_scope != (self._send_scope(document),):
                raise HarnessError(
                    ErrorCode.APPROVAL_REQUIRED, "matching send approval is required"
                )
            attempt_id = session_id + ":send-attempt"
            action_id = session_id + ":cli-request"
            resource = document["workspace_identity"] + ":" + self._send_scope(document)
            with self.uow.begin_immediate():
                self._require_handoff_current(document)
                request = ApprovalConsumeRequest(
                    grant.grant_id,
                    ContentHash.parse(document["execution_plan_hash"]),
                    0,
                    document["send_approver"],
                    attempt_id,
                    self.clock.now(),
                    self.authority.verify(grant, document["send_public_key"]),
                    grant.store_version,
                )
                ticket = self.approval_consumer.register_in_transaction(
                    request, session_id + ":send"
                )
                consumed = self.approval_consumer.consume_in_transaction(ticket, request)
                if consumed.successful_consumes != 1:
                    raise HarnessError(ErrorCode.APPROVAL_REPLAY, "send approval consumption lost")
                lease = self.leases.acquire(
                    LeaseAcquireRequest(
                        resource,
                        "workbench-worker",
                        attempt_id,
                        self.clock.now(),
                        document["expires_at"],
                    )
                )
                attempt = ActionAttempt(attempt_id, action_id, 1).with_plan(
                    ContentHash.parse(document["plan_content_hash"]),
                    ContentHash.parse(document["execution_plan_hash"]),
                )
                attempt = attempt.ready().claim(
                    worker_id="workbench-worker", claim_id=session_id + ":send-claim"
                )
                attempt = attempt.with_lease(
                    lease_id=lease.lease_id, fencing_token=lease.fencing_token
                )
                attempt = attempt.with_runtime_attestation(
                    ContentHash.parse(document["runtime_identity"])
                )
                attempt = attempt.start(self.clock.now())
                self.attempts.create(attempt)
                journal = CliInvocationJournal(
                    invocation_id=session_id + ":invocation",
                    execution_plan_hash=ContentHash.parse(document["execution_plan_hash"]),
                    request_hash=ContentHash.parse(document["request_payload_hash"]),
                    runtime_hash=ContentHash.parse(document["runtime_hash"]),
                )
                self.invocations.create_prepared(journal, session_id=session_id)
                document.update(
                    send_ticket_id=ticket.ticket_id,
                    send_lease_id=lease.lease_id,
                    send_fencing_token=lease.fencing_token,
                    send_resource=resource,
                    send_attempt_id=attempt_id,
                    send_action_id=action_id,
                    invocation_id=journal.invocation_id,
                )
                self._save(document, WorkbenchState.SEND_PREPARED, EventType.APPROVAL_CONSUMED)
                self._save(document, WorkbenchState.SEND_PREPARED, EventType.ACTION_CLAIMED)
                self._save(document, WorkbenchState.SEND_PREPARED, EventType.LEASE_ACQUIRED)
                self._save(document, WorkbenchState.SEND_PREPARED, EventType.RUNTIME_ATTESTED)
                self._save(document, WorkbenchState.SEND_PREPARED, EventType.ACTION_STARTED)
                self._save(
                    document, WorkbenchState.SEND_PREPARED, EventType.REMOTE_INVOCATION_PREPARED
                )
            return document

    def dispatch_send(self, session_id: str) -> dict[str, Any]:
        """CLI を 1 回だけ起動する。**Transaction は保持しない。**"""
        with (
            self.operation_gate.admission("WorkbenchService.dispatch_send", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            document = self.inspect(session_id)
            self._require_state(document, WorkbenchState.SEND_PREPARED)
            self._require_send_preconditions(document)
            journal = self._require_invocation(session_id, InvocationState.PREPARED_DURABLE)
            spec = self._resolve_launch(
                document["provider_id"], document["model_id"], document.get("reasoning_effort")
            )
            payload = self.artifacts.get(ContentHash.parse(document["request_artifact_hash"]))
            self._require_spawn_identity(document, spec, payload)

            attempted = journal.attempted()
            with self.uow.begin_immediate():
                self._require_handoff_current(document)
                self.invocations.update(attempted, expected_store_version=journal.store_version)
                # `spec/00-common.md §1.14.1` の Remote Invocation Registry 写像。
                # `EXECUTION_ATTEMPTED` は **ローカル書込みの OperationJournal** が持つ
                # Event である。送信側には OperationJournal が無いので発行しない。
                self._save(
                    document, WorkbenchState.SEND_ATTEMPTED, EventType.REMOTE_REQUEST_DISPATCHING
                )
            # ここから先は Transaction の外。**外部 Process が走るあいだ DB を掴まない。**
            if isinstance(spec, HttpGenerationSpec):
                if self.chatgpt is None:
                    raise HarnessError(
                        ErrorCode.RUNTIME_SPEC_MISMATCH, "ChatGPT transport unavailable"
                    )
                http_result = self.chatgpt.run(
                    spec,
                    payload=payload,
                    timeout_seconds=self.limits.timeout_seconds,
                    maximum_bytes=self.limits.max_stdout_bytes,
                )
                observation = http_result.observation()
                if http_result.outcome != "COMPLETED":
                    return self._record_unknown(document, attempted, observation)
                return self._record_completed(document, attempted, http_result.payload, observation)
            result = self.runner.run(
                spec,
                stdin_payload=payload,
                timeout_seconds=self.limits.timeout_seconds,
                max_stdout_bytes=self.limits.max_stdout_bytes,
                max_stderr_bytes=self.limits.max_stderr_bytes,
            )
            observation = {
                "outcome": result.outcome.value,
                "exit_code": result.exit_code,
                "stdout_bytes": len(result.stdout),
                "stdout_truncated": result.stdout_truncated,
                "stderr_bytes": result.stderr_bytes_observed,
                "stderr_truncated": result.stderr_truncated,
                "diagnostic_id": result.diagnostic_id,
                "stderr_classifications": list(result.stderr_classifications),
                "stderr_source_locations": list(result.stderr_source_locations),
                "descendant_cleanup": result.descendant_cleanup.value,
                "residual_pids": list(result.residual_pids),
            }
            if result.outcome is not CliOutcome.COMPLETED:
                # 終了を観測できていない。**未送信・無課金と決めつけない。**
                return self._record_unknown(document, attempted, observation)
            if not result.descendants_settled:
                # 起動した Group に Process が残っている。何をしているか分からない
                # ものが動いたままなので、**出力が読めていても不明で閉じる。**
                return self._record_unknown(document, attempted, observation)
            return self._record_completed(document, attempted, result.stdout, observation)

    def mark_send_unknown(self, session_id: str) -> dict[str, Any]:
        """再起動後に `SEND_ATTEMPTED` で残った Session を明示的に不明で閉じる。"""
        document = self.inspect(session_id)
        self._require_state(document, WorkbenchState.SEND_ATTEMPTED)
        journal = self._require_invocation(session_id, InvocationState.EXECUTION_ATTEMPTED)
        return self._record_unknown(
            document,
            journal,
            {
                "outcome": "UNKNOWN",
                "exit_code": None,
                "stdout_bytes": 0,
                "stdout_truncated": False,
                "stderr_bytes": 0,
                "stderr_truncated": False,
                "diagnostic_id": "operator-declared-unknown",
            },
        )

    # -- 差分と適用 ---------------------------------------------------------

    def diff(self, session_id: str) -> dict[str, Any]:
        """CAS の検査済み Bytes だけから差分を作る。**画面が返した本文を使わない。**"""
        document = self.inspect(session_id)
        self._require_file_task(document)
        if document["state"] not in {
            WorkbenchState.PROPOSAL_READY.value,
            WorkbenchState.APPLY_APPROVED.value,
            WorkbenchState.APPLY_PREPARED.value,
            WorkbenchState.APPLY_ATTEMPTED.value,
            WorkbenchState.APPLIED.value,
            WorkbenchState.APPLY_UNKNOWN.value,
        }:
            raise HarnessError(
                ErrorCode.APPROVAL_REQUIRED, "no verified proposal exists for this session"
            )
        proposal = self._proposal_bytes(document)
        before = self.workspace.read(document["target"])
        before_text = self._decode(before)
        after_text = self._decode(proposal)
        unified = list(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile="a/" + document["target"],
                tofile="b/" + document["target"],
                n=3,
            )
        )
        return {
            "session_id": session_id,
            "state": document["state"],
            "target": document["target"],
            "before_hash": str(hash_bytes(before)),
            "plan_before_hash": document["before_hash"],
            "after_hash": document["proposal_hash"],
            "before_text": before_text,
            "after_text": after_text,
            "unified_diff": "".join(unified),
            "target_changed_outside": str(hash_bytes(before)) != document["before_hash"],
            "apply_execution_plan_hash": document.get("apply_execution_plan_hash"),
            "apply_plan_expires_at": document.get("apply_expires_at"),
        }

    def approve_apply(
        self,
        session_id: str,
        *,
        apply_execution_plan_hash: str,
        proposal_hash: str,
        auth_session: str,
    ) -> dict[str, Any]:
        with (
            self.operation_gate.admission("WorkbenchService.approve_apply", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            subject = self.authority.subject(auth_session)
            document = self.inspect(session_id)
            self._require_file_task(document)
            self._require_state(document, WorkbenchState.PROPOSAL_READY)
            if document["apply_execution_plan_hash"] != apply_execution_plan_hash:
                raise HarnessError(
                    ErrorCode.APPROVAL_INVALIDATED, "approval names a different apply plan"
                )
            if document["proposal_hash"] != proposal_hash:
                raise HarnessError(
                    ErrorCode.APPROVAL_INVALIDATED, "approval names a different proposal"
                )
            if (
                document.get("send_grant_id") == apply_execution_plan_hash
            ):  # pragma: no cover - guard
                raise HarnessError(ErrorCode.APPROVAL_INVALIDATED, "send approval cannot apply")
            self._require_apply_preconditions(document)
            grant, public_key = self.authority.issue(
                run_id=session_id + ":apply",
                plan_content_hash=ContentHash.parse(document["apply_plan_content_hash"]),
                execution_plan_hash=ContentHash.parse(apply_execution_plan_hash),
                scope=(document["target"],),
                subject=subject,
                now=self.clock.now(),
                expires_at=document["apply_expires_at"],
                maximum_clock_skew_seconds=_CLOCK_SKEW_TOLERANCE_SECONDS,
            )
            with self.uow.begin_immediate():
                self.grants.issue(grant)
                document.update(
                    apply_grant_id=grant.grant_id,
                    apply_approver=subject,
                    apply_public_key=public_key,
                )
                self._save(document, WorkbenchState.APPLY_APPROVED, EventType.APPROVAL_ISSUED)
            return document

    def run_apply(self, session_id: str) -> dict[str, Any]:
        with (
            self.operation_gate.admission("WorkbenchService.run_apply", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            document = self.inspect(session_id)
            self._require_file_task(document)
            self._require_state(document, WorkbenchState.APPLY_APPROVED)
            self._require_apply_preconditions(document)
            grant = self.grants.get(document["apply_grant_id"])
            if grant is None or grant.action_scope != (document["target"],):
                raise HarnessError(
                    ErrorCode.APPROVAL_REQUIRED, "matching apply approval is required"
                )
            proposal = self._proposal_bytes(document)
            attempt_id = session_id + ":apply-attempt"
            action_id = session_id + ":local-file"
            resource = document["workspace_identity"] + ":" + document["target"]
            with self.uow.begin_immediate():
                request = ApprovalConsumeRequest(
                    grant.grant_id,
                    ContentHash.parse(document["apply_execution_plan_hash"]),
                    0,
                    document["apply_approver"],
                    attempt_id,
                    self.clock.now(),
                    self.authority.verify(grant, document["apply_public_key"]),
                    grant.store_version,
                )
                ticket = self.approval_consumer.register_in_transaction(
                    request, session_id + ":apply"
                )
                consumed = self.approval_consumer.consume_in_transaction(ticket, request)
                if consumed.successful_consumes != 1:
                    raise HarnessError(ErrorCode.APPROVAL_REPLAY, "apply approval consumption lost")
                lease = self.leases.acquire(
                    LeaseAcquireRequest(
                        resource,
                        "workbench-writer",
                        attempt_id,
                        self.clock.now(),
                        document["apply_expires_at"],
                    )
                )
                attempt = ActionAttempt(attempt_id, action_id, 1).with_plan(
                    ContentHash.parse(document["apply_plan_content_hash"]),
                    ContentHash.parse(document["apply_execution_plan_hash"]),
                )
                attempt = attempt.ready().claim(
                    worker_id="workbench-writer", claim_id=session_id + ":apply-claim"
                )
                attempt = attempt.with_lease(
                    lease_id=lease.lease_id, fencing_token=lease.fencing_token
                )
                attempt = attempt.with_runtime_attestation(
                    ContentHash.parse(document["runtime_identity"])
                )
                attempt = attempt.start(self.clock.now())
                self.attempts.create(attempt)
                document.update(
                    apply_ticket_id=ticket.ticket_id,
                    apply_lease_id=lease.lease_id,
                    apply_fencing_token=lease.fencing_token,
                    apply_resource=resource,
                    apply_attempt_id=attempt_id,
                    apply_action_id=action_id,
                )
                self._save(document, WorkbenchState.APPLY_APPROVED, EventType.APPROVAL_CONSUMED)
                # 適用は送信とは別の Action である。**同じ Attempt を使い回さない。**
                self._save(document, WorkbenchState.APPLY_APPROVED, EventType.ACTION_CLAIMED)
                self._save(document, WorkbenchState.APPLY_APPROVED, EventType.LEASE_ACQUIRED)
                self._save(document, WorkbenchState.APPLY_APPROVED, EventType.RUNTIME_ATTESTED)
                self._save(document, WorkbenchState.APPLY_APPROVED, EventType.ACTION_STARTED)
            guard = self.guard_factory(lease)
            prepared = self.workspace.prepare(
                relative_path=document["target"],
                effect_id=session_id + ":apply-effect",
                before_hash=ContentHash.parse(document["before_hash"]),
                replacement=proposal,
                guard=guard,
            )
            prepared["workspace_snapshot"] = document["snapshot"]
            journal = OperationJournal(
                operation_journal_id=session_id + ":apply-journal",
                operation_id=session_id + ":apply-operation",
                effect_id=session_id + ":apply-effect",
                run_id=session_id,
                action_id=action_id,
                attempt_id=attempt_id,
                operation_type="LOCAL_FILE_COMMIT",
                target_resource_identity=resource,
                fencing_token=lease.fencing_token,
                before_hash=ContentHash.parse(document["before_hash"]),
                expected_after_hash=ContentHash.parse(document["proposal_hash"]),
                prepared_event_id=f"{session_id}:{self.ledger.stream_head(session_id) + 1}",
                prepared_at=self.clock.now(),
                durability_level="STORAGE_SYNC",
            )
            with self.uow.begin_immediate():
                self.journals.create_prepared(journal)
                self.attempts.update(
                    attempt.prepare_effect(journal.operation_journal_id),
                    expected_store_version=attempt.store_version,
                )
                document.update(prepared=prepared, apply_effect_id=journal.effect_id)
                self._save(document, WorkbenchState.APPLY_PREPARED, EventType.ACTION_PREPARED)
            attempted = journal.mark_execution_attempted(
                event_id=f"{session_id}:{self.ledger.stream_head(session_id) + 1}",
                at=self.clock.now(),
            )
            with self.uow.begin_immediate():
                self.journals.update(attempted, expected_store_version=journal.store_version)
                self._save(document, WorkbenchState.APPLY_ATTEMPTED, EventType.EXECUTION_ATTEMPTED)
            observed = self.workspace.commit(prepared, guard)
            return self._complete_apply(document, attempted, observed)

    def recover_apply(self, session_id: str) -> dict[str, Any]:
        """Rename 後・Receipt 前の crash を観測だけで畳む。**再書込みしない。**"""
        with (
            self.operation_gate.admission("WorkbenchService.recover_apply", kind="effect")
            if self.operation_gate is not None
            else nullcontext()
        ):
            document = self.inspect(session_id)
            self._require_file_task(document)
            if document["state"] in {
                WorkbenchState.SEND_FAILED.value,
                WorkbenchState.PROPOSAL_READY.value,
            }:
                # 旧版で残った送信Leaseだけを、保存済みの終了観測に基づいて解放する。
                # 外部送信・適用・承認の再発行は行わない。
                with self.uow.begin_immediate():
                    self._release_completed_send_lease(document)
                    document["recovery_decision"] = "COMPLETED_SEND_LEASE_RELEASED_NO_REPLAY"
                    self._save(document, parse_state(document["state"]), EventType.RECOVERY_DECIDED)
                return document
            if document["state"] not in {
                WorkbenchState.APPLY_PREPARED.value,
                WorkbenchState.APPLY_ATTEMPTED.value,
                WorkbenchState.APPLIED.value,
            }:
                raise HarnessError(
                    ErrorCode.UNRECONCILED_EFFECT_PRESENT,
                    "session has no apply effect to reconcile",
                )
            journal = self.journals.get_by_effect_id(document["apply_effect_id"])
            observed = hash_bytes(self.workspace.read(document["target"]))
            if journal is None or observed != journal.expected_after_hash:
                raise HarnessError(
                    ErrorCode.EFFECT_UNKNOWN, "recovery cannot prove the expected effect; no replay"
                )
            if journal.state not in (
                EffectState.EXECUTION_ATTEMPTED,
                EffectState.EFFECT_OBSERVED,
                EffectState.RECEIPT_DURABLE,
            ):
                raise HarnessError(
                    ErrorCode.EFFECT_UNKNOWN,
                    "unexpected effect before attempted event; repair needed",
                )
            with self.uow.begin_immediate():
                state = parse_state(document["state"])
                self._save(document, state, EventType.RECOVERY_STARTED)
                document["recovery_decision"] = "EXPECTED_EFFECT_OBSERVED_NO_REPLAY"
                self._save(document, state, EventType.RECOVERY_DECIDED)
            return self._complete_apply(document, journal, observed)

    # -- Session の読み出し -------------------------------------------------

    def inspect(self, session_id: str) -> dict[str, Any]:
        record = self.sessions.get(session_id)
        if record is None:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "workbench session was not found")
        document = self._load_artifact(record.document_hash)
        if (document.get("session_id"), document.get("state"), document.get("version")) != (
            record.session_id,
            record.state,
            record.version,
        ):
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                "session projection does not match the immutable record",
            )
        if document.get("contract") not in (SESSION_CONTRACT_VERSION, TEXT_SESSION_CONTRACT):
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "stored session contract is not supported"
            )
        stream = self.ledger.load_stream(session_id)
        if (
            not self.ledger.verify_chain(session_id).valid
            or not stream
            or stream[-1].payload_hash != record.document_hash
        ):
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION, "session projection is not bound to ledger head"
            )
        plan = self.builder.build(canonicalize(document["frozen_request"]))
        if (
            str(plan.execution_plan_hash) != document["execution_plan_hash"]
            or str(plan.plan_content_hash) != document["plan_content_hash"]
        ):
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "stored send plan does not reproduce"
            )
        if self._is_text_task(document):
            request = self._load_artifact(ContentHash.parse(document["request_artifact_hash"]))
            if document["provider_id"] == "chatgpt":
                request = logical_request(request)
            validate_task(
                document["task_kind"],
                document["output_format"],
                request.get("reference_text", ""),
                self.limits,
            )
            if (
                request.get("task_kind") != document["task_kind"]
                or request.get("output_format") != document["output_format"]
                or request.get("contract") != TEXT_REQUEST_CONTRACT
                or request.get("output_contract") != TEXT_OUTPUT_CONTRACT
                or document["task_kind"] == "file_edit"
                or document["target"] is not None
                or document["before_hash"] is not None
                or str(hash_bytes(request["reference_text"].encode("utf-8")))
                != document["reference_hash"]
            ):
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT, "text task projection disagrees"
                )
            if any(
                key in document
                for key in ("apply_execution_plan_hash", "apply_result", "effect_receipt_hash")
            ):
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT, "text artifact carries a file effect"
                )
        return document

    @staticmethod
    def _is_text_task(document: dict[str, Any]) -> bool:
        return document.get("contract") == TEXT_SESSION_CONTRACT

    @staticmethod
    def _send_scope(document: dict[str, Any]) -> str:
        if WorkbenchService._is_text_task(document):
            return "artifact-send:" + str(document["task_kind"])
        return "cli-send:" + str(document["target"])

    @staticmethod
    def _require_file_task(document: dict[str, Any]) -> None:
        if WorkbenchService._is_text_task(document):
            raise HarnessError(
                ErrorCode.PATH_OUTSIDE_CAPABILITY, "text artifacts cannot be applied to files"
            )

    @staticmethod
    def task_catalog() -> list[dict[str, str]]:
        return task_catalog()

    def result(self, session_id: str) -> dict[str, Any]:
        document = self.inspect(session_id)
        if (
            not self._is_text_task(document)
            or document["state"] != WorkbenchState.PROPOSAL_READY.value
        ):
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "a completed text artifact is required"
            )
        return {
            "session_id": session_id,
            "task_kind": document["task_kind"],
            "output_format": document["output_format"],
            "result_text": self._safe_text(self._proposal_bytes(document)),
            "artifact_hash": document["proposal_hash"],
            "artifact_size": document["proposal_size"],
            "provider_id": document["provider_id"],
            "model_id": document["model_id"],
            "reasoning_effort": document.get("reasoning_effort"),
            "generated_at": document["generated_at"],
            "verification": "FORMAT_AND_SCANNER_ONLY",
            "filename": "artifact-"
            + session_id
            + (".md" if document["output_format"] == "markdown" else ".txt"),
            "media_type": "text/markdown"
            if document["output_format"] == "markdown"
            else "text/plain",
        }

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in self.sessions.list_recent(limit):
            document = self._load_artifact(record.document_hash)
            rows.append(
                {
                    "session_id": record.session_id,
                    "state": record.state,
                    "version": record.version,
                    "created_at": document.get("created_at"),
                    "provider_id": document.get("provider_id"),
                    "model_id": document.get("model_id"),
                    "reasoning_effort": document.get("reasoning_effort"),
                    "target": document.get("target"),
                    "task_kind": document.get("task_kind", "file_edit"),
                    "output_format": document.get("output_format"),
                    "failure": document.get("failure"),
                    "handoff": document.get("handoff"),
                    "can_continue": document["state"] in CONTINUABLE_STATES,
                    "resumable": record.state
                    in {
                        WorkbenchState.DRAFTED.value,
                        WorkbenchState.SEND_APPROVED.value,
                        WorkbenchState.SEND_PREPARED.value,
                        WorkbenchState.PROPOSAL_READY.value,
                        WorkbenchState.APPLY_APPROVED.value,
                    },
                }
            )
        return rows

    def conversation_history(self, session_id: str) -> dict[str, Any]:
        """Display the full lineage, including pending proposals, without sending."""
        history, binding = self._collect_history(session_id, None, for_send=False)
        if history is None or binding is None:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "history could not be constructed")
        return {
            "history": history,
            "binding": binding,
            "can_continue": all(t["state"] in CONTINUABLE_STATES for t in history["turns"]),
            "note": (
                "このHarness内の会話と実際に保存された生成結果を引き継ぐ。"
                "CLI本体の非公開会話・思考過程は取り込まない。"
                "未適用の提案は未適用のまま示し、生成コードの試験は自動実行しない。"
            ),
        }

    def _collect_history(
        self,
        parent_session_id: str | None,
        conversation_id: str | None,
        *,
        for_send: bool = True,
        selection: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if parent_session_id is None and conversation_id is None:
            return None, None
        lineage: list[dict[str, Any]] = []
        seen: set[str] = set()
        current = parent_session_id
        while current is not None:
            if current in seen:
                raise HarnessError(
                    ErrorCode.EVENT_ORDER_VIOLATION, "history lineage contains a cycle"
                )
            seen.add(current)
            # Bound traversal independently of the final payload (no last-N history selection).
            if len(seen) > self.limits.max_request_bytes // 32:
                raise HarnessError(ErrorCode.CONTEXT_BUDGET_EXCEEDED, "full history is too large")
            turn = self.inspect(current)
            # History may survive a Harness update. Old approvals still check runtime identity;
            # verified historical data only needs the same isolated workspace.
            if turn["workspace_identity"] != self.workspace.identity():
                raise HarnessError(
                    ErrorCode.RUNTIME_SPEC_MISMATCH, "history belongs to another workspace"
                )
            if for_send and turn["state"] not in CONTINUABLE_STATES:
                raise HarnessError(
                    ErrorCode.UNRECONCILED_EFFECT_PRESENT,
                    "history contains an active, approved or unknown effect; "
                    "finish or reconcile it first",
                )
            lineage.append(turn)
            link = turn.get("handoff")
            current = link.get("parent_session_id") if isinstance(link, dict) else None
            if current is not None and not isinstance(current, str):
                raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "invalid history parent")
        lineage.reverse()
        if lineage:
            inherited = (lineage[0].get("handoff") or {}).get("conversation_id")
            if conversation_id is not None and conversation_id != inherited:
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                    "a continuation cannot replace its source conversation",
                )
            conversation_id = inherited
            for turn in lineage:
                if (turn.get("handoff") or {}).get("conversation_id") != conversation_id:
                    raise HarnessError(
                        ErrorCode.ARTIFACT_CONTENT_CONFLICT, "history sources disagree"
                    )
        messages: list[dict[str, Any]] = []
        local_hash: str | None = None
        if conversation_id is not None:
            if self.conversations is None:
                raise HarnessError(
                    ErrorCode.RUNTIME_SPEC_MISMATCH, "conversation reader is unavailable"
                )
            saved = self.conversations.read_history(
                conversation_id,
                maximum_bytes=MAX_VERIFIED_HISTORY_BYTES
                if selection is not None
                else self.limits.max_request_bytes,
            )
            messages = list(saved.messages)
            local_hash = str(saved.source_hash)
        turns: list[dict[str, Any]] = []
        verified_bytes = len(canonicalize(messages))
        for index, turn in enumerate(lineage, 1):
            verified = self._history_turn(turn, index)
            verified_bytes += len(canonicalize(verified))
            if verified_bytes > MAX_VERIFIED_HISTORY_BYTES:
                raise HarnessError(
                    ErrorCode.CONTEXT_BUDGET_EXCEEDED, "history verification limit exceeded"
                )
            turns.append(verified)
        receipt = None
        total_messages, total_turns = len(messages), len(turns)
        if selection is not None:
            messages, turns, receipt = select_history(messages, turns, selection)
        history: dict[str, Any] = {
            "contract": "workbench-conversation-history/2",
            "trust": "UNTRUSTED_REFERENCE_DATA",
            "local_messages": messages,
            "turns": turns,
            "omitted_messages": total_messages - len(messages),
        }
        if receipt is not None:
            history["omitted_turns"] = total_turns - len(turns)
            history["selection"] = {
                key: value for key, value in receipt.items() if key not in ("excluded",)
            }
            history["selection"]["excluded_count"] = len(receipt["excluded"])
        body = canonicalize(history)
        if len(body) > self.limits.max_request_bytes:
            raise HarnessError(
                ErrorCode.CONTEXT_BUDGET_EXCEEDED,
                "full history exceeds the request limit; start a separate conversation explicitly",
            )
        self._require_no_rejected_content(body, "CONVERSATION_HISTORY")
        binding: dict[str, Any] = {
            "parent_session_id": parent_session_id,
            "conversation_id": conversation_id,
            "history_hash": str(hash_bytes(body)),
            "source_binding_hash": str(
                hash_canonical(
                    {
                        "local_source_hash": local_hash,
                        "sessions": [
                            {
                                "session_id": t["session_id"],
                                "document_hash": str(hash_bytes(canonicalize(t))),
                            }
                            for t in lineage
                        ],
                    },
                    artifact_type="workbench-history-sources",
                    schema_major=1,
                )
            ),
            "local_message_count": len(messages),
            "turn_count": len(turns),
            "history_bytes": len(body),
        }
        if receipt is not None:
            binding.update(
                selection=selection,
                selection_receipt=receipt,
                all_sources_continuable=all(t["state"] in CONTINUABLE_STATES for t in lineage),
            )
        return history, binding

    def context_preview(
        self,
        *,
        parent_session_id: str | None,
        conversation_id: str | None,
        history_selection: dict[str, Any] | None,
    ) -> dict[str, Any]:
        selection = validate_selection(history_selection)
        history, binding = self._collect_history(
            parent_session_id,
            conversation_id,
            selection=selection,
            for_send=False,
        )
        return {
            "history": history,
            "binding": binding,
            "request_limit_bytes": self.limits.max_request_bytes,
            "token_count_assurance": "UNKNOWN",
            "can_continue": (
                binding.get("all_sources_continuable", True) if binding is not None else True
            ),
        }

    def _history_turn(self, document: dict[str, Any], sequence: int) -> dict[str, Any]:
        request_hash = ContentHash.parse(document["request_artifact_hash"])
        request = self._load_artifact(request_hash)
        if str(request_hash) != document["request_payload_hash"]:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "history request hash disagrees"
            )
        if document["provider_id"] == "chatgpt":
            request = logical_request(request)
        if self._is_text_task(document):
            reference = request.get("reference_text")
            if (
                not isinstance(reference, str)
                or str(hash_bytes(reference.encode("utf-8"))) != document["reference_hash"]
            ):
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT, "history reference bytes disagree"
                )
            return {
                "sequence": sequence,
                "task_kind": document["task_kind"],
                "provider_id": document["provider_id"],
                "model_id": document["model_id"],
                "reasoning_effort": document.get("reasoning_effort"),
                "instruction": document["instruction"],
                "reference_text": reference,
                "output_format": document["output_format"],
                "result_text": self._safe_text(self._proposal_bytes(document))
                if document.get("proposal_hash")
                else None,
                "target_relative_path": None,
                "replacement_text": None,
                "unified_diff": None,
                "state": document["state"],
                "proposal_applied": False,
                "failure": document.get("failure"),
                "verification": {
                    "artifact_check": "FORMAT_AND_SCANNER_ONLY",
                    "external_actions": "NOT_PERFORMED",
                },
            }
        original = request.get("current_file_text")
        if (
            not isinstance(original, str)
            or str(hash_bytes(original.encode("utf-8"))) != document["before_hash"]
        ):
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "history original bytes disagree"
            )
        proposal = None
        diff = None
        if document.get("proposal_hash") is not None:
            proposal = self._safe_text(self._proposal_bytes(document))
            diff = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    proposal.splitlines(keepends=True),
                    fromfile="a/" + document["target"],
                    tofile="b/" + document["target"],
                )
            )
        receipt_observation = None
        receipt_hash = document.get("effect_receipt_hash")
        if receipt_hash is not None:
            receipt = self._load_artifact(ContentHash.parse(receipt_hash))
            self.validate_record("EffectReceipt", receipt)
            if (
                receipt.get("effect_subject_id") != document["target"]
                or receipt.get("before_hash") != document["before_hash"]
                or receipt.get("expected_after_hash") != document.get("proposal_hash")
                or receipt.get("observed_hash") != document.get("proposal_hash")
            ):
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                    "history receipt does not match the proposal",
                )
            receipt_observation = {
                key: (
                    self._history_hash(receipt[key])
                    if key in ("before_hash", "expected_after_hash", "observed_hash")
                    else receipt.get(key)
                )
                for key in (
                    "before_hash",
                    "expected_after_hash",
                    "observed_hash",
                    "observation_method",
                    "confirmation_level",
                    "durability_level",
                )
            }
        elif document["state"] == WorkbenchState.APPLIED.value:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "applied history receipt is missing"
            )
        apply_result = document.get("apply_result")
        if apply_result is not None:
            if (
                not isinstance(apply_result, dict)
                or apply_result.get("observed_hash") != document.get("proposal_hash")
                or apply_result.get("expected_hash") != document.get("proposal_hash")
            ):
                raise HarnessError(
                    ErrorCode.ARTIFACT_CONTENT_CONFLICT, "history apply observation disagrees"
                )
            apply_result = dict(apply_result)
            for key in ("observed_hash", "expected_hash"):
                apply_result[key] = self._history_hash(apply_result[key])
        observation = document.get("send_observation")
        return {
            "sequence": sequence,
            "provider_id": document["provider_id"],
            "model_id": document["model_id"],
            "reasoning_effort": document.get("reasoning_effort"),
            "target_relative_path": document["target"],
            "instruction": document["instruction"],
            "current_file_text_at_request": original,
            "replacement_text": proposal,
            "unified_diff": diff,
            "state": document["state"],
            "proposal_applied": document["state"] == WorkbenchState.APPLIED.value,
            "failure": document.get("failure"),
            "send_observation": (
                {k: observation.get(k) for k in ("outcome", "exit_code", "descendant_cleanup")}
                if isinstance(observation, dict)
                else None
            ),
            "verification": {
                "apply_result": apply_result,
                "effect_receipt_hash": self._history_hash(receipt_hash) if receipt_hash else None,
                "receipt_observation": receipt_observation,
                "generated_code_tests": "NOT_RUN_BY_HARNESS",
            },
        }

    @staticmethod
    def _history_hash(value: Any) -> dict[str, Any]:
        # Only verified, Harness-owned observation hashes use this codec. User/model
        # messages, instructions, source code and diffs stay byte-for-byte scannable.
        if not isinstance(value, str):
            raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "invalid history hash")
        try:
            digest = ContentHash.parse(value)
        except ValueError:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "invalid history hash"
            ) from None
        return history_hash_projection(digest)

    def _require_handoff_current(self, document: dict[str, Any]) -> None:
        expected = document.get("handoff")
        if expected is None:
            return
        if not isinstance(expected, dict):
            raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "invalid stored handoff")
        _, observed = self._collect_history(
            expected.get("parent_session_id"),
            expected.get("conversation_id"),
            selection=validate_selection(expected.get("selection")),
        )
        if observed != expected:
            raise HarnessError(
                ErrorCode.APPROVAL_INVALIDATED,
                "conversation history changed after review; create and review a new request",
            )

    # -- 内部 ---------------------------------------------------------------

    def _record_completed(
        self,
        document: dict[str, Any],
        journal: CliInvocationJournal,
        stdout: bytes,
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        """Process の終了を観測できた場合だけ決定的に扱う。"""
        response_hash = hash_bytes(stdout)
        captured = journal.captured(response_hash)
        failure: dict[str, Any] | None = None
        proposal: CodeProposal | TextArtifact | None = None
        if (
            observation.get("transport") == "HTTPS_RESPONSES_SSE"
            and observation.get("terminal_event") != "response.completed"
        ):
            failure = {
                "class": observation.get("error_code") or "HTTP_GENERATION_FAILED",
                "detail": "ChatGPTの生成は完了しませんでした。自動再送は行いません。",
            }
        elif (
            observation.get("transport") != "HTTPS_RESPONSES_SSE" and observation["exit_code"] != 0
        ):
            failure = {"class": "NON_ZERO_EXIT", "detail": "CLI exited with a non-zero code"}
        else:
            try:
                proposal = (
                    self.text_response_parser(
                        document["provider_id"], stdout, self.limits.max_proposal_bytes
                    )
                    if self._is_text_task(document)
                    else self._parse_proposal(document["provider_id"], stdout)
                )
            except HarnessError as error:
                failure = {"class": "RESPONSE_CONTRACT_VIOLATION", "detail": str(error)}
        output_bytes = (
            proposal.content
            if isinstance(proposal, TextArtifact)
            else proposal.replacement
            if proposal is not None
            else b""
        )
        if proposal is not None:
            denial = self.masking.run(self._safe_text(output_bytes))
            if denial.rejected:
                failure = {
                    "class": "PROPOSAL_REJECTED_BY_SCANNER",
                    "detail": "categories: "
                    + ",".join(sorted(str(c) for c in denial.rejected_categories)),
                }
                proposal = None
        with self.uow.begin_immediate():
            self.invocations.update(captured, expected_store_version=journal.store_version)
            document["send_observation"] = observation
            document["response_hash"] = str(response_hash)
            self._release_completed_send_lease(document)
            if proposal is None:
                document["failure"] = failure
                self._save(document, WorkbenchState.SEND_FAILED, EventType.ACTION_FAILED)
                return document
            media_type = (
                "text/markdown" if document.get("output_format") == "markdown" else "text/plain"
            )
            document["proposal_hash"] = str(self._put(output_bytes, media_type))
            document["proposal_size"] = len(output_bytes)
            self._save(
                document, WorkbenchState.SEND_ATTEMPTED, EventType.REMOTE_INVOCATION_COMPLETED
            )
            self._save(document, WorkbenchState.SEND_ATTEMPTED, EventType.INPUT_ARTIFACT_CLASSIFIED)
            if self._is_text_task(document):
                document["generated_at"] = self.clock.now()
                self._save(
                    document, WorkbenchState.PROPOSAL_READY, EventType.INPUT_ARTIFACT_CLASSIFIED
                )
                return document
        return self._build_apply_plan(document)

    def _release_completed_send_lease(self, document: dict[str, Any]) -> None:
        """終了と子孫消滅を確認できた送信だけを解放する。呼出元がTransactionを所有。"""
        observation = document.get("send_observation", {})
        http_completed = (
            observation.get("transport") == "HTTPS_RESPONSES_SSE"
            and observation.get("outcome") == "COMPLETED"
            and observation.get("terminal_event")
            in (
                "response.completed",
                "response.failed",
                "response.incomplete",
                "error",
                "HTTP_REJECTED",
                "POLICY_REJECTED",
            )
        )
        cli_completed = (
            observation.get("transport") is None
            and observation.get("outcome") == CliOutcome.COMPLETED.value
            and observation.get("descendant_cleanup") == "CONFIRMED_EMPTY"
            and not observation.get("residual_pids")
        )
        if not (http_completed or cli_completed):
            raise HarnessError(ErrorCode.EFFECT_UNKNOWN, "send completion is not proven")
        self._require_invocation(document["session_id"], InvocationState.RESPONSE_CAPTURED)
        self._settle_lease(document, "send")

    def _settle_lease(self, document: dict[str, Any], kind: str) -> None:
        """完了を検証した呼出元だけが使用する、所有者・Token一致付き解放。"""
        lease = self.leases.get(document[kind + "_lease_id"])
        if (
            lease is None
            or lease.attempt_id != document[kind + "_attempt_id"]
            or lease.resource_key != document[kind + "_resource"]
            or lease.fencing_token != document[kind + "_fencing_token"]
        ):
            raise HarnessError(
                ErrorCode.STALE_FENCING_TOKEN, "completed operation lease does not match"
            )
        now = self.clock.now()
        if lease.status is LeaseStatus.RELEASED:
            document[kind + "_lease_settlement"] = "RELEASED"
        elif timestamp_seconds(now) >= timestamp_seconds(lease.expires_at):
            # 期限切れの古いTokenで後継Leaseを書き換えない。
            document[kind + "_lease_settlement"] = "EXPIRED"
        else:
            self.leases.release(lease, now=now)
            document[kind + "_lease_settlement"] = "RELEASED"

    def _record_unknown(
        self,
        document: dict[str, Any],
        journal: CliInvocationJournal,
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        unknown = journal.unknown()
        with self.uow.begin_immediate():
            self.invocations.update(unknown, expected_store_version=journal.store_version)
            document["send_observation"] = observation
            residual = observation.get("residual_pids") or []
            document["failure"] = {
                "class": "EFFECT_UNKNOWN",
                "detail": (
                    "生成の完了を観測できていない。送信済みか、利用枠を消費したかは"
                    "判定できない。自動再送・自動 Provider 切替は行わない。"
                )
                if not residual
                else (
                    "起動した Process Group に Process が残っている（PID: "
                    + ",".join(str(pid) for pid in residual)
                    + "）。動作範囲を保証できないので不明として閉じる。"
                    "残った Process は利用者が確認して停止する。"
                ),
            }
            self._save(document, WorkbenchState.SEND_UNKNOWN, EventType.REMOTE_INVOCATION_UNCERTAIN)
            self._save(document, WorkbenchState.SEND_UNKNOWN, EventType.EFFECT_UNKNOWN)
        return document

    def _build_apply_plan(self, document: dict[str, Any]) -> dict[str, Any]:
        """検査済み出力 Bytes に束縛した **別の** 適用 Plan を作る。"""
        session_id = document["session_id"]
        now = self.clock.now()
        expires_at = _plus_seconds(now, self.default_ttl_seconds)
        snapshot = self.workspace.snapshot()
        policy = self.policy_snapshot()
        build = PlanBuildInput(
            intent_hash=ContentHash.parse(document["instruction_hash"]),
            workspace_snapshot_hash=hash_canonical(
                snapshot, artifact_type="local-worktree-snapshot", schema_major=1
            ),
            input_read_evidence_hash=hash_canonical(
                {
                    "target_relative_path": document["target"],
                    "before_hash": document["before_hash"],
                    "observed_before_hash": snapshot.get(document["target"]),
                },
                artifact_type="cli-workbench-apply-evidence",
                schema_major=1,
            ),
            context_bundle_hash=ContentHash.parse(document["proposal_hash"]),
            policy_snapshot_hash=hash_canonical(
                {
                    "workbench_policy": policy,
                    "apply_policy": {
                        "mode": "FULL_FILE_REPLACEMENT",
                        "single_target": True,
                        "generated_code_execution": "NEVER",
                        "git_commit": "NEVER",
                    },
                    "runtime_identity": self.runtime_identity(),
                },
                artifact_type="cli-workbench-apply-policy",
                schema_major=1,
            ),
            token_profile_hash=hash_canonical(
                self.limits.projection(), artifact_type="cli-workbench-limits", schema_major=1
            ),
            schema_set_hash=self.schema_set_hash,
            capability_snapshot_hash=hash_canonical(
                {"write_scope": [document["target"]]},
                artifact_type="cli-workbench-write-scope",
                schema_major=1,
            ),
            entitlement_snapshot_hash=None,
            normalized_actions=(
                PlanAction(
                    "LOCAL_FILE_WRITE",
                    {
                        "expected_before_hash": document["before_hash"],
                        "replacement_hash": document["proposal_hash"],
                        "source_invocation_hash": document["response_hash"],
                        "source_execution_plan_hash": document["execution_plan_hash"],
                        "workspace_identity": document["workspace_identity"],
                    },
                    {"target_relative_path": document["target"]},
                ),
            ),
            runtime_envelope_spec_hash=ContentHash.parse(document["runtime_identity"]),
            invocation_manifest_hash=hash_canonical(
                {
                    "apply_writer": "harness-workspace-writer/1",
                    "durability": "STORAGE_SYNC",
                },
                artifact_type="cli-workbench-apply-manifest",
                schema_major=1,
            ),
            token_budget_policy_hash=hash_canonical(
                {"apply": "NO_PROVIDER_CALL"},
                artifact_type="cli-workbench-apply-budget",
                schema_major=1,
            ),
            control_data_policy_hash=hash_canonical(
                {"proposal_bytes_source": "SERVER_SIDE_CAS_ONLY"},
                artifact_type="cli-workbench-apply-control-data",
                schema_major=1,
            ),
            planner_algorithm_version=_PLANNER_VERSION,
        )
        authority = PlanAuthority(
            run_id=session_id,
            execution_plan_id=session_id + ":apply-plan",
            plan_version=1,
            issued_at=now,
            expires_at=expires_at,
            planner_identity=_PRODUCER,
            authority_scope="LOCAL_FILE_WRITE",
        )
        frozen = freeze_plan_request(build, authority)
        plan = self.builder.build(frozen)
        with self.uow.begin_immediate():
            document.update(
                apply_frozen_request=json.loads(frozen),
                apply_plan_content_hash=str(plan.plan_content_hash),
                apply_execution_plan_hash=str(plan.execution_plan_hash),
                apply_expires_at=expires_at,
                apply_snapshot=snapshot,
            )
            self._save(document, WorkbenchState.PROPOSAL_READY, EventType.PLAN_RESOLVED)
        return document

    def _complete_apply(
        self, document: dict[str, Any], journal: OperationJournal, observed: ContentHash
    ) -> dict[str, Any]:
        if observed != journal.expected_after_hash:
            raise HarnessError(ErrorCode.EFFECT_UNKNOWN, "apply effect could not be reconciled")
        if self.leases.current_fencing_token(document["apply_resource"]) != journal.fencing_token:
            raise HarnessError(
                ErrorCode.STALE_FENCING_TOKEN, "receipt requires the current fencing token"
            )
        if journal.state is EffectState.EXECUTION_ATTEMPTED:
            observed_journal = journal.mark_observed(
                observed_hash=observed, observation_method="FD_HASH_READ", at=self.clock.now()
            )
            with self.uow.begin_immediate():
                self.journals.update(observed_journal, expected_store_version=journal.store_version)
                self._save(document, WorkbenchState.APPLY_ATTEMPTED, EventType.EFFECT_OBSERVED)
        elif journal.state in (EffectState.EFFECT_OBSERVED, EffectState.RECEIPT_DURABLE):
            observed_journal = journal
        else:
            raise HarnessError(
                ErrorCode.UNRECONCILED_EFFECT_PRESENT, "apply journal cannot be reconciled"
            )
        receipt = self._receipt(document, journal, observed)
        if journal.state is not EffectState.RECEIPT_DURABLE:
            with self.uow.begin_immediate():
                document["effect_receipt_hash"] = str(
                    self._put(canonicalize(receipt), "application/json")
                )
                stored = observed_journal.store_receipt(receipt["receipt_id"])
                self.journals.update(stored, expected_store_version=observed_journal.store_version)
                self._save(
                    document, WorkbenchState.APPLY_ATTEMPTED, EventType.EFFECT_RECEIPT_STORED
                )
        attempt = self.attempts.get(document["apply_attempt_id"])
        if attempt is None:
            raise HarnessError(ErrorCode.UNRECONCILED_EFFECT_PRESENT, "apply attempt missing")
        after_snapshot = self.workspace.snapshot()
        expected_snapshot = dict(document["snapshot"])
        expected_snapshot[document["target"]] = document["proposal_hash"]
        with self.uow.begin_immediate():
            stored_attempt = attempt.with_receipt(receipt["receipt_id"])
            self.attempts.update(stored_attempt, expected_store_version=attempt.store_version)
            self.attempts.update(
                stored_attempt.succeed(self.clock.now()),
                expected_store_version=stored_attempt.store_version,
            )
            document["apply_result"] = {
                "observed_hash": str(observed),
                "expected_hash": document["proposal_hash"],
                "target_matches": after_snapshot.get(document["target"])
                == document["proposal_hash"],
                "no_other_file_changed": after_snapshot == expected_snapshot,
                "ledger_chain_valid": self.ledger.verify_chain(document["session_id"]).valid,
                "quality_assurance": (
                    "この適用は Bytes と Receipt の一致だけを保証する。"
                    "生成コードの機能・品質・安全性は保証しない。"
                    "Git commit も push も Release も行っていない。"
                ),
            }
            self._settle_lease(document, "apply")
            self._save(document, WorkbenchState.APPLIED, EventType.ACTION_COMMITTED)
        return document

    def _receipt(
        self, document: dict[str, Any], journal: OperationJournal, observed: ContentHash
    ) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "receipt_id": document["session_id"] + ":apply-receipt",
            "effect_id": journal.effect_id,
            "operation_journal_id": journal.operation_journal_id,
            "run_id": journal.run_id,
            "action_id": journal.action_id,
            "attempt_id": journal.attempt_id,
            "effect_type": "WORKSPACE_WRITE",
            "effect_subject_type": "WORKSPACE_FILE",
            "effect_subject_id": document["target"],
            "target_resource_identity": journal.target_resource_identity,
            "before_hash": str(journal.before_hash),
            "expected_after_hash": str(journal.expected_after_hash),
            "observed_hash": str(observed),
            "observation_method": "FD_HASH_READ",
            "confirmation_level": "LOCAL_OBSERVED",
            "fencing_token": journal.fencing_token,
            "prepared_event_id": journal.prepared_event_id,
            "execution_attempted_event_id": journal.execution_attempted_event_id,
            "observed_at": self.clock.now(),
            "durability_level": "STORAGE_SYNC",
        }
        receipt.update(
            schema_name="EffectReceipt",
            schema_version="1.0.0",
            record_id=receipt["receipt_id"],
            created_at=self.clock.now(),
            producer=_PRODUCER,
        )
        receipt["receipt_hash"] = str(
            hash_canonical(receipt, artifact_type="effect-receipt", schema_major=1)
        )
        receipt["content_hash"] = str(
            hash_canonical(receipt, artifact_type="effect-receipt-record", schema_major=1)
        )
        self.validate_record("EffectReceipt", receipt)
        return receipt

    def _parse_proposal(self, provider_id: str, stdout: bytes) -> CodeProposal:
        # 外枠 Parser は Port として注入される。Application は具象を import しない。
        return self.response_parser(provider_id, stdout, self.limits.max_proposal_bytes)

    def _proposal_bytes(self, document: dict[str, Any]) -> bytes:
        digest = ContentHash.parse(document["proposal_hash"])
        self.artifacts.verify(digest).raise_if_repair_required()
        payload = self.artifacts.get(digest)
        if hash_bytes(payload) != digest:
            raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "stored proposal changed")
        return payload

    def _read_eligible(self, relative_path: str) -> tuple[bytes, dict[str, str]]:
        decision = classify_target(relative_path)
        if decision.denied:
            raise HarnessError(
                ErrorCode.PATH_OUTSIDE_CAPABILITY, f"target rejected: {decision.reason}"
            )
        snapshot = self.workspace.snapshot()
        if relative_path not in snapshot:
            raise HarnessError(
                ErrorCode.PATH_OUTSIDE_CAPABILITY, "target must already exist in the worktree"
            )
        payload = self.workspace.read(relative_path)
        if len(payload) > self.limits.max_source_bytes:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "target exceeds the declared size limit"
            )
        return payload, snapshot

    @staticmethod
    def _decode(payload: bytes) -> str:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "target is not valid UTF-8"
            ) from None
        if "\x00" in text:
            raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "target contains NUL")
        return text

    @staticmethod
    def _safe_text(payload: bytes) -> str:
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "proposal is not valid UTF-8"
            ) from None

    def _require_no_rejected_content(self, payload: bytes, stage: str) -> None:
        """決定論スキャナ。**原文も一致箇所も持ち出さず、分類名だけを返す。**"""
        report = self.masking.run(self._safe_text(payload))
        if report.rejected:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"{stage} rejected by the deterministic scanner: "
                + ",".join(sorted(str(c) for c in report.rejected_categories)),
            )

    def _require_state(self, document: dict[str, Any], expected: WorkbenchState) -> None:
        if document["state"] != expected.value:
            raise HarnessError(
                ErrorCode.APPROVAL_REQUIRED,
                f"operation requires state {expected.value}, session is {document['state']}",
            )

    def _resolve_launch(
        self, provider_id: str, model_id: str, reasoning_effort: str | None
    ) -> CliLaunchSpec | HttpGenerationSpec:
        if provider_id == "chatgpt":
            if self.chatgpt is None:
                raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "ChatGPT transport unavailable")
            return self.chatgpt.resolve(
                provider_id=provider_id, model_id=model_id, reasoning_effort=reasoning_effort
            )
        if reasoning_effort is None:
            return self.profiles.resolve(provider_id=provider_id, model_id=model_id)
        return self.profiles.resolve(
            provider_id=provider_id, model_id=model_id, reasoning_effort=reasoning_effort
        )

    def _verify_launch(self, spec: CliLaunchSpec | HttpGenerationSpec) -> None:
        if isinstance(spec, HttpGenerationSpec):
            if self.chatgpt is None:
                raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "ChatGPT transport unavailable")
            self.chatgpt.verify(spec)
        else:
            self.profiles.verify(spec)

    def _attest_launch(self, spec: CliLaunchSpec | HttpGenerationSpec) -> Any:
        if isinstance(spec, HttpGenerationSpec):
            if self.chatgpt is None:
                raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "ChatGPT transport unavailable")
            return self.chatgpt.attest(spec)
        return self.profiles.attest(spec)

    def _require_send_preconditions(self, document: dict[str, Any]) -> None:
        self._require_handoff_current(document)
        self._require_time(document["expires_at"])
        self._require_workspace(document)
        spec = self._resolve_launch(
            document["provider_id"], document["model_id"], document.get("reasoning_effort")
        )
        self._verify_launch(spec)
        if str(spec.runtime_hash) != document["runtime_hash"]:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI runtime changed since the send plan"
            )
        if self.workspace.snapshot() != document["snapshot"]:
            raise HarnessError(
                ErrorCode.APPROVAL_INVALIDATED, "worktree changed since the send plan"
            )

    def _require_apply_preconditions(self, document: dict[str, Any]) -> None:
        self._require_time(document["apply_expires_at"])
        self._require_workspace(document)
        observed = hash_bytes(self.workspace.read(document["target"]))
        if str(observed) != document["before_hash"]:
            raise HarnessError(
                ErrorCode.APPROVAL_INVALIDATED, "target bytes changed after the proposal"
            )

    def _require_workspace(self, document: dict[str, Any]) -> None:
        if (
            self.workspace.identity() != document["workspace_identity"]
            or self.runtime_identity() != document["runtime_identity"]
        ):
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "workspace or harness runtime changed since plan"
            )

    def _require_spawn_identity(
        self, document: dict[str, Any], spec: CliLaunchSpec | HttpGenerationSpec, payload: bytes
    ) -> None:
        """spawn 直前の再照合。**Application の確認だけで通さない。**"""
        self._verify_launch(spec)
        # 制限が効いていることを **もう一度測り**、承認時と同じであることを見る。
        # 承認から起動までのあいだに境界が緩んでいたら、ここで止まる。
        current = dict(self._attest_launch(spec))
        if current != document.get("restriction_attestation"):
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "pre-execution restrictions changed since the approval",
            )
        if str(spec.runtime_hash) != document["runtime_hash"]:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI runtime changed before spawn")
        if str(hash_bytes(payload)) != document["request_payload_hash"]:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT, "request payload changed before spawn"
            )
        if spec.provider_id != document["provider_id"] or spec.model_id != document["model_id"]:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "resolved provider or model does not match"
            )
        self._require_time(document["expires_at"])

    def _require_invocation(
        self, session_id: str, expected: InvocationState
    ) -> CliInvocationJournal:
        journal = self.invocations.get_by_session(session_id)
        if journal is None or journal.state is not expected:
            raise HarnessError(
                ErrorCode.EVENT_ORDER_VIOLATION,
                "CLI invocation journal is not in the required durable state",
            )
        return journal

    def _require_time(self, expires_at: str) -> None:
        if timestamp_seconds(self.clock.now()) >= timestamp_seconds(expires_at):
            raise HarnessError(ErrorCode.CLOCK_SKEW_EXCEEDED, "plan has expired")

    def _put(self, payload: bytes, media_type: str) -> ContentHash:
        digest = hash_bytes(payload)
        self.artifacts.put(
            payload,
            ArtifactMetadata(media_type, len(payload), "INTERNAL", "UNTRUSTED_INPUT"),
            artifact_id=str(digest),
            stored_at=self.clock.now(),
        )
        return digest

    def _load_artifact(self, digest: ContentHash) -> dict[str, Any]:
        self.artifacts.verify(digest).raise_if_repair_required()
        payload = self.artifacts.get(digest)
        if hash_bytes(payload) != digest:
            raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "stored artifact changed")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise HarnessError(ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "record must be an object")
        return result

    def _save(self, document: dict[str, Any], state: WorkbenchState, event: EventType) -> None:
        previous = int(document["version"])
        current = parse_state(document["state"])
        if current is not state:
            require_transition(current, state)
        document.update(state=state.value, version=previous + 1)
        digest = self._put(canonicalize(document), "application/json")
        self.sessions.save(
            WorkbenchRecord(document["session_id"], state.value, previous + 1, digest),
            expected_version=previous,
        )
        self.ledger.append(
            [NewEvent(document["session_id"], event.value, digest, self.clock.now())],
            expected_stream_sequence=self.ledger.stream_head(document["session_id"]),
        )


def _plus_seconds(now: str, seconds: int) -> str:
    """`now` を基準に期限を作る。**Plan Content へは入らない Authority 側の値である。**

    暦計算は `domain/timestamps.py` の正本へ委ねる。ここで独自に組み立てない。
    """
    return timestamp_from_seconds(timestamp_seconds(now) + seconds)
