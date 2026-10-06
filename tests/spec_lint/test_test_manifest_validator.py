"""`tools/validate_test_manifest.py` の拒否経路（`AT-TEST-MANIFEST-001`）。

CIはこのValidatorを**実Registryに対してのみ**実行している。実Registryは
（当然ながら）合格するので、拒否側の分岐は一度も実行されていなかった。
検査器が検査をやめても誰も気付かない状態にあたる。

そこで合成Registryを作り、壊し方ごとに期待するCodeで落ちることを確かめる。
Validatorはargparseを持つScriptなので、Import せず**CIと同じ呼び方**で
起動する。Import経由にすると引数解釈やExit Codeの経路が試験から外れる。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPO_ROOT / "tools" / "validate_test_manifest.py"
REGISTRIES = REPO_ROOT / "design-source" / "registries"

sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
from case_probe import observe_unit_case  # noqa: E402

#: Validator が stdout へ出す Error Code。`[CODE]` の形で1行に1種類ずつ並ぶ。
_CODE_RE = re.compile(r"\[([A-Z0-9_]+)\]")

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_BAD_INPUT = 4


def _template() -> dict[str, Any]:
    """実Registryの1行目を雛形にする。

    手書きの雛形だと、Validatorが必須項目を増やしたときに雛形だけが
    古くなり、壊していないはずの試験が別の理由で落ちる。正本から
    採れば雛形は勝手に追随する。
    """
    rows = yaml.safe_load((REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))["test_cases"]
    return dict(rows[0])


def _write_registry(directory: Path, cases: list[dict[str, Any]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tests.yaml").write_text(
        yaml.safe_dump({"test_cases": cases}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # errors.yaml は改変しない。expected_error_code の照合先として必要になる。
    (directory / "errors.yaml").write_text(
        (REGISTRIES / "errors.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return directory


def _run(directory: Path) -> subprocess.CompletedProcess[str]:
    from monitored_validator import run_validator
    from unit_execution_monitor import _ACTIVE

    if _ACTIVE is not None:
        return run_validator(directory)
    return subprocess.run(  # noqa: S603 - list[str]。不変条件#8によりshellを介さない
        [sys.executable, str(VALIDATOR), "--registries", str(directory)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _manifest_validation_result(result: subprocess.CompletedProcess[str]) -> str:
    """Validator の判定を `MANIFEST_VALIDATION_RESULT` の語彙へ写す。

    Validator は判定を **Exit Code** で返す。`states.yaml` の
    `MANIFEST_VALIDATION_RESULT` は同じ判定を `ACCEPTED` / `REJECTED` と呼ぶ。
    写像はここ1箇所に置き、**暗黙に変換しない**。

    `test_manifest_validation_result_maps_both_directions` が両方向を実測する。
    片側だけを見ていると、Validator が常に同じ Exit Code を返すようになっても
    気付けない。
    """
    if result.returncode == EXIT_OK:
        return "ACCEPTED"
    if result.returncode == EXIT_VIOLATION:
        return "REJECTED"
    raise AssertionError(
        f"判定として解釈できない Exit Code: {result.returncode}\n{result.stdout}{result.stderr}"
    )


def _observed_error_codes(result: subprocess.CompletedProcess[str]) -> list[str]:
    """stdout に現れた Error Code を**全部**返す。

    1件だけ選ばない。Registry が単一の Code を期待している Case で複数出るなら、
    それは期待値と実装の食い違いであって、都合のよい1件を選ぶ話ではない。
    """
    return sorted(set(_CODE_RE.findall(result.stdout)))


def _run_mutated(tmp_path: Path, **overrides: Any) -> subprocess.CompletedProcess[str]:
    case = _template()
    case.update(overrides)
    return _run(_write_registry(tmp_path / "registries", [case]))


# --------------------------------------------------------------------------
# 雛形そのものは通ること（試験の土台の健全性）
# --------------------------------------------------------------------------


def test_unmodified_template_passes(tmp_path: Path) -> None:
    """壊していない入力が落ちるなら、以降の試験は何も証明しない。"""
    result = _run(_write_registry(tmp_path / "registries", [_template()]))
    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "violations : 0" in result.stdout


def test_manifest_validation_result_maps_both_directions(tmp_path: Path) -> None:
    """Exit Code と State の写像が両方向で成り立つこと。

    片側だけを固定すると、Validator が常に同じ Exit Code を返すようになっても
    「REJECTED を観測した」と書き続けてしまう。
    """
    ok = _run(_write_registry(tmp_path / "ok", [_template()]))
    assert _manifest_validation_result(ok) == "ACCEPTED"
    assert _observed_error_codes(ok) == []

    bad = _run_mutated(tmp_path / "bad", expected_state="ACCEPTED/REJECTED")
    assert _manifest_validation_result(bad) == "REJECTED"
    assert _observed_error_codes(bad) == ["AMBIGUOUS_EXPECTED_STATE"]


# --------------------------------------------------------------------------
# AT-TEST-MANIFEST-001
# --------------------------------------------------------------------------


def test_missing_hash_column_is_rejected(tmp_path: Path) -> None:
    """`expectation_descriptor_hash` の列そのものが無い。

    Hash列の欠落は「期待値を固定していない」ことを意味する。落とせなければ
    Manifestは後から書き換え可能になり、Evidenceの意味が消える。

    この壊し方は Code を**2つ**出す。列が無いので値が `None` になり、Hash 書式の
    検査にも引っかかるためである。Registry の Case は単一 Code を期待するので、
    Case の marker は Code が1つだけ出る `input_fixture_hash` 側へ置く。
    Registry の `scenario` は
    `manifest_missing_expectation_or_input_hash_column` で、どちらの列でもよい。
    **都合のよい1件を選んだのではなく、Case が要求する形の入力を選んでいる。**
    """
    case = _template()
    del case["expectation_descriptor_hash"]
    result = _run(_write_registry(tmp_path / "registries", [case]))

    assert result.returncode == EXIT_VIOLATION
    assert _observed_error_codes(result) == ["HASH_COLUMN_MISSING", "HASH_PATTERN_INVALID"]


@pytest.mark.case("AT-TEST-MANIFEST-001/HASH_COLUMN_MISSING")
@pytest.mark.unit_subject("MANIFEST_VALIDATION_RESULT", durability="NOT_APPLICABLE")
def test_missing_input_fixture_hash_column_is_also_rejected(
    tmp_path: Path, case_observation: Any
) -> None:
    """`input_fixture_hash` は値がnull可でも、**列**の欠落は許さない。

    null は「Fixtureを取らない」という宣言であり、列が無いのは
    「決めていない」である。両者を同じ扱いにしない。
    """
    case = _template()
    assert case["input_fixture_hash"] is None, "雛形はnullのはず"
    del case["input_fixture_hash"]
    result = _run(_write_registry(tmp_path / "registries", [case]))

    assert result.returncode == EXIT_VIOLATION
    codes = _observed_error_codes(result)
    assert codes == ["HASH_COLUMN_MISSING"], codes

    state = _manifest_validation_result(result)
    observe_unit_case(
        case_observation,
        "AT-TEST-MANIFEST-001/HASH_COLUMN_MISSING",
        state=state,
        subject_id="manifest-validation-hash-column-missing",
        error_code=codes[0],
        payload={
            "removed_column": "input_fixture_hash",
            "validator_exit_code": result.returncode,
            "observed_error_codes": codes,
            "manifest_rejected": state == "REJECTED",
        },
    )


@pytest.mark.case("AT-TEST-MANIFEST-001/HASH_FORMAT")
@pytest.mark.unit_subject("MANIFEST_VALIDATION_RESULT", durability="NOT_APPLICABLE")
def test_fixture_hash_without_prefix_is_rejected(tmp_path: Path, case_observation: Any) -> None:
    """`sha256:` 接頭辞の無いFixture Hash。

    接頭辞はAlgorithmの宣言である。裸の64桁を受け付けると、将来
    Algorithmを増やしたときに既存値の解釈が確定しない。
    """
    result = _run_mutated(tmp_path, input_fixture_hash="a" * 64)

    assert result.returncode == EXIT_VIOLATION
    codes = _observed_error_codes(result)
    assert codes == ["FIXTURE_HASH_FORMAT_INVALID"], codes

    state = _manifest_validation_result(result)
    observe_unit_case(
        case_observation,
        "AT-TEST-MANIFEST-001/HASH_FORMAT",
        state=state,
        subject_id="manifest-validation-hash-format",
        error_code=codes[0],
        payload={
            "input_fixture_hash": "a" * 64,
            "validator_exit_code": result.returncode,
            "observed_error_codes": codes,
            "manifest_validation_result": state,
        },
    )


@pytest.mark.parametrize(
    "value",
    [
        "sha256:" + "A" * 64,  # 大文字。Hash文字列の正規形を1つに固定する
        "sha256:" + "a" * 63,  # 桁不足
        "sha256:" + "a" * 65,  # 桁超過
        "sha1:" + "a" * 64,  # 別Algorithm
        "",
        123,
    ],
)
def test_malformed_fixture_hash_variants_are_rejected(tmp_path: Path, value: object) -> None:
    result = _run_mutated(tmp_path, input_fixture_hash=value)
    assert result.returncode == EXIT_VIOLATION
    assert "[FIXTURE_HASH_FORMAT_INVALID]" in result.stdout


@pytest.mark.case("AT-TEST-MANIFEST-001/AMBIGUOUS_EXPECTED")
@pytest.mark.unit_subject("MANIFEST_VALIDATION_RESULT", durability="NOT_APPLICABLE")
def test_multiple_expected_states_in_one_case_is_rejected(
    tmp_path: Path, case_observation: Any
) -> None:
    """1 Caseへ複数の期待状態を詰める。

    「BLOCKED_POLICY または BLOCKED_REPAIR_REQUIRED」を1行で書けると、
    どちらが起きても合格になる。判定が緩む方向へ黙って倒れるため、
    Manifestの段階で落とす。
    """
    ambiguous = "BLOCKED_POLICY,BLOCKED_REPAIR_REQUIRED"
    result = _run_mutated(tmp_path, expected_state=ambiguous)

    assert result.returncode == EXIT_VIOLATION
    codes = _observed_error_codes(result)
    assert codes == ["AMBIGUOUS_EXPECTED_STATE"], codes

    state = _manifest_validation_result(result)
    observe_unit_case(
        case_observation,
        "AT-TEST-MANIFEST-001/AMBIGUOUS_EXPECTED",
        state=state,
        subject_id="manifest-validation-ambiguous-expected",
        error_code=codes[0],
        payload={
            "expected_state_field": ambiguous,
            "validator_exit_code": result.returncode,
            "observed_error_codes": codes,
            "manifest_validation_result": state,
        },
    )


def test_slash_separated_expected_state_is_rejected(tmp_path: Path) -> None:
    result = _run_mutated(tmp_path, expected_state="ACCEPTED/REJECTED")
    assert result.returncode == EXIT_VIOLATION
    assert "[AMBIGUOUS_EXPECTED_STATE]" in result.stdout


# --------------------------------------------------------------------------
# 周辺の拒否条件。同じ合成Registryで確かめられる
# --------------------------------------------------------------------------


def test_duplicate_case_key_is_rejected(tmp_path: Path) -> None:
    """同じ `test_id/case_id` が2行。後勝ちで静かに上書きされない。"""
    case = _template()
    result = _run(_write_registry(tmp_path / "registries", [case, dict(case)]))

    assert result.returncode == EXIT_VIOLATION
    assert "[DUPLICATE_CASE]" in result.stdout


def test_unknown_error_code_is_rejected(tmp_path: Path) -> None:
    """Registryに無いError Codeを期待値に書けない（不変条件#18）。"""
    result = _run_mutated(tmp_path, expected_error_code="NOT_A_REAL_ERROR_CODE")

    assert result.returncode == EXIT_VIOLATION
    assert "[UNKNOWN_ERROR_CODE]" in result.stdout


