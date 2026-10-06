"""Case観測をpytestの外へ出す。

`case_observation` fixtureを取った試験は、その試験が観測した値を記録する。
環境変数 `CASE_OBSERVATION_SINK` が指すFileがあれば、記録をJSON Linesで追記する。

Sinkが無いとき（通常のpytest実行）は何も書かない。**試験の意味は変えない。**
記録は副産物であり、assertの代わりではない。

## なぜ「実行した試験がPASSした」だけでEvidenceにしないか

pytestのPASSは「assertが通った」であって、「observed値が何だったか」を持たない。
PASSだけをEvidenceへ変換すると、Registryの期待値との突合が
**試験の中で一度行われただけ**になり、Evidence側では検証不能な主張になる。

だからここでは観測値を運び、突合は `tools/emit_case_evidence.py` が
Registryとの間でもう一度行う。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from case_observation import CaseObservation

_SINK_ENV = "CASE_OBSERVATION_SINK"


def _case_id_for(request: pytest.FixtureRequest) -> str:
    marker = request.node.get_closest_marker("case")
    if marker is None or not marker.args:
        raise pytest.UsageError(
            f"{request.node.nodeid}: case_observation を使う試験は "
            "@pytest.mark.case('AT-XXX-001/CASE_ID') を持つこと"
        )
    return str(marker.args[0])


@pytest.fixture
def case_observation(request: pytest.FixtureRequest) -> Any:
    """観測記録。試験終了時にSinkがあれば追記する。"""
    observation = CaseObservation(case_id=_case_id_for(request), node_id=request.node.nodeid)
    yield observation

    sink = os.environ.get(_SINK_ENV)
    if not sink:
        return
    path = Path(sink)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(observation.as_dict(), ensure_ascii=False) + "\n")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit_subject(name, durability): measured Unit SUT contract")


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> Any:
    item = pyfuncitem
    from unit_execution_monitor import UnitExecutionMonitor

    observation = getattr(item, "funcargs", {}).get("case_observation")
    subject = item.get_closest_marker("unit_subject")
    if observation is None or subject is None:
        yield
        return
    if len(subject.args) != 1 or subject.kwargs.get("durability") != "NOT_APPLICABLE":
        raise pytest.UsageError("explicit Unit Subject and durability contract required")
    scratch = getattr(item, "funcargs", {}).get("tmp_path")
    monitor = UnitExecutionMonitor((scratch,) if isinstance(scratch, Path) else ())
    monitor.start()
    try:
        outcome = yield
    finally:
        report = monitor.finish()
        report["subject_type"] = subject.args[0]
        report["durability_observation"] = subject.kwargs["durability"]
        observation.unit_execution = report
        # Replace any manually supplied counters with observations of this call.
        observation.side_effects = None
        if report["complete"]:
            observation.observe_side_effects(**report["counts"])
    report["assertions"] = [
        {
            "expression": "pytest test call completed without an exception",
            "result": outcome.excinfo is None,
        }
    ]
