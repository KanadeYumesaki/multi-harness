#!/usr/bin/env python3
"""Secret Scanner（Gitleaks）の既知検出を、狭い位置と根拠で分類できているか確かめる。

## 何を保証するか

`.gitleaksignore` は `path:rule:line` の完全一致だけを書く。Commit無しの形式なので
作業木と全履歴の走査の両方で同じ位置にだけ効く。行が動けば再検出される。

各項目は `ci/secret-scan-classification.json` の記録と1対1で対応し、記録は
その行の内容Hashを持つ。行が変われば本検査が失敗し、分類をやり直すまで通らない。
値そのものはどちらのFileにも書かない。

分類ごとの根拠は、本Toolが現在の行から機械的に再計算する。履歴Reportではさらに
検出CommitのGit objectから行・File全体・消費者の根拠を再計算する。過去のPythonは
importも実行もせず、Tokenizer定数もASTのLiteralだけを読む。

    PUBLIC_TOKENIZER_IDENTIFIER            Production定数と同じ公開Tokenizer名
    PYTHON_EXPRESSION_NO_CREDENTIAL_LITERAL 文字列Literalを含まないPythonの式
    INCOMPLETE_PRIVATE_KEY_TEST_MARKER      試験内の鍵見出しだけ。鍵本体が無い
    SYNTHETIC_TEST_VECTOR                   試験内の合成値。構造上の合成根拠を要求

`SYNTHETIC_TEST_VECTOR` は、目印語・連番・交互連番のいずれかを持つことを要求し、
同じLiteralがsrc/・tools/・examples/に無いこと（Productionの消費者が無いこと）も
確かめる。これは「外部の実資格情報ではない」ことの数学的証明ではない。
Gitleaksの提供元別Ruleに一致せず（`generic-api-key` 等の汎用Ruleだけ）、
構造が合成であることを示す根拠である。

## Gitleaks Reportとの突合

`--gitleaks-report` に値を伏せたReport（`--redact=100`）を渡すと、Reportの検出集合と
分類記録の集合が一致することも確かめる。未分類の検出が1件でもあれば失敗する。

## 終了Code

    0  全項目の根拠を再計算でき、突合も一致した
    1  根拠の不一致・未分類・余剰
    2  入力不正
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import itertools
import json
import os
import re
import subprocess
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_INPUT_INVALID = 2

CLASSIFICATIONS = frozenset(
    {
        "PUBLIC_TOKENIZER_IDENTIFIER",
        "PYTHON_EXPRESSION_NO_CREDENTIAL_LITERAL",
        "INCOMPLETE_PRIVATE_KEY_TEST_MARKER",
        "SYNTHETIC_TEST_VECTOR",
    }
)
# 試験以外に置いてよい分類。試験値やPEM見出しをProductionへ置く理由は無い。
NON_TEST_ALLOWED = frozenset(
    {"PUBLIC_TOKENIZER_IDENTIFIER", "PYTHON_EXPRESSION_NO_CREDENTIAL_LITERAL"}
)
IGNORE_LINE = re.compile(
    r"^(?P<path>[^:*?\[\]\s][^:*?\[\]]*):(?P<rule>[a-z0-9-]+):(?P<line>[1-9][0-9]*)$"
)
MARKER_WORD = re.compile(
    r"(example|fake|dummy|canary|synthetic|placeholder|not[-_]?a[-_]?secret)", re.I
)
PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\\n]*[A-Za-z0-9+/=\s\\n]{64,}-----END", re.S
)
CONSUMER_ROOTS = ("src", "tools", "examples")
MIN_VECTOR_LENGTH = 16


COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
TOKENIZER_PATH = "src/harness/infrastructure/tokenizer/deterministic_counter.py"
TEXT_SUFFIXES = frozenset({".py", ".json", ".md", ".yaml", ".txt"})


class SourceUnreadable(Exception):
    """根拠を読めない。元のI/Oメッセージや対象Bytesは外へ出さない。"""


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    line: int
    end_line: int
    commit: str | None

    @property
    def location(self) -> tuple[str, str, int]:
        return (self.path, self.rule, self.line)


class ContentSource:
    """現在の木、または不変のGit objectを同じ検証経路から読む。"""

    def __init__(self, root: Path, commit: str | None = None) -> None:
        self.root = root
        self.commit = commit
        self._texts: dict[str, str | None] = {}
        self._tree: dict[str, tuple[str, str]] | None = None
        if commit is not None:
            actual = self._git("rev-parse", "--verify", commit + "^{commit}").decode().strip()
            if actual != commit:
                raise SourceUnreadable("GIT_COMMIT_INVALID")
            self._tree = {}
            for entry in self._git("ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
                if not entry:
                    continue
                try:
                    header, name = entry.split(b"\t", 1)
                    mode, kind, blob = header.decode("ascii").split()
                    path = name.decode("utf-8")
                except (ValueError, UnicodeError) as exc:
                    raise SourceUnreadable("GIT_TREE_INVALID") from exc
                if kind != "blob" or not _is_exact_relative_path(path):
                    raise SourceUnreadable("GIT_TREE_INVALID")
                self._tree[path] = (mode, blob)

    def _git(self, *args: str) -> bytes:
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        try:
            result = subprocess.run(  # noqa: S603 — 固定Git操作と検証済みHash。Shell無し。
                ["/usr/bin/git", "--no-optional-locks", "-C", str(self.root), *args],
                capture_output=True,
                timeout=30,
                check=True,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SourceUnreadable("GIT_READ_FAILED") from exc
        return result.stdout

    def read_text(self, path: str) -> str | None:
        if path in self._texts:
            return self._texts[path]
        if not _is_exact_relative_path(path):
            raise SourceUnreadable("SOURCE_PATH_INVALID")
        try:
            if self._tree is not None:
                entry = self._tree.get(path)
                if entry is None:
                    data = None
                elif entry[0] not in {"100644", "100755"}:
                    raise SourceUnreadable("SOURCE_NOT_REGULAR")
                else:
                    data = self._git("cat-file", "blob", entry[1])
            else:
                target = self.root
                for part in path.split("/"):
                    target = target / part
                    if target.is_symlink():
                        raise SourceUnreadable("SOURCE_SYMLINK")
                try:
                    data = target.read_bytes()
                except FileNotFoundError:
                    data = None
            text = None if data is None else data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise SourceUnreadable("SOURCE_READ_FAILED") from exc
        self._texts[path] = text
        return text

    def consumer_paths(self) -> list[str]:
        if self._tree is not None:
            return sorted(
                p
                for p in self._tree
                if p.split("/", 1)[0] in CONSUMER_ROOTS and Path(p).suffix in TEXT_SUFFIXES
            )
        paths: list[str] = []
        try:
            for top in CONSUMER_ROOTS:
                base = self.root / top
                if not base.exists():
                    continue
                if base.is_symlink():
                    raise SourceUnreadable("SOURCE_SYMLINK")
                for directory, dirs, files in os.walk(base, onerror=_walk_error):
                    for name in sorted(dirs + files):
                        child = Path(directory) / name
                        if child.is_symlink():
                            raise SourceUnreadable("SOURCE_SYMLINK")
                    paths.extend(
                        (Path(directory) / name).relative_to(self.root).as_posix()
                        for name in files
                        if Path(name).suffix in TEXT_SUFFIXES
                    )
        except OSError as exc:
            raise SourceUnreadable("SOURCE_ENUMERATION_FAILED") from exc
        return sorted(paths)


def _walk_error(error: OSError) -> None:
    raise SourceUnreadable("SOURCE_ENUMERATION_FAILED") from error


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def string_literals(path: str, line: str) -> list[str]:
    """行の文字列Literalを返す。Pythonはtokenize、それ以外はJSON風の二重引用符。"""
    if path.endswith(".py"):
        values: list[str] = []
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline))
        except (tokenize.TokenError, IndentationError):
            return ["<UNPARSEABLE>"]
        for token in tokens:
            if token.type != tokenize.STRING:
                continue
            try:
                value = ast.literal_eval(token.string)
            except (ValueError, SyntaxError):
                values.append(token.string)
                continue
            values.append(value.decode("latin-1") if isinstance(value, bytes) else str(value))
        return values
    return [m.group(1) for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', line)]


def _monotone_run(chars: list[str]) -> int:
    """隣接差が同じ向きの±1で続く最長の長さ。9→0 / 0→9 の折返しを含む。"""
    best = 1 if chars else 0
    current, direction = 1, 0
    for previous, char in itertools.pairwise(chars):
        step = ord(char.lower()) - ord(previous.lower())
        if previous.isdigit() and char.isdigit() and abs(step) == 9:
            step = -1 if step == 9 else 1
        if abs(step) == 1 and (direction in (0, step)):
            current, direction = current + 1, step
        elif abs(step) == 1:
            current, direction = 2, step
        else:
            current, direction = 1, 0
        best = max(best, current)
    return best


def _interleaved_run(segment: str) -> int:
    """英字と数字が交互に並び、それぞれが±1で進む最長の部分列長（例: 1a2b3c）。"""
    best = 0
    for start in range(len(segment)):
        end = start
        while end < len(segment):
            if end > start and segment[end].isalpha() == segment[end - 1].isalpha():
                break
            window = segment[start : end + 1]
            letters = [c for c in window if c.isalpha()]
            digits = [c for c in window if c.isdigit()]
            if _monotone_run(letters) != len(letters) or _monotone_run(digits) != len(digits):
                break
            end += 1
        best = max(best, end - start)
    return best


def synthetic_evidence(value: str) -> list[str]:
    """合成値だと言える構造上の根拠を返す。無ければ空。"""
    evidence: list[str] = []
    if MARKER_WORD.search(value):
        evidence.append("MARKER_WORD")
    for segment in re.findall(r"[0-9A-Za-z]+", value):
        if _monotone_run(list(segment)) >= 6:
            evidence.append("SEQUENTIAL_RUN")
            break
    for segment in re.findall(r"[0-9A-Za-z]{8,}", value):
        letters = [c for c in segment if c.isalpha()]
        digits = [c for c in segment if c.isdigit()]
        if (
            len(letters) >= 3
            and len(digits) >= 3
            and _monotone_run(letters) == len(letters)
            and _monotone_run(digits) == len(digits)
        ):
            evidence.append("INTERLEAVED_PROGRESSION")
            break
    for segment in re.findall(r"[0-9A-Za-z]+", value):
        if _interleaved_run(segment) >= 6:
            evidence.append("INTERLEAVED_RUN")
            break
    return evidence


def _tokenizer_constant(source: ContentSource) -> str:
    """履歴を実行せず、当該木の公開定数の単一Literalだけを読む。"""
    text = source.read_text(TOKENIZER_PATH)
    if text is None:
        raise SourceUnreadable("TOKENIZER_CONSTANT_MISSING")
    try:
        module = ast.parse(text)
    except SyntaxError as exc:
        raise SourceUnreadable("TOKENIZER_CONSTANT_INVALID") from exc
    values: list[str] = []
    for node in module.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "BYTE_BOUND_TOKENIZER_NAME" for t in targets):
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                raise SourceUnreadable("TOKENIZER_CONSTANT_INVALID")
            values.append(node.value.value)
    if len(values) != 1:
        raise SourceUnreadable("TOKENIZER_CONSTANT_INVALID")
    return values[0]


def _consumers(source: ContentSource, literal: str, own_path: str) -> list[str]:
    found: list[str] = []
    for path in source.consumer_paths():
        if path == own_path:
            continue
        text = source.read_text(path)
        if text is None:
            raise SourceUnreadable("CONSUMER_DISAPPEARED")
        if literal in text:
            found.append(path)
    return found


def verify_record(
    root: Path, record: dict[str, Any], tokenizer: str, *, source: ContentSource | None = None
) -> list[str]:
    """1件の分類記録の根拠を再計算する。失敗理由（値を含まない）を返す。"""
    path, line_no, classification = record["path"], record["line"], record["classification"]
    where = f"{path}:{record['rule']}:{line_no}"
    source = source or ContentSource(root)
    text = source.read_text(path)
    if text is None:
        return [f"{where}: file is missing"]
    lines = text.split("\n")
    if line_no > len(lines):
        return [f"{where}: line is out of range"]
    line = lines[line_no - 1]
    problems: list[str] = []
    if _sha256_text(line) != record["line_sha256"]:
        return [f"{where}: line content changed; classify again before ignoring"]
    in_tests = path.startswith("tests/")
    if classification not in NON_TEST_ALLOWED and not in_tests:
        problems.append(f"{where}: {classification} is only allowed under tests/")
    literals = string_literals(path, line)
    if classification == "PUBLIC_TOKENIZER_IDENTIFIER":
        if tokenizer not in literals:
            problems.append(f"{where}: no literal equals the production tokenizer constant")
        extra = [v for v in literals if len(v) >= MIN_VECTOR_LENGTH and v != tokenizer]
        if extra:
            problems.append(f"{where}: other long literal on the line needs its own review")
    elif classification == "PYTHON_EXPRESSION_NO_CREDENTIAL_LITERAL":
        if not path.endswith(".py") or literals:
            problems.append(f"{where}: line must be Python without any string literal")
    elif classification == "INCOMPLETE_PRIVATE_KEY_TEST_MARKER":
        markers = [v for v in literals if "-----BEGIN" in v]
        if not markers or any("-----END" in v or len(v) >= 100 for v in markers):
            problems.append(f"{where}: expected a short key header without a key body")
        if PEM_BLOCK.search(text):
            problems.append(f"{where}: file contains a complete PEM private key block")
    elif classification == "SYNTHETIC_TEST_VECTOR":
        vectors = [v for v in literals if len(v) >= MIN_VECTOR_LENGTH]
        vectors = [v for v in vectors if not re.fullmatch(r"[a-z_]+", v)]
        if not vectors:
            problems.append(f"{where}: no candidate test vector literal on the line")
        for index, value in enumerate(vectors):
            evidence = synthetic_evidence(value)
            if not evidence:
                problems.append(f"{where}: literal #{index} has no structural synthetic evidence")
            if evidence != record.get("evidence", {}).get(str(index)):
                problems.append(f"{where}: literal #{index} evidence differs from the record")
            consumers = _consumers(source, value, path)
            if consumers:
                problems.append(f"{where}: literal #{index} is used outside tests: {consumers}")
    return problems


def load_ignore(path: Path) -> set[tuple[str, str, int]]:
    entries: set[tuple[str, str, int]] = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        match = IGNORE_LINE.fullmatch(text)
        if match is None:
            print(f"{path.name}:{number}: only exact 'path:rule:line' entries are allowed")
            raise SystemExit(EXIT_INPUT_INVALID)
        entry = (match.group("path"), match.group("rule"), int(match.group("line")))
        if entry in entries:
            print(f"{path.name}:{number}: duplicate entry")
            raise SystemExit(EXIT_INPUT_INVALID)
        entries.add(entry)
    return entries


def _read_json(path: Path) -> Any:
    """読めない・壊れた入力は「不一致なし」に見せず、入力不正で止める。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"{path.name}: unreadable or invalid JSON ({type(exc).__name__})")
        raise SystemExit(EXIT_INPUT_INVALID) from exc


