#!/usr/bin/env python3
"""Hash固定のRuntime依存Lock Fileを生成する（§16.5 supply-chain）。

CIは `pip-audit --strict --require-hashes -r requirements.txt` を実行する。
`--require-hashes` は「解決され得る全配布物のHashが列挙されていること」を要求する。

`pip download` の結果だけをHash化すると、**実行した環境のPython Version・
Platformに対応するWheelのHashしか得られない**。CI matrixは 3.11 と 3.12 を
含むため、3.12で作ったLockは3.11のjobでHash不一致になる。

そのためPyPI JSON APIから当該Versionの**全配布物**（各PythonVersion／Platform向け
WheelとSource Distribution）のsha256を取得して列挙する。
`pip-compile --generate-hashes` と同じ方針である。

入力はPin済みの `<name>==<version>` だけを受け取る。Version解決は行わない。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Final

PYPI_JSON: Final[str] = "https://pypi.org/pypi/{name}/{version}/json"

HEADER: Final[str] = """\
# Runtime dependency lock（Hash固定）。
#
# 生成: python tools/build_requirements_lock.py --out requirements.txt <pkg==ver> ...
# 検査: python tools/build_requirements_lock.py --check
#       （Pinは本File自身から読む。Workflowへ二重記載しない）
#
# 各Versionの**全配布物**のsha256を列挙する。CI matrixが複数のPython Versionを
# 含むため、1環境分のWheel Hashだけでは `pip --require-hashes` が通らない。
#
# 直接の依存
#   jsonschema : Core Schema検証（§1.15、AT-SCHEMA-*）
#   PyYAML     : Registry読込み（design-source/registries/*.yaml）
# 以下は上記の推移的依存であり、Hash固定のため明示する。
"""


def fetch_hashes(name: str, version: str) -> list[str]:
    url = PYPI_JSON.format(name=name, version=version)
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - 固定https
        payload = json.loads(response.read().decode("utf-8"))
    digests = sorted({entry["digests"]["sha256"] for entry in payload.get("urls", [])})
    if not digests:
        raise ValueError(f"no distributions found for {name}=={version}")
    return digests


def render(pins: list[str]) -> str:
    lines = [HEADER]
    for pin in sorted(pins, key=str.lower):
        if "==" not in pin:
            raise ValueError(f"pin must be '<name>==<version>': {pin}")
        name, version = pin.split("==", 1)
        digests = fetch_hashes(name, version)
        lines.append(f"{name}=={version} \\")
        for index, digest in enumerate(digests):
            suffix = "" if index == len(digests) - 1 else " \\"
            lines.append(f"    --hash=sha256:{digest}{suffix}")
    return "\n".join(lines) + "\n"


def pins_from_lock(path: Path) -> list[str]:
    """既存Lockから固定済みのPinを読み戻す。

    `--check` をCIから呼ぶとき、Pin一覧をWorkflowへ二重記載しないための入口。
    Pinが2箇所にあると、片方だけ更新した状態を検出できない。
    """
    pins: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped or stripped.startswith("--hash"):
            continue
        pins.append(stripped.removesuffix("\\").strip())
    return pins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pins", nargs="*", help="<name>==<version>")
    parser.add_argument("--out", type=Path, default=Path("requirements.txt"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    pins = args.pins
    if not pins:
        if not args.check:
            parser.error("pins are required unless --check is used")
        if not args.out.is_file():
            print(f"{args.out} not found", file=sys.stderr)
            return 1
        pins = pins_from_lock(args.out)
        if not pins:
            print(f"{args.out} declares no pinned package", file=sys.stderr)
            return 1

    rendered = render(pins)
    if args.check:
        if not args.out.is_file() or args.out.read_text(encoding="utf-8") != rendered:
            print(
                f"{args.out} is stale; run tools/build_requirements_lock.py",
                file=sys.stderr,
            )
            return 1
        print(f"{args.out}: up to date")
        return 0

    args.out.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.out} with {len(args.pins)} pinned package(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
