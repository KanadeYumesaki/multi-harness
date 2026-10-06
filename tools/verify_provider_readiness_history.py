#!/usr/bin/env python3
"""保存 `provider-readiness-v3` を Git の固定入力から再現する。履歴の Python は実行しない。

## なぜ要るか

`docs/audit/provider-readiness-v3.json` には2つの要求が同時にかかっていた。

* 作業木から再生成して Byte 一致すること
* その SHA-256 が回答済み Decision Package 5件へ束縛されたまま動かないこと

監査器は `src/harness` の Module を数える。したがって Production Module を1件でも
足すと、**再生成しても再生成しなくても**どちらかが破れる。CC-03 で実測した
（再生成すると Package 由来の試験が8件、しないと再現試験が1件落ちる）。

Owner は案 A を選んだ。過去 Report は**固定 Git 入力から**再現し、保存 Bytes と
Package 束縛は動かさない。現行コードの監査は別に続ける。

## 何を実行するか

実行するのは**現行の監査器だけ**である。固定 commit から取り出すのは監査器の
「入力」であって、履歴の Python を走らせない。生成器の履歴 Hash は Manifest へ
記録し、入力集合の欠落・追加・改変を検出する材料にする。

`readiness-consumer-gap` 側（`verify_readiness_report_history.py`）と同じ考え方だが、
あちらは `measure(sources)` に Source 文字列を渡せる。こちらの `measure()` は
Filesystem を直接読むため、入力を一時 Directory へ配置してから現行監査器を
そこへ向ける。**監査器の判定 Code は1行も変えない。**

## 失敗を握り潰さない

分類済みの Error Code だけを返し、Source や Git の stderr を漏らさない
（不変条件#7／#9）。判定不能を成功へ倒さない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs/audit/provider-readiness-v3.history.json"

#: 実行する現行監査器。**これだけが Python として動く。**
GENERATOR = "tools/audit_chat_provider_readiness.py"
#: 監査器が `SymbolIndex` を取る先。同じく現行を使う。
GENERATOR_SUPPORT = "tools/audit_provider_readiness_v2.py"
GENERATORS = (GENERATOR, GENERATOR_SUPPORT)

#: 監査器が読む入力。**正本の Path 契約をそのまま写す。**件数は書かない。
FIXED_INPUTS = (
    "CLAUDE.md",
    "registry-snapshot.json",
    "design-source/registries/route-policy.yaml",
    "docs/audit/chat-provider-readiness.json",
)
SCHEMA_PREFIX = "schemas/core/"
SCHEMA_SUFFIX = ".schema.json"
SOURCE_PREFIX = "src/harness/"
SOURCE_SUFFIX = ".py"
DESIGN_PREFIX = "design-v"
DESIGN_SUFFIX = "-runtime-go.md"

MAX_INPUTS = 10000
MAX_SOURCE_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 60
AUDIT_TIMEOUT_SECONDS = 600


class HistoryError(ValueError):
    """機械判定可能な分類だけを返す。Source や Git stderr を漏らさない。"""


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
            timeout=GIT_TIMEOUT_SECONDS,
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
    """Repository 相対の通常 Path だけを通す。外へ出る形を拒否する。"""
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


def _is_input(path: str) -> bool:
    """監査器の入力かどうか。**一覧を手入力しない**（不変条件#18）。"""
    if path in FIXED_INPUTS:
        return True
    if path.startswith(SCHEMA_PREFIX) and path.endswith(SCHEMA_SUFFIX):
        return True
    if path.startswith(SOURCE_PREFIX) and path.endswith(SOURCE_SUFFIX):
        return True
    return "/" not in path and path.startswith(DESIGN_PREFIX) and path.endswith(DESIGN_SUFFIX)


def _report_paths() -> tuple[str, str]:
    return ("docs/audit/provider-readiness-v3.json", "docs/audit/provider-readiness-v3.md")


def _tree(root: Path, commit: str) -> dict[str, str]:
    """固定 commit の対象 Blob を列挙する。通常 File 以外は拒否する。"""
    report_path, md_path = _report_paths()
    wanted = {report_path, md_path, *GENERATORS}
    listing = _git(root, "ls-tree", "-rz", "--full-tree", commit)
    result: dict[str, str] = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if not _is_input(path) and path not in wanted:
            continue
        _path(path)
        if mode not in {"100644", "100755"} or kind != "blob":
            # Symlink や Submodule を入力として受け取らない。
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


def build_record(root: Path, commit: Any) -> dict[str, Any]:
    """固定 commit の入力集合を Git から列挙する。保存も Owner 決定も行わない。"""
    commit = _commit(commit)
    tree = _tree(root, commit)
    blobs = _blobs(root, tree)
    report_path, md_path = _report_paths()
    required = (*GENERATORS, report_path, md_path)
    if any(path not in blobs for path in required):
        raise HistoryError("HISTORY_REPORT_ABSENT")
    return {
        "source_commit": commit,
        "source_tree": _git(root, "rev-parse", commit + "^{tree}").decode().strip(),
        "generators": [{"path": path, "sha256": sha(blobs[path])} for path in sorted(GENERATORS)],
        "report": {"path": report_path, "sha256": sha(blobs[report_path])},
        "markdown": {"path": md_path, "sha256": sha(blobs[md_path])},
        "inputs": [
            {"path": path, "sha256": sha(blobs[path])} for path in sorted(blobs) if _is_input(path)
        ],
    }


