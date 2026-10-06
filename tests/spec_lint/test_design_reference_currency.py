"""旧設計書File名への参照の分類（TASK-DESIGN-MIGRATION-HARDENING-001 項目7）。

`design-v1.8-runtime-go.md` は改名で**存在しないPath**になった。
残ってよい参照（起きたことの記録）と、残ってはいけない参照（運用上の指し先）は
同じ文字列であり、意図だけが違う。意図は機械には見えないので許可リストで固定する。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "tools" / "check_design_reference_currency.py"

sys.path.insert(0, str(REPO_ROOT / "tools"))

from check_design_reference_currency import (  # noqa: E402
    ALLOWED_HISTORICAL_REFERENCES,
    CURRENT_DESIGN,
    LEGACY_NAMES,
    PRIVATE_HISTORY_ALLOWLIST,
    allowed_references,
    find_references,
)


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(CHECKER), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_checker_passes_on_the_committed_tree() -> None:
    """現在の木では、旧名参照がすべて既知の歴史参照であること。"""
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_current_design_exists() -> None:
    assert (REPO_ROOT / CURRENT_DESIGN).is_file()


def test_every_allowlisted_file_has_a_reason() -> None:
    """理由を書けないFileを許可リストへ入れないこと。

    理由が書けないなら、それは歴史記録ではなく直し忘れである。
    """
    for relative, reason in allowed_references(REPO_ROOT).items():
        assert reason.strip(), relative
        assert len(reason) > 5, f"{relative}: 理由が短すぎる"


def test_allowlist_matches_reality_exactly() -> None:
    """許可リストと実際の参照集合が一致すること。

    片方向だけの検査だと、参照しなくなったFileが残り続けて
    「何を守っているのか」が分からなくなる。
    """
    found = set(find_references(REPO_ROOT))
    assert found == set(allowed_references(REPO_ROOT))


def test_operational_files_do_not_reference_the_old_design() -> None:
    """運用上の指し先が旧名を指していないこと。

    ここが陳腐化していると、実行時に「Fileが無い」で落ちる。
    """
    operational = [
        ".github/workflows/ci.yml",
        "ci/github-actions-ci.yml",
        "run_verification.sh",
        "README.md",
        "tools/package_release_zip.py",
        "spec/spec-manifest.json",
        "run-checks.sh",
    ]
    for relative in operational:
        path = REPO_ROOT / relative
        assert path.is_file(), relative
        text = path.read_text(encoding="utf-8")
        for legacy in LEGACY_NAMES:
            assert legacy not in text, f"{relative} が旧設計書名を指している"
        assert relative not in allowed_references(REPO_ROOT)


def test_new_stale_reference_is_detected(tmp_path: Path) -> None:
    """許可外Fileが旧名を参照したら失敗すること。"""
    (tmp_path / CURRENT_DESIGN).write_text("x\n", encoding="utf-8")
    (tmp_path / "brand_new_script.sh").write_text(
        f"#!/bin/sh\npython tools/lint_spec.py --design {LEGACY_NAMES[0]}\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "NEW_STALE_REFERENCE" in result.stderr
    assert "brand_new_script.sh" in result.stderr


def test_stale_baseline_entry_is_detected(tmp_path: Path) -> None:
    """許可Fileが参照しなくなったら失敗すること（リストの陳腐化）。"""
    (tmp_path / CURRENT_DESIGN).write_text("x\n", encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "BASELINE_STALE" in result.stderr


def test_missing_current_design_is_detected(tmp_path: Path) -> None:
    """現行設計書が無い状態をPASSにしないこと。"""
    result = _run(tmp_path)
    assert result.returncode == 1
    assert CURRENT_DESIGN in result.stderr


def test_synthetic_historical_evidence_is_not_rewritten(tmp_path: Path, monkeypatch) -> None:
    """合成の履歴文書を検査器が書換えず、旧参照の消失は拒否する。私的履歴は別監査。"""
    import check_design_reference_currency as checker

    document = tmp_path / "synthetic-history.md"
    original = ("SYNTHETIC HISTORY\n" + LEGACY_NAMES[0] + "\n").encode()
    document.write_bytes(original)
    (tmp_path / CURRENT_DESIGN).write_text("synthetic current design\n")
    monkeypatch.setattr(
        checker,
        "ALLOWED_HISTORICAL_REFERENCES",
        {document.name: "synthetic historical reference fixture"},
    )
    assert checker.main(["--root", str(tmp_path)]) == 0
    assert document.read_bytes() == original
    altered = b"synthetic historical reference incorrectly erased\n"
    document.write_bytes(altered)
    assert checker.main(["--root", str(tmp_path)]) == 1
    assert document.read_bytes() == altered, "Checker must not silently repair history"


def _supplement(root: Path, entries: object) -> None:
    path = root / PRIVATE_HISTORY_ALLOWLIST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")


def test_without_the_private_supplement_only_the_public_table_applies(tmp_path: Path) -> None:
    """補助許可Fileが無い木（配布コピー）では、本表だけが効くこと。"""
    assert allowed_references(tmp_path) == ALLOWED_HISTORICAL_REFERENCES


def test_a_record_outside_both_tables_is_still_a_new_stale_reference(tmp_path: Path) -> None:
    """非公開の記録と同じ名前でも、補助許可Fileが無ければ許可しないこと。

    補助Fileを消せば記録側の参照が通ってしまう、という形にしない。
    """
    (tmp_path / CURRENT_DESIGN).write_text("x\n", encoding="utf-8")
    record = tmp_path / "blocked" / "records" / "BLK-SYNTHETIC.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"design": LEGACY_NAMES[0]}), encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "NEW_STALE_REFERENCE blocked/records/BLK-SYNTHETIC.json" in result.stderr


def test_the_private_supplement_is_merged_when_present(tmp_path: Path) -> None:
    """補助許可Fileがある木（非公開側）では、両方の項目が効くこと。"""
    (tmp_path / CURRENT_DESIGN).write_text("x\n", encoding="utf-8")
    record = tmp_path / "synthetic-record.json"
    record.write_text(json.dumps({"design": LEGACY_NAMES[0]}), encoding="utf-8")
    _supplement(tmp_path, {"synthetic-record.json": "synthetic historical record fixture"})
    merged = allowed_references(tmp_path)
    assert merged["synthetic-record.json"] == "synthetic historical record fixture"
    assert set(ALLOWED_HISTORICAL_REFERENCES) <= set(merged)


@pytest.mark.parametrize(
    "entries",
    [
        {},
        {"synthetic-record.json": ""},
        {"synthetic-record.json": "short"},
        ["synthetic-record.json"],
        {"tools/check_design_reference_currency.py": "duplicate of the public table"},
    ],
)
def test_a_malformed_private_supplement_is_input_invalid(tmp_path: Path, entries: object) -> None:
    """補助許可Fileの形が不正なら、空扱いにせず入力不正で止めること。"""
    (tmp_path / CURRENT_DESIGN).write_text("x\n", encoding="utf-8")
    _supplement(tmp_path, entries)
    result = _run(tmp_path)
    assert result.returncode == 4
    assert PRIVATE_HISTORY_ALLOWLIST in result.stderr


def test_an_unreadable_private_supplement_is_input_invalid(tmp_path: Path) -> None:
    (tmp_path / CURRENT_DESIGN).write_text("x\n", encoding="utf-8")
    path = tmp_path / PRIVATE_HISTORY_ALLOWLIST
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 4