def _is_exact_relative_path(value: object) -> bool:
    """Glob・絶対Path・親参照を含まない1つのFileだけを指すか。"""
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        return False
    if any(ch in value for ch in "*?[]:") or any(ord(ch) < 32 for ch in value):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def load_records(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    records = payload.get("findings") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(records, list)
    ):
        print('classification must be {"version": 1, "findings": [...]}')
        raise SystemExit(EXIT_INPUT_INVALID)
    required = {"path", "rule", "line", "line_sha256", "classification", "reason"}
    for index, record in enumerate(records):
        keys = set(record) if isinstance(record, dict) else set()
        if not required <= keys or not keys <= required | {"evidence"}:
            print(f"record {index}: keys must be {sorted(required)} (+ evidence)")
            raise SystemExit(EXIT_INPUT_INVALID)
        if (
            not isinstance(record["classification"], str)
            or record["classification"] not in CLASSIFICATIONS
        ):
            print(f"record {index}: unknown classification")
            raise SystemExit(EXIT_INPUT_INVALID)
        if not _is_exact_relative_path(record["path"]):
            print(f"record {index}: path must be an exact relative path")
            raise SystemExit(EXIT_INPUT_INVALID)
        if not isinstance(record["line"], int) or isinstance(record["line"], bool):
            print(f"record {index}: line must be a positive integer")
            raise SystemExit(EXIT_INPUT_INVALID)
        if record["line"] < 1 or not re.fullmatch(r"[0-9a-f]{64}", str(record["line_sha256"])):
            print(f"record {index}: line must be positive and line_sha256 must be 64 hex")
            raise SystemExit(EXIT_INPUT_INVALID)
        if (
            not isinstance(record["rule"], str)
            or re.fullmatch(r"[a-z0-9-]+", record["rule"]) is None
        ):
            print(f"record {index}: invalid rule")
            raise SystemExit(EXIT_INPUT_INVALID)
        if not isinstance(record["reason"], str) or not record["reason"].strip():
            print(f"record {index}: reason is required")
            raise SystemExit(EXIT_INPUT_INVALID)
        if (record["classification"] == "SYNTHETIC_TEST_VECTOR") != ("evidence" in record):
            print(f"record {index}: evidence is required exactly for SYNTHETIC_TEST_VECTOR")
            raise SystemExit(EXIT_INPUT_INVALID)
        if "evidence" in record and (
            not isinstance(record["evidence"], dict)
            or any(
                not isinstance(k, str)
                or not isinstance(v, list)
                or not all(isinstance(item, str) for item in v)
                for k, v in record["evidence"].items()
            )
        ):
            print(f"record {index}: evidence must map string indexes to string lists")
            raise SystemExit(EXIT_INPUT_INVALID)
    return records