def reproduce(root: Path, record: dict[str, Any]) -> tuple[bytes, bytes]:
    """入力の欠落・追加・改変を照合し、現行監査器で Report を作り直す。

    一時 Directory へ入力を配置し、そこへ**現行**監査器だけを持ち込んで走らせる。
    履歴の Python は配置しても実行しない（配置するのは監査器が読む入力だけ）。
    """
    measured = build_record(root, record.get("source_commit"))
    if measured != record:
        # commit だけで一致を仮定しない。集合・順序・Hash まで一致を要求する。
        raise HistoryError("HISTORY_MANIFEST_MISMATCH")
    blobs = _blobs(root, _tree(root, record["source_commit"]))

    with tempfile.TemporaryDirectory(prefix="provider-readiness-history-") as workspace:
        stage = Path(workspace)
        for item in record["inputs"]:
            target = stage / _path(item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blobs[item["path"]])
        tools_dir = stage / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        for generator in GENERATORS:
            # **現行**を持ち込む。履歴の生成器は Hash 照合にだけ使う。
            shutil.copy2(root / generator, stage / generator)

        produced = stage / "reproduced.json"
        produced_md = stage / "reproduced.md"
        try:
            completed = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
                [
                    sys.executable,
                    str(stage / GENERATOR),
                    "--out",
                    str(produced),
                    "--md-out",
                    str(produced_md),
                ],
                cwd=stage,
                capture_output=True,
                check=False,
                timeout=AUDIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise HistoryError("HISTORY_AUDIT_TIMEOUT") from exc
        except OSError as exc:
            raise HistoryError("HISTORY_AUDIT_UNAVAILABLE") from exc
        if completed.returncode or not produced.is_file() or not produced_md.is_file():
            raise HistoryError("HISTORY_AUDIT_FAILED")
        raw = produced.read_bytes()
        markdown = produced_md.read_bytes()

    if raw != blobs[record["report"]["path"]] or markdown != blobs[record["markdown"]["path"]]:
        raise HistoryError("HISTORY_REPRODUCTION_MISMATCH")
    return raw, markdown


def _read_regular(root: Path, relative: str) -> bytes:
    path = root / _path(relative)
    if path.is_symlink() or not path.is_file():
        raise HistoryError("HISTORY_SAVED_REPORT_MISMATCH")
    return path.read_bytes()


def package_bindings(root: Path, commit: str) -> list[dict[str, str]]:
    """回答時点の参照集合をManifestの一覧とは独立にGitから導出する。"""
    commit = _commit(commit)
    listing = _git(root, "ls-tree", "-rz", "--full-tree", commit, "--", "docs/decision")
    tree: dict[str, str] = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = _path(raw_path.decode("utf-8"))
        if not path.endswith((".json", ".md")):
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise HistoryError("HISTORY_NON_REGULAR_FILE")
        tree[path] = oid
    needle = _report_paths()[0].encode("utf-8")
    return [
        {"path": path, "sha256": sha(content)}
        for path, content in sorted(_blobs(root, tree).items())
        if needle in content
    ]


def verify(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """保存 Report の再現と、それを参照する Package の Bytes 保全を確かめる。"""
    if (
        set(manifest) != {"version", "saved_report", "bound_packages", "package_source_commit"}
        or manifest["version"] != "1.0"
    ):
        raise HistoryError("HISTORY_MANIFEST_INVALID")

    saved = manifest["saved_report"]
    raw, markdown = reproduce(root, saved)
    for key, expected in (("report", raw), ("markdown", markdown)):
        if _read_regular(root, saved[key]["path"]) != expected:
            raise HistoryError("HISTORY_SAVED_REPORT_MISMATCH")

    bound = manifest["bound_packages"]
    if not isinstance(bound, list) or not bound:
        raise HistoryError("HISTORY_MANIFEST_INVALID")
    report_path = saved["report"]["path"]
    for item in bound:
        if set(item) != {"path", "sha256"}:
            raise HistoryError("HISTORY_MANIFEST_INVALID")
        content = _read_regular(root, item["path"])
        # **回答 Field だけでなく Package 全体の Bytes** を照合する。
        if sha(content) != item["sha256"]:
            raise HistoryError("HISTORY_PACKAGE_MISMATCH")
        if report_path.encode("utf-8") not in content:
            # 参照していない Package を束縛一覧へ入れていないこと。
            raise HistoryError("HISTORY_PACKAGE_BINDING_MISMATCH")

    expected_packages = package_bindings(root, manifest["package_source_commit"])
    if bound != expected_packages:
        # 一覧の欠落・余分・重複・順序変更とHashの追随変更を拒否する。
        raise HistoryError("HISTORY_PACKAGE_SET_MISMATCH")

    return {
        "status": "HISTORICAL_REPRODUCED",
        "saved_report_commit": saved["source_commit"],
        "bound_package_count": len(bound),
        "is_runtime_evidence": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--emit-record",
        metavar="COMMIT",
        help="固定 commit から Manifest の saved_report 部分を作って表示する",
    )
    args = parser.parse_args(argv)
    try:
        if args.emit_record:
            print(json.dumps(build_record(ROOT, args.emit_record), ensure_ascii=False, indent=2))
            return 0
        result = verify(ROOT, json.loads(args.manifest.read_text(encoding="utf-8")))
    except (HistoryError, OSError, ValueError, KeyError, TypeError) as exc:
        code = str(exc) if isinstance(exc, HistoryError) else "HISTORY_INPUT_INVALID"
        print(json.dumps({"status": "UNVERIFIED", "error": code}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
