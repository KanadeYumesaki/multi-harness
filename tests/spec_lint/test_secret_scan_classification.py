"""Secret Scan 分類検査器（`tools/check_secret_scan_classification.py`）の受理・拒否契約。

## なぜ要るか

`.gitleaksignore` に書いた位置は Gitleaks が報告しなくなる。除外が広すぎたり、行が
変わっても効き続けたりすれば、新しい資格情報を見逃す。検査器は

* `path:rule:line` の完全一致だけを受け付け、Glob や Commit 無し以外の形を拒む
* 除外と分類記録を 1 対 1 に対応させる
* 分類の根拠を現在の行から再計算し、行が変われば失敗する
* 値を伏せた Gitleaks Report と突合し、未分類・余剰を失敗にする

ことを約束する。ここでは合成の Root を作ってそれを確かめる。

## 合成値の作り方

試験値は実行時に部品を連結して作る。この File 自体の行を Secret Scanner の
検出にしないためであり、実在の資格情報ではない。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import check_secret_scan_classification as checker

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]

# 目印語と連番を持つ合成値。構造上の根拠が再計算できる。
VECTOR = "syn" + "thetic-" + "abcdefgh" + "-" + "0" * 8
# 構造上の根拠を持たない値（目印語・連番・交互連番のどれも無い）。
NO_EVIDENCE = "q7Zp" + "2Lx9" + "Rm4T" + "v8Kw"
FIXTURE = "tests/test_fixture_values.py"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line(value: str) -> str:
    return "VALUE = " + json.dumps(value)


def _root(tmp_path: Path, *, value: str = VECTOR, path: str = FIXTURE) -> Path:
    root = tmp_path / "root"
    target = root / path
    target.parent.mkdir(parents=True)
    target.write_text(f"# synthetic fixture\n{_line(value)}\n", encoding="utf-8")
    return root


def _record(value: str = VECTOR, path: str = FIXTURE, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": path,
        "rule": "generic-api-key",
        "line": 2,
        "line_sha256": _sha(_line(value)),
        "classification": "SYNTHETIC_TEST_VECTOR",
        "reason": "合成の試験値",
        "evidence": {"0": checker.synthetic_evidence(value)},
    }
    record.update(overrides)
    return record


def _write(root: Path, records: list[dict[str, Any]], ignore: list[str] | None = None) -> list[str]:
    if ignore is None:
        ignore = [f"{r['path']}:{r['rule']}:{r['line']}" for r in records]
    (root / ".gitleaksignore").write_text("\n".join(["# exact", *ignore]) + "\n")
    (root / "classification.json").write_text(
        json.dumps({"version": 1, "findings": records}), encoding="utf-8"
    )
    return ["--root", str(root), "--classification", "classification.json"]


def _report(tmp_path: Path, findings: list[tuple[str, str, int]], secret: str = "REDACTED") -> str:
    path = tmp_path / "gitleaks.json"
    path.write_text(
        json.dumps(
            [{"File": f, "RuleID": r, "StartLine": n, "Secret": secret} for f, r, n in findings]
        ),
        encoding="utf-8",
    )
    return str(path)


def _main(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = checker.main(args)
    out = capsys.readouterr().out
    return code, out


# ---------------------------------------------------------------------------
# 構造上の合成根拠
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("placeholder-" + "x" * 8, ["MARKER_WORD"]),
        ("zz-" + "abcdef" + "-zz", ["SEQUENTIAL_RUN"]),
        ("zz-" + "987654" + "-zz", ["SEQUENTIAL_RUN"]),
        ("zz-" + "1a2b3c4d" + "-zz", ["INTERLEAVED_PROGRESSION", "INTERLEAVED_RUN"]),
        ("zz" + "1a2b3c" + "zz", ["INTERLEAVED_RUN"]),
        (NO_EVIDENCE, []),
    ],
)
def test_synthetic_evidence_is_structural(value: str, expected: list[str]) -> None:
    assert checker.synthetic_evidence(value) == expected


# ---------------------------------------------------------------------------
# 受理
# ---------------------------------------------------------------------------


def test_a_consistent_classification_passes_with_a_matching_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()])
    report = _report(tmp_path, [(FIXTURE, "generic-api-key", 2)])
    code, out = _main([*args, "--report-kind", "dir", "--gitleaks-report", report], capsys)
    assert code == checker.EXIT_OK, out
    assert VECTOR not in out


# ---------------------------------------------------------------------------
# 位置と内容の束縛
# ---------------------------------------------------------------------------


def test_a_changed_line_fails_until_classified_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, value=VECTOR + "0")
    args = _write(root, [_record()])
    code, out = _main(args, capsys)
    assert code == checker.EXIT_MISMATCH
    assert "line content changed" in out
    assert VECTOR not in out


def test_an_ignore_entry_without_a_record_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    extra = f"{FIXTURE}:generic-api-key:1"
    args = _write(root, [_record()], ignore=[f"{FIXTURE}:generic-api-key:2", extra])
    code, out = _main(args, capsys)
    assert code == checker.EXIT_MISMATCH
    assert f"ignore entry without a classification record: {extra}" in out


def test_a_record_missing_from_the_ignore_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()], ignore=[])
    code, out = _main(args, capsys)
    assert code == checker.EXIT_MISMATCH
    assert "classification record not in the ignore file" in out


@pytest.mark.parametrize(
    "entry",
    [
        "tests/*.py:generic-api-key:2",
        "tests/test_fixture_values.py:generic-api-key",
        "tests/test_fixture_values.py:*:2",
        "0123abcd:tests/test_fixture_values.py:generic-api-key:2",
        "tests/test_fixture_values.py",
    ],
)
def test_only_exact_path_rule_line_ignore_entries_are_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entry: str
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()], ignore=[entry])
    with pytest.raises(SystemExit) as raised:
        _main(args, capsys)
    assert raised.value.code == checker.EXIT_INPUT_INVALID


def test_a_duplicate_ignore_entry_is_input_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    entry = f"{FIXTURE}:generic-api-key:2"
    args = _write(root, [_record()], ignore=[entry, entry])
    with pytest.raises(SystemExit) as raised:
        _main(args, capsys)
    assert raised.value.code == checker.EXIT_INPUT_INVALID


# ---------------------------------------------------------------------------
# 分類ごとの根拠
# ---------------------------------------------------------------------------


def test_a_test_vector_outside_tests_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, path="docs/values.py")
    args = _write(root, [_record(path="docs/values.py")])
    code, out = _main(args, capsys)
    assert code == checker.EXIT_MISMATCH
    assert "only allowed under tests/" in out


def test_a_test_vector_used_by_production_code_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    (root / "src").mkdir()
    (root / "src/consumer.py").write_text(_line(VECTOR) + "\n", encoding="utf-8")
    args = _write(root, [_record()])
    code, out = _main(args, capsys)
    assert code == checker.EXIT_MISMATCH
    assert "is used outside tests: ['src/consumer.py']" in out
    assert VECTOR not in out


def test_a_value_without_structural_evidence_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, value=NO_EVIDENCE)
    args = _write(root, [_record(value=NO_EVIDENCE)])
    code, out = _main(args, capsys)
    assert code == checker.EXIT_MISMATCH
    assert "has no structural synthetic evidence" in out
    assert NO_EVIDENCE not in out


def test_recorded_evidence_must_match_the_recomputed_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record(evidence={"0": ["INTERLEAVED_RUN"]})])
    code, out = _main(args, capsys)
    assert code == checker.EXIT_MISMATCH
    assert "evidence differs from the record" in out


def test_an_expression_record_must_hold_no_string_literal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    record = _record(classification="PYTHON_EXPRESSION_NO_CREDENTIAL_LITERAL")
    del record["evidence"]
    args = _write(root, [record])
    code, out = _main(args, capsys)
    assert code == checker.EXIT_MISMATCH
    assert "line must be Python without any string literal" in out


# ---------------------------------------------------------------------------
# Gitleaks Report との突合
# ---------------------------------------------------------------------------


def test_an_unclassified_report_finding_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()])
    report = _report(
        tmp_path, [(FIXTURE, "generic-api-key", 2), ("src/new.py", "generic-api-key", 7)]
    )
    code, out = _main([*args, "--report-kind", "dir", "--gitleaks-report", report], capsys)
    assert code == checker.EXIT_MISMATCH
    assert "unclassified finding src/new.py:generic-api-key:7" in out


def test_a_classified_location_the_report_no_longer_finds_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()])
    code, out = _main(
        [*args, "--report-kind", "dir", "--gitleaks-report", _report(tmp_path, [])], capsys
    )
    assert code == checker.EXIT_MISMATCH
    assert "classified location not reported" in out


def test_an_unredacted_report_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()])
    report = _report(tmp_path, [(FIXTURE, "generic-api-key", 2)], secret=VECTOR)
    with pytest.raises(SystemExit) as raised:
        _main([*args, "--report-kind", "dir", "--gitleaks-report", report], capsys)
    assert raised.value.code == checker.EXIT_INPUT_INVALID
    assert VECTOR not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 壊れた・広い記録を拒む
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"path": "../outside.py"},
        {"path": "tests/*.py"},
        {"path": "/abs/tests/x.py"},
        {"line": 0},
        {"line": "2"},
        {"line_sha256": "not-a-hash"},
        {"reason": " "},
        {"classification": "LOOKS_FINE"},
    ],
)
def test_a_malformed_record_is_input_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], override: dict[str, Any]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record(**override)], ignore=[])
    with pytest.raises(SystemExit) as raised:
        _main(args, capsys)
    assert raised.value.code == checker.EXIT_INPUT_INVALID


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({"version": 2, "findings": []})])
def test_a_malformed_classification_file_is_input_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    root = _root(tmp_path)
    args = _write(root, [])
    (root / "classification.json").write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        _main(args, capsys)
    assert raised.value.code == checker.EXIT_INPUT_INVALID


# ---------------------------------------------------------------------------
# 配布される分類
# ---------------------------------------------------------------------------


def test_the_distributed_classification_recomputes_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """配布物に入る除外と分類記録が、現在の行から根拠を再計算できる。

    Gitleaks 本体の実行と突合はここでは行わない（Tool の入手は環境依存）。突合は
    `docs/PUBLICATION.md` の手順で、値を伏せた Report を渡して行う。
    """
    code, out = _main(["--root", str(REPO_ROOT)], capsys)
    assert code == checker.EXIT_OK, out


# 履歴の別Bytes/同じ見出し/過去の消費者は現在の分類で隠さない。
def _commit(root: Path) -> str:
    for argv in [
        ["/usr/bin/git", "init", "-q"],
        ["/usr/bin/git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "-qm",
            "synthetic fixture",
        ],
    ]:
        # 固定の合成Git操作。試験値をCommandへ展開しない。
        subprocess.run(argv, cwd=root, check=True, capture_output=True, timeout=10)  # noqa: S603
    return subprocess.run(  # noqa: S603 — 固定のGit読取り。
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def _git_report(tmp_path: Path, commits: list[str], *, end_line: int = 2) -> str:
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps(
            [
                {
                    "File": FIXTURE,
                    "RuleID": "generic-api-key",
                    "StartLine": 2,
                    "EndLine": end_line,
                    "Secret": "REDACTED",
                    "Commit": commit,
                }
                for commit in commits
            ]
        )
    )
    return str(path)


def test_history_finding_with_different_bytes_cannot_use_current_classification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, value=NO_EVIDENCE)
    old = _commit(root)
    (root / FIXTURE).write_text("# synthetic fixture\n" + _line(VECTOR) + "\n")
    args = _write(root, [_record()])
    current = _commit(root)
    report = _git_report(tmp_path, [current, old])
    code, out = _main([*args, "--report-kind", "git", "--gitleaks-report", report], capsys)
    assert code == checker.EXIT_MISMATCH
    assert "line content changed" in out
    assert old in out
    assert VECTOR not in out and NO_EVIDENCE not in out


def test_matching_history_commit_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()])
    commit = _commit(root)
    code, out = _main(
        [*args, "--report-kind", "git", "--gitleaks-report", _git_report(tmp_path, [commit])],
        capsys,
    )
    assert code == checker.EXIT_OK, out


def test_old_production_consumer_is_checked_at_the_finding_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    (root / "src").mkdir()
    consumer = root / "src/consumer.py"
    consumer.write_text(_line(VECTOR) + "\n")
    old = _commit(root)
    consumer.unlink()
    args = _write(root, [_record()])
    _commit(root)
    code, out = _main(
        [*args, "--report-kind", "git", "--gitleaks-report", _git_report(tmp_path, [old])], capsys
    )
    assert code == checker.EXIT_MISMATCH
    assert "is used outside tests" in out
    assert VECTOR not in out


def test_a_historical_complete_key_body_is_not_hidden_by_a_current_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    header = "-----" + "BEGIN PRIVATE KEY-----"
    line = _line(header)
    target = root / FIXTURE
    target.write_text(
        "# fixture\n" + line + "\n# " + header + "\n" + "A" * 64 + "\n-----END PRIVATE KEY-----\n"
    )
    old = _commit(root)
    target.write_text("# fixture\n" + line + "\n")
    record = _record(classification="INCOMPLETE_PRIVATE_KEY_TEST_MARKER", line_sha256=_sha(line))
    del record["evidence"]
    args = _write(root, [record])
    _commit(root)
    code, out = _main(
        [*args, "--report-kind", "git", "--gitleaks-report", _git_report(tmp_path, [old])], capsys
    )
    assert code == checker.EXIT_MISMATCH
    assert "complete PEM private key block" in out


@pytest.mark.parametrize("commit", ["f" * 40, "", "HEAD", "a" * 39, None])
def test_unknown_or_malformed_history_commit_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], commit: str | None
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()])
    _commit(root)
    report = _git_report(tmp_path, [commit])  # type: ignore[list-item]
    if commit == "f" * 40:
        code, out = _main([*args, "--report-kind", "git", "--gitleaks-report", report], capsys)
        assert code == checker.EXIT_INPUT_INVALID
        assert "GIT_READ_FAILED" in out
    else:
        with pytest.raises(SystemExit) as raised:
            _main([*args, "--report-kind", "git", "--gitleaks-report", report], capsys)
        assert raised.value.code == checker.EXIT_INPUT_INVALID


@pytest.mark.parametrize(
    "override",
    [
        {"StartLine": True},
        {"StartLine": "2"},
        {"StartLine": 0},
        {"EndLine": 1},
        {"File": "../outside"},
        {"File": "x\nvalue"},
        {"RuleID": []},
        {"File": None},
        {"Secret": None},
    ],
)
def test_malformed_report_fields_fail_closed_without_printing_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], override: dict[str, Any]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()])
    report = _report(tmp_path, [(FIXTURE, "generic-api-key", 2)])
    payload = json.loads(Path(report).read_text())
    payload[0].update(override)
    Path(report).write_text(json.dumps(payload))
    with pytest.raises(SystemExit) as raised:
        _main([*args, "--report-kind", "dir", "--gitleaks-report", report], capsys)
    assert raised.value.code == checker.EXIT_INPUT_INVALID


def test_report_kind_is_required_and_not_guessed_from_missing_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()])
    with pytest.raises(SystemExit) as raised:
        _main([*args, "--gitleaks-report", _report(tmp_path, [])], capsys)
    assert raised.value.code == checker.EXIT_INPUT_INVALID


def test_history_line_range_cannot_extend_past_the_git_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    args = _write(root, [_record()])
    commit = _commit(root)
    code, out = _main(
        [
            *args,
            "--report-kind",
            "git",
            "--gitleaks-report",
            _git_report(tmp_path, [commit], end_line=100),
        ],
        capsys,
    )
    assert code == checker.EXIT_MISMATCH
    assert "line range invalid" in out


def test_historical_python_is_not_executed_to_read_the_tokenizer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    tokenizer = root / checker.TOKENIZER_PATH
    tokenizer.parent.mkdir(parents=True)
    marker = tmp_path / "should-not-exist"
    name = "public-tokenizer-name"
    tokenizer.write_text(
        "BYTE_BOUND_TOKENIZER_NAME = "
        + json.dumps(name)
        + "\nraise RuntimeError('must not execute')\n"
    )
    target = root / FIXTURE
    target.write_text("# fixture\n" + _line(name) + "\n")
    record = _record(classification="PUBLIC_TOKENIZER_IDENTIFIER", line_sha256=_sha(_line(name)))
    del record["evidence"]
    args = _write(root, [record])
    commit = _commit(root)
    code, out = _main(
        [*args, "--report-kind", "git", "--gitleaks-report", _git_report(tmp_path, [commit])],
        capsys,
    )
    assert code == checker.EXIT_OK, out
    assert not marker.exists()