def load_gitleaks_report(path: Path, kind: Literal["git", "dir"]) -> list[Finding]:
    return parse_gitleaks_report(_read_json(path), kind)


def parse_gitleaks_report(payload: Any, kind: Literal["git", "dir"]) -> list[Finding]:
    """一度読んだBytesのJSONを検証。値は診断へ出さない。"""
    findings: list[Finding] = []
    if not isinstance(payload, list):
        print("gitleaks report must be a JSON list of findings")
        raise SystemExit(EXIT_INPUT_INVALID)
    for index, item in enumerate(payload):
        if not isinstance(item, dict) or item.get("Secret") not in ("REDACTED", ""):
            print(f"finding {index}: report must be produced with --redact=100")
            raise SystemExit(EXIT_INPUT_INVALID)
        line = item.get("StartLine")
        end_line = item.get("EndLine", line)
        rule = item.get("RuleID")
        if (
            not _is_exact_relative_path(item.get("File"))
            or not isinstance(rule, str)
            or re.fullmatch(r"[a-z0-9-]+", rule) is None
            or type(line) is not int
            or line < 1
            or type(end_line) is not int
            or end_line < line
        ):
            print(f"finding {index}: invalid path/rule/line range")
            raise SystemExit(EXIT_INPUT_INVALID)
        commit = item.get("Commit")
        if kind == "git":
            if not isinstance(commit, str) or COMMIT_SHA.fullmatch(commit) is None:
                print(f"finding {index}: git report requires an exact commit SHA")
                raise SystemExit(EXIT_INPUT_INVALID)
        elif commit not in (None, ""):
            print(f"finding {index}: dir report must not contain a commit")
            raise SystemExit(EXIT_INPUT_INVALID)
        findings.append(Finding(item["File"], rule, line, end_line, commit or None))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--ignore-file", type=Path, default=Path(".gitleaksignore"))
    parser.add_argument(
        "--classification", type=Path, default=Path("ci/secret-scan-classification.json")
    )
    parser.add_argument("--gitleaks-report", type=Path, action="append", default=[])
    parser.add_argument("--report-kind", choices=("git", "dir"))
    args = parser.parse_args(argv)
    if args.gitleaks_report and args.report_kind is None:
        parser.error("--report-kind git|dir is required with --gitleaks-report")
    try:
        return _check(args)
    except SourceUnreadable as exc:
        print(f"classification source unreadable: {exc}")
        return EXIT_INPUT_INVALID


