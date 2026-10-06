"""Masking の Event 列と観測 Policy を機械で固定する（設計書 v1.18 §1.16.2.3）。

## なぜこの試験が要るか

19件の Case は `REQUIRED_EMPTY` を宣言しながら、正本 Ledger ではなく偽の
`LedgerProbe` を観測していた。宣言と観測がずれていても、どちらも「空」だった
ために誰も気づかなかった。**見ていないことと、見て0件だったことが同じ形に
なる**のが原因である。

Owner Decision（`MASK-EVT-1`／`MASK-EVT-2`）で契約は決まった。決まった契約は、
次に同じずれが起きたときに落ちる形で置いておかないと、また静かにずれる。

## 何を確かめるか

| # | 規則 |
|---|---|
| 1 | Unit Case が `MaskingService` を駆動しない |
| 2 | Unit Case が Ledger Port を使わない |
| 3 | `REQUIRED_EMPTY` は実 Ledger 観測なしでは作れない（既存試験が担保） |
| 4 | `NOT_APPLICABLE` は `event_sequence` が `null`（既存試験が担保） |
| 5 | `SECOND_SCAN_IS_THE_GATE` の期待列が到達可能で、Event Registry に実在する |

3と4は `test_unit_evidence_contract.py` が持っている。ここでは重ねない。

## 対象 Case は正本から導く

対象は Registry（`tests.yaml`）で `NOT_APPLICABLE` を宣言した Masking の Case である。
Owner 回答（`MASK-EVT-1`／`MASK-EVT-2`）の保存 Package は公開用の配布コピーに
収録しない。回答が指す Case がこの集合に含まれること、期待列が回答と全体一致する
ことは、非公開側の試験（`tests/private_history/`）が確かめる。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SECOND_SCAN = "AT-MASKING-001/SECOND_SCAN_IS_THE_GATE"

#: Ledger を触る型。Unit Case の経路に現れてはならない。
_LEDGER_SYMBOLS = (
    "LedgerProbe",
    "RealLedgerView",
    "SqliteEventLedgerRepository",
    "EventLedgerPort",
)


def _cases() -> list[dict[str, Any]]:
    return yaml.safe_load(
        (REPO_ROOT / "design-source/registries/tests.yaml").read_text(encoding="utf-8")
    )["test_cases"]


def _unit_targets() -> set[str]:
    """Registry が `NOT_APPLICABLE` と宣言した Masking の Case。一覧を書かない。

    Event を観測しない宣言である。**観測しないなら駆動もしない。** Owner が
    `UNIT_NOT_APPLICABLE` と答えた Case はすべてここに含まれる（非公開側で確認）。
    """
    return {
        f"{case['test_id']}/{case['case_id']}"
        for case in _cases()
        if "MASKING" in case["test_id"] and case.get("event_observation_policy") == "NOT_APPLICABLE"
    }


def _markers() -> dict[str, list[str]]:
    """`@pytest.mark.case` から Case ID と試験の対応を採る。"""
    found: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call) or getattr(deco.func, "attr", None) != "case":
                    continue
                if deco.args and isinstance(deco.args[0], ast.Constant):
                    found.setdefault(str(deco.args[0].value), []).append(
                        f"{path.relative_to(REPO_ROOT)}::{node.name}"
                    )
    return found


def _reachable_source(node_id: str) -> str:
    """試験本体と、そこから呼ぶ観測 helper の Source を集める。

    helper を1段辿るのは、観測の実体が helper 側にあるからである。
    呼び出し元だけ見ると、helper が何を触っていても見えない。
    """
    rel, _, func = node_id.partition("::")
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(text)
    bodies: list[str] = []
    for item in ast.walk(tree):
        if isinstance(item, ast.FunctionDef) and item.name == func:
            bodies.append(ast.get_source_segment(text, item) or "")
    for body in list(bodies):
        for item in ast.walk(tree):
            if isinstance(item, ast.FunctionDef) and item.name.startswith("_observe"):
                if item.name in body:
                    bodies.append(ast.get_source_segment(text, item) or "")
    return "\n".join(bodies)


# ---------------------------------------------------------------------------
# 1. Unit Case が MaskingService を駆動しない
# ---------------------------------------------------------------------------


def test_unit_cases_do_not_drive_the_masking_service() -> None:
    """`MaskingService` は Ledger を持つ。Unit Case の経路に現れてはならない。

    駆動すれば Event が Append され、`event_sequence` は `null` でなくなる。
    そのとき Evidence は矛盾するが、矛盾に気づくのは生成段である。ここで落とす。
    """
    markers = _markers()
    offenders: list[str] = []
    for case in sorted(_unit_targets()):
        for node_id in markers.get(case, []):
            source = _reachable_source(node_id)
            if "MaskingService" in source or "service.execute" in source:
                offenders.append(f"{case} -> {node_id}")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# 2. Unit Case が Ledger Port を使わない
# ---------------------------------------------------------------------------


def test_unit_cases_do_not_touch_any_ledger() -> None:
    """偽 Probe も含めて、Ledger を触る型が経路に現れてはならない。

    偽 Probe が返す 0 を「見て0件」の根拠にしたのが、そもそもの発端である
    （Owner Decision MASK-EVT-2）。**読まないことを読まないままにする。**
    """
    markers = _markers()
    offenders: list[str] = []
    for case in sorted(_unit_targets()):
        for node_id in markers.get(case, []):
            source = _reachable_source(node_id)
            for symbol in _LEDGER_SYMBOLS:
                if symbol in source:
                    offenders.append(f"{case} -> {node_id}: {symbol}")
    assert not offenders, offenders


def test_the_fake_ledger_probe_is_gone_from_the_masking_pipeline_suite() -> None:
    """偽 Ledger を観測する helper が Masking Pipeline の試験に残っていないこと。

    使われていない helper でも、残っていれば次の Case がそれを呼ぶ。
    """
    path = REPO_ROOT / "tests/integration/masking/test_masking_pipeline.py"
    source = path.read_text(encoding="utf-8")
    assert "LedgerProbe" not in source, "偽 Ledger を観測する経路が残っている"


# ---------------------------------------------------------------------------
# 5. SECOND_SCAN の期待列
# ---------------------------------------------------------------------------


def test_second_scan_expectation_is_reachable() -> None:
    """`SPANS_PROPOSED` を含むなら `SCAN1_CANDIDATES_READY` も含むこと。

    Masker を呼ぶには Scan#1 を通過している必要があり、通過していれば候補は
    揃っている。片方だけを含む列は構成上どの入力でも作れない（§1.16.2.3）。
    到達しない期待値は、実装をどう直しても満たせない。
    """
    for case in _cases():
        events = list(case.get("expected_event_sequence") or [])
        if "INPUT_MASKING_SPANS_PROPOSED" not in events:
            continue
        assert "INPUT_MASKING_SCAN1_CANDIDATES_READY" in events, (
            f"{case['test_id']}/{case['case_id']}: 到達しない期待列である"
        )


def test_second_scan_events_are_all_registered() -> None:
    """期待列の Event がすべて Event Registry に実在すること。新規追加をしない。"""
    snapshot = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    known = set(snapshot["event_types"])
    registry = next(c for c in _cases() if f"{c['test_id']}/{c['case_id']}" == SECOND_SCAN)
    unknown = [e for e in registry["expected_event_sequence"] if e not in known]
    assert not unknown, unknown


# ---------------------------------------------------------------------------
# Case 集合
# ---------------------------------------------------------------------------


def test_no_masking_case_still_declares_required_empty() -> None:
    """Masking の Case に `REQUIRED_EMPTY` が残っていないこと。

    Masking Pipeline は Ledger Port を持たないので、この宣言を満たせない。
    """
    leftover = {
        f"{c['test_id']}/{c['case_id']}": c.get("event_observation_policy")
        for c in _cases()
        if "MASKING" in c["test_id"] and c.get("event_observation_policy") == "REQUIRED_EMPTY"
    }
    assert not leftover, leftover


@pytest.mark.parametrize("case_id", sorted(_unit_targets()))
def test_each_unit_case_declares_an_empty_expectation(case_id: str) -> None:
    """`NOT_APPLICABLE` を名乗る Case が Event 列を持たないこと。"""
    by_key = {f"{c['test_id']}/{c['case_id']}": c for c in _cases()}
    assert not (by_key[case_id].get("expected_event_sequence") or [])
