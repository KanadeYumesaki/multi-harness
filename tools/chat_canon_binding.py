#!/usr/bin/env python3
"""回答時点 Canon を Git 履歴だけで解決する共通実装。

## なぜ 1 つにまとめるか

Builder の ``history_match()`` と Recorder の ``_resolve_historical_design()``
は同じことを別々に書いていた。片方だけ直すと、監査と記録が食い違う。
食い違いは Hash の不一致として出るのではなく、**どちらも通るのに別の Canon を
見ている**という形で出る。だから 1 つにする。

## 現行正本を代用しない

**解決の根拠は Git 履歴だけである。** 作業ツリーの設計 File も、いまの
``registry-snapshot.json`` も読まない。読んだ瞬間、設計版を上げたときに
「回答時点の Canon」が「現行の Canon」へすり替わる。それは Owner が
CPB-1-A（回答時点の Canon を保持）と CPB-2-A（Git 履歴を信頼根にする）で
選ばなかった形である。

そのため、この Module には作業ツリーの Path 定数が 1 つも無い。

## 欠けている Field を埋めない

``schema_catalog_hash`` を持たない回答済み Package がある。**無いことが回答
時点の形である**（CPB-5-A）。``None`` も空文字も現行 Snapshot の値も入れない。
Key の有無をそのまま運び、比較した Field だけを ``compared_fields`` に残す。

## 判定不能は必ず分類して止める

Git が失敗した、commit が無い、Path が 0 件、Path が複数件、Hash が合わない。
これらは「たぶん大丈夫」にせず、``CanonResolutionError`` の ``code`` で区別
できるようにして停止する。呼び手はこの ``code`` を機械的に扱える。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

#: Snapshot の File 名。**版に依らず 1 つである。**
SNAPSHOT_NAME: Final[str] = "registry-snapshot.json"

#: Package が名乗る Canon の Field。``schema_catalog_hash`` は無い Package がある。
CANON_HASH_FIELDS: Final[tuple[str, ...]] = (
    "design_sha256",
    "registry_snapshot_hash",
    "schema_catalog_hash",
)

#: 解決に失敗した理由。**存在しない値ではなく、区別できる分類で返す。**
FAILURE_CODES: Final[tuple[str, ...]] = (
    "BINDING_FIELD_MISSING",
    "GIT_HISTORY_UNAVAILABLE",
    "GIT_HISTORY_DESIGN_VERSION_ABSENT",
    "GIT_HISTORY_DESIGN_PATH_ABSENT",
    "GIT_HISTORY_DESIGN_PATH_AMBIGUOUS",
    "GIT_HISTORY_DESIGN_HASH_MISMATCH",
    "GIT_HISTORY_SNAPSHOT_ABSENT",
    "GIT_HISTORY_SNAPSHOT_HASH_MISMATCH",
)


class CanonResolutionError(RuntimeError):
    """回答時点 Canon を Git 履歴から解決できなかった。

    ``code`` は :data:`FAILURE_CODES` のいずれかである。文面ではなく ``code``
    で分岐すること。
    """

    def __init__(self, code: str, detail: str) -> None:
        if code not in FAILURE_CODES:
            raise ValueError(f"unknown failure code: {code}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def design_filename(version: str) -> str:
    """その版の設計正本の File 名。

    **Literal で書かない。** 旧版名を Literal で置くと
    ``tools/check_design_reference_currency.py`` が陳腐化参照として数える。
    """
    return f"design-v{version}-runtime-go.md"


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def git_bytes(repo_root: Path, *args: str) -> bytes:
    """Git の出力を **生 Bytes** で受け取る。

    ``text=True`` を使うと改行が変換され、Hash が本物と変わりうる。Hash を
    測る経路で復号を挟まない。
    """
    completed = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        ["git", *args],  # noqa: S607
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise CanonResolutionError(
            "GIT_HISTORY_UNAVAILABLE",
            f"git {' '.join(args)} (rc={completed.returncode}): {stderr}",
        )
    return completed.stdout


def _paths_named(repo_root: Path, commit: str, filename: str) -> list[str]:
    """その commit の Tree にある、名前が一致する Path。

    ``endswith`` だけで判じると ``xdesign-v1.0-runtime-go.md`` まで拾う。
    Path 区切りで区切って比べる。
    """
    listing = git_bytes(repo_root, "ls-tree", "-r", "--name-only", commit)
    found: list[str] = []
    for raw in listing.decode("utf-8").splitlines():
        path = raw.strip()
        if path == filename or path.endswith("/" + filename):
            found.append(path)
    return sorted(found)


def _commits_touching(repo_root: Path, pathspec: str) -> list[str]:
    """その Path に触れた commit を **古い順** で返す。

    古い順にするのは、その Bytes を持ち込んだ commit を指したいからである。
    どの commit を引いても Bytes は同じだが、指し先は決定的でなければならない。
    """
    out = git_bytes(repo_root, "log", "--all", "--reverse", "--format=%H", "--", pathspec)
    return out.decode("utf-8").split()


def _require(item: Mapping[str, Any], *fields: str) -> None:
    missing = [name for name in fields if not item.get(name)]
    if missing:
        raise CanonResolutionError(
            "BINDING_FIELD_MISSING",
            f"記録に必要な Field が無い: {missing}",
        )


def resolve_historical_design(repo_root: Path, item: Mapping[str, Any]) -> dict[str, Any]:
    """記録済みの commit だけを根拠に、その版の設計正本を解決する。

    ``item`` は ``commit`` / ``design_version`` / ``package_design_sha256`` を
    持つ。**それ以外を根拠にしない。** 固定表も、現行の設計 Path も見ない。

    解決できない場合は :class:`CanonResolutionError` を送出する。戻り値で
    「たぶん一致」を返す経路は無い。
    """
    _require(item, "commit", "design_version", "package_design_sha256")
    commit = str(item["commit"])
    version = str(item["design_version"])
    expected = str(item["package_design_sha256"])
    wanted = design_filename(version)

    paths = _paths_named(repo_root, commit, wanted)
    if not paths:
        raise CanonResolutionError(
            "GIT_HISTORY_DESIGN_PATH_ABSENT",
            f"v{version} commit={commit} に {wanted} が無い",
        )
    if len(paths) > 1:
        raise CanonResolutionError(
            "GIT_HISTORY_DESIGN_PATH_AMBIGUOUS",
            f"v{version} commit={commit} paths={paths}",
        )

    resolved = sha256_bytes(git_bytes(repo_root, "show", f"{commit}:{paths[0]}"))
    if resolved != expected:
        raise CanonResolutionError(
            "GIT_HISTORY_DESIGN_HASH_MISMATCH",
            f"v{version} expected={expected} actual={resolved}",
        )
    return {
        "design_version": version,
        "commit": commit,
        "path": paths[0],
        "recorded_hash": expected,
        "resolved_hash": resolved,
        "matches": True,
    }


def _resolve_design_by_version(repo_root: Path, version: str, expected: str) -> dict[str, Any]:
    """commit を記録していない Package のために、版名から履歴を探す。

    見つけた commit の Bytes が記録 Hash と一致したときだけ返す。**作業ツリー
    へは落ちない。** 履歴に無ければ止まる。
    """
    wanted = design_filename(version)
    commits = _commits_touching(repo_root, f"*{wanted}")
    if not commits:
        raise CanonResolutionError(
            "GIT_HISTORY_DESIGN_VERSION_ABSENT",
            f"v{version} の設計正本が Git 履歴に無い",
        )
    for commit in commits:
        paths = _paths_named(repo_root, commit, wanted)
        if len(paths) > 1:
            raise CanonResolutionError(
                "GIT_HISTORY_DESIGN_PATH_AMBIGUOUS",
                f"v{version} commit={commit} paths={paths}",
            )
        if not paths:
            continue
        resolved = sha256_bytes(git_bytes(repo_root, "show", f"{commit}:{paths[0]}"))
        if resolved == expected:
            return {
                "design_version": version,
                "commit": commit,
                "path": paths[0],
                "recorded_hash": expected,
                "resolved_hash": resolved,
                "matches": True,
            }
    raise CanonResolutionError(
        "GIT_HISTORY_DESIGN_HASH_MISMATCH",
        f"v{version} の記録 Hash {expected} を持つ commit が履歴に無い",
    )


def _resolve_snapshot(
    repo_root: Path,
    binding: Mapping[str, Any],
    *,
    version: str,
    design_hash: str,
) -> dict[str, Any]:
    """記録 Hash と一致する ``registry-snapshot.json`` を履歴から探す。

    Snapshot は自身が ``design_version`` と ``design_sha256`` を持つ。だから
    設計・Registry・Schema の 3 つを **1 つの履歴 Artifact で結び付けられる。**

    ``schema_catalog_hash`` は Package が持つときだけ比べる。持たない Package
    へ現行値を補わない。
    """
    #: Package が記録した Hash のうち、実際に比べるもの。
    hash_fields = ["registry_snapshot_hash"]
    if "schema_catalog_hash" in binding:
        hash_fields.append("schema_catalog_hash")
    compared = ["design_version", "design_sha256", *hash_fields]

    commits = _commits_touching(repo_root, SNAPSHOT_NAME)
    if not commits:
        raise CanonResolutionError(
            "GIT_HISTORY_SNAPSHOT_ABSENT",
            f"{SNAPSHOT_NAME} が Git 履歴に無い",
        )
    for commit in commits:
        try:
            blob = git_bytes(repo_root, "show", f"{commit}:{SNAPSHOT_NAME}")
        except CanonResolutionError:
            continue  # 削除した commit。次を見る
        try:
            snapshot = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(snapshot, dict):
            continue
        if snapshot.get("design_version") != version:
            continue
        if snapshot.get("design_sha256") != design_hash:
            continue
        if any(snapshot.get(name) != binding.get(name) for name in hash_fields):
            continue
        return {
            "commit": commit,
            "path": SNAPSHOT_NAME,
            "compared_fields": compared,
            "matches": True,
        }
    raise CanonResolutionError(
        "GIT_HISTORY_SNAPSHOT_HASH_MISMATCH",
        f"v{version} の記録 Hash を持つ {SNAPSHOT_NAME} が履歴に無い（比較 {compared}）",
    )


def recorded_answer_digest(choices: Mapping[str, str]) -> str:
    """回答 ID の並びから回答 Hash を作る。

    **この算式は全 Package 共通ではない。** 実測すると、改行で終端する Package
    と終端しない Package が混在している。ここに置くのは、CPB Package の Recorder
    が実際に使った算式 1 つだけである。他の Package へ当てて「改ざん」と判ずる
    ことはしない。

    Recorder と Builder の両方がこれを呼ぶ。2 箇所に書くと、片方だけ直したとき
    に記録と検証が静かに食い違う。
    """
    body = "\n".join(f"{qid}={choices[qid]}" for qid in sorted(choices)) + "\n"
    return sha256_bytes(body.encode("utf-8"))


def answer_time_binding(package: Mapping[str, Any]) -> dict[str, Any]:
    """Package が名乗る Canon を取り出す。**Key の有無をそのまま運ぶ。**

    ``schema_catalog_hash`` が無い Package では Key ごと落とす。``None`` を
    入れると「値が無い」と「Field が無い」を区別できなくなる。
    """
    binding: dict[str, Any] = {"design_version": package.get("design_version")}
    for field in CANON_HASH_FIELDS:
        if field in package:
            binding[field] = package[field]
    return binding


def resolve_answer_time_canon(
    repo_root: Path,
    binding: Mapping[str, Any],
    *,
    package_id: str | None = None,
) -> dict[str, Any]:
    """回答時点の Canon を Git 履歴から解決する。**送出せずに結果を返す。**

    戻り値は必ず次を持つ。

    * ``package_id`` / ``design_version`` … 何を解決したか
    * ``design`` … 使った commit と一意 Path、記録 Hash と解決 Hash
    * ``snapshot`` … 使った commit と、実際に比べた Field
    * ``matches`` … 一致したか
    * ``failure`` … 失敗した理由の分類（成功時は ``None``）
    """
    result: dict[str, Any] = {
        "package_id": package_id,
        "design_version": binding.get("design_version"),
        "schema_catalog_hash_present": "schema_catalog_hash" in binding,
        "design": None,
        "snapshot": None,
        "matches": False,
        "failure": None,
    }
    try:
        _require(binding, "design_version", "design_sha256", "registry_snapshot_hash")
        version = str(binding["design_version"])
        design_hash = str(binding["design_sha256"])
        result["design"] = _resolve_design_by_version(repo_root, version, design_hash)
        result["snapshot"] = _resolve_snapshot(
            repo_root, binding, version=version, design_hash=design_hash
        )
    except CanonResolutionError as exc:
        result["failure"] = {"code": exc.code, "detail": exc.detail}
        return result
    result["matches"] = True
    return result


def verify_answer_time_canon(
    repo_root: Path,
    package: Mapping[str, Any],
    *,
    package_id: str | None = None,
) -> dict[str, Any]:
    """解決できなければ送出する版。試験と Builder はこちらを使う。"""
    resolved = resolve_answer_time_canon(
        repo_root, answer_time_binding(package), package_id=package_id
    )
    failure = resolved["failure"]
    if failure is not None:
        raise CanonResolutionError(str(failure["code"]), str(failure["detail"]))
    return resolved
