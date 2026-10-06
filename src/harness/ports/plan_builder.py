"""Frozen Plan入力から検証済みPlanを返す境界。"""

from typing import Protocol

from harness.domain.plan import ExecutionPlan


class PlanBuilderPort(Protocol):
    def build(self, frozen_request: bytes) -> ExecutionPlan: ...
