"""別PYTHONHASHSEEDで同じFrozen Inputを再Buildし、成功前に照合する。"""

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.plan import ExecutionPlan, build_execution_plan
from harness.domain.plan_build_payload import read_plan_request

_MAX_REQUEST_BYTES = 16 * 1024 * 1024


def _fingerprint(plan: ExecutionPlan) -> bytes:
    return (
        str(plan.plan_content_hash)
        + " "
        + str(plan.execution_plan_hash)
        + " "
        + str(plan.content_hash)
    ).encode("ascii")


@dataclass(frozen=True)
class VerifiedPlanBuilder:
    timeout_seconds: float = 15.0

    def build(self, frozen_request: bytes) -> ExecutionPlan:
        if len(frozen_request) > _MAX_REQUEST_BYTES or not 0 < self.timeout_seconds <= 60:
            raise HarnessError(ErrorCode.PLAN_NONDETERMINISTIC, "plan verification limits exceeded")
        build_input, authority = read_plan_request(frozen_request)
        plan = build_execution_plan(build_input, authority)
        seed = "1" if os.environ.get("PYTHONHASHSEED") == "0" else "0"
        source = Path(__file__).resolve().parents[3]
        environment = {"PATH": os.defpath, "PYTHONPATH": str(source), "PYTHONHASHSEED": seed}
        try:
            child = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable,
                    "-B",
                    "-s",
                    "-m",
                    "harness.infrastructure.planning.verified_builder",
                ],
                input=frozen_request,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                cwd=source,
                env=environment,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise HarnessError(
                ErrorCode.PLAN_NONDETERMINISTIC, "plan verification process failed"
            ) from None
        if child.returncode != 0 or child.stdout != _fingerprint(plan):
            # 子の出力・stderrや入力本文を診断へ転載しない。
            raise HarnessError(
                ErrorCode.PLAN_NONDETERMINISTIC, "cross-process plan verification mismatch"
            )
        return plan


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
        if len(payload) > _MAX_REQUEST_BYTES:
            return 2
        build_input, authority = read_plan_request(payload)
        sys.stdout.buffer.write(_fingerprint(build_execution_plan(build_input, authority)))
    except (ValueError, TypeError, KeyError, HarnessError, UnicodeError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
