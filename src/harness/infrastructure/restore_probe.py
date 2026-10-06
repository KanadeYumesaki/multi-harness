"""合成のPort接続試験。これ単独では本番Restore保護の証跡にならない。"""

from harness.application.effect_orchestrator import EffectOrchestrator, EffectRequest
from harness.domain.policy_freshness import EffectKind


class _NullLedger:
    """合成Probe専用。予期しないLedger追記を拒否して試験失敗にする。"""

    def stream_head(self, stream_id: str) -> int:
        return 0

    def append(self, events: object, *, expected_stream_sequence: int) -> None:
        raise AssertionError("probe must not append to a ledger")


def restore_window_probe_request() -> EffectRequest:
    """Restore 区間で試す作用要求。鮮度は通し、**Portで止まることを見る。**"""
    return EffectRequest(
        attempt_id="restore-window-probe",
        stream_id="restore-window-probe",
        effect_kind=EffectKind.EXTERNAL_SEND,
        # 十分に先の期限にして鮮度で止めない。止めるのはRestore Guardである。
        policy_expires_at="2999-01-01T00:00:00Z",
        endpoint="https://example.invalid/probe",
        payload=b"restore-window-probe",
    )


def build_restore_window_probe(guard: object) -> EffectOrchestrator:
    """実装クラスと合成Guardの接続だけを確認する部品試験。

    本番の通常PortやDB受付制御の保護を実証するものではない。
    本番の復元中拒否は、同じDBを使う通常入口から別途検証する。
    """
    return EffectOrchestrator(
        clock=_ProbeClock(),
        ledger=_NullLedger(),  # type: ignore[arg-type]
        external_send=guard,  # type: ignore[arg-type]
        process_launch=guard,  # type: ignore[arg-type]
        workspace_write=guard,  # type: ignore[arg-type]
        paid_execution=guard,  # type: ignore[arg-type]
    )


class _ProbeClock:
    def now(self) -> str:
        return "2026-01-01T00:00:00Z"
