#!/usr/bin/env python3
"""Design正本からAI実装用の自己完結spec shardを決定的に生成する。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# 版番号の正本は設計書の表題である。ここへ文字列で書かない（不変条件#18）。
# Script実行時は sys.path[0] が tools/ になるが、Testが importlib で読み込む
# 経路では解決されないため明示的に足す。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from design_identity import design_version  # noqa: E402


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def section(text: str, start: int, end: int | None = None) -> str:
    """`# <start>. ` から `# <end>. ` の直前までを切り出す。

    終端節が存在しない場合はValueErrorで停止する。旧実装は終端を
    `(?=^# <end>\\. |\\Z)` と書いており、終端節が見つからないと黙って文書末尾までを
    返した。shardが膨張しても検出できないためFail-Closedへ変更した。
    """
    if end is None:
        pattern = rf"(?ms)^# {start}\. .*?\Z"
    else:
        pattern = rf"(?ms)^# {start}\. .*?(?=^# {end}\. )"
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"section {start} not found, or terminator section {end} missing")
    return match.group(0).rstrip() + "\n"


def subsection(text: str, heading: str, next_heading: str | None) -> str:
    """`## <heading>` から `## <next_heading>` の直前までを切り出す。

    旧実装は見出し行を `^## <heading>(?:\\s.*)?$` と書いていた。`(?ms)` のDOTALL下では
    `(?:\\s.*)?` が**貪欲**に文書末尾まで飲み込み、続く `$` がEOFで成立し、終端の
    `(?=... |\\Z)` が `\\Z` 側で満たされる。最初の試行でマッチが成立して後戻りが
    起きないため、**全subsectionが設計書末尾まで伸びていた**
    （1.11は109行のはずが5,780行、02-input-readは設計書6,423行に対し10,940行）。

    見出し行の貪欲マッチを零幅の先読みへ置き換え、終端の `\\Z` 退避も外す。
    """
    if next_heading is None:
        end = r"(?=\Z)"
    else:
        end = rf"(?=^## {re.escape(next_heading)}(?:\s|$))"
    pattern = rf"(?ms)^## {re.escape(heading)}(?=\s|$).*?{end}"
    match = re.search(pattern, text)
    if not match:
        raise ValueError(
            f"subsection {heading!r} not found, "
            f"or terminator {next_heading!r} missing after it"
        )
    return match.group(0).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", required=True, type=Path)
    parser.add_argument("--registries", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    source = args.design.read_bytes()
    text = source.decode("utf-8")
    design_hash = sha256_bytes(source)

    shards: dict[str, str] = {
        "00-common.md": section(text, 0, 1) + "\n" + section(text, 1, 2) + "\n" + section(text, 2, 3) + "\n" + section(text, 15, 16),
        "01-canonical-hash.md": subsection(text, "1.11 Canonical JSON／Hash規約", "1.12 Token Profile Snapshot"),
        "02-input-read.md": subsection(text, "1.16 Input Read Capability／Control-Data境界", "1.17 WSL2 Mount Boundary") + "\n" + subsection(text, "1.17 WSL2 Mount Boundary", "1.18 Artifact保持・GC・容量"),
        "03-token-profile.md": subsection(text, "1.12 Token Profile Snapshot", "1.13 Trust Level／Data Classification"),
        "10-mvp0a.md": section(text, 3, 4),
        "11-mvp0b.md": section(text, 4, 5),
        "12-mvp1a.md": section(text, 6, 7),
        "13-mvp0c.md": section(text, 5, 6),
        "14-mvp1d.md": section(text, 9, 10),
        "30-policy-freshness.md": subsection(text, "14.4 Central Policy Distribution", "14.5 Secret Manager"),
        "90-tests.md": section(text, 18, 19) + "\n" + section(text, 19, 20) + "\n" + section(text, 26, 27) + "\n" + section(text, 27, 28) + "\n" + section(text, 28, 29),
        "99-reference/reference-phases.md": "\n".join(section(text, n, n + 1).rstrip() for n in (7, 8, 10, 11, 12, 13, 14)) + "\n",
    }

    # Schemaは個別ファイルに分離し、対象Schemaだけを文脈へ入れられるようにする。
    schema_block = subsection(text, "15.9 Core Schema Catalog v1", "15.10 追加Runbook")
    schema_matches = list(re.finditer(r"(?m)^### (\d+)\. ([A-Za-z][A-Za-z0-9]+)\s*$", schema_block))
    for index, match in enumerate(schema_matches):
        finish = schema_matches[index + 1].start() if index + 1 < len(schema_matches) else len(schema_block)
        name = match.group(2)
        shards[f"20-schemas/{name}.md"] = schema_block[match.start():finish].rstrip() + "\n"

    args.out.mkdir(parents=True, exist_ok=True)
    files = []
    banner = f"<!-- generated; source_hash={design_hash}; do not edit -->\n\n"
    for rel, body in sorted(shards.items()):
        path = args.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (banner + body).encode("utf-8")
        path.write_bytes(data)
        files.append({"path": rel, "sha256": sha256_bytes(data)})

    registry_hashes = {}
    for path in sorted(args.registries.glob("*.yaml")):
        registry_hashes[path.name] = sha256_bytes(path.read_bytes())
    manifest = {
        "manifest_version": "1.0",
        "source_version": design_version(args.design),
        "source_hash": design_hash,
        "source_file": args.design.name,
        "registry_hashes": registry_hashes,
        "files": files,
    }
    manifest_path = args.out / "spec-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(files)} shards and {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
