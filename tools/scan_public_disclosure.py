#!/usr/bin/env python3
"""公開コピーの作業木と全Git履歴から、個人・環境の開示情報を探す。

Secret Scanner（Gitleaks）は資格情報を探すが、個人Home Path・連絡先・署名鍵の
識別子は対象外である。公開前にはこれらも所在と根拠で区分する必要がある
（`docs/PUBLICATION.md` §公開前の必要条件 2〜3）。

## 値を出力しない

出力はPath・行番号・種別・Commitだけにする。検出値そのものは標準出力・JSON・
例外のどこにも出さない。Reportへ転記した時点で、隠したい値を再掲することになる。

## 種別

    personal_home    個人Home Directoryを指すPath。汎用名（user, runner 等）は除く
    email            予約済みでないDomainのメールアドレス
    ssh_public_key   SSH公開鍵の本体
    ssh_fingerprint  SSH鍵のFingerprint（`SHA256:` + base64 43文字）
    sensitive_term   非公開のHash一覧（`--sensitive-terms`）に一致する語

`sensitive_term` の一覧はSHA-256だけを受け取る。平文の語をこのToolや公開コピーへ
置かない。一覧を渡さない場合、この種別は検査しない（結果の `scope` に明記する）。

## 分類済み（allowlist）

`ci/disclosure-allowlist.json` の項目は `path`・`line`・`kind`・`line_sha256` の
完全一致でだけ効く。行が1文字でも変われば未分類へ戻る。使われない項目は失敗にする
（広い除外が残り続けるのを防ぐ）。`sensitive_term` は必要な著作権表示だけを分類できる。

## 終了Code

    0  未分類の検出なし
    1  未分類の検出あり、または使われないallowlist項目あり
    2  入力不正
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_INPUT_INVALID = 2

KINDS = ("personal_home", "email", "ssh_public_key", "ssh_fingerprint", "sensitive_term")
CLASSIFICATIONS = {
    # 著作権者表示。LICENSE等から削ってはならない（THIRD_PARTY_NOTICES.md）。
    "REQUIRED_COPYRIGHT_NOTICE",
    # 試験のためだけに作った値。実在の個人・鍵を指さない。
    "SYNTHETIC_TEST_VECTOR",
    # 手順書の例示。利用者が自分の値へ置き換える前提の値。
    "DOCUMENTED_PLACEHOLDER",
}

# 汎用・例示のHome名。個人を識別しない。
GENERIC_HOME_NAMES = frozenset(
    {
        "user",
        "username",
        "runner",
        "you",
        "me",
        "example",
        "alice",
        "bob",
        "tester",
        "nobody",
        "root",
        "ubuntu",
        "synthetic",
        "test",
        "<user>",
        "<name>",
        "$user",
        "${user}",
        "%username%",
    }
)

HOME_PATH = re.compile(
    r"(?:/home/|/Users/|\\home\\|\\Users\\|/mnt/[A-Za-z]/Users/|[A-Za-z]:[\\/]Users[\\/])"
    r"(?P<name>[^/\\\s'\"`<>|:;,()\[\]{}]+|<[^>\s]+>|\$\{?[A-Za-z_]+\}?|%[A-Za-z_]+%)"
)
EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+\-])(?P<local>[A-Za-z0-9._%+\-]+)@"
    r"(?P<domain>[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)"
)
SSH_PUBLIC_KEY = re.compile(
    r"(?:ssh-ed25519|ssh-rsa|ssh-dss|ecdsa-sha2-nistp(?:256|384|521)|"
    r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r"\s+AAAA[0-9A-Za-z+/]{16,}={0,3}"
)
SSH_FINGERPRINT = re.compile(r"(?<![0-9A-Za-z+/])SHA256:[0-9A-Za-z+/]{43}(?![0-9A-Za-z+/=])")
TOKEN = re.compile(r"[A-Za-z0-9._%+\-@]+")
WORD = re.compile(r"[A-Za-z0-9]+")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# RFC 2606 / RFC 6761 / RFC 6762 の予約名と、ICANNが私用へ予約した `.internal`。
RESERVED_TLDS = frozenset({"example", "invalid", "test", "localhost", "local", "internal"})
RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    source: str
    commit: str | None
    line_sha256: str | None

    def key(self) -> tuple[str, int, str, str | None]:
        return (self.path, self.line, self.kind, self.line_sha256)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "line": self.line,
            "kind": self.kind,
            "source": self.source,
        }
        if self.commit is not None:
            payload["commit"] = self.commit
        if self.line_sha256 is not None:
            payload["line_sha256"] = self.line_sha256
        return payload


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def email_is_reserved(domain: str) -> bool:
    """実在の宛先になりえないDomainか。値そのものは返さない。"""
    labels = domain.lower().split(".")
    tld = labels[-1]
    if tld.isdigit():
        # `actions/checkout@v4.1.1` のようなVersion表記。メールではない。
        return True
    if len(tld) == 1:
        # 1文字のTLDは委任されていない。構造上どこにも届かない合成値。
        return True
    if tld in RESERVED_TLDS:
        return True
    return ".".join(labels[-2:]) in RESERVED_DOMAINS


def home_name_is_generic(name: str) -> bool:
    # 1 文字の名前（`/home/u/ws`）は例示の置き場であり、個人を識別しない。
    return len(name) == 1 or name.lower() in GENERIC_HOME_NAMES


def _terms_in(text: str) -> set[str]:
    tokens = {m.group(0).lower() for m in TOKEN.finditer(text)}
    tokens |= {m.group(0).lower() for m in WORD.finditer(text)}
    tokens |= {m.group("local").lower() for m in EMAIL.finditer(text)}
    return {_sha256_text(token) for token in tokens}


def scan_line(text: str, sensitive: frozenset[str]) -> list[str]:
    """1行の検出種別を返す。値は返さない。"""
    kinds: list[str] = []
    if any(not home_name_is_generic(m.group("name")) for m in HOME_PATH.finditer(text)):
        kinds.append("personal_home")
    if any(not email_is_reserved(m.group("domain")) for m in EMAIL.finditer(text)):
        kinds.append("email")
    if SSH_PUBLIC_KEY.search(text):
        kinds.append("ssh_public_key")
    if SSH_FINGERPRINT.search(text):
        kinds.append("ssh_fingerprint")
    if sensitive and _terms_in(text) & sensitive:
        kinds.append("sensitive_term")
    return kinds


def scan_text(
    path: str, data: bytes, sensitive: frozenset[str], *, source: str, commit: str | None
) -> tuple[list[Finding], bool]:
    """Bytesを行ごとに走査する。NULを含むものはBinaryとして扱い、走査しない。"""
    if b"\0" in data:
        return [], False
    text = data.decode("utf-8", errors="replace")
    findings: list[Finding] = []
    for number, line in enumerate(text.split("\n"), start=1):
        for kind in scan_line(line, sensitive):
            findings.append(Finding(path, number, kind, source, commit, _sha256_text(line)))
    return findings, True


def _git(repo: Path, *args: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise SystemExit(EXIT_INPUT_INVALID)
    result = subprocess.run(  # noqa: S603 - 固定executable、shell不使用（不変条件#8）
        [git, "-C", str(repo), *args], check=False, capture_output=True
    )
    if result.returncode != 0:
        print(f"git {args[0]} failed (rc={result.returncode})", file=sys.stderr)
        raise SystemExit(EXIT_INPUT_INVALID)
    return result.stdout


def scan_worktree(repo: Path, sensitive: frozenset[str]) -> tuple[list[Finding], Counter[str]]:
    """追跡中のFileを作業木から読む。未追跡Fileは公開対象ではないので読まない。"""
    stats: Counter[str] = Counter()
    findings: list[Finding] = []
    for raw in _git(repo, "ls-files", "-z").split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8")
        path = repo / rel
        if path.is_symlink() or not path.is_file():
            stats["non_regular_skipped"] += 1
            continue
        found, scanned = scan_text(
            rel, path.read_bytes(), sensitive, source="worktree", commit=None
        )
        stats["text_files" if scanned else "binary_files"] += 1
        findings.extend(found)
    return findings, stats


def _commit_metadata_findings(
    repo: Path, sensitive: frozenset[str]
) -> tuple[list[Finding], int, list[str]]:
    commits = [c for c in _git(repo, "rev-list", "--all").decode().split() if c]
    findings: list[Finding] = []
    for commit in commits:
        raw = _git(repo, "cat-file", "commit", commit).decode("utf-8", errors="replace")
        for number, line in enumerate(raw.split("\n"), start=1):
            for kind in scan_line(line, sensitive):
                # Commit本文の行Hashは出さない。作者行のHashは推測照合に使えるため。
                findings.append(Finding("<commit>", number, kind, "commit_metadata", commit, None))
    return findings, len(commits), commits


def scan_history(repo: Path, sensitive: frozenset[str]) -> tuple[list[Finding], Counter[str]]:
    """全Refから到達できるCommit・Tag・Blobを走査する。同一Blobは1回だけ読む。"""
    stats: Counter[str] = Counter()
    findings, commit_count, commits = _commit_metadata_findings(repo, sensitive)
    stats["commits"] = commit_count
    for tag in [t for t in _git(repo, "tag", "--list").decode().split() if t]:
        kind = _git(repo, "cat-file", "-t", f"refs/tags/{tag}").decode().strip()
        if kind == "tag":
            body = _git(repo, "cat-file", "tag", f"refs/tags/{tag}").decode("utf-8", "replace")
            for number, line in enumerate(body.split("\n"), start=1):
                for found_kind in scan_line(line, sensitive):
                    findings.append(Finding("<tag>", number, found_kind, "tag", None, None))
            stats["annotated_tags"] += 1
    seen: set[str] = set()
    for commit in commits:
        listing = _git(repo, "ls-tree", "-r", "-z", commit).split(b"\0")
        for entry in listing:
            if not entry:
                continue
            meta, _, name = entry.partition(b"\t")
            mode, kind, obj = meta.decode().split()
            if kind != "blob" or obj in seen:
                continue
            seen.add(obj)
            if mode == "120000":
                stats["symlink_blobs"] += 1
            data = _git(repo, "cat-file", "blob", obj)
            found, scanned = scan_text(
                name.decode("utf-8"), data, sensitive, source="history", commit=commit
            )
            stats["text_blobs" if scanned else "binary_blobs"] += 1
            findings.extend(found)
    return findings, stats


def _read_json(path: Path) -> Any:
    """読めない・壊れた入力は「検出なし」に見せず、入力不正で止める。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"{path.name}: unreadable or invalid JSON ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(EXIT_INPUT_INVALID) from exc


