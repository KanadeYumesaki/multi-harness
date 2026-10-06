"""回答時点の Canon と現行監査入力を明示的に分ける。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chat_canon_binding import design_filename, git_bytes, verify_answer_time_canon

RECORD_BASELINE = "01abdeefb47fdfe40bb90ea30095a138ae73d9b0"


def _resolved(root: Path, record: Path) -> dict[str, Any]:
    relative = record.relative_to(root).as_posix()
    original = git_bytes(root, "show", f"{RECORD_BASELINE}:{relative}")
    if record.read_bytes() != original:
        raise ValueError("RECORDED_CANON_RECORD_CHANGED")
    return verify_answer_time_canon(root, json.loads(original))


def recorded_snapshot(root: Path, record: Path, *, current: bool = False) -> dict[str, Any]:
    if current:
        return json.loads((root / "registry-snapshot.json").read_bytes())
    bound = _resolved(root, record)["snapshot"]
    return json.loads(git_bytes(root, "show", f"{bound['commit']}:{bound['path']}"))


def recorded_design_text(root: Path, record: Path, *, current: bool = False) -> str:
    if current:
        snapshot = recorded_snapshot(root, record, current=True)
        return (root / design_filename(str(snapshot["design_version"]))).read_text(encoding="utf-8")
    bound = _resolved(root, record)["design"]
    return git_bytes(root, "show", f"{bound['commit']}:{bound['path']}").decode("utf-8")


def require_separate_current_output(
    current: bool, outputs: tuple[Path, ...], saved: tuple[Path, ...]
) -> None:
    # Both modes are read-only with respect to saved decision/audit records.
    root = Path(__file__).resolve().parents[1]
    directories = (root / "docs/decision", root / "docs/audit")
    protected = (
        *saved,
        *(p for directory in directories for p in directory.rglob("*") if p.is_file()),
    )
    for output in outputs:
        resolved = output.resolve()
        if any(resolved.is_relative_to(directory.resolve()) for directory in directories):
            raise ValueError("SAVED_RECORD_OUTPUT_DENIED")
        if any(
            resolved == path.resolve()
            or (output.exists() and path.exists() and output.samefile(path))
            for path in protected
        ):
            raise ValueError("SAVED_RECORD_OUTPUT_DENIED")