def test_evidence_status_pass_without_evidence_hash_is_rejected(tmp_path: Path) -> None:
    """PASSと書くならEvidenceを添えさせる（不変条件#16）。

    「実行していない試験をPASSと書かない」を、Manifestの側で機械的に縛る。
    """
    result = _run_mutated(tmp_path, evidence_status="PASS")

    assert result.returncode == EXIT_VIOLATION
    assert "[FIXTURE_HASH_REQUIRED_FOR_PASS]" in result.stdout
    assert "[EVIDENCE_HASH_REQUIRED_FOR_PASS]" in result.stdout


def test_durability_tier_is_required_for_crash_cases(tmp_path: Path) -> None:
    """`AT-CRASH-*` は永続性Tierの宣言が要る。"""
    result = _run_mutated(tmp_path, test_id="AT-CRASH-001", durability_tier=None)

    assert result.returncode == EXIT_VIOLATION
    assert "[DURABILITY_TIER_REQUIRED]" in result.stdout


def test_durability_tier_on_a_non_crash_case_is_rejected(tmp_path: Path) -> None:
    """関係の無いCaseへTierを書くのも拒否する。意味の無い欄を残さない。"""
    result = _run_mutated(tmp_path, durability_tier="T1_PROCESS_KILL")

    assert result.returncode == EXIT_VIOLATION
    assert "[DURABILITY_TIER_NOT_APPLICABLE]" in result.stdout