def load_sensitive(path: Path | None) -> frozenset[str]:
    if path is None:
        return frozenset()
    payload = _read_json(path)
    hashes = payload.get("sha256") if isinstance(payload, dict) else None
    if not isinstance(hashes, list) or not all(
        isinstance(h, str) and SHA256_HEX.fullmatch(h) for h in hashes
    ):
        print('sensitive terms file must be {"sha256": [<64 hex>...]}', file=sys.stderr)
        raise SystemExit(EXIT_INPUT_INVALID)
    return frozenset(hashes)


def _is_exact_relative_path(value: object) -> bool:
    """Glob・絶対Path・親参照を含まない1つのFileだけを指すか。広い除外を作らせない。"""
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        return False
    if any(ch in value for ch in "*?[]"):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def load_allowlist(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    payload = _read_json(path)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(entries, list)
    ):
        print('allowlist must be {"version": 1, "entries": [...]}', file=sys.stderr)
        raise SystemExit(EXIT_INPUT_INVALID)
    required = {"path", "line", "kind", "line_sha256", "classification", "reason"}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != required:
            print(
                f"allowlist entry {index}: keys must be exactly {sorted(required)}", file=sys.stderr
            )
            raise SystemExit(EXIT_INPUT_INVALID)
        if entry["kind"] not in KINDS or entry["classification"] not in CLASSIFICATIONS:
            print(f"allowlist entry {index}: unknown kind or classification", file=sys.stderr)
            raise SystemExit(EXIT_INPUT_INVALID)
        if not isinstance(entry["line"], int) or entry["line"] < 1:
            print(f"allowlist entry {index}: line must be a positive integer", file=sys.stderr)
            raise SystemExit(EXIT_INPUT_INVALID)
        if not SHA256_HEX.fullmatch(str(entry["line_sha256"])):
            print(f"allowlist entry {index}: line_sha256 must be 64 hex", file=sys.stderr)
            raise SystemExit(EXIT_INPUT_INVALID)
        if not _is_exact_relative_path(entry["path"]):
            print(f"allowlist entry {index}: path must be an exact relative path", file=sys.stderr)
            raise SystemExit(EXIT_INPUT_INVALID)
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            print(f"allowlist entry {index}: reason is required", file=sys.stderr)
            raise SystemExit(EXIT_INPUT_INVALID)
        if entry["kind"] == "sensitive_term" and entry["classification"] != (
            "REQUIRED_COPYRIGHT_NOTICE"
        ):
            # 所有者の識別子を試験値や例示として残す理由は無い。
            print(
                f"allowlist entry {index}: sensitive_term may only be a required copyright notice",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_INPUT_INVALID)
    return entries


