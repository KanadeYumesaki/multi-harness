#!/usr/bin/env python3
"""Case Runner：Registryから対象Caseを取り、配線済みCaseだけを実行する。

## なぜ「配線」を明示的に扱うか

Case Evidence は `observed_state`／`observed_error_code`／`observed_event_sequence`／
`side_effects`／`input_fixture_hash` を必要とする。このうち **`side_effects` は
観測しないと作れない**。副作用Spyを組んでいないCaseは値を持たない。

ここで危ないのは「観測していない」を「0件だった」と書いてしまうことである。
両者は見た目が同じで、意味が正反対になる。Emitterは `side_effects=None` を
FAIL にするが、Runner側が気を利かせて 0 を埋めれば素通りしてしまう。

だから Runner は **未配線Caseを実行しない**。ゼロ副作用として扱わず、
`UNWIRED` として数える。Evidence を作るのは配線済みCaseだけである。

## Case一覧を持たない

対象Caseは `design-source/registries/tests.yaml` の `phase_scope` から取る。
Runner に Case ID の一覧を書かない（不変条件#18）。Registryが動けば対象も動く。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_case_coverage import REPO_ROOT, scan_markers
from emit_case_evidence import (
    CaseRunResult,
    EvidenceEmissionError,
    FaultInjectionSetting,
    SideEffectObservation,
    emit_case_evidence,
)

__all__ = [
    "CaseAdapter",
    "CaseRunnerError",
    "CaseWiring",
    "PytestCaseAdapter",
    "RunOutcome",
    "WiringReport",
    "build_pytest_adapters",
    "load_scope_case_ids",
    "run_scope",
    "scan_case_wiring",
    "wiring_report",
    "wiring_report_for_case_ids",
]

#: 観測記録の受け渡しに使う環境変数。tests/conftest.py と対になる。
OBSERVATION_SINK_ENV = "CASE_OBSERVATION_SINK"

#: 未配線の理由。**Registryと試験Fileの実物からだけ導く。**
#: 数字だけの報告は「試験が無い」と「試験はあるが観測していない」を同じ見た目に
#: してしまう。次にやることが違うのだから、分けて名前を付ける。
UNWIRED_NO_MARKER = "NO_CASE_MARKER"
UNWIRED_MARKER_WITHOUT_OBSERVATION = "MARKER_WITHOUT_OBSERVATION"

#: Caseを1件走らせて観測結果を返すAdapter。期待値もstatusも返さない。
CaseAdapter = Callable[[], CaseRunResult]


class CaseRunnerError(RuntimeError):
    """Runnerの前提が壊れている。判定不能のまま進めない（不変条件#9）。"""


@dataclass(frozen=True, slots=True)
class WiringReport:
    """どのCaseがEvidenceを作れる状態かを述べる。"""

    scope: str
    required: tuple[str, ...]
    wired: tuple[str, ...]
    unwired: tuple[str, ...]
    unknown: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.unwired and not self.unknown

    def as_dict(self) -> dict[str, object]:
        # 既存Keyは名前も意味も変えない。`wired` は追加である。件数だけでは
        # 「どのCaseが配線済みか」を突き合わせられず、CLIと内部Builderが
        # 同じ集合を指しているかを確かめられない。
        return {
            "release_scope": self.scope,
            "required_case_count": len(self.required),
            "wired_case_count": len(self.wired),
            "unwired_case_count": len(self.unwired),
            "unknown_adapter_count": len(self.unknown),
            "wired": list(self.wired),
            "unwired": list(self.unwired),
            "unknown_adapters": list(self.unknown),
        }


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """1回のRunで何が起きたか。Evidenceを作れなかったCaseも残す。"""

    emitted: tuple[tuple[str, Path], ...]
    skipped_unwired: tuple[str, ...]
    failures: tuple[tuple[str, str], ...]


#: `event_observation_policy` の語彙。正本は設計書 §19.1.1 と tests.yaml である。
_POLICY_REQUIRED_EMPTY = "REQUIRED_EMPTY"
_POLICY_NOT_APPLICABLE = "NOT_APPLICABLE"
_EVENT_OBSERVATION_POLICIES = frozenset({_POLICY_REQUIRED_EMPTY, _POLICY_NOT_APPLICABLE})


