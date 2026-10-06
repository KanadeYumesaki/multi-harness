"""英語正本と日本語同期ミラーの一致（Owner Decision E-1 第3項）。

同内容2Fileは、片方だけが更新された瞬間に「正本を名乗るものが2つあり
突き合わせが無い」状態へ落ちる。命名規則を決めるだけでは防げないので、
一致を検査する側を置く。

Design Hash は `sha256(File Bytes)` であり、`registry-snapshot.json` の
`design_sha256`、`spec/spec-manifest.json` の `source_hash`、README、
BLOCKED Record の `design_sha256` がその値へ束縛される。Bytesが1つでも違えば
別の設計書であり、どちらの束縛が正しいか判定できない（不変条件#9）。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = REPO_ROOT / "design-v1.25-runtime-go.md"
MIRROR = (
    REPO_ROOT
    / "マルチプロバイダーAI業務実行統制基盤_フェーズ別詳細設計書_v1.25_Runtime_GO判定実行版.md"
)
CHECKER = REPO_ROOT / "tools" / "check_design_mirror.py"


def _run(canonical: Path, mirror: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(CHECKER), "--canonical", str(canonical), "--mirror", str(mirror)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def test_both_design_files_exist() -> None:
    assert CANONICAL.is_file(), "英語名の正本が無い"
    assert MIRROR.is_file(), "日本語名の同期ミラーが無い"


def test_committed_files_are_byte_identical() -> None:
    """行末や正規化の違いも許容しない。Bytes完全一致である。"""
    assert CANONICAL.read_bytes() == MIRROR.read_bytes()


def test_checker_passes_on_the_committed_tree() -> None:
    result = _run(CANONICAL, MIRROR)
    assert result.returncode == 0, result.stdout + result.stderr
    digest = "sha256:" + hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    assert digest in result.stdout


def test_checker_fails_when_the_mirror_drifts(tmp_path: Path) -> None:
    """1 Byteでも違えば止まること。「だいたい同じ」を通さない。"""
    canonical = tmp_path / "canonical.md"
    mirror = tmp_path / "mirror.md"
    canonical.write_text("## フェーズ別詳細設計書 v1.9（x）\n", encoding="utf-8")
    mirror.write_text("## フェーズ別詳細設計書 v1.9（x）\n ", encoding="utf-8")
    result = _run(canonical, mirror)
    assert result.returncode == 1
    assert "out of sync" in result.stderr


def test_checker_fails_when_a_side_is_missing(tmp_path: Path) -> None:
    """片方が消えている状態をPASSにしない。"""
    canonical = tmp_path / "canonical.md"
    canonical.write_text("x\n", encoding="utf-8")
    result = _run(canonical, tmp_path / "absent.md")
    assert result.returncode == 1
    assert "missing" in result.stderr


def test_design_version_is_derived_from_the_title_not_hand_entered() -> None:
    """生成物の版番号を設計書表題から導くこと（不変条件#18）。

    以前は `build_spec_shards.py` と `build_registry_snapshot.py` へ `"1.8"` と
    手入力されており、v1.9へ改名しても生成物は `1.8` を主張し続けた。
    導出へ変えたので、v1.10化では版番号の手当てが不要になる。
    """
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from design_identity import design_version

    # 期待値もFile名から導く。ここへ版番号を書くと、改訂のたびに
    # **この試験のほうが**古い版を「現行」と主張し始める。
    expected = re.fullmatch(r"design-v(\d+\.\d+)-runtime-go\.md", CANONICAL.name)
    assert expected is not None, f"正本File名から版番号を読めない: {CANONICAL.name}"
    version = expected.group(1)
    assert design_version(CANONICAL) == version, "表題とFile名の版番号が食い違う"
    assert design_version(MIRROR) == version

    # 禁止する手入力Literalも導出する。現行版と、改名検査が知っている全旧版。
    # 新しい版が出るたびに LEGACY_NAMES へ足す運用なので、ここは自動で増える。
    from check_design_reference_currency import LEGACY_NAMES

    known = {version}
    for name in LEGACY_NAMES:
        legacy = re.search(r"v(\d+\.\d+)", name)
        if legacy:
            known.add(legacy.group(1))
    assert len(known) >= 4, f"既知の設計書版が少なすぎる: {sorted(known)}"

    for tool in ("build_spec_shards.py", "build_registry_snapshot.py"):
        source = (REPO_ROOT / "tools" / tool).read_text(encoding="utf-8")
        assert "design_version(" in source, f"{tool} が版番号を導出していない"
        # 版番号の手入力が復活していないこと。
        for known_version in sorted(known):
            for hand_entered in (f'"{known_version}"', f"'{known_version}'"):
                assert hand_entered not in source, f"{tool} に版番号の手入力が残る: {hand_entered}"


def test_generated_artifacts_agree_on_the_derived_version() -> None:
    """生成物の版番号が設計書表題と一致すること。"""
    import json

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from design_identity import design_version

    expected = design_version(CANONICAL)
    snapshot = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    manifest = json.loads((REPO_ROOT / "spec" / "spec-manifest.json").read_text(encoding="utf-8"))
    assert snapshot["design_version"] == expected
    assert manifest["source_version"] == expected
    assert manifest["source_file"] == CANONICAL.name


def test_unreadable_title_fails_closed(tmp_path: Path) -> None:
    """表題が読めないとき既定値へ落とさず停止すること。"""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from design_identity import design_version

    broken = tmp_path / "broken.md"
    broken.write_text("# 表題が無い\n", encoding="utf-8")
    with pytest.raises(ValueError, match="版番号を読めない"):
        design_version(broken)
