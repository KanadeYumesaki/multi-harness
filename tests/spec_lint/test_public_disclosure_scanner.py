"""開示検査器（`tools/scan_public_disclosure.py`）の受理・拒否契約。

## なぜ要るか

公開前の必要条件は「検出が 0 件」ではなく「検出を所在と根拠で区分し、未分類を
残さない」ことである（`docs/PUBLICATION.md`）。分類の除外が広すぎたり、行が
変わっても効き続けたりすると、区分は形だけになる。ここでは検出値を合成し、

* 検出すべき形を検出する
* 例示・予約済みの形を検出しない
* 分類は path・line・kind・行 Hash の完全一致でだけ効き、行が変われば戻る
* Glob・絶対 Path・親参照・壊れた入力を拒む
* 結果のどこにも値を出さない

を確かめる。

## 合成値の作り方

検出値は実行時に部品を連結して作る。この File 自体の行が検出に掛からないように
するためで、実在の個人・鍵・宛先を指さない。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import scan_public_disclosure as scanner

pytestmark = pytest.mark.spec_lint

# 実行時に組み立てる合成値。どれも実在の宛先・鍵を指さない。
HOME_NAME = "zq" + "fixtureperson"
PERSONAL_HOME = "/" + "home" + "/" + HOME_NAME + "/work/repo"
PERSONAL_EMAIL = "fixture.person" + "@" + "mail-provider" + ".net"
SSH_KEY = "ssh-" + "ed25519 " + "AAAA" + "C3NzaC1lZDI1NTE5" + "A" * 32
FINGERPRINT = "SHA256" + ":" + "Q" * 43
TERM = "zq" + "privateterm"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(root: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(  # noqa: S603 - 固定 argv、shell 不使用
        ["git", "-C", str(root), *args],  # noqa: S607
        check=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "user.name", "Synthetic fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "synthetic")
    return root


def _entry(path: str, line: int, kind: str, text: str, classification: str) -> dict[str, object]:
    return {
        "path": path,
        "line": line,
        "kind": kind,
        "line_sha256": _sha(text),
        "classification": classification,
        "reason": "合成の試験入力。実在の個人を指さない",
    }


def _write_allowlist(root: Path, entries: list[dict[str, object]]) -> Path:
    path = root.parent / "allowlist.json"
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    return path


def _run(
    root: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, dict[str, Any], str]:
    report = root.parent / "report.json"
    code = scanner.main(["--repo", str(root), "--report", str(report), *extra])
    captured = capsys.readouterr()
    text = captured.out + captured.err
    payload = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
    if report.exists():
        text += report.read_text(encoding="utf-8")
    return code, payload, text


def _assert_no_value(text: str) -> None:
    for value in (HOME_NAME, PERSONAL_EMAIL, SSH_KEY, FINGERPRINT, TERM):
        assert value not in text


# ---------------------------------------------------------------------------
# 行単位の判定
# ---------------------------------------------------------------------------


def test_each_disclosure_kind_is_detected() -> None:
    assert scanner.scan_line(f"cd {PERSONAL_HOME}", frozenset()) == ["personal_home"]
    assert scanner.scan_line(f"contact: {PERSONAL_EMAIL}", frozenset()) == ["email"]
    assert scanner.scan_line(SSH_KEY + " fixture", frozenset()) == ["ssh_public_key"]
    assert scanner.scan_line(f"key {FINGERPRINT}", frozenset()) == ["ssh_fingerprint"]
    assert scanner.scan_line(f"by {TERM}", frozenset({_sha(TERM)})) == ["sensitive_term"]


@pytest.mark.parametrize(
    "line",
    [
        "cd /" + "home/user/work",
        "cd /" + "home/runner/work",
        "cd /" + "home/<user>/work",
        "cd /" + "home/$USER/work",
        "cd /" + "home/u/ws",
        "C:" + "\\Users\\%USERNAME%\\work",
        "mail " + "someone" + "@" + "example.com",
        "mail " + "someone" + "@" + "host.invalid",
        "mail " + "someone" + "@" + "host.test",
        "uses: actions/checkout" + "@" + "v4.1.1",
    ],
)
def test_generic_or_reserved_forms_are_not_findings(line: str) -> None:
    assert scanner.scan_line(line, frozenset()) == []


def test_sensitive_terms_are_only_checked_when_a_hash_list_is_given() -> None:
    assert scanner.scan_line(f"by {TERM}", frozenset()) == []


# ---------------------------------------------------------------------------
# 作業木・履歴と分類
# ---------------------------------------------------------------------------


def test_unclassified_findings_fail_and_the_report_carries_no_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"docs/a.md": f"line one\ncd {PERSONAL_HOME}\n"})
    code, report, text = _run(root, capsys)
    assert code == scanner.EXIT_FINDINGS
    assert report["result"] == "UNRESOLVED_FINDINGS"
    assert report["values_exported"] is False
    assert [(f["path"], f["line"], f["kind"]) for f in report["unresolved"]] == [
        ("docs/a.md", 2, "personal_home")
    ]
    _assert_no_value(text)


def test_an_exact_allowlist_entry_classifies_the_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line = f"contact: {PERSONAL_EMAIL}"
    root = _repo(tmp_path, {"tests/t.py": f"# fixture\n# {line}\n"})
    allowlist = _write_allowlist(
        root, [_entry("tests/t.py", 2, "email", f"# {line}", "SYNTHETIC_TEST_VECTOR")]
    )
    code, report, text = _run(root, capsys, "--allowlist", str(allowlist))
    assert code == scanner.EXIT_OK
    assert report["result"] == "NO_UNRESOLVED_FINDINGS"
    assert report["counts"] == {
        "unresolved": {},
        "classified": {"SYNTHETIC_TEST_VECTOR": 1},
        "unused_allowlist_entries": 0,
    }
    _assert_no_value(text)


def test_a_changed_line_returns_to_unclassified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line = f"# contact: {PERSONAL_EMAIL}"
    root = _repo(tmp_path, {"tests/t.py": f"{line} (edited)\n"})
    allowlist = _write_allowlist(
        root, [_entry("tests/t.py", 1, "email", line, "SYNTHETIC_TEST_VECTOR")]
    )
    code, report, _ = _run(root, capsys, "--allowlist", str(allowlist))
    assert code == scanner.EXIT_FINDINGS
    assert len(report["unresolved"]) == 1
    assert report["unused_allowlist_entries"] == [
        {"path": "tests/t.py", "line": 1, "kind": "email"}
    ]


def test_an_unused_allowlist_entry_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"README.md": "nothing to report\n"})
    allowlist = _write_allowlist(
        root, [_entry("README.md", 1, "email", "gone", "DOCUMENTED_PLACEHOLDER")]
    )
    code, report, _ = _run(root, capsys, "--allowlist", str(allowlist))
    assert code == scanner.EXIT_FINDINGS
    assert report["unresolved"] == []
    assert report["counts"]["unused_allowlist_entries"] == 1


def test_history_scan_finds_a_value_removed_from_the_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"notes.md": f"{SSH_KEY} old\n"})
    (root / "notes.md").write_text("replaced\n", encoding="utf-8")
    _git(root, "commit", "--quiet", "-am", "remove")
    code, _, _ = _run(root, capsys)
    assert code == scanner.EXIT_OK
    code, report, text = _run(root, capsys, "--history")
    assert code == scanner.EXIT_FINDINGS
    assert [(f["source"], f["kind"]) for f in report["unresolved"]] == [
        ("history", "ssh_public_key")
    ]
    assert report["scope"]["history_all_refs"] is True
    _assert_no_value(text)


def test_commit_metadata_is_scanned_without_a_line_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"a.txt": "a\n"})
    _git(root, "commit", "--quiet", "--allow-empty", "-m", f"see {PERSONAL_HOME}")
    code, report, text = _run(root, capsys, "--history")
    assert code == scanner.EXIT_FINDINGS
    (finding,) = report["unresolved"]
    assert finding["source"] == "commit_metadata"
    assert "line_sha256" not in finding
    _assert_no_value(text)


def test_sensitive_terms_are_matched_by_hash_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"LICENSE": f"Copyright (c) 2026 {TERM}\n"})
    terms = tmp_path / "terms.json"
    terms.write_text(json.dumps({"sha256": [_sha(TERM)]}), encoding="utf-8")
    code, report, text = _run(root, capsys, "--sensitive-terms", str(terms))
    assert code == scanner.EXIT_FINDINGS
    assert report["scope"]["sensitive_terms_checked"] is True
    _assert_no_value(text)
    allowlist = _write_allowlist(
        root,
        [
            _entry(
                "LICENSE",
                1,
                "sensitive_term",
                f"Copyright (c) 2026 {TERM}",
                "REQUIRED_COPYRIGHT_NOTICE",
            )
        ],
    )
    code, _, _ = _run(root, capsys, "--sensitive-terms", str(terms), "--allowlist", str(allowlist))
    assert code == scanner.EXIT_OK


# ---------------------------------------------------------------------------
# 広い除外・壊れた入力を拒む
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["tests/*.py", "tests/t?.py", "tests/[ab].py", "/abs/t.py", "../t.py", "tests//t.py", ""],
)
def test_allowlist_paths_must_name_exactly_one_relative_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    root = _repo(tmp_path, {"README.md": "x\n"})
    allowlist = _write_allowlist(root, [_entry(path, 1, "email", "x", "SYNTHETIC_TEST_VECTOR")])
    with pytest.raises(SystemExit) as raised:
        _run(root, capsys, "--allowlist", str(allowlist))
    assert raised.value.code == scanner.EXIT_INPUT_INVALID


def test_a_sensitive_term_may_only_be_a_required_copyright_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"README.md": "x\n"})
    allowlist = _write_allowlist(
        root, [_entry("README.md", 1, "sensitive_term", "x", "SYNTHETIC_TEST_VECTOR")]
    )
    with pytest.raises(SystemExit) as raised:
        _run(root, capsys, "--allowlist", str(allowlist))
    assert raised.value.code == scanner.EXIT_INPUT_INVALID


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 2, "entries": []}),
        json.dumps({"version": 1, "entries": [{"path": "a"}]}),
    ],
)
def test_a_malformed_allowlist_is_input_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    root = _repo(tmp_path, {"README.md": "x\n"})
    allowlist = root.parent / "allowlist.json"
    allowlist.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        _run(root, capsys, "--allowlist", str(allowlist))
    assert raised.value.code == scanner.EXIT_INPUT_INVALID


@pytest.mark.parametrize(
    "content", ["{not json", json.dumps({"sha256": [TERM]}), json.dumps(["x"])]
)
def test_a_sensitive_term_list_must_hold_hashes_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    root = _repo(tmp_path, {"README.md": "x\n"})
    terms = tmp_path / "terms.json"
    terms.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        _run(root, capsys, "--sensitive-terms", str(terms))
    assert raised.value.code == scanner.EXIT_INPUT_INVALID
    _assert_no_value(capsys.readouterr().err)


# ---------------------------------------------------------------------------
# 配布される分類一覧
# ---------------------------------------------------------------------------


def test_the_distributed_allowlist_is_well_formed_and_narrow() -> None:
    root = Path(__file__).resolve().parents[2]
    entries = scanner.load_allowlist(root / "ci/disclosure-allowlist.json")
    keys = [(e["path"], e["line"], e["kind"]) for e in entries]
    assert len(keys) == len(set(keys))
    for entry in entries:
        assert (root / entry["path"]).is_file(), entry["path"]