def load_scope_case_ids(registries: Path, scope: str) -> tuple[str, ...]:
    """Scope対象のCase IDをRegistryから取る。手入力の一覧を持たない。"""
    path = registries / "tests.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CaseRunnerError(f"REGISTRY_UNREADABLE: {path}: {exc}") from exc
    cases = document.get("test_cases") if isinstance(document, Mapping) else None
    if not cases:
        raise CaseRunnerError(f"REGISTRY_EMPTY: {path} に test_cases が無い")

    selected = sorted(
        f"{case['test_id']}/{case['case_id']}"
        for case in cases
        if scope in (case.get("phase_scope") or [])
    )
    if not selected:
        # Scope名の打ち間違いを「対象0件」として静かに成功させない。
        raise CaseRunnerError(f"EMPTY_SCOPE: {scope} に属するCaseが1件も無い")
    return tuple(selected)


def load_event_observation_policies(registries: Path) -> dict[str, str]:
    """Case ごとの `event_observation_policy` を Registry から引く。

    正本は `tests.yaml` である。宣言の無い Case は辞書へ入れない。
    **無いことを既定値へ倒さない。** 倒せば、宣言していない Case が黙って
    Ledger 観測の免除を受ける経路ができる（Owner Decision DCR-5 / E1）。
    """
    path = registries / "tests.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CaseRunnerError(f"REGISTRY_UNREADABLE: {path}: {exc}") from exc
    cases = document.get("test_cases") if isinstance(document, Mapping) else None
    if not cases:
        raise CaseRunnerError(f"REGISTRY_EMPTY: {path} に test_cases が無い")

    policies: dict[str, str] = {}
    for case in cases:
        policy = case.get("event_observation_policy")
        if policy is None:
            continue
        if policy not in _EVENT_OBSERVATION_POLICIES:
            raise CaseRunnerError(
                f"UNKNOWN_EVENT_OBSERVATION_POLICY: "
                f"{case['test_id']}/{case['case_id']} の {policy!r} は語彙に無い"
            )
        policies[f"{case['test_id']}/{case['case_id']}"] = policy
    return policies


def wiring_report_for_case_ids(
    registries: Path, scope: str, wired_case_ids: Collection[str]
) -> WiringReport:
    """配線済みCase IDの集合から報告を作る。**Adapterを持たずに呼べる。**

    配線状況は静的に決まる。Adapterを組まないと報告できない作りにすると、
    「状況を見るだけ」の操作が実行時の都合（`fixtures_dir` など）を抱え込む。
    """
    required = load_scope_case_ids(registries, scope)
    required_set = set(required)
    wired_set = set(wired_case_ids)
    wired = tuple(case_id for case_id in required if case_id in wired_set)
    unwired = tuple(case_id for case_id in required if case_id not in wired_set)
    # Registryに無いCaseへAdapterを書いている状態も報告する。Case IDの
    # 打ち間違いや、Scopeから外れたCaseの配線が残っている場合に出る。
    unknown = tuple(sorted(wired_set - required_set))
    return WiringReport(
        scope=scope, required=required, wired=wired, unwired=unwired, unknown=unknown
    )


def wiring_report(
    registries: Path, scope: str, adapters: Mapping[str, CaseAdapter]
) -> WiringReport:
    """Registryの必要Caseと、実際に配線済みのAdapterを突き合わせる。

    **Adapterを呼ばない。** 見るのはKeyだけである。
    """
    return wiring_report_for_case_ids(registries, scope, adapters.keys())


