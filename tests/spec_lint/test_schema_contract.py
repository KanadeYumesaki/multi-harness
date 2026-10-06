"""Schema契約のBaseline検査（`tools/check_schema_contract.py`）。

Core Schemaには正本が2つある。設計書から生成される `spec/20-schemas/<Name>.md` の
必須Field宣言と、`tools/build_core_schemas.py` の手書きTableから生成される
`schemas/core/<Name>/<ver>.schema.json` である。

**この2つを突き合わせる検査が無かった。** 結果として全22 Schemaのうち20 Schemaで
乖離が積み上がり、Spec Lintは一度も落ちなかった。ここで落とすのはその再発だけである。

既存乖離を今すぐ全部直すことは求めない（Owner Decision D-1a はContext系5に限定した）。
求めるのは「**増えていないこと**」と「是正済みと宣言したものが戻っていないこと」である。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "tests" / "schema-contract-baseline.yaml"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, "tools/check_schema_contract.py", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_schema_contract_has_not_regressed() -> None:
    """CIと同じToolを同じ引数で呼ぶ。Logicを再実装しない。"""
    result = _run()
    assert result.returncode == 0, (
        "Schema契約が後退した。設計書の必須FieldとJSON Schemaの乖離が増えたか、"
        "是正済み宣言のSchemaが戻っている。\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_baseline_is_not_part_of_the_spec() -> None:
    """Baselineを`design-source/registries/`へ置かない。

    あそこは設計書由来の正本で、中身は`spec-manifest`のHashに入る。
    進捗（今どこまで是正したか）を混ぜると、是正のたびに正本側のHashが動く。
    `tests/case-coverage.yaml`と同じ理由で`tests/`へ置く。
    """
    stray = REPO_ROOT / "design-source" / "registries" / "schema-contract-baseline.yaml"
    assert not stray.exists(), f"Baselineが正本Registryの側にある: {stray}"
    assert BASELINE.is_file(), f"Baselineが見つからない: {BASELINE}"


def test_baseline_lists_field_names_not_counts() -> None:
    """件数ではなく全Field名を列挙する。

    件数だけを固定すると、1件直して1件増えたときに素通りする。
    """
    document = yaml.safe_load(BASELINE.read_text(encoding="utf-8"))
    known = document["known_divergence"]
    assert known, "known_divergence が空。全Schemaが是正済みなら resolved へ移すこと"
    for key, item in known.items():
        for kind in ("absent", "optional_only"):
            values = item.get(kind) or []
            assert all(isinstance(name, str) for name in values), (
                f"{key}.{kind} にField名以外がある"
            )


def test_new_divergence_is_detected() -> None:
    """Baselineから1件消すと検出されること。

    「増えたら落ちる」が本当に働くかを、Tool自身に対して確かめる。
    Baselineは読み取りのみで、Fileは書き換えない。
    """
    import json

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from check_schema_contract import evaluate, load_baseline, measure

    measured = measure()
    baseline = load_baseline()
    assert not evaluate(measured, baseline), "前提: 現状はBaselineと一致しているはず"

    victim = sorted(baseline["known_divergence"])[0]
    tampered = json.loads(json.dumps(baseline))
    del tampered["known_divergence"][victim]
    failures = evaluate(measured, tampered)
    assert any("NEW_DIVERGENCE" in line for line in failures), failures


def test_resolved_regression_is_detected() -> None:
    """未是正Schemaを`resolved`へ入れると検出されること。"""
    import json

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from check_schema_contract import evaluate, load_baseline, measure

    measured = measure()
    baseline = load_baseline()
    victim = sorted(baseline["known_divergence"])[0]
    tampered = json.loads(json.dumps(baseline))
    tampered["resolved"] = [victim]
    failures = evaluate(measured, tampered)
    assert any("RESOLVED_SCHEMA_REGRESSED" in line for line in failures), failures


def test_ci_runs_the_schema_contract_check() -> None:
    """CIにも同じ工程を置く。ローカルだけの検査は忘れられる。"""
    for workflow in (".github/workflows/ci.yml", "ci/github-actions-ci.yml"):
        text = (REPO_ROOT / workflow).read_text(encoding="utf-8")
        assert "check_schema_contract.py" in text, f"{workflow} にSchema契約検査の工程が無い"
