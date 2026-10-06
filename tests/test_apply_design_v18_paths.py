from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import apply_design_v18  # noqa: E402


def test_apply_design_v18_normalizes_legacy_runtime_go_kit_paths(tmp_path: Path) -> None:
    """廃止済みkit参照を正本へ再挿入せず、root直下の実在Fileへ正規化する。"""
    legacy_kit = f"runtime-go-v{1}.{7}-kit"
    source = tmp_path / "source.md"
    output = tmp_path / "out.md"
    source.write_text(
        (REPO_ROOT / "design-v1.25-runtime-go.md").read_text(encoding="utf-8")
        + f"\nlegacy verifier: {legacy_kit}/verify_runtime_go.py\n"
        + f"legacy snapshot: {legacy_kit}/registry-snapshot.json\n",
        encoding="utf-8",
    )

    assert (
        apply_design_v18.main(
            [
                "--design",
                str(source),
                "--registries",
                str(REPO_ROOT / "design-source" / "registries"),
                "--out",
                str(output),
            ]
        )
        == 0
    )

    rendered = output.read_text(encoding="utf-8")
    assert legacy_kit not in rendered
    assert "legacy verifier: verify_runtime_go.py" in rendered
    assert "legacy snapshot: registry-snapshot.json" in rendered