@dataclass(frozen=True, slots=True)
class PytestCaseAdapter:
    """1 Caseに紐づく統合試験を実際に走らせ、観測値を持ち帰る。

    ここが「pytestのPASSをEvidenceにしない」の実体である。走らせるのは
    試験だが、Evidenceへ渡すのは**試験が記録した観測値**であって、
    exit code ではない。観測値が無ければ pytest が緑でも失敗にする。
    """

    case_id: str
    node_ids: tuple[str, ...]
    repo_root: Path
    fixtures_dir: Path
    #: Registry が宣言した Event 観測 Policy。宣言が無ければ `None`。
    #: `None` は「Ledger 観測を要求する」であって、免除ではない。
    event_observation_policy: str | None = None
    execution_records_dir: Path | None = None

    def __call__(self) -> CaseRunResult:
        if not self.node_ids:
            raise CaseRunnerError(f"NO_TEST_NODES: {self.case_id} に対応する試験が無い")
        observations = self._collect()
        merged = self._merge(observations)
        fixture_path = self._write_fixture(merged)

        side_effects = merged.get("side_effects")
        if side_effects is None:
            # ここで 0 を埋めない。未測定と0件は意味が正反対である。
            raise CaseRunnerError(
                f"SIDE_EFFECTS_NOT_OBSERVED: {self.case_id} は副作用を観測していない"
            )
        fault = merged.get("fault_injection")
        return CaseRunResult(
            case_id=self.case_id,
            gate_ids=(),
            input_fixture_path=fixture_path,
            observed_state=str(merged["observed_state"]),
            observed_error_code=merged["observed_error_code"],
            observed_events=tuple(merged["observed_event_sequence"]),
            side_effects=SideEffectObservation(**side_effects),
            actual_subject_id=str(merged["actual_subject_id"]),
            ledger_head_before=merged["ledger_head_before"],
            ledger_head_after=merged["ledger_head_after"],
            test_node_ids=self.node_ids,
            fault_injection=FaultInjectionSetting(**fault) if fault else None,
        )

    # -- 内部 ------------------------------------------------------------

    def _collect(self) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="case-observation-") as tmp:
            sink = Path(tmp) / "observations.jsonl"
            env = dict(os.environ)
            env[OBSERVATION_SINK_ENV] = str(sink)
            env["PYTHONPATH"] = os.pathsep.join(
                str(self.repo_root.resolve() / part) for part in ("src", "tools")
            )
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                *self.node_ids,
            ]
            started = (
                dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            )
            record_dir = None
            if self.execution_records_dir is not None:
                root = self.execution_records_dir.resolve()
                repo = self.repo_root.resolve()
                if root == repo or repo in root.parents:
                    raise CaseRunnerError("EXECUTION_RECORD_IN_REPOSITORY")
                # mkdir(exist_ok=False)は過去の実行記録を上書きしないための予約。
                parts = self.case_id.split("/")
                if len(parts) != 2 or any(not p or p in (".", "..") for p in parts):
                    raise CaseRunnerError("INVALID_EXECUTION_CASE_ID")
                record_dir = root.joinpath(*parts)
                record_dir.mkdir(parents=True, exist_ok=False)
            # S603: 固定の list[str]。shell を使わない（不変条件#8）。
            result = subprocess.run(  # noqa: S603
                command,
                cwd=str(self.repo_root),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            if record_dir is not None:
                raw = sink.read_bytes() if sink.is_file() else b""
                (record_dir / "observations.jsonl").write_bytes(raw)
                receipt = {
                    "contract": "case-execution-record/1",
                    "case_id": self.case_id,
                    "command": command,
                    "cwd": str(self.repo_root.resolve()),
                    "started_at": started,
                    "recorded_at": dt.datetime.now(dt.UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "exit_code": result.returncode,
                    "runner_source_hash": "sha256:"
                    + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "raw_observations_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    # stdout/stderrは本文を保存せず、改変検出用Hashだけを残す。
                    "stdout_hash": "sha256:" + hashlib.sha256(result.stdout.encode()).hexdigest(),
                    "stderr_hash": "sha256:" + hashlib.sha256(result.stderr.encode()).hexdigest(),
                }
                (record_dir / "execution.json").write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            records = []
            if sink.is_file():
                for line in sink.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        records.append(json.loads(line))

        if result.returncode != 0:
            # 例外や失敗を握り潰してPASSにしない（不変条件#9）。
            tail = (result.stdout or result.stderr)[-800:]
            raise CaseRunnerError(
                f"TEST_EXECUTION_FAILED: {self.case_id} exit={result.returncode}\n{tail}"
            )
        if not records:
            raise CaseRunnerError(
                f"NO_OBSERVATION_RECORDED: {self.case_id} は case_observation へ"
                "観測値を記録していない。pytestのPASSだけをEvidenceにしない"
            )
        return [record for record in records if record.get("case_id") == self.case_id]

    def _merge(self, records: list[dict]) -> dict:
        recorded = [record for record in records if record.get("recorded")]
        if not recorded:
            raise CaseRunnerError(
                f"OBSERVATION_INCOMPLETE: {self.case_id} は record() を呼んでいない"
            )
        exempt = self.event_observation_policy == _POLICY_NOT_APPLICABLE
        if any(not record.get("ledger_observed") for record in recorded) and not exempt:
            # 空配列で埋めない。「見て0件」と「見ていない」は意味が正反対である。
            #
            # 免除されるのは Registry 正本が `NOT_APPLICABLE` と宣言した Case だけで
            # ある（Owner Decision DCR-5 / E1）。宣言の無い Case も、
            # `REQUIRED_EMPTY` の Case も従来どおり観測必須のままにする。
            # 免除しても未観測を0件へ変換しない。Evidence では
            # `event_observation=NOT_APPLICABLE` と `event_sequence=null` になる。
            raise CaseRunnerError(
                f"LEDGER_NOT_OBSERVED: {self.case_id} は Ledger から "
                "observed_event_sequence を取得していない（設計書§19.1.1）"
            )
        if exempt and any(record.get("observed_event_sequence") for record in recorded):
            # 免除された Case が Event を持ち帰っている。宣言と観測が食い違う。
            raise CaseRunnerError(
                f"UNEXPECTED_EVENT_OBSERVATION: {self.case_id} は NOT_APPLICABLE を "
                "宣言しながら Event を観測している"
            )
        keys = (
            "observed_state",
            "observed_error_code",
            "observed_event_sequence",
            "actual_subject_id",
            "side_effects",
            "input_payload",
            "fault_injection",
            "ledger_head_before",
            "ledger_head_after",
        )
        first = recorded[0]
        for other in recorded[1:]:
            differing = [key for key in keys if first.get(key) != other.get(key)]
            if differing:
                # 同じCaseに複数の試験が別々の観測を主張している。
                # どちらが正かを実装側で選ばない。
                raise CaseRunnerError(
                    f"AMBIGUOUS_OBSERVATION: {self.case_id} の観測が一致しない: {differing}"
                )
        return first

    def _write_fixture(self, merged: dict) -> Path:
        payload = merged.get("input_payload")
        if payload is None:
            raise CaseRunnerError(
                f"INPUT_FIXTURE_MISSING: {self.case_id} は record_input() を呼んでいない"
            )
        test_id, _, case_name = self.case_id.partition("/")
        target = self.fixtures_dir / test_id / f"{case_name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return target


@dataclass(frozen=True, slots=True)
class CaseWiring:
    """1 CaseがEvidenceを作れる状態にあるか。**試験を実行しない。**

    `marker_node_ids` は `@pytest.mark.case` を付けた試験、
    `observing_node_ids` はそのうち `case_observation` を受け取る試験である。
    両者を分けて持つのは、未配線の理由が「試験が無い」のか「試験はあるが
    観測していない」のかで、次にやることが変わるためである。
    """

    case_id: str
    marker_node_ids: tuple[str, ...]
    observing_node_ids: tuple[str, ...]

    @property
    def wired(self) -> bool:
        return bool(self.observing_node_ids)

    @property
    def unwired_reason(self) -> str | None:
        """未配線の理由。配線済みなら `None`。"""
        if self.wired:
            return None
        if not self.marker_node_ids:
            return UNWIRED_NO_MARKER
        return UNWIRED_MARKER_WITHOUT_OBSERVATION


def scan_case_wiring(*, registries: Path, scope: str, repo_root: Path) -> dict[str, CaseWiring]:
    """Scope対象Caseごとの配線状況を**静的に**調べる。

    走査するのはRegistryと試験Fileの構文木だけである。試験を実行せず、
    Adapterを呼ばず、Provider・DB・Evidence・一時Fileへ触れない。

    CLIの配線報告と `build_pytest_adapters()` は**この1関数を共有する**。
    別々に数えれば、片方が0件・片方が107件という食い違いがまた起きる。
    数え方が2つあることが問題であって、数字が違うことは症状にすぎない。
    """
    markers = scan_markers()
    wiring: dict[str, CaseWiring] = {}
    for case_id in load_scope_case_ids(registries, scope):
        nodes = tuple(sorted(markers.get(case_id, ())))
        observing = tuple(node for node in nodes if _uses_observation(repo_root, node))
        wiring[case_id] = CaseWiring(
            case_id=case_id, marker_node_ids=nodes, observing_node_ids=observing
        )
    return wiring


def build_pytest_adapters(
    *, registries: Path, scope: str, repo_root: Path, fixtures_dir: Path
) -> dict[str, PytestCaseAdapter]:
    """`scan_case_wiring()` の結果からAdapterを組む。Case IDもNode IDも手入力しない。

    `case_observation` を使っていない試験のAdapterは作らない。作れば
    実行時に `NO_OBSERVATION_RECORDED` で落ちるだけで、未配線であることが
    「実行失敗」に見えてしまう。配線済みと未配線は別の状態である。
    """
    policies = load_event_observation_policies(registries)
    wiring = scan_case_wiring(registries=registries, scope=scope, repo_root=repo_root)
    return {
        case_id: PytestCaseAdapter(
            case_id=case_id,
            node_ids=case.observing_node_ids,
            repo_root=repo_root,
            fixtures_dir=fixtures_dir,
            event_observation_policy=policies.get(case_id),
        )
        for case_id, case in wiring.items()
        if case.wired
    }


def _uses_observation(repo_root: Path, node_id: str) -> bool:
    """その試験関数が `case_observation` を受け取っているか。"""
    import ast

    path_part, _, func_name = node_id.partition("::")
    path = repo_root / path_part
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func_name:
            names = {arg.arg for arg in node.args.args}
            return "case_observation" in names
    return False


def _validate_result(case_id: str, result: CaseRunResult) -> None:
    if result.case_id != case_id:
        raise CaseRunnerError(
            f"ADAPTER_CASE_MISMATCH: {case_id} のAdapterが {result.case_id} を返した"
        )
    if result.side_effects is None:
        # ここを 0 で埋めない。観測していないことと 0 件だったことは別である。
        raise CaseRunnerError(
            f"SIDE_EFFECTS_NOT_OBSERVED: {case_id} は副作用を観測していない。"
            "未配線Caseをゼロ副作用として扱わない"
        )


def run_scope(
    *,
    registries: Path,
    scope: str,
    adapters: Mapping[str, CaseAdapter],
    evidence_dir: Path,
    repo_root: Path,
    environment_manifest: Path,
    case_ids: Iterable[str] | None = None,
) -> RunOutcome:
    """配線済みCaseを走らせてEvidenceを出す。未配線Caseは実行しない。"""
    report = wiring_report(registries, scope, adapters)
    if report.unknown:
        raise CaseRunnerError(
            f"UNKNOWN_ADAPTERS: Registryに無いCaseへAdapterがある: {list(report.unknown)}"
        )

    targets = report.required if case_ids is None else tuple(case_ids)
    unexpected = sorted(set(targets) - set(report.required))
    if unexpected:
        raise CaseRunnerError(f"OUT_OF_SCOPE_CASES: {unexpected}")

    emitted: list[tuple[str, Path]] = []
    skipped: list[str] = []
    failures: list[tuple[str, str]] = []

    for case_id in targets:
        adapter = adapters.get(case_id)
        if adapter is None:
            skipped.append(case_id)
            continue
        try:
            result = adapter()
            _validate_result(case_id, result)
            path = emit_case_evidence(
                result,
                evidence_dir=evidence_dir,
                repo_root=repo_root,
                environment_manifest=environment_manifest,
                registries=registries,
            )
        except (CaseRunnerError, EvidenceEmissionError) as exc:
            # 握り潰さない。どのCaseがなぜEvidenceを作れなかったかを残す。
            failures.append((case_id, str(exc)))
            continue
        emitted.append((case_id, path))

    return RunOutcome(
        emitted=tuple(emitted),
        skipped_unwired=tuple(skipped),
        failures=tuple(failures),
    )


def main(argv: list[str] | None = None) -> int:
    """配線状況だけを報告する。**Caseを実行せず、Evidenceを出さない。**

    以前ここは `{}` を固定で渡しており、同じFileの `build_pytest_adapters()`
    が107件を返す一方でCLIは0件と報告していた。数え方が2つあったのが原因で、
    数字の食い違いはその症状だった。いまは `scan_case_wiring()` を共有する。

    「全部配線できた」はCaseの成功でもRuntime GOでもない。未配線が残る間は
    非0で終わるという既存の契約をそのまま保つ。
    """
    parser = argparse.ArgumentParser(
        description="Case Runnerの配線状況を報告する。Caseは実行せず、Evidenceも出さない。"
    )
    parser.add_argument("--registries", required=True, type=Path)
    parser.add_argument("--release-scope", required=True)
    args = parser.parse_args(argv)

    wiring = scan_case_wiring(
        registries=args.registries, scope=args.release_scope, repo_root=REPO_ROOT
    )
    report = wiring_report_for_case_ids(
        args.registries,
        args.release_scope,
        [case_id for case_id, case in wiring.items() if case.wired],
    )
    payload = report.as_dict()
    # 未配線を数字だけで残さない。理由は実物から導いたものだけを書く。
    payload["unwired_reasons"] = {
        case_id: wiring[case_id].unwired_reason for case_id in report.unwired
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
