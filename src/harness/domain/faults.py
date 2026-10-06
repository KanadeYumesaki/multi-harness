"""MVP0-A Fault Injectionの純粋Domain制御。

実プロセス停止・ENOSPC・EIOの注入はInfrastructure側で実装する。ここでは注入計画が
Policyで明示的に許可されない限り、一切のFaultを発火しないことを保証する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from harness.domain.errors import ErrorCode, HarnessError

__all__ = ["FaultInjector", "FaultMode", "FaultPlan", "FaultPoint", "InjectedFault"]


class FaultPoint(Enum):
    BEFORE_ACTION_PREPARED = "BEFORE_ACTION_PREPARED"
    AFTER_ACTION_PREPARED = "AFTER_ACTION_PREPARED"
    BEFORE_EXECUTION_ATTEMPTED = "BEFORE_EXECUTION_ATTEMPTED"
    AFTER_EXECUTION_ATTEMPTED = "AFTER_EXECUTION_ATTEMPTED"
    AFTER_TEMP_WRITE_BEFORE_FSYNC = "AFTER_TEMP_WRITE_BEFORE_FSYNC"
    AFTER_ATOMIC_REPLACE_BEFORE_OBSERVE = "AFTER_ATOMIC_REPLACE_BEFORE_OBSERVE"
    AFTER_EFFECT_OBSERVED = "AFTER_EFFECT_OBSERVED"
    BEFORE_RECEIPT_STORE = "BEFORE_RECEIPT_STORE"
    AFTER_RECEIPT_STORE_BEFORE_COMMIT = "AFTER_RECEIPT_STORE_BEFORE_COMMIT"
    DURING_LEDGER_APPEND = "DURING_LEDGER_APPEND"


class FaultMode(Enum):
    EXCEPTION = "EXCEPTION"
    CRASH = "CRASH"
    DELAY = "DELAY"
    DISK_FULL = "DISK_FULL"
    IO_ERROR = "IO_ERROR"


@dataclass(frozen=True, slots=True)
class FaultPlan:
    points: Mapping[FaultPoint, FaultMode]

    def mode_for(self, point: FaultPoint) -> FaultMode | None:
        return self.points.get(point)


class InjectedFault(RuntimeError):
    def __init__(self, point: FaultPoint, mode: FaultMode) -> None:
        self.point = point
        self.mode = mode
        super().__init__(f"fault injected at {point.value} ({mode.value})")


@dataclass(frozen=True, slots=True)
class FaultInjector:
    plan: FaultPlan
    permitted: bool

    def maybe_fault(self, point: FaultPoint) -> None:
        mode = self.plan.mode_for(point)
        if mode is None:
            return
        if not self.permitted:
            raise HarnessError(
                ErrorCode.FAULT_INJECTION_NOT_PERMITTED,
                "fault injection is disabled by the policy snapshot",
            )
        if mode is FaultMode.EXCEPTION:
            raise InjectedFault(point, mode)
        raise HarnessError(
            ErrorCode.STORAGE_WRITE_FAILED,
            "this runtime does not permit non-exception fault injection without an I/O adapter",
        )
