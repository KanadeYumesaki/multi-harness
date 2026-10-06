"""## Case Adapter はここではない

本 File が持っていた `@pytest.mark.case` は
`tests/integration/sqlite/test_approval_plan_orchestrator.py` へ移した。
Case は Event 列まで要求するが、本 File は Ledger を観測しない。
Ledger を観測しない試験を Adapter にすると「State だけ一致した Case」になる。

本 File の試験は消していない。Domain／Repository の振る舞いは引き続き検証する。
"""

from __future__ import annotations

import pytest

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.faults import (
    FaultInjector,
    FaultMode,
    FaultPlan,
    FaultPoint,
    InjectedFault,
)

pytestmark = pytest.mark.unit


def test_fault_injection_is_denied_when_policy_disallows_it() -> None:
    injector = FaultInjector(
        FaultPlan(points={FaultPoint.BEFORE_ACTION_PREPARED: FaultMode.EXCEPTION}),
        permitted=False,
    )

    with pytest.raises(HarnessError) as error:
        injector.maybe_fault(FaultPoint.BEFORE_ACTION_PREPARED)
    assert error.value.code is ErrorCode.FAULT_INJECTION_NOT_PERMITTED


def test_fault_injection_is_noop_for_unconfigured_point_and_raises_only_configured_exception() -> (
    None
):
    injector = FaultInjector(
        FaultPlan(points={FaultPoint.BEFORE_ACTION_PREPARED: FaultMode.EXCEPTION}),
        permitted=True,
    )
    injector.maybe_fault(FaultPoint.AFTER_ACTION_PREPARED)

    with pytest.raises(InjectedFault):
        injector.maybe_fault(FaultPoint.BEFORE_ACTION_PREPARED)
