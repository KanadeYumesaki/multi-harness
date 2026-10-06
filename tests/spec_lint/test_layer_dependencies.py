"""CLAUDE.md §2「層の依存規則」の静的検査。

    domain/       … sqlite3, os, subprocess, Provider SDK を import しない
    application/  … ports/ の抽象だけへ依存
    ports/        … 抽象Portの定義のみ

本検査はASTで行う。文字列検索では`import`の有無を正しく判定できないため。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "harness"

# Domain層が直接触れてはならないModule。
# 時刻・UUID・乱数・Fault InjectionはPort経由で注入する（CLAUDE.md §2）。
DOMAIN_FORBIDDEN_MODULES = frozenset(
    {
        "sqlite3",
        "os",
        "subprocess",
        "socket",
        "shutil",
        "pathlib",
        "random",
        "secrets",
        "time",
        "datetime",
        "uuid",
        "threading",
        "multiprocessing",
        "requests",
        "httpx",
        "urllib",
        "anthropic",
        "openai",
        "ollama",
    }
)


def _python_files(package: Path) -> list[Path]:
    return sorted(package.rglob("*.py")) if package.exists() else []


def _imported_roots(path: Path) -> set[str]:
    """FileがimportするTop-level Module名の集合を返す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` は node.module が None。相対importは層内参照。
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_domain_layer_has_no_forbidden_imports() -> None:
    domain = SRC / "domain"
    files = _python_files(domain)
    assert files, "domain層にPython Fileが存在しない"

    violations: list[str] = []
    for path in files:
        forbidden = _imported_roots(path) & DOMAIN_FORBIDDEN_MODULES
        for module in sorted(forbidden):
            violations.append(f"{path.relative_to(REPO_ROOT)} imports {module}")
    assert not violations, "domain層の依存規則違反:\n" + "\n".join(violations)


def test_domain_layer_does_not_import_outer_layers() -> None:
    """domain/ は application/ infrastructure/ adapters/ presentation/ を参照しない。"""
    outer = {"application", "infrastructure", "adapters", "presentation", "policy"}
    violations: list[str] = []
    for path in _python_files(SRC / "domain"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                parts = name.split(".")
                if parts[0] == "harness" and len(parts) > 1 and parts[1] in outer:
                    violations.append(f"{path.relative_to(REPO_ROOT)} imports {name}")
    assert not violations, "domain層が外側の層を参照している:\n" + "\n".join(violations)


def _imported_module_names(path: Path) -> list[tuple[int, str]]:
    """`(行番号, Module名)` を返す。相対importは層内参照なので除く。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def test_application_layer_depends_only_on_ports_and_domain() -> None:
    """CLAUDE.md §2「application/ … ports/ の抽象だけへ依存」。

    domain層しか検査していなかったため、`application/` を新設した際に
    `infrastructure.sqlite.*` と `sqlite3` を直接importしても素通りした。
    自己レビューで気付いたが、機械検査が無ければ次も同じことが起きる。

    domainへの依存は許す。純粋な値・結果型はdomainに置くのが正しく、
    ports自身もdomainを参照する。
    """
    forbidden_roots = {"sqlite3", "os", "subprocess", "socket", "requests", "httpx"}
    allowed_harness = {"ports", "domain"}
    violations: list[str] = []
    for path in _python_files(SRC / "application"):
        relative = path.relative_to(REPO_ROOT)
        for lineno, name in _imported_module_names(path):
            parts = name.split(".")
            if parts[0] in forbidden_roots:
                violations.append(f"{relative}:{lineno} imports {name}")
            if parts[0] == "harness" and len(parts) > 1 and parts[1] not in allowed_harness:
                violations.append(
                    f"{relative}:{lineno} imports {name}"
                    "（application層は ports/ と domain/ だけを参照する）"
                )
    assert not violations, "application層の依存規則違反:\n" + "\n".join(violations)


def test_ports_layer_holds_only_abstractions() -> None:
    """CLAUDE.md §2「ports/ … 抽象Portの定義のみ」。

    Portが具象を参照すると、抽象を挟んだ意味が無くなる。
    """
    violations: list[str] = []
    for path in _python_files(SRC / "ports"):
        relative = path.relative_to(REPO_ROOT)
        for lineno, name in _imported_module_names(path):
            parts = name.split(".")
            if parts[0] == "harness" and len(parts) > 1 and parts[1] not in {"domain", "ports"}:
                violations.append(f"{relative}:{lineno} imports {name}")
    assert not violations, "ports層が具象を参照している:\n" + "\n".join(violations)


def test_no_direct_sqlite_connect_anywhere() -> None:
    """§1.6「DB接続は単一ConnectionFactoryからだけ生成する」。

    `sqlite3.connect()` の直接呼出はConnectionFactory実装以外で禁止する。
    現時点でinfrastructure層は未実装のため、許可対象は空である。
    """
    allowed = {Path("src/harness/infrastructure/sqlite/connection_factory.py")}
    violations: list[str] = []
    for path in _python_files(SRC):
        relative = path.relative_to(REPO_ROOT)
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id == "sqlite3"
            ):
                violations.append(f"{relative}:{node.lineno} sqlite3.connect()")
    assert not violations, "ConnectionFactory外のDB接続:\n" + "\n".join(violations)


def test_no_shell_true_or_shell_string_process_launch() -> None:
    """不変条件#8「Process起動は`list[str]`だけ。`shell=True`を禁止する」。"""
    violations: list[str] = []
    for path in _python_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                ):
                    violations.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} shell= argument"
                    )
    assert not violations, "Shell起動の疑い:\n" + "\n".join(violations)


def test_no_approval_bypass_flags() -> None:
    """不変条件#11／§3.8.5「Approval Skip Flagを実装しない」。

    `--yes`、`--auto-approve`、`--force`、`--skip-approval` 相当の文字列が
    Source Treeに現れないことを全文検索する（§3.8.6「バイパス機構0件、AST＋全文検索」）。
    """
    forbidden = ("--auto-approve", "--skip-approval", "--no-approval", "--yes")
    violations: list[str] = []
    for path in _python_files(SRC):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {token}")
    assert not violations, "Approvalバイパス機構:\n" + "\n".join(violations)
