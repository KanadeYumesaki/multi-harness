"""受入Caseと試験の対応（`tools/check_case_coverage.py`）。

Registryには MVP0-A だけで110件のCaseがある。試験名をCase名へ寄せては
いたが、機械的な紐付けは無かった。その状態では次の3つが区別できない。

  * Caseに対応する試験がある
  * 試験は無いが、未実装だと分かっている
  * 試験が無いことに誰も気付いていない

3つ目が最も危ない。ここで落とすのはその状態だけである。
Markerが付いていることと、その試験がCaseを十分に検証していることは別で、
後者は本Toolの守備範囲ではない。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
COVERAGE = REPO_ROOT / "tests" / "case-coverage.yaml"


def test_case_coverage_is_complete() -> None:
    """CIと同じToolを同じ引数で呼ぶ。Logicを再実装しない。

    再実装するとTool側の変更に追随せず、ローカルとCIで判定が割れる。
    """
    result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, "tools/check_case_coverage.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Caseと試験の対応に欠落がある。試験へ @pytest.mark.case を付けるか、"
        "tests/case-coverage.yaml へ理由付きで登録すること。\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_coverage_registry_is_not_part_of_the_spec() -> None:
    """対応表を `design-source/registries/` へ置かない。

    あそこは設計書から抽出した正本の置き場で、中身は spec-manifest の
    Hashに入る。対応表を混ぜると**試験を1件足すたびに設計書側の生成物
    Hashが動く**。実際に一度置いてしまい、spec shard の再生成検査が落ちた。

    正本（設計が何を要求するか）と進捗（今どこまで検証したか）は
    変わる理由が違う。同じ場所へ置かない。
    """
    stray = REPO_ROOT / "design-source" / "registries" / "case-coverage.yaml"
    assert not stray.exists(), f"対応表が正本Registryの側にある: {stray}"
    assert COVERAGE.is_file(), f"対応表が見つからない: {COVERAGE}"


def test_every_unimplemented_case_carries_a_reason() -> None:
    """理由の無い未実装宣言を残さない。

    Case IDの羅列だけなら「未着手」「諦めた」「忘れた」が同じ見た目になる。
    理由が書いてあれば、次に読む人がどれなのか判断できる。
    """
    document = yaml.safe_load(COVERAGE.read_text(encoding="utf-8"))
    rows = document["not_implemented"] or []
    # MVP0-A は全Case Covered に到達したため `not_implemented` は空である。
    # **試験は消さない。** 空でなくなった瞬間から理由の有無を再び検査する。
    # 消すと、次に未実装が増えたときに理由なしで並べられる。
    missing = [row["case"] for row in rows if not str(row.get("reason", "")).strip()]
    assert not missing, f"理由の無い未実装宣言: {missing}"


def test_reason_check_still_works_when_entries_return() -> None:
    """未実装が再び増えたとき、理由の無い宣言を捕まえられること。

    空になったからといって検査を消すと、次に増えたとき素通りする。
    """
    rows = [{"case": "AT-X-001/NO_REASON", "reason": "  "}]
    missing = [row["case"] for row in rows if not str(row.get("reason", "")).strip()]
    assert missing == ["AT-X-001/NO_REASON"]


def test_unimplemented_declarations_are_unique() -> None:
    """同じCaseを2回宣言しない。件数が水増しされる。"""
    document = yaml.safe_load(COVERAGE.read_text(encoding="utf-8"))
    declared = [row["case"] for row in document["not_implemented"]]
    duplicates = sorted({case for case in declared if declared.count(case) > 1})
    assert not duplicates, f"not_implemented の重複: {duplicates}"


def test_case_marker_is_registered_in_pytest_config() -> None:
    """`--strict-markers` 下では未登録Markerは実行時エラーになる。

    Markerの登録漏れは「Markerを付けたのに集計されない」ではなく
    「試験が起動しない」として出る。設定と検査Toolを同時に見る。
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "--strict-markers" in text
    assert '"case:' in text or "'case:" in text, "case Markerがpyproject.tomlに未登録"


def test_ci_runs_the_case_coverage_check() -> None:
    """CIにも同じ工程を置く。ローカルだけの検査は忘れられる。"""
    for workflow in (".github/workflows/ci.yml", "ci/github-actions-ci.yml"):
        text = (REPO_ROOT / workflow).read_text(encoding="utf-8")
        assert "check_case_coverage.py" in text, f"{workflow} に対応検査の工程が無い"