def classify(
    findings: list[Finding], allowlist: list[dict[str, Any]]
) -> tuple[list[Finding], list[tuple[Finding, str]], list[dict[str, Any]]]:
    index = {
        (e["path"], e["line"], e["kind"], e["line_sha256"]): e["classification"] for e in allowlist
    }
    used: set[tuple[str, int, str, str]] = set()
    unresolved: list[Finding] = []
    classified: list[tuple[Finding, str]] = []
    for finding in findings:
        key = finding.key()
        if finding.line_sha256 is not None and key in index:
            used.add(key)  # type: ignore[arg-type]
            classified.append((finding, index[key]))
        else:
            unresolved.append(finding)
    unused = [
        {"path": e["path"], "line": e["line"], "kind": e["kind"]}
        for e in allowlist
        if (e["path"], e["line"], e["kind"], e["line_sha256"]) not in used
    ]
    return unresolved, classified, unused


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--history", action="store_true", help="全Refの履歴も走査する")
    parser.add_argument("--allowlist", type=Path, default=Path("ci/disclosure-allowlist.json"))
    parser.add_argument("--sensitive-terms", type=Path, help="非公開のSHA-256一覧（JSON）")
    parser.add_argument("--report", type=Path, help="値を含まない結果JSONの出力先")
    args = parser.parse_args(argv)

    repo: Path = args.repo.resolve()
    sensitive = load_sensitive(args.sensitive_terms)
    allowlist_path = args.allowlist if args.allowlist.is_absolute() else repo / args.allowlist
    allowlist = load_allowlist(allowlist_path)

    findings, stats = scan_worktree(repo, sensitive)
    if args.history:
        history_findings, history_stats = scan_history(repo, sensitive)
        findings.extend(history_findings)
        stats.update(history_stats)

    unresolved, classified, unused = classify(findings, allowlist)
    head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    report = {
        "tool": "tools/scan_public_disclosure.py",
        "values_exported": False,
        "head": head,
        "scope": {
            "worktree_tracked_files": True,
            "history_all_refs": bool(args.history),
            "sensitive_terms_checked": bool(sensitive),
            "sensitive_term_count": len(sensitive),
        },
        "stats": dict(sorted(stats.items())),
        "counts": {
            "unresolved": dict(sorted(Counter(f.kind for f in unresolved).items())),
            "classified": dict(sorted(Counter(c for _, c in classified).items())),
            "unused_allowlist_entries": len(unused),
        },
        "unresolved": [f.to_json() for f in sorted(unresolved, key=_order)],
        "classified": [
            {**f.to_json(), "classification": c}
            for f, c in sorted(classified, key=lambda item: _order(item[0]))
        ],
        "unused_allowlist_entries": unused,
    }
    failed = bool(unresolved or unused)
    report["result"] = "UNRESOLVED_FINDINGS" if failed else "NO_UNRESOLVED_FINDINGS"
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(
        f"disclosure scan: unresolved={len(unresolved)} classified={len(classified)} "
        f"unused_allowlist={len(unused)} history={bool(args.history)} "
        f"sensitive_terms={'yes' if sensitive else 'no'} result={report['result']}"
    )
    return EXIT_FINDINGS if failed else EXIT_OK


def _order(finding: Finding) -> tuple[str, str, int, str]:
    return (finding.source, finding.path, finding.line, finding.kind)


if __name__ == "__main__":
    raise SystemExit(main())
