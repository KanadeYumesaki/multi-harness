"""Case Runner の契約（Phase 6 の起点）。

ここで固定するのは次の3点である。

1. 対象Caseの一覧を**持たない**。Registryから取る（不変条件#18）。
2. 未配線Caseを**ゼロ副作用として扱わない**。実行せず `UNWIRED` として数える。
3. 失敗を握り潰さない。どのCaseがなぜEvidenceを作れなかったかを残す（不変条件#9）。

2番が要る理由は、`side_effects=None` を Emitter が FAIL にしても、Runner側が
気を利かせて 0 を埋めれば素通りしてしまうためである。「観測していない」と
「0件だった」は見た目が同じで意味が正反対になる。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRIES = REPO_ROOT / "design-source" / "registries"

sys.path.insert(0, str(REPO_ROOT / "tools"))

import case_runner  # noqa: E402
from case_runner import (  # noqa: E402
    UNWIRED_MARKER_WITHOUT_OBSERVATION,
    UNWIRED_NO_MARKER,
    CaseRunnerError,
    build_pytest_adapters,
    load_scope_case_ids,
    run_scope,
    scan_case_wiring,
    wiring_report,
    wiring_report_for_case_ids,
)
from emit_case_evidence import CaseRunResult, SideEffectObservation  # noqa: E402


def _registry_case_ids(scope: str) -> set[str]:
    """試験側でRegistryを独立に読む。Runnerの実装を再利用しない。"""
    document = yaml.safe_load((REGISTRIES / "tests.yaml").read_text(encoding="utf-8"))
    return {
        f"{case['test_id']}/{case['case_id']}"
        for case in document["test_cases"]
        if scope in (case.get("phase_scope") or [])
    }


def _result(case_id: str, *, side_effects: SideEffectObservation | None) -> CaseRunResult:
    return CaseRunResult(
        case_id=case_id,
        gate_ids=(),
        input_fixture_path=REPO_ROOT / "registry-snapshot.json",
        observed_state="READY",
        observed_error_code=None,
        observed_events=(),
        side_effects=side_effects,
    )


def test_case_ids_come_from_the_registry_not_from_a_hand_written_list() -> None:
    """対象CaseはRegistry導出であること。Runnerに一覧を書かない。"""
    derived = load_scope_case_ids(REGISTRIES, "MVP0-A")
    assert set(derived) == _registry_case_ids("MVP0-A")
    assert len(derived) == len(set(derived)), "重複がある"
    assert list(derived) == sorted(derived), "順序が決定的でない"

    source = (REPO_ROOT / "tools" / "case_runner.py").read_text(encoding="utf-8")
    for case_id in list(derived)[:5]:
        test_id = case_id.split("/")[0]
        assert test_id not in source, f"Runnerに Case ID を書いている: {test_id}"


def test_unknown_scope_is_rejected_rather_than_returning_nothing() -> None:
    """Scope名の打ち間違いを「対象0件」として静かに成功させない。"""
    with pytest.raises(CaseRunnerError, match="EMPTY_SCOPE"):
        load_scope_case_ids(REGISTRIES, "MVP9-Z")


def test_wiring_report_counts_unwired_cases() -> None:
    """配線済み0件の状態が、そのまま数字として出ること。"""
    report = wiring_report(REGISTRIES, "MVP0-A", {})
    assert len(report.required) == len(_registry_case_ids("MVP0-A"))
    assert report.wired == ()
    assert set(report.unwired) == set(report.required)
    assert not report.complete, "未配線が残るのに complete を名乗っている"


def test_adapters_outside_the_registry_are_reported() -> None:
    """Registryに無いCaseへAdapterを書いている状態を検出する。

    Case IDの打ち間違いや、Scopeから外れたCaseの配線残りがここで出る。
    """
    report = wiring_report(
        REGISTRIES, "MVP0-A", {"AT-NO-SUCH-001/NOPE": lambda: _result("x", side_effects=None)}
    )
    assert report.unknown == ("AT-NO-SUCH-001/NOPE",)
    assert not report.complete


def test_unwired_cases_are_skipped_not_treated_as_zero_side_effects(tmp_path: Path) -> None:
    """未配線Caseを実行しない。0埋めしたEvidenceを作らない。"""
    target = sorted(_registry_case_ids("MVP0-A"))[0]
    outcome = run_scope(
        registries=REGISTRIES,
        scope="MVP0-A",
        adapters={},
        evidence_dir=tmp_path / "evidence",
        repo_root=REPO_ROOT,
        environment_manifest=tmp_path / "env.json",
        case_ids=[target],
    )
    assert outcome.emitted == ()
    assert outcome.skipped_unwired == (target,)
    assert outcome.failures == ()
    assert not (tmp_path / "evidence").exists(), "未配線Caseに対してFileを作った"


def test_adapter_returning_no_side_effects_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    """Adapterが副作用を観測していなければ失敗として残す。

    Runner側で 0 を埋めない。埋めれば Emitter の FAIL 判定を迂回できてしまう。
    """
    target = sorted(_registry_case_ids("MVP0-A"))[0]
    outcome = run_scope(
        registries=REGISTRIES,
        scope="MVP0-A",
        adapters={target: lambda: _result(target, side_effects=None)},
        evidence_dir=tmp_path / "evidence",
        repo_root=REPO_ROOT,
        environment_manifest=tmp_path / "env.json",
        case_ids=[target],
    )
    assert outcome.emitted == ()
    assert outcome.skipped_unwired == ()
    assert len(outcome.failures) == 1
    failed_case, reason = outcome.failures[0]
    assert failed_case == target
    assert "SIDE_EFFECTS_NOT_OBSERVED" in reason
    assert not (tmp_path / "evidence").exists()


def test_adapter_returning_another_case_is_rejected(tmp_path: Path) -> None:
    """AdapterがひもづくCaseと違う結果を返したら止める。

    取り違えたまま通すと、あるCaseの観測が別のCaseのEvidenceになる。
    """
    cases = sorted(_registry_case_ids("MVP0-A"))
    target, other = cases[0], cases[1]
    outcome = run_scope(
        registries=REGISTRIES,
        scope="MVP0-A",
        adapters={
            target: lambda: _result(
                other,
                side_effects=SideEffectObservation(0, 0, 0, 0, 0),
            )
        },
        evidence_dir=tmp_path / "evidence",
        repo_root=REPO_ROOT,
        environment_manifest=tmp_path / "env.json",
        case_ids=[target],
    )
    assert outcome.emitted == ()
    assert len(outcome.failures) == 1
    assert "ADAPTER_CASE_MISMATCH" in outcome.failures[0][1]


def test_out_of_scope_case_ids_are_rejected(tmp_path: Path) -> None:
    """Scope外のCaseを名指しで走らせられない。"""
    with pytest.raises(CaseRunnerError, match="OUT_OF_SCOPE_CASES"):
        run_scope(
            registries=REGISTRIES,
            scope="MVP0-A",
            adapters={},
            evidence_dir=tmp_path / "evidence",
            repo_root=REPO_ROOT,
            environment_manifest=tmp_path / "env.json",
            case_ids=["AT-NO-SUCH-001/NOPE"],
        )


def _real_adapters(tmp_path: Path) -> dict[str, object]:
    return build_pytest_adapters(
        registries=REGISTRIES,
        scope="MVP0-A",
        repo_root=REPO_ROOT,
        fixtures_dir=tmp_path / "fixtures",
    )


def test_wired_adapters_are_a_subset_of_the_registry(tmp_path: Path) -> None:
    """実際のAdapter集合がRegistryの対象Caseからはみ出さないこと。

    件数は書かない。どの時点でも成り立つべき関係だけを固定する。
    """
    adapters = _real_adapters(tmp_path)
    report = wiring_report(REGISTRIES, "MVP0-A", adapters)
    assert report.unknown == (), f"Registryに無いCaseへAdapterがある: {report.unknown}"
    assert set(report.wired) | set(report.unwired) == set(report.required)
    assert set(report.wired) & set(report.unwired) == set()


def test_every_wired_adapter_has_runnable_test_nodes(tmp_path: Path) -> None:
    """配線済みAdapterには実行できる試験Nodeがあること。

    Node の無いAdapterを配線済みに数えると、実行段で初めて落ちる。
    「配線済み」と「実行できる」を同じ意味にする。
    """
    for case_id, adapter in sorted(_real_adapters(tmp_path).items()):
        assert adapter.node_ids, f"{case_id}: 試験Nodeが無い"
        for node_id in adapter.node_ids:
            path_part, _, func = node_id.partition("::")
            assert (REPO_ROOT / path_part).is_file(), f"{case_id}: {path_part} が無い"
            assert func, f"{case_id}: 関数名が無い"


def test_wiring_covers_the_exact_required_case_set(tmp_path: Path) -> None:
    """DCR-1-A実装後。配線完了をRegistry集合との完全一致で維持する。"""
    report = wiring_report(REGISTRIES, "MVP0-A", _real_adapters(tmp_path))
    assert report.unwired == ()
    assert report.unknown == ()
    assert set(report.wired) == _registry_case_ids("MVP0-A")


# -- CC-02: CLIと内部Builderの配線報告を一致させる ---------------------------


def _cli(
    capsys: pytest.CaptureFixture[str], *, scope: str = "MVP0-A"
) -> tuple[int, dict[str, Any]]:
    """CLIをその場で呼び、終了値とJSONを返す。"""
    code = case_runner.main(["--registries", str(REGISTRIES), "--release-scope", scope])
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    return code, payload


def test_cli_and_builder_agree_on_case_ids_not_only_on_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLIと内部Builderが**同じCase集合**を指すこと。

    以前CLIは `{}` を固定で渡しており 0 件、Builderは 107 件を返していた。
    件数だけを比べると、たまたま数が揃った日に食い違いを見逃す。
    必要・配線済み・未配線・未知の**Case IDそのもの**を突き合わせる。
    """
    _, payload = _cli(capsys)
    builder = wiring_report(REGISTRIES, "MVP0-A", _real_adapters(tmp_path))

    assert payload["required_case_count"] == len(builder.required)
    assert payload["wired"] == list(builder.wired)
    assert payload["unwired"] == list(builder.unwired)
    assert payload["unknown_adapters"] == list(builder.unknown)
    assert payload["wired_case_count"] == len(builder.wired)
    assert payload["unwired_case_count"] == len(builder.unwired)
    assert payload["unknown_adapter_count"] == len(builder.unknown)
    # 必要集合はRegistry導出であって、CLI側の別Listではない。
    assert set(payload["wired"]) | set(payload["unwired"]) == _registry_case_ids("MVP0-A")


