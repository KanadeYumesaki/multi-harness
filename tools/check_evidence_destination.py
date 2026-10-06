#!/usr/bin/env python3
"""Evidence出力先がRepository外のWSL native filesystemであることを検査する。

## なぜ検査が要るか

Runbookは同じ手順のなかで2つを要求していた。

1. Evidenceを`runtime-evidence/<release-id>/`（**Repository内**）へ出力する
2. `git status --short`が空であること

**両立しない。** Evidenceを1件でも出力した時点で1が2を壊す。
Evidenceは観測結果であり、観測対象のソースツリーの一部ではない。
出力先をRepository外へ固定し、その前提を機械検査する。

## Windows共有Mountを拒否する理由

`/mnt/c`、`drvfs`、`9p`、`overlay`上ではPOSIXのfsync・Rename・Permissionの
意味が保証されない。設計書§13は「Temp write → File fsync → Atomic Rename →
Directory fsync」を要求するが、これらのfilesystemではその順序が守られたかを
確かめられない。**確かめられない場所で作った証跡は証跡にならない**（不変条件#12）。

## 終了Code

| Code | 意味 |
|---|---|
| `0` | 出力先はRepository外のnative filesystem |
| `3` | Repository内、`/mnt/*`配下、または禁止filesystem type |
| `4` | 入出力が不正 |

`3` は `collect_runtime_environment.py` の「WSL2以外またはforeign filesystem上で
停止する」終了Codeと揃えてある。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

# POSIXのfsync・Rename保証が無い、または外部から差し替えられるfilesystem。
FOREIGN_FILESYSTEMS: Final[frozenset[str]] = frozenset(
    {"9p", "drvfs", "cifs", "smb3", "nfs", "nfs4", "fuseblk", "fuse", "overlay", "vboxsf"}
)

MOUNTINFO: Final[Path] = Path("/proc/self/mountinfo")


def filesystem_type(target: Path) -> str | None:
    """`target` を含む最長一致Mount pointのfilesystem typeを返す。

    `/proc/self/mountinfo` から読む。`findmnt` の出力を解析しない
    （外部Commandの表示形式へ依存しない）。
    """
    try:
        lines = MOUNTINFO.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    best_len = -1
    best_type: str | None = None
    resolved = target.resolve()
    for line in lines:
        # <id> <parent> <maj:min> <root> <mountpoint> <opts> ... - <fstype> <source> <opts>
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or not right_fields:
            continue
        mount_point = left_fields[4].replace("\\040", " ")
        fstype = right_fields[0]
        try:
            mount_path = Path(mount_point)
        except ValueError:
            continue
        if resolved == mount_path or mount_path in resolved.parents:
            if len(mount_point) > best_len:
                best_len = len(mount_point)
                best_type = fstype
    return best_type


def problems(evidence_dir: Path, workspace: Path) -> list[str]:
    found: list[str] = []
    evidence = evidence_dir.resolve()
    repo = workspace.resolve()

    if evidence == repo or repo in evidence.parents:
        found.append(
            f"EVIDENCE_IN_REPOSITORY {evidence}: Repository({repo})配下である。"
            "Evidence出力がソースツリーを汚し、git status --short を空に保てない"
        )

    for label, path in (("EVIDENCE_DIR", evidence), ("WORKSPACE", repo)):
        if path == Path("/mnt") or Path("/mnt") in path.parents:
            found.append(f"WINDOWS_MOUNT {label} {path}: /mnt 配下でEvidenceを生成しない")
        fstype = filesystem_type(path)
        if fstype is None:
            found.append(f"UNKNOWN_FILESYSTEM {label} {path}: mountinfoから判定できない")
        elif fstype in FOREIGN_FILESYSTEMS:
            found.append(
                f"FOREIGN_FILESYSTEM {label} {path}: filesystem type={fstype}。"
                "fsync・Atomic Renameの保証が無い場所でEvidenceを生成しない"
            )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.workspace.is_dir():
        print(f"input invalid: workspace not a directory: {args.workspace}", file=sys.stderr)
        return 4
    if not args.evidence_dir.exists():
        print(
            f"input invalid: evidence dir does not exist: {args.evidence_dir}"
            "（先に mkdir -p すること）",
            file=sys.stderr,
        )
        return 4

    found = problems(args.evidence_dir, args.workspace)
    if found:
        for line in found:
            print(line, file=sys.stderr)
        return 3

    evidence = args.evidence_dir.resolve()
    print("evidence destination ok")
    print(f"  evidence_dir = {evidence} (fs={filesystem_type(evidence)})")
    print(f"  workspace    = {args.workspace.resolve()} (fs={filesystem_type(args.workspace)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
