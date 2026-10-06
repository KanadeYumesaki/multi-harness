"""履歴の完全再現と、現在の監査を別々に実測して守る。

履歴は合成 Git 入力（`build_consumer_history`）、凍結 Report は合成の凍結 File で測る。
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import audit_readiness_consumers as audit
import verify_readiness_report_history as history


@pytest.fixture(autouse=True)
def synthetic_history(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synthetic_history_fixture import build_consumer_history

    fixture = build_consumer_history(tmp_path_factory.mktemp("consumer-contract") / "history")
    monkeypatch.setattr(history, "ROOT", fixture.root)
    monkeypatch.setattr(
        history, "DEFAULT_MANIFEST", fixture.root / "docs/audit/readiness-consumer-gap.history.json"
    )


@pytest.fixture
def synthetic_frozen_reports(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> None:
    """凍結 Report の既定 Path を合成の File へ向ける。

    保存 Report（非公開の監査記録）は公開用の配布コピーに無い。凍結を守る規則は
    File の有無ではなく出力先の照合なので、合成の凍結 File で同じ規則を測る。
    保存 Report そのものの保護は `tests/private_history/` の試験が確かめる。
    履歴の検証器も既定 Path から相対 Path を作るので、凍結の試験だけに使う。
    """
    frozen = tmp_path_factory.mktemp("frozen-consumer-report")
    report = frozen / "readiness-consumer-gap.json"
    markdown = frozen / "readiness-consumer-gap.md"
    report.write_text('{"synthetic_test_input": true}\n', encoding="utf-8")
    markdown.write_text("SYNTHETIC FROZEN REPORT\n", encoding="utf-8")
    monkeypatch.setattr(audit, "DEFAULT_OUT", report)
    monkeypatch.setattr(audit, "DEFAULT_MD", markdown)


@pytest.fixture
def manifest() -> dict:
    return json.loads(history.DEFAULT_MANIFEST.read_text(encoding="utf-8"))


def test_both_historical_inputs_and_package_hash_are_verified(manifest: dict) -> None:
    result = history.verify(history.ROOT, manifest)
    assert result["status"] == "HISTORICAL_REPRODUCED"
    assert result["is_runtime_evidence"] is False
    assert manifest["saved_report"]["report"] != manifest["package_input"]["record"]["report"]


@pytest.mark.parametrize(
    "change", ["missing", "extra", "reorder", "hash", "tree", "generator", "report"]
)
def test_changed_manifest_is_rejected(manifest: dict, change: str) -> None:
    record = copy.deepcopy(manifest["saved_report"])
    if change == "missing":
        record["inputs"].pop()
    elif change == "extra":
        record["inputs"].append({"path": "tools/unknown.py", "sha256": history.sha(b"")})
    elif change == "reorder":
        record["inputs"].reverse()
    elif change == "hash":
        record["inputs"][0]["sha256"] = history.sha(b"changed")
    elif change == "tree":
        record["source_tree"] = "0" * 40
    else:
        record[change]["sha256"] = history.sha(b"changed")
    with pytest.raises(history.HistoryError, match="HISTORY_MANIFEST_MISMATCH"):
        history.reproduce(history.ROOT, record)


def test_missing_history_never_falls_back_to_working_tree(manifest: dict) -> None:
    record = copy.deepcopy(manifest["saved_report"])
    record["source_commit"] = "0" * 40
    with pytest.raises(history.HistoryError, match="HISTORY_GIT_UNAVAILABLE"):
        history.reproduce(history.ROOT, record)


@pytest.mark.parametrize("value", ["HEAD", "--all", "../HEAD", "", None])
def test_only_full_commit_ids_are_accepted(value: object) -> None:
    with pytest.raises(history.HistoryError, match="HISTORY_COMMIT_INVALID"):
        history.build_record(history.ROOT, value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["../report", "/outside/report", "a/../b", "a\\b", "a\nb", "."])
def test_invalid_paths_are_rejected(value: str) -> None:
    with pytest.raises(history.HistoryError, match="HISTORY_PATH_INVALID"):
        history._path(value)


def test_git_timeout_stops_without_exposing_output() -> None:
    with (
        patch.object(history.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)),
        pytest.raises(history.HistoryError, match="^HISTORY_GIT_TIMEOUT$"),
    ):
        history._git(history.ROOT, "status")


def test_current_analyzer_change_cannot_rewrite_history(manifest: dict) -> None:
    original = audit.measure

    def changed(sources: list[tuple[str, str]] | None = None) -> dict:
        result = original(sources)
        result["counts"]["total"] += 1
        return result

    with (
        patch.object(audit, "measure", changed),
        pytest.raises(history.HistoryError, match="HISTORY_REPRODUCTION_MISMATCH"),
    ):
        history.reproduce(history.ROOT, manifest["saved_report"])


def test_changed_saved_report_and_package_are_rejected(tmp_path: Path, manifest: dict) -> None:
    for record in (manifest["saved_report"],):
        for key in ("report", "markdown"):
            path = record[key]["path"]
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((history.ROOT / path).read_bytes())
    package = manifest["package_input"]["package"]
    target = tmp_path / package["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((history.ROOT / package["path"]).read_bytes())
    original = history._git

    def read_real_history(_root: Path, *args: str, data: bytes | None = None) -> bytes:
        return original(history.ROOT, *args, data=data)

    report = tmp_path / manifest["saved_report"]["report"]["path"]
    before = report.read_bytes()
    with patch.object(history, "_git", read_real_history):
        report.write_bytes(before + b" ")
        with pytest.raises(history.HistoryError, match="HISTORY_SAVED_REPORT_MISMATCH"):
            history.verify(tmp_path, manifest)
        report.write_bytes(before)
        target.write_bytes(target.read_bytes() + b" ")
        with pytest.raises(history.HistoryError, match="HISTORY_PACKAGE_MISMATCH"):
            history.verify(tmp_path, manifest)


def test_live_scan_includes_new_files_once(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "first.py").write_text("answer = 1\n", encoding="utf-8")
    with patch.object(audit, "ROOT", tmp_path):
        before = audit.measure()
        (tools / "second.py").write_text("answer = 2\n", encoding="utf-8")
        with patch.object(audit, "_python_files", wraps=audit._python_files) as listing:
            after = audit.measure()
        assert listing.call_count == 1
        assert after["scanned_files"] == before["scanned_files"] + 1


def test_invalid_python_is_not_silently_counted_as_audited() -> None:
    with pytest.raises(ValueError, match="AUDIT_SOURCE_INVALID"):
        audit.measure([("tools/broken.py", "def (")])


@pytest.mark.parametrize("field", ["--out", "--md-out"])
@pytest.mark.parametrize("frozen", ["DEFAULT_OUT", "DEFAULT_MD"])
@pytest.mark.usefixtures("synthetic_frozen_reports")
def test_frozen_targets_cannot_be_overwritten(tmp_path: Path, field: str, frozen: str) -> None:
    protected = getattr(audit, frozen)
    before = protected.read_bytes()
    argv = {"--out": str(tmp_path / "new.json"), "--md-out": str(tmp_path / "new.md")}
    argv[field] = str(protected)
    with pytest.raises(SystemExit) as exc:
        audit.main([value for pair in argv.items() for value in pair])
    assert exc.value.code == 1
    assert protected.read_bytes() == before
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("synthetic_frozen_reports")
def test_symlink_alias_to_frozen_report_is_rejected(tmp_path: Path) -> None:
    alias = tmp_path / "alias.json"
    alias.symlink_to(audit.DEFAULT_OUT)
    with pytest.raises(SystemExit) as exc:
        audit.main(["--out", str(alias), "--md-out", str(tmp_path / "new.md")])
    assert exc.value.code == 1
    assert not (tmp_path / "new.md").exists()


@pytest.mark.usefixtures("synthetic_frozen_reports")
def test_new_report_is_reproducible_without_overwriting_existing(tmp_path: Path) -> None:
    before = audit.DEFAULT_OUT.read_bytes()
    first, second = tmp_path / "first", tmp_path / "second"
    for directory in (first, second):
        directory.mkdir()
        assert (
            audit.main(
                [
                    "--out",
                    str(directory / "live.json"),
                    "--md-out",
                    str(directory / "live.md"),
                ]
            )
            == 0
        )
    assert (first / "live.json").read_bytes() == (second / "live.json").read_bytes()
    assert (first / "live.md").read_bytes() == (second / "live.md").read_bytes()
    with pytest.raises(SystemExit) as exc:
        audit.main(["--out", str(first / "live.json"), "--md-out", str(first / "live.md")])
    assert exc.value.code == 1
    assert audit.DEFAULT_OUT.read_bytes() == before
