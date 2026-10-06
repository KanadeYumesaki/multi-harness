"""spec shardが設計書の該当範囲だけを含むことの検査。

CLAUDE.md §4 は「設計書全文をPromptへ入れない。`spec/`の該当shardとRegistryの
該当部分だけを使う」と定める。shardが設計書全文へ膨張するとこの前提が崩れるが、
`lint_spec.py` はmanifest Hashの一致しか見ないため、膨張したままでも0違反で通る。

回帰対象の不具合（2026-08-06検出・修正済み）:
`build_spec_shards.subsection()` の見出し行が `^## <heading>(?:\\s.*)?$` と書かれ、
`(?ms)` のDOTALL下で `(?:\\s.*)?` が貪欲に文書末尾まで飲み込んでいた。続く `$` が
EOFで成立し、終端 `(?=^## <next>|\\Z)` が `\\Z` 側で満たされるため、最初の試行で
マッチが確定し後戻りが起きない。結果として全subsection shardが設計書末尾まで伸び、
`02-input-read.md` は設計書6,422行に対し10,940行（同一節を2回含む）になっていた。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = REPO_ROOT / "spec"
REGISTRIES = REPO_ROOT / "design-source" / "registries"

sys.path.insert(0, str(REPO_ROOT / "tools"))
import build_spec_shards as bss  # type: ignore[import-not-found]  # noqa: E402

sys.path.pop(0)


def _design_path() -> Path:
    design = REPO_ROOT / "design-v1.25-runtime-go.md"
    assert design.is_file(), f"ASCII固定名の設計書が見つからない: {design}"
    return design


@pytest.fixture(scope="module")
def design_text() -> str:
    return _design_path().read_text(encoding="utf-8")


def _shard_files() -> list[Path]:
    return sorted(SPEC_DIR.rglob("*.md"))


# --------------------------------------------------------------------------
# 切り出し関数そのものの契約
# --------------------------------------------------------------------------

# build_spec_shards.main() が実際に使う (見出し, 終端見出し) の組。
SUBSECTION_PAIRS = [
    ("1.11 Canonical JSON／Hash規約", "1.12 Token Profile Snapshot"),
    ("1.12 Token Profile Snapshot", "1.13 Trust Level／Data Classification"),
    ("1.16 Input Read Capability／Control-Data境界", "1.17 WSL2 Mount Boundary"),
    ("1.17 WSL2 Mount Boundary", "1.18 Artifact保持・GC・容量"),
    ("14.4 Central Policy Distribution", "14.5 Secret Manager"),
    ("15.9 Core Schema Catalog v1", "15.10 追加Runbook"),
]


@pytest.mark.parametrize(("heading", "terminator"), SUBSECTION_PAIRS)
def test_subsection_stops_before_terminator(
    design_text: str, heading: str, terminator: str
) -> None:
    """切り出し結果へ終端見出しが混入しない。"""
    body = bss.subsection(design_text, heading, terminator)
    assert body.startswith(f"## {heading}")
    assert f"## {terminator}" not in body


@pytest.mark.parametrize(("heading", "terminator"), SUBSECTION_PAIRS)
def test_subsection_does_not_reach_end_of_document(
    design_text: str, heading: str, terminator: str
) -> None:
    """設計書末尾まで伸びない。不具合時は全件が設計書とほぼ同じ行数になっていた。"""
    body = bss.subsection(design_text, heading, terminator)
    design_lines = design_text.count("\n") + 1
    body_lines = body.count("\n") + 1
    assert body_lines < design_lines / 2, (
        f"{heading!r} が {body_lines} 行に膨張している（設計書 {design_lines} 行）"
    )


def test_subsection_raises_when_terminator_absent() -> None:
    """終端見出しが無い場合に黙って末尾まで返さない（Fail-Closed）。"""
    text = "## A heading\nbody line\n\n## C heading\nmore\n"
    with pytest.raises(ValueError, match="terminator"):
        bss.subsection(text, "A heading", "B heading")


def test_subsection_does_not_match_prefix_heading() -> None:
    """`1.1` が `1.11` を誤って掴まない。"""
    text = "## 1.11 Target\nkeep\n\n## 1.12 Next\ndrop\n"
    with pytest.raises(ValueError):
        bss.subsection(text, "1.1", "1.12 Next")


def test_section_raises_when_terminator_absent() -> None:
    text = "# 3. Third\nbody\n\n# 9. Ninth\nother\n"
    with pytest.raises(ValueError, match="terminator"):
        bss.section(text, 3, 4)


def test_section_stops_before_terminator() -> None:
    text = "# 3. Third\nbody\n\n# 4. Fourth\nother\n"
    assert bss.section(text, 3, 4) == "# 3. Third\nbody\n"


# --------------------------------------------------------------------------
# 生成済みshardの境界
# --------------------------------------------------------------------------


def test_no_shard_is_larger_than_the_design(design_text: str) -> None:
    design_lines = design_text.count("\n") + 1
    oversized = []
    for path in _shard_files():
        lines = path.read_text(encoding="utf-8").count("\n") + 1
        if lines >= design_lines:
            oversized.append(f"{path.relative_to(REPO_ROOT)}: {lines} lines")
    assert not oversized, "設計書以上に大きいshard:\n" + "\n".join(oversized)


def test_each_top_level_section_appears_in_at_most_one_shard() -> None:
    """同一節が複数shardへ重複して現れない。

    不具合時は `# 3. MVP0-A` が 10-mvp0a.md と 02-input-read.md の双方に存在した。
    """
    import re

    heading_re = re.compile(r"(?m)^# (\d+)\. ")
    owners: dict[str, list[str]] = {}
    for path in _shard_files():
        text = path.read_text(encoding="utf-8")
        for number in set(heading_re.findall(text)):
            owners.setdefault(number, []).append(str(path.relative_to(REPO_ROOT)))

    duplicated = {n: files for n, files in owners.items() if len(files) > 1}
    assert not duplicated, f"複数shardへ重複した節: {duplicated}"


def test_each_top_level_section_appears_at_most_once_within_a_shard() -> None:
    """1つのshard内で同一節見出しが2回現れない。"""
    import re

    heading_re = re.compile(r"(?m)^# (\d+)\. ")
    violations = []
    for path in _shard_files():
        found = heading_re.findall(path.read_text(encoding="utf-8"))
        for number in set(found):
            if found.count(number) > 1:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}: '# {number}.' x{found.count(number)}"
                )
    assert not violations, "shard内で重複した節見出し:\n" + "\n".join(violations)


def test_subsection_shards_contain_no_top_level_section_heading() -> None:
    """subsection由来のshardは節見出しを含まない（含むなら範囲を越えている）。"""
    import re

    heading_re = re.compile(r"(?m)^# \d+\. ")
    subsection_shards = [
        "01-canonical-hash.md",
        "02-input-read.md",
        "03-token-profile.md",
        "30-policy-freshness.md",
    ]
    violations = []
    for name in subsection_shards:
        text = (SPEC_DIR / name).read_text(encoding="utf-8")
        if heading_re.search(text):
            violations.append(f"{name}: {heading_re.findall(text)}")
    assert not violations, "subsection shardへの節見出し混入:\n" + "\n".join(violations)


def test_schema_shards_contain_exactly_one_schema_each() -> None:
    """20-schemas/*.md は1 Schemaだけを含む。

    不具合時は最後のSchema（InputReadCapability）が§15.9末尾以降を丸ごと吸収し、
    1,151行になっていた。
    """
    import re

    schema_re = re.compile(r"(?m)^### \d+\. ([A-Za-z][A-Za-z0-9]+)\s*$")
    violations = []
    for path in sorted((SPEC_DIR / "20-schemas").glob("*.md")):
        found = schema_re.findall(path.read_text(encoding="utf-8"))
        if len(found) != 1:
            violations.append(f"{path.name}: {len(found)} schemas {found[:5]}")
        elif found[0] != path.stem:
            violations.append(f"{path.name}: contains {found[0]}")
    assert not violations, "Schema shardの境界異常:\n" + "\n".join(violations)


# --------------------------------------------------------------------------
# 再生成の決定性と同期
# --------------------------------------------------------------------------


def test_regeneration_is_deterministic_and_matches_checked_in_shards(
    tmp_path: Path,
) -> None:
    """同じ設計書から2回生成して同一Bytesになり、かつ現在のspec/と一致する。"""
    outputs = []
    for index in range(2):
        out = tmp_path / f"gen{index}"
        result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
            [
                sys.executable,
                "tools/build_spec_shards.py",
                "--design",
                _design_path().name,
                "--registries",
                str(REGISTRIES.relative_to(REPO_ROOT)),
                "--out",
                str(out),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(
            {
                p.relative_to(out).as_posix(): p.read_bytes()
                for p in sorted(out.rglob("*"))
                if p.is_file()
            }
        )

    assert outputs[0] == outputs[1], "spec shard生成が非決定的"

    checked_in = {
        p.relative_to(SPEC_DIR).as_posix(): p.read_bytes()
        for p in sorted(SPEC_DIR.rglob("*"))
        if p.is_file()
    }
    assert checked_in == outputs[0], (
        "spec/ が設計書から再生成した内容と一致しない。"
        "build_spec_shards.py を実行して再生成すること。"
    )
