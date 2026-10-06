"""Capture whitelisted input-read metadata; never serialize payload bytes."""

from __future__ import annotations

from approval_observation import ApprovalEffectProbe

from harness.domain.hashing import hash_canonical
from harness.ports.task_intake import InputReadOutcome, InputReadRequest
from input_read_case_evidence import INPUT_READ_RESULT_CLASS, validate_input_read


class InputReadEffectProbe(ApprovalEffectProbe):
    """Reuse tested Python I/O probes and add CAS/Mock Masker attempts.

    The monitor is installed around the SUT, after fixture/namespace setup.
    It is not an OS sandbox and does not observe arbitrary native code.
    """

    def __init__(self):
        super().__init__()
        self.artifact_put_attempts = 0
        self.masker_invocations = 0

    def install(self, monkeypatch):
        from harness.infrastructure.artifact.store import ArtifactStore

        # Resolve libc before the measured read window. ctypes may invoke ldconfig
        # on first load; this setup is not claimed as a zero-process cold start.
        from harness.infrastructure.filesystem.openat2 import _load_libc
        from harness.infrastructure.masking.mock_masker import StaticPhraseMasker

        _load_libc()
        super().install(monkeypatch)

        def deny(field):
            def called(*args, **kwargs):
                setattr(self, field, getattr(self, field) + 1)
                raise RuntimeError("INPUT_EFFECT_ATTEMPT: " + field)

            return called

        monkeypatch.setattr(ArtifactStore, "put", deny("artifact_put_attempts"))
        monkeypatch.setattr(StaticPhraseMasker, "propose_spans", deny("masker_invocations"))


def capture_input_read(result, request, ledger, effects, *, head_before):
    if type(result) is not InputReadOutcome or type(request) is not InputReadRequest:
        raise ValueError("INPUT_RESULT_TYPE_NOT_OBSERVED")
    typed = {
        "result_class": INPUT_READ_RESULT_CLASS,
        "subject_type": "INPUT_READ_DECISION",
        "subject_id": result.read_decision_id,
        "state": result.state,
        "error_code": result.error_code.value if result.error_code else None,
        "durability_observation": "NOT_APPLICABLE",
        "request_binding": {
            "subject_id": request.read_decision_id,
            "stream_id": request.stream_id,
            "capability_path_hash": str(
                hash_canonical(
                    {
                        "capability_id": request.capability_id,
                        "relative_path": request.relative_path,
                    },
                    artifact_type="input-read-subject",
                    schema_major=1,
                )
            ),
        },
        "capability_path_hash": str(result.capability_path_hash),
        "payload_present": result.payload is not None,
        "read_evidence_present": result.read_evidence is not None,
        "classification_present": result.classification is not None,
        "masking_denial_present": result.masking_denial is not None,
        "masker_invocations": result.masking_denial.masker_invocation_count
        if result.masking_denial
        else None,
        "result_events": [e.value for e in result.events],
        "result_head_before": result.ledger_head_before,
        "result_head_after": result.ledger_head_after,
        "ledger": {
            "head_before": head_before,
            "head_after": ledger.stream_head(request.stream_id),
            "chain_valid": ledger.verify_chain(request.stream_id).valid,
            "records": [
                {
                    "stream_id": e.stream_id,
                    "event_type": e.event_type,
                    "payload_hash": str(e.payload_hash),
                }
                for e in ledger.load_stream(request.stream_id, after_sequence=head_before)
            ],
        },
        "effects": {
            name: getattr(effects, name)
            for name in (
                "network_calls",
                "process_launches",
                "workspace_commits",
                "external_effects",
                "artifact_put_attempts",
                "masker_invocations",
            )
        },
    }
    validate_input_read(typed)
    return typed