def _check(args: argparse.Namespace) -> int:
    root: Path = args.root.resolve()

    ignore_path = args.ignore_file if args.ignore_file.is_absolute() else root / args.ignore_file
    record_path = (
        args.classification if args.classification.is_absolute() else root / args.classification
    )
    ignore = load_ignore(ignore_path)
    records = load_records(record_path)
    keyed = {(r["path"], r["rule"], r["line"]): r for r in records}
    problems: list[str] = []
    if len(keyed) != len(records):
        problems.append("classification has duplicate path:rule:line records")
    for missing in sorted(ignore - set(keyed)):
        problems.append(
            f"ignore entry without a classification record: {':'.join(map(str, missing))}"
        )
    for missing in sorted(set(keyed) - ignore):
        problems.append(
            f"classification record not in the ignore file: {':'.join(map(str, missing))}"
        )

    current = ContentSource(root)
    needs_tokenizer = any(r["classification"] == "PUBLIC_TOKENIZER_IDENTIFIER" for r in records)
    tokenizer = _tokenizer_constant(current) if needs_tokenizer else ""
    for record in records:
        problems.extend(verify_record(root, record, tokenizer, source=current))

    history: dict[str, ContentSource] = {}

    for report in args.gitleaks_report:
        findings = load_gitleaks_report(report, args.report_kind)
        found = {f.location for f in findings}
        for extra in sorted(found - set(keyed)):
            problems.append(f"{report.name}: unclassified finding {':'.join(map(str, extra))}")
        for stale in sorted(set(keyed) - found):
            problems.append(
                f"{report.name}: classified location not reported {':'.join(map(str, stale))}"
            )
        for finding in findings:
            record = keyed.get(finding.location)
            if record is None:
                continue
            source = current
            if finding.commit is not None:
                if finding.commit not in history:
                    history[finding.commit] = ContentSource(root, finding.commit)
                source = history[finding.commit]
            text = source.read_text(finding.path)
            if text is None or finding.end_line > len(text.split("\n")):
                problems.append(f"{report.name}: finding file missing or line range invalid")
                continue
            historical_tokenizer = (
                _tokenizer_constant(source)
                if record["classification"] == "PUBLIC_TOKENIZER_IDENTIFIER"
                else ""
            )
            for problem in verify_record(root, record, historical_tokenizer, source=source):
                context = "working tree" if finding.commit is None else finding.commit
                problems.append(f"{report.name}@{context}: {problem}")

    for problem in problems:
        print(problem)
    counts: dict[str, int] = {}
    for record in records:
        counts[record["classification"]] = counts.get(record["classification"], 0) + 1
    print(
        f"secret scan classification: records={len(records)} "
        f"reports={len(args.gitleaks_report)} problems={len(problems)} "
        f"by_class={json.dumps(dict(sorted(counts.items())))}"
    )
    return EXIT_MISMATCH if problems else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
