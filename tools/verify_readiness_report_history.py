#!/usr/bin/env python3
"""保存 Report を Git の固定入力から再現する。履歴の Python は実行しない。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import audit_readiness_consumers as audit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs/audit/readiness-consumer-gap.history.json"
MAX_INPUTS = 10000
MAX_SOURCE_BYTES = 16 * 1024 * 1024


class HistoryError(ValueError):
    """機械判定可能な分類だけを返し、Source や Git stderr を漏らさない。"""


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
            ["git", "--no-replace-objects", "-c", "protocol.allow=never", *args],  # noqa: S607
            cwd=root,
            input=data,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise HistoryError("HISTORY_GIT_TIMEOUT") from exc
    except OSError as exc:
        raise HistoryError("HISTORY_GIT_UNAVAILABLE") from exc
    if result.returncode:
        raise HistoryError("HISTORY_GIT_UNAVAILABLE")
    return result.stdout


def _commit(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise HistoryError("HISTORY_COMMIT_INVALID")
    return value


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise HistoryError("HISTORY_PATH_INVALID")
    parsed = PurePosixPath(value)
    if (
        not parsed.parts
        or parsed.is_absolute()
        or parsed.as_posix() != value
        or any(part in {".", ".."} for part in parsed.parts)
        or any(char in value for char in ("\\", "\0", "\n", "\r", ":"))
    ):
        raise HistoryError("HISTORY_PATH_INVALID")
    return value


def _input_path(path: str) -> bool:
    return (
        path.endswith(".py")
        and path != audit.SELF
        and any(path.startswith(directory + "/") for directory in audit.SCAN_DIRS)
    )


def _order(path: str) -> tuple[int, str]:
    # 既存 Report の走査順（Directory 契約順、Directory 内 Code Point 順）を保つ。
    return next(
        i for i, directory in enumerate(audit.SCAN_DIRS) if path.startswith(directory + "/")
    ), path


def _tree(root: Path, commit: str) -> dict[str, str]:
    listing = _git(root, "ls-tree", "-rz", "--full-tree", commit)
    result: dict[str, str] = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if not _input_path(path) and path not in {
            audit.SELF,
            str(audit.DEFAULT_OUT.relative_to(audit.ROOT)),
            str(audit.DEFAULT_MD.relative_to(audit.ROOT)),
        }:
            continue
        _path(path)
        if mode not in {"100644", "100755"} or kind != "blob":
            raise HistoryError("HISTORY_NON_REGULAR_FILE")
        result[path] = oid
    return result


def _blobs(root: Path, tree: dict[str, str]) -> dict[str, bytes]:
    paths = sorted(tree)
    if len(paths) > MAX_INPUTS:
        raise HistoryError("HISTORY_INPUT_LIMIT")
    raw = _git(root, "cat-file", "--batch", data="".join(tree[p] + "\n" for p in paths).encode())
    result: dict[str, bytes] = {}
    offset = 0
    for path in paths:
        end = raw.find(b"\n", offset)
        header = raw[offset:end].split()
        if end < 0 or len(header) != 3 or header[:2] != [tree[path].encode(), b"blob"]:
            raise HistoryError("HISTORY_BLOB_INVALID")
        size = int(header[2])
        if not 0 <= size <= MAX_SOURCE_BYTES:
            raise HistoryError("HISTORY_INPUT_LIMIT")
        start = end + 1
        content = raw[start : start + size]
        if len(content) != size or raw[start + size : start + size + 1] != b"\n":
            raise HistoryError("HISTORY_BLOB_INVALID")
        result[path] = content
        offset = start + size + 1
    if offset != len(raw):
        raise HistoryError("HISTORY_BLOB_INVALID")
    return result


def build_record(root: Path, commit: str) -> dict[str, Any]:
    """発見済み commit の全入力を列挙する。保存・Owner 決定は行わない。"""
    commit = _commit(commit)
    tree = _tree(root, commit)
    blobs = _blobs(root, tree)
    report_path = str(audit.DEFAULT_OUT.relative_to(audit.ROOT))
    md_path = str(audit.DEFAULT_MD.relative_to(audit.ROOT))
    required = (audit.SELF, report_path, md_path)
    if any(path not in blobs for path in required):
        raise HistoryError("HISTORY_REPORT_ABSENT")
    return {
        "source_commit": commit,
        "source_tree": _git(root, "rev-parse", commit + "^{tree}").decode().strip(),
        "generator": {"path": audit.SELF, "sha256": sha(blobs[audit.SELF])},
        "report": {"path": report_path, "sha256": sha(blobs[report_path])},
        "markdown": {"path": md_path, "sha256": sha(blobs[md_path])},
        "inputs": [
            {"path": path, "sha256": sha(blobs[path])}
            for path in sorted((p for p in blobs if _input_path(p)), key=_order)
        ],
    }


def reproduce(root: Path, record: dict[str, Any]) -> tuple[bytes, bytes]:
    """入力の欠落・追加・順序・Hash と JSON/Markdown 全 Bytes を照合する。"""
    measured = build_record(root, record.get("source_commit"))
    if measured != record:
        raise HistoryError("HISTORY_MANIFEST_MISMATCH")
    blobs = _blobs(root, _tree(root, record["source_commit"]))
    try:
        sources = [(item["path"], blobs[item["path"]].decode("utf-8")) for item in record["inputs"]]
        report = audit.measure(sources)
    except (UnicodeError, SyntaxError, ValueError) as exc:
        raise HistoryError("HISTORY_SOURCE_INVALID") from exc
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    markdown = audit._markdown(report).encode("utf-8")
    if raw != blobs[record["report"]["path"]] or markdown != blobs[record["markdown"]["path"]]:
        raise HistoryError("HISTORY_REPRODUCTION_MISMATCH")
    return raw, markdown


def verify(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if (
        set(manifest) != {"version", "saved_report", "package_input"}
        or manifest["version"] != "1.0"
    ):
        raise HistoryError("HISTORY_MANIFEST_INVALID")
    saved = manifest["saved_report"]
    raw, markdown = reproduce(root, saved)
    for key, expected in (("report", raw), ("markdown", markdown)):
        path = root / _path(saved[key]["path"])
        if path.is_symlink() or path.read_bytes() != expected:
            raise HistoryError("HISTORY_SAVED_REPORT_MISMATCH")
    binding = manifest["package_input"]
    package_path = root / _path(binding["package"]["path"])
    if package_path.is_symlink() or sha(package_path.read_bytes()) != binding["package"]["sha256"]:
        raise HistoryError("HISTORY_PACKAGE_MISMATCH")
    package = json.loads(package_path.read_bytes())
    record = binding["record"]
    if package["bound_to"]["consumer_audit"] != record["report"]:
        raise HistoryError("HISTORY_PACKAGE_BINDING_MISMATCH")
    reproduce(root, record)
    return {
        "status": "HISTORICAL_REPRODUCED",
        "saved_report_commit": saved["source_commit"],
        "package_input_commit": record["source_commit"],
        "is_runtime_evidence": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        result = verify(ROOT, json.loads(args.manifest.read_text(encoding="utf-8")))
    except (HistoryError, OSError, ValueError, KeyError, TypeError) as exc:
        code = str(exc) if isinstance(exc, HistoryError) else "HISTORY_INPUT_INVALID"
        print(json.dumps({"status": "UNVERIFIED", "error": code}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