def test_builder_and_cli_share_one_wiring_scan(tmp_path: Path) -> None:
    """AdapterがScanの結果そのものから組まれていること。

    数え方が2つあることが食い違いの原因だった。Adapter側とReport側が
    別々にMarkerを解釈しないよう、同じScanを共有していることを固定する。
    """
    wiring = scan_case_wiring(registries=REGISTRIES, scope="MVP0-A", repo_root=REPO_ROOT)
    adapters = _real_adapters(tmp_path)

    assert set(wiring) == _registry_case_ids("MVP0-A"), "Scanの対象がScope対象Caseと違う"
    assert {case_id for case_id, case in wiring.items() if case.wired} == set(adapters)
    for case_id, adapter in adapters.items():
        assert adapter.node_ids == wiring[case_id].observing_node_ids  # type: ignore[attr-defined]

    # Adapterから作った報告と、Case IDだけから作った報告が一致すること。
    # 一致しなければ、数え方がまだ2つ残っている。
    from_adapters = wiring_report(REGISTRIES, "MVP0-A", adapters)
    from_case_ids = wiring_report_for_case_ids(REGISTRIES, "MVP0-A", set(adapters))
    assert from_adapters == from_case_ids


def test_unwired_cases_carry_a_reason_derived_from_the_repository(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未配線を数字だけで残さず、理由を実物から導くこと。

    「試験が無い」のか「試験はあるが `case_observation` を受け取っていない」
    のかで次にやることが変わる。理由はMarkerの実物から導き、Case IDごとの
    説明をToolへ書かない（不変条件#18）。
    """
    from check_case_coverage import scan_markers

    _, payload = _cli(capsys)
    reasons = payload["unwired_reasons"]

    assert set(reasons) == set(payload["unwired"]), "未配線Caseと理由の集合が一致しない"
    markers = scan_markers()
    for case_id, reason in reasons.items():
        expected = (
            UNWIRED_NO_MARKER if case_id not in markers else (UNWIRED_MARKER_WITHOUT_OBSERVATION)
        )
        assert reason == expected, f"{case_id}: 理由が実物と合わない"


def test_cli_keeps_the_non_success_exit_contract(capsys: pytest.CaptureFixture[str]) -> None:
    """未配線が残る限り非0で終わること。

    配線が進んだからといって成功へ倒さない。「全部配線できた」はCaseの成功
    でもRuntime GOでもない。
    """
    code, payload = _cli(capsys)
    if payload["unwired"]:
        assert code != 0, "未配線が残るのに成功で終わっている"
    else:
        assert code == 0


def test_cli_rejects_an_unknown_scope(capsys: pytest.CaptureFixture[str]) -> None:
    """Scope名の打ち間違いを「対象0件・配線完了」にしない。"""
    with pytest.raises(CaseRunnerError, match="EMPTY_SCOPE"):
        _cli(capsys, scope="MVP9-Z")


def test_wiring_payload_keeps_its_existing_keys(capsys: pytest.CaptureFixture[str]) -> None:
    """既存の出力Keyを消さない・意味を変えないこと（後方互換）。"""
    _, payload = _cli(capsys)
    for key in (
        "release_scope",
        "required_case_count",
        "wired_case_count",
        "unwired_case_count",
        "unknown_adapter_count",
        "unwired",
        "unknown_adapters",
    ):
        assert key in payload, f"既存Keyが消えている: {key}"
    assert payload["release_scope"] == "MVP0-A"


_IGNORED_PARTS = frozenset(
    {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache"}
)


def _snapshot(*roots: Path) -> set[tuple[str, int]]:
    """File名とSizeの集合。書込みが起きれば差分として出る。"""
    seen: set[tuple[str, int]] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if _IGNORED_PARTS & set(path.parts):
                continue
            if path.is_file():
                seen.add((str(path), path.stat().st_size))
    return seen


def test_cli_wiring_report_neither_executes_cases_nor_writes_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """配線報告が観測の**準備情報**にとどまること。

    Caseの実行、Provider呼出し、DB作成、Evidence生成、一時Fileの作成を
    起こさない。`git status` だけではrepo外への書込みを見つけられないので、
    呼出しの監視と一時領域の差分の両方で確かめる。
    """
    import sqlite3
    import tempfile

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"配線報告が副作用を起こした: {args!r}")

    # Case実行（pytest起動）・一時領域・DBのいずれにも触れないこと。
    monkeypatch.setattr(case_runner.subprocess, "run", _forbidden)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", _forbidden)
    monkeypatch.setattr(tempfile, "mkdtemp", _forbidden)
    monkeypatch.setattr(sqlite3, "connect", _forbidden)
    # Adapter自体を呼べば、そこから先はすべて起きる。入口で止める。
    monkeypatch.setattr(case_runner.PytestCaseAdapter, "__call__", _forbidden)

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))

    watched = (REPO_ROOT / "tests", REPO_ROOT / "tools", REPO_ROOT / "docs", temp_root)
    before = _snapshot(*watched)
    code, payload = _cli(capsys)
    after = _snapshot(*watched)

    assert payload["required_case_count"] > 0, "報告が空になっている"
    assert code != 0 or not payload["unwired"]
    assert after == before, f"配線報告がFileを書き換えた: {after ^ before}"
    assert list(temp_root.iterdir()) == [], "一時領域へ書き込んでいる"
    assert not list(REPO_ROOT.glob("*.db")), "DBを作っている"