def test_non_boolean_flag_is_rejected(tmp_path: Path) -> None:
    """`release_allowed: "true"` のような文字列を通さない。

    YAMLの文字列 `"false"` は真値として扱われうる。型で落とす。
    """
    result = _run_mutated(tmp_path, release_allowed="true")

    assert result.returncode == EXIT_VIOLATION
    assert "[FLAG_NOT_BOOLEAN]" in result.stdout


def test_release_allowed_with_manual_queue_is_rejected(tmp_path: Path) -> None:
    """自動再実行禁止かつ手動Queue行きなのにRelease可、という矛盾。"""
    result = _run_mutated(
        tmp_path,
        auto_reexecution_prohibited=True,
        manual_queue_expected=True,
        release_allowed=True,
    )

    assert result.returncode == EXIT_VIOLATION
    assert "[RELEASE_ALLOWED_WITH_MANUAL_QUEUE]" in result.stdout


# --------------------------------------------------------------------------
# 入力そのものが読めない場合
# --------------------------------------------------------------------------


def test_absent_registry_directory_exits_with_input_error(tmp_path: Path) -> None:
    """違反(1)と入力不正(4)を区別する。読めなかったのに合格扱いしない。"""
    result = _run(tmp_path / "does-not-exist")
    assert result.returncode == EXIT_BAD_INPUT
    assert "registry load failed" in result.stderr
