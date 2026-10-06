#!/usr/bin/env python3
"""Runtime dependencyの明白なCopyleft License混入をFail-Closedで拒否する。

## requirements.txt の行はPackage名だけではない

`--require-hashes` 運用のため、requirements.txt は次の形を取る。

    attrs==26.1.0 \\
        --hash=sha256:c647aa... \\
        --hash=sha256:d03ceb...

各行を素朴に `=` で切ってPackage名として扱うと、継続行から `--hash` という
名前を取り出してしまい `PackageNotFoundError` で落ちる。Option行・
行末コメント・行継続を除いてから名前を取り出す。

## 環境Markerは捨てずに評価する

    colorama==0.4.6 ; sys_platform == 'win32'

Markerを読み飛ばして名前だけ拾うと、Linux上では入っていない
Distributionのmetadataを引きに行って `PackageNotFoundError` で落ちる。
かといって「引けなければ無視」にすると、**入っていないPackageは
License検査を素通りする**。Markerを評価し、

* この環境に適用される → metadataが無ければ違反として報告する
* 適用されない        → 検査対象外として**件数を明示して**報告する

の2つに分ける。黙って減らさない。適用外の分をどこかで検査する責任は
残るので、数が見えるようにしておく。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression
from packaging.markers import InvalidMarker, Marker
from packaging.requirements import InvalidRequirement, Requirement as PackageRequirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

DENIED = re.compile(
    r"\b(?:AGPL(?:v?-?[123](?:\.0)?)?|GPLv?-?[23](?:\.0)?|GNU (?:Affero )?General Public License)\b",
    re.I,
)

DEFAULT_EXCEPTIONS = Path("ci/license-exceptions.yaml")
DEFAULT_RUNTIME_LOCK = Path("requirements.txt")


class Requirement(NamedTuple):
    name: str
    marker: str
    version: str = ""


def parse_requirement_names(text: str) -> list[str]:
    """requirements.txt からDistribution名だけを取り出す。"""
    return [requirement.name for requirement in parse_requirements(text)]


def parse_requirements(text: str) -> list[Requirement]:
    return list(_iter_requirements(text))


def _iter_requirements(text: str) -> Iterator[Requirement]:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # `--hash=...`、`--index-url`、`-r other.txt` はいずれも要求ではない。
        # Package名が `-` で始まることはないため、この判定で取りこぼさない。
        if line.startswith("-"):
            continue
        line = line.split("#", 1)[0]  # 行末コメント
        line = line.rstrip("\\").strip()  # 行継続
        specifier, _, marker = line.partition(";")
        specifier = specifier.strip()
        if not specifier:
            continue
        name = re.split(r"[<>=!~\[]", specifier, maxsplit=1)[0].strip()
        if name:
            match = re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[^]]+\])?==([^, <>!=~]+)", specifier)
            yield Requirement(
                name=name, marker=marker.strip(), version=match.group(1) if match else ""
            )


def applies_here(marker: str) -> bool:
    """環境Markerが現在の環境へ適用されるか。

    解釈できないMarkerは**適用されるものとして扱う**。読めないものを
    「関係ない」と決めつけると、検査対象から静かに外れる。
    """
    if not marker:
        return True
    try:
        return bool(Marker(marker).evaluate())
    except InvalidMarker:
        return True


def load_exceptions(path: Path) -> dict[str, dict[str, Any]]:
    """例外登録を読む。理由の無い例外は受け付けない。"""
    if not path.is_file():
        return {}
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: dict[str, dict[str, Any]] = {}
    for row in document.get("exceptions") or []:
        package = canonicalize_name(str(row["package"]), validate=True)
        if not str(row.get("reason") or "").strip():
            raise ValueError(f"license exception without a reason: {package}")
        if str(row.get("scope") or "") != "BUILD_TOOLING":
            raise ValueError(f"license exception {package}: only BUILD_TOOLING scope is supported")
        if package in entries:
            raise ValueError(f"duplicate license exception: {package}")
        entries[package] = row
    return entries


def locked_requirements(text: str) -> list[Requirement]:
    """監査入口は完全Pinだけを受理する。互換の名前抽出APIとは別の責任。

    include/URL/optionを無視して監査対象を減らさない。Hash行だけは
    Lockの継続行として認める（配布物Hashの検査はpip --require-hashesが担う）。
    """
    entries: list[Requirement] = []
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("#", 1)[0].removesuffix("\\").strip()
        if line.startswith("--hash="):
            if not re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", line):
                raise ValueError("invalid dependency hash")
            continue
        if line.startswith("-"):
            raise ValueError("dependency lock options/includes are not supported")
        try:
            parsed = PackageRequirement(line)
        except InvalidRequirement as exc:
            raise ValueError("invalid locked requirement") from exc
        specifiers = list(parsed.specifier)
        if parsed.url or len(specifiers) != 1 or specifiers[0].operator != "==":
            raise ValueError(f"exact dependency pin required: {parsed.name}")
        pin = specifiers[0].version
        try:
            Version(pin)
        except InvalidVersion as exc:
            raise ValueError(f"invalid dependency version: {parsed.name}") from exc
        name = canonicalize_name(parsed.name)
        if name in names:
            raise ValueError(f"duplicate dependency pin: {name}")
        names.add(name)
        entries.append(Requirement(parsed.name, str(parsed.marker or ""), pin))
    if not entries:
        raise ValueError("dependency lock is empty")
    return entries


def license_fields(meta: Any) -> tuple[str, list[str]]:
    """PEP 639の式を優先し、無効な式を旧Metadataで通さない。

    License以外のClassifierは根拠にならない。古い配布物はLicense/Trove
    License Classifierを使うが、法的な許諾そのものの認定はしない。
    """
    expressions = meta.get_all("License-Expression") or []
    if expressions:
        if len(expressions) != 1:
            raise ValueError("multiple License-Expression fields")
        if len(str(expressions[0])) > 4096:
            raise ValueError("license metadata exceeds audit limit")
        try:
            normalized = str(canonicalize_license_expression(str(expressions[0])))
        except InvalidLicenseExpression as exc:
            raise ValueError("invalid License-Expression") from exc
        return "License-Expression", [normalized]
    values: list[str] = []
    legacy = str(meta.get("License") or "").strip()
    if legacy and legacy.upper() not in {"UNKNOWN", "UNSPECIFIED", "NONE", "N/A"}:
        values.append(legacy)
    for classifier in meta.get_all("Classifier") or []:
        value = str(classifier).strip()
        if value.startswith("License ::") and value not in {
            "License :: OSI Approved",
            "License :: Other/Proprietary License",
        }:
            values.append(value)
    if not values:
        raise ValueError("license metadata missing")
    if any(len(value) > 4096 for value in values):
        raise ValueError("license metadata exceeds audit limit")
    return "legacy License / License Classifier", values


def audit(requirements: Path, exceptions_path: Path, runtime_lock: Path) -> dict[str, Any]:
    """検査したBytesのHashと固定版に束縛したMetadataを返す。"""
    requirement_bytes = requirements.read_bytes()
    runtime_bytes = runtime_lock.read_bytes()  # 不在なら例外を有効化せず停止する
    exceptions_bytes = exceptions_path.read_bytes() if exceptions_path.is_file() else b""
    exceptions = load_exceptions(exceptions_path)
    runtime_names = {
        canonicalize_name(entry.name)
        for entry in locked_requirements(runtime_bytes.decode("utf-8"))
    }
    entries = locked_requirements(requirement_bytes.decode("utf-8"))
    rows: list[dict[str, Any]] = []
    findings: list[str] = []
    for entry in entries:
        name = canonicalize_name(entry.name)
        row: dict[str, Any] = {
            "name": name,
            "locked_version": entry.version,
            "marker": entry.marker,
            "status": "FAIL",
            "installed_version": None,
        }
        rows.append(row)
        if not applies_here(entry.marker):
            row["status"] = "OUT_OF_SCOPE"
            continue
        try:
            dist = metadata.distribution(entry.name)
        except metadata.PackageNotFoundError:
            findings.append(f"{name}: applies to this environment but is not installed")
            continue
        row["installed_version"] = dist.version
        try:
            if Version(dist.version) != Version(entry.version):
                findings.append(f"{name}: installed version differs from locked version")
                continue
            if canonicalize_name(str(dist.metadata.get("Name") or "")) != name:
                findings.append(f"{name}: installed distribution name mismatch")
                continue
            source, values = license_fields(dist.metadata)
        except (InvalidVersion, ValueError) as exc:
            findings.append(f"{name}: {exc}")
            continue
        row["license_source"] = source
        row["license_values"] = values
        if any(DENIED.search(value) for value in values):
            exception = exceptions.get(name)
            if exception is None:
                findings.append(f"{name}: denied license")
            elif name in runtime_names:
                findings.append(
                    f"{name}: BUILD_TOOLING exception is void because the package "
                    "is in the runtime lock"
                )
            else:
                row["status"] = "ACCEPTED_BUILD_TOOLING_EXCEPTION"
        else:
            row["status"] = "PASS"
    return {
        "contract": "dependency-license-audit/1",
        "status": "FAIL" if findings else "PASS",
        "requirements_sha256": hashlib.sha256(requirement_bytes).hexdigest(),
        "runtime_lock_sha256": hashlib.sha256(runtime_bytes).hexdigest(),
        "exceptions_sha256": hashlib.sha256(exceptions_bytes).hexdigest(),
        "declared": len(entries),
        "checked": sum(r["status"] != "OUT_OF_SCOPE" for r in rows),
        "accepted_exceptions": sum(r["status"] == "ACCEPTED_BUILD_TOOLING_EXCEPTION" for r in rows),
        "out_of_scope": sum(r["status"] == "OUT_OF_SCOPE" for r in rows),
        "dependencies": rows,
        "findings": findings,
        "scope": "Installed distribution metadata at the locked version; not legal certification",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_RUNTIME_LOCK)
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    parser.add_argument("--runtime-lock", type=Path, default=DEFAULT_RUNTIME_LOCK)
    parser.add_argument("--out", type=Path, help="new JSON evidence file; never overwrite")
    args = parser.parse_args(argv)
    if args.out is not None and args.out.exists():
        print("license audit output already exists")
        return 1
    try:
        result = audit(args.requirements, args.exceptions, args.runtime_lock)
        if args.out is not None:
            with args.out.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        print(f"license audit input/evidence failure: {type(exc).__name__}")
        return 1
    for finding in result["findings"]:
        print(finding)
    accepted = [
        r["name"]
        for r in result["dependencies"]
        if r["status"] == "ACCEPTED_BUILD_TOOLING_EXCEPTION"
    ]
    out_of_scope = [r["name"] for r in result["dependencies"] if r["status"] == "OUT_OF_SCOPE"]
    if accepted:
        print(f"copyleft accepted as build tooling ({len(accepted)}): {', '.join(accepted)}")
    if out_of_scope:
        print(f"out of scope on this platform ({len(out_of_scope)}): {', '.join(out_of_scope)}")
    print(
        f"dependencies from {args.requirements}: {result['declared']} declared / "
        f"{result['checked']} checked / {result['accepted_exceptions']} accepted exceptions / "
        f"{result['out_of_scope']} out of scope; findings: {len(result['findings'])}"
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
