"""保存 `provider-readiness-v3` の履歴再現 Verifier の契約。

ここで固定するのは次の4点である。

1. 固定 Git 入力から保存 Bytes を再現できる（Owner Decision 案A）。
2. 入力の欠落・改変・集合不一致・Report 改変・Package 不一致は**失敗する**。
3. 現行 Source を動かすと**現行監査の測定値は動く**が、**固定再現は動かない**。
4. 過去の再現を Runtime Evidence へ流用しない。

**Verifier が「通す」ことではなく「拒む」ことを測る。** 通るだけなら、入力を
空にしても通る Verifier と区別がつかない。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import verify_provider_readiness_history as history  # noqa: E402
from synthetic_history_fixture import PROVIDER_PACKAGES, build_provider_history  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "docs/audit/provider-readiness-v3.history.json"
AUDIT_TOOL = REPO_ROOT / "tools/audit_chat_provider_readiness.py"
SAVED_V3 = REPO_ROOT / "docs/audit/provider-readiness-v3.json"


@pytest.fixture(autouse=True)
def synthetic_history(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    fixture = build_provider_history(tmp_path_factory.mktemp("provider-contract") / "history")
    monkeypatch.setattr(request.module, "REPO_ROOT", fixture.root)
    monkeypatch.setattr(
        request.module,
        "MANIFEST_PATH",
        fixture.root / "docs/audit/provider-readiness-v3.history.json",
    )
    monkeypatch.setattr(
        request.module, "AUDIT_TOOL", fixture.root / "tools/audit_chat_provider_readiness.py"
    )
    monkeypatch.setattr(
        request.module, "SAVED_V3", fixture.root / "docs/audit/provider-readiness-v3.json"
    )


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _expect(manifest: dict[str, Any], code: str) -> None:
    with pytest.raises(history.HistoryError) as caught:
        history.verify(REPO_ROOT, manifest)
    assert str(caught.value) == code, f"期待した分類ではない: {caught.value}"


# -- 1. 成立 -----------------------------------------------------------------


def test_the_saved_report_reproduces_from_pinned_git_inputs() -> None:
    result = history.verify(REPO_ROOT, _manifest())
    assert result["status"] == "HISTORICAL_REPRODUCED"
    assert result["is_runtime_evidence"] is False
    assert result["bound_package_count"] == len(_manifest()["bound_packages"])


def test_the_manifest_pins_a_full_commit_and_every_input() -> None:
    """commit だけで一致を仮定していないこと。

    集合・Path・Hash まで記録していなければ、入力が入れ替わっても気付けない。
    """
    saved = _manifest()["saved_report"]
    assert len(saved["source_commit"]) == 40
    assert saved["inputs"], "入力集合が空である"
    for item in saved["inputs"]:
        assert set(item) == {"path", "sha256"}
        assert item["sha256"].startswith("sha256:")
    # 生成器も記録する。監査器が変われば再現が変わる。
    assert {item["path"] for item in saved["generators"]} == set(history.GENERATORS)


# -- 2. 拒否経路 -------------------------------------------------------------


def test_a_missing_input_is_rejected() -> None:
    """入力を Manifest から削って通してはいけない。"""
    manifest = _manifest()
    manifest["saved_report"]["inputs"].pop()
    _expect(manifest, "HISTORY_MANIFEST_MISMATCH")


def test_an_added_input_is_rejected() -> None:
    manifest = _manifest()
    manifest["saved_report"]["inputs"].append({"path": "CLAUDE.md", "sha256": "sha256:" + "0" * 64})
    _expect(manifest, "HISTORY_MANIFEST_MISMATCH")


def test_a_tampered_input_hash_is_rejected() -> None:
    manifest = _manifest()
    manifest["saved_report"]["inputs"][0]["sha256"] = "sha256:" + "0" * 64
    _expect(manifest, "HISTORY_MANIFEST_MISMATCH")


def test_a_reordered_input_set_is_rejected() -> None:
    """順序も契約である。走査順が変われば Report が変わりうる。"""
    manifest = _manifest()
    manifest["saved_report"]["inputs"].reverse()
    _expect(manifest, "HISTORY_MANIFEST_MISMATCH")


def test_a_tampered_report_hash_is_rejected() -> None:
    manifest = _manifest()
    manifest["saved_report"]["report"]["sha256"] = "sha256:" + "0" * 64
    _expect(manifest, "HISTORY_MANIFEST_MISMATCH")


def test_an_unknown_commit_is_rejected() -> None:
    manifest = _manifest()
    manifest["saved_report"]["source_commit"] = "0" * 40
    _expect(manifest, "HISTORY_GIT_UNAVAILABLE")


def test_a_short_commit_is_rejected() -> None:
    """短縮 commit を受け付けない。曖昧な指定で固定したことにしない。"""
    manifest = _manifest()
    manifest["saved_report"]["source_commit"] = "60c73eb"
    _expect(manifest, "HISTORY_COMMIT_INVALID")


def test_a_package_whose_bytes_moved_is_rejected() -> None:
    """回答 Field だけでなく Package 全体の Bytes を保全すること。"""
    manifest = _manifest()
    manifest["bound_packages"][0]["sha256"] = "sha256:" + "0" * 64
    _expect(manifest, "HISTORY_PACKAGE_MISMATCH")


def test_a_package_that_does_not_reference_the_report_is_rejected() -> None:
    """参照していない Package を束縛一覧へ紛れ込ませない。"""
    manifest = _manifest()
    other = REPO_ROOT / "docs/decision/OWNER-ANSWERS.yaml"
    manifest["bound_packages"].append(
        {"path": str(other.relative_to(REPO_ROOT)), "sha256": history.sha(other.read_bytes())}
    )
    _expect(manifest, "HISTORY_PACKAGE_BINDING_MISMATCH")


def test_an_empty_package_binding_is_rejected() -> None:
    """束縛一覧を空にして通してはいけない。空なら保全していない。"""
    manifest = _manifest()
    manifest["bound_packages"] = []
    _expect(manifest, "HISTORY_MANIFEST_INVALID")


def test_an_escaping_path_is_rejected() -> None:
    manifest = _manifest()
    manifest["bound_packages"][0]["path"] = "../outside.json"
    _expect(manifest, "HISTORY_PATH_INVALID")


def test_an_unknown_manifest_shape_is_rejected() -> None:
    manifest = _manifest()
    manifest["extra"] = 1
    _expect(manifest, "HISTORY_MANIFEST_INVALID")


def test_the_cli_reports_unverified_without_leaking_details() -> None:
    """失敗時も分類 Code だけを返し、Source や Git stderr を漏らさないこと。"""
    broken = copy.deepcopy(_manifest())
    broken["saved_report"]["source_commit"] = "0" * 40
    import tempfile

    with tempfile.TemporaryDirectory(prefix="history-cli-") as tmp:
        path = Path(tmp) / "manifest.json"
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
            [
                sys.executable,
                str(REPO_ROOT / "tools/verify_provider_readiness_history.py"),
                "--manifest",
                str(path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 1
    body = json.loads(result.stdout)
    assert body == {"status": "UNVERIFIED", "error": "HISTORY_GIT_UNAVAILABLE"}


# -- 3. 現行監査と固定再現の分離 ---------------------------------------------


def test_changing_current_source_moves_the_current_audit_but_not_the_history(
    tmp_path: Path,
) -> None:
    """現行 Source を足すと現行監査の測定値は動き、固定再現は動かないこと。

    これが案A の要点である。両者が同じ入力を見ていれば、Module を足すたびに
    保存 Report が壊れる。分かれていることを**実測で**示す。

    足した Module は必ず消す。消し忘れると次の実行から前提が変わる。
    """
    added = REPO_ROOT / "src/harness/_cc03b_history_probe.py"
    assert not added.exists(), "前回の一時 Module が残っている"

    def _current_counts() -> dict[str, Any]:
        out = tmp_path / f"current-{added.exists()}.json"
        result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
            [
                sys.executable,
                str(AUDIT_TOOL),
                "--out",
                str(out),
                "--md-out",
                str(out.with_suffix(".md")),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        return dict(json.loads(out.read_text(encoding="utf-8"))["source_index"])

    before_counts = _current_counts()
    before_history = history.verify(REPO_ROOT, _manifest())
    before_saved = SAVED_V3.read_bytes()

    added.write_text('"""一時 Probe。試験の最後に消す。"""\n', encoding="utf-8")
    try:
        after_counts = _current_counts()
        after_history = history.verify(REPO_ROOT, _manifest())
        after_saved = SAVED_V3.read_bytes()
    finally:
        added.unlink()

    assert after_counts["modules"] == before_counts["modules"] + 1, (
        "現行監査が Module 増を見ていない"
    )
    assert after_history == before_history, "固定再現が現行 Source に影響された"
    assert after_saved == before_saved, "保存 Report の Bytes が動いた"
    assert not added.exists(), "一時 Module を消していない"


@pytest.mark.parametrize("removed_path", PROVIDER_PACKAGES)
def test_every_omitted_package_is_rejected(removed_path: str) -> None:
    manifest = _manifest()
    manifest["bound_packages"] = [
        row for row in manifest["bound_packages"] if row["path"] != removed_path
    ]
    _expect(manifest, "HISTORY_PACKAGE_SET_MISMATCH")


def test_duplicate_package_is_rejected() -> None:
    manifest = _manifest()
    manifest["bound_packages"].append(copy.deepcopy(manifest["bound_packages"][0]))
    _expect(manifest, "HISTORY_PACKAGE_SET_MISMATCH")


def test_reordered_packages_are_rejected() -> None:
    manifest = _manifest()
    manifest["bound_packages"].reverse()
    _expect(manifest, "HISTORY_PACKAGE_SET_MISMATCH")


def test_package_hash_cannot_follow_modified_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest()
    path = manifest["bound_packages"][0]["path"]
    changed = (REPO_ROOT / path).read_bytes() + b"\n"
    manifest["bound_packages"][0]["sha256"] = history.sha(changed)
    read = history._read_regular
    monkeypatch.setattr(
        history, "_read_regular", lambda root, name: changed if name == path else read(root, name)
    )
    _expect(manifest, "HISTORY_PACKAGE_SET_MISMATCH")
