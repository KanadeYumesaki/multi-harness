"""§15.5 Runtime Attestation。解決したSpecと実測Attestationの不一致を検出する。

## 何を防ぐ規則か

Planは「このProvider設定で実行する」という前提のもとに承認される。
実行直前の実測（Attestation）がその前提と違うなら、**承認された内容とは
別のものを実行しようとしている**。

Config Drift は攻撃でなくても起きる。Providerが既定Modelを変えた、
環境変数が別の値になっていた、といった日常的な理由で起きる。
だからこそ、実行のたびに測って突き合わせる。

## Effect開始「前」に止める

不一致の検出は `RUNTIME_ATTESTATION` の時点で行う。Effectを1つでも
起こしてから気付いても、起きたことは取り消せない。

## Retry／Fallback／Queue へ流さない

Driftは再試行で直らない。同じ設定で測れば同じ不一致が出る。
別Providerへ Fallback するのは**さらに承認から遠ざかる**。
Queueへ溜めれば、承認と違う設定の実行が遅れて起きるだけである。

`RUNTIME_SPEC_MISMATCH` は `auto_reexecution_prohibited: true` の Case であり、
自動での作り直しを禁じる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness.domain._registry_generated import ErrorCode, EventType
from harness.domain.hashing import ContentHash

__all__ = [
    "ConfigDriftVerdict",
    "RuntimeAttestation",
    "detect_config_drift",
]

_BLOCKED_POLICY: Final[str] = "BLOCKED_POLICY"
_RUNTIME_VERIFIED: Final[str] = "RUNTIME_VERIFIED"

# 不一致検出時のEvent列（§19.1 `AT-CONFIG-001/DRIFT` の期待列）。
_DRIFT_EVENTS: Final[tuple[EventType, ...]] = (
    EventType.RUNTIME_SPEC_RESOLVED,
    EventType.RUNTIME_ATTESTED,
    EventType.RUNTIME_SPEC_MISMATCH,
    EventType.ACTION_BLOCKED,
)

# 一致時のEvent列。止めていないので `ACTION_BLOCKED` は出さない。
_VERIFIED_EVENTS: Final[tuple[EventType, ...]] = (
    EventType.RUNTIME_SPEC_RESOLVED,
    EventType.RUNTIME_ATTESTED,
)


@dataclass(frozen=True, slots=True)
class RuntimeAttestation:
    """Planが前提としたSpecと、実行直前に実測したAttestation。"""

    attempt_id: str
    #: Planへ束縛された、承認時点のRuntime Spec Hash。
    resolved_spec_hash: ContentHash
    #: 実行直前に実測したHash。
    attested_spec_hash: ContentHash


@dataclass(frozen=True, slots=True)
class ConfigDriftVerdict:
    """Drift検出の結果。"""

    attempt_id: str
    state: str
    error_code: ErrorCode | None
    events: tuple[EventType, ...]
    #: 再試行してよいか。Driftは再試行で直らないので常に False。
    auto_reexecution_allowed: bool
    #: 別Providerへ切り替えてよいか。承認から遠ざかるので常に False。
    fallback_allowed: bool

    @property
    def drifted(self) -> bool:
        return self.error_code is not None


def detect_config_drift(attestation: RuntimeAttestation) -> ConfigDriftVerdict:
    """解決Specと実測Attestationを突き合わせる。

    一致しなければ `BLOCKED_POLICY` で止める。**Effectは1つも起こさない。**
    """
    if attestation.resolved_spec_hash == attestation.attested_spec_hash:
        return ConfigDriftVerdict(
            attempt_id=attestation.attempt_id,
            state=_RUNTIME_VERIFIED,
            error_code=None,
            events=_VERIFIED_EVENTS,
            # 一致した場合も、再試行やFallbackはこのGateが許可するものではない。
            auto_reexecution_allowed=False,
            fallback_allowed=False,
        )

    return ConfigDriftVerdict(
        attempt_id=attestation.attempt_id,
        state=_BLOCKED_POLICY,
        error_code=ErrorCode.RUNTIME_SPEC_MISMATCH,
        events=_DRIFT_EVENTS,
        # 同じ設定で測れば同じ不一致が出る。再試行では直らない。
        auto_reexecution_allowed=False,
        # 別Providerへ切り替えると、承認された内容からさらに遠ざかる。
        fallback_allowed=False,
    )
