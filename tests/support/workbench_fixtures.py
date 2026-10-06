"""Workbench 試験の共有部品。

## 偽 CLI は「実 Process」として起動する

Harness は env を継承させない。だから `FAKE_CLI_MODE` を環境変数で渡せない。
代わりに **モードを焼き込んだ小さな実行体** を書き、profile の `argv_prefix` へ
その Path を入れる。こうすると argv・env・cwd・stdin・終了 Code・出力上限・
timeout・孫 Process まで、本物と同じ経路で試験できる。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

FAKE_CLI_SOURCE = Path(__file__).resolve().parent / "fake_cli.py"

__all__ = [
    "FAKE_CLI_SOURCE",
    "StubBoundaryProbe",
    "build_demo_worktree",
    "contained_argv_prefix",
    "sandbox_policy_for",
    "write_fake_cli",
    "write_runtime_profile",
]


class StubBoundaryProbe:
    """試験専用の能力検証。**Production は決して使わない。**

    実際の境界測定（`LinuxBoundaryProbe`）は Launcher を 2 回起動するので、偽 CLI を
    使う Flow 試験ごとに走らせると時間が要る。ここでは「測っていない」ことを
    `details` に明示したまま通す。

    実測そのものは `tests/integration/workbench/test_boundary_gate.py` が
    `LinuxBoundaryProbe` で行い、この Machine では **Provider を起動させない** こと
    まで確かめている。承認や安全制限を飛ばす Flag ではなく、測定の代役である。
    """

    def __init__(self, *, satisfied: bool = True, reasons: tuple[str, ...] = ()) -> None:
        self._satisfied = satisfied
        self._reasons = reasons

    def verdict(self, provider_id: str) -> Any:
        from harness.ports.cli_workbench import BoundaryVerdict

        return BoundaryVerdict(
            satisfied=self._satisfied,
            blocking_reasons=self._reasons,
            details={"measured": False, "stub": "tests only", "provider_id": provider_id},
        )


def write_fake_cli(
    directory: Path,
    *,
    mode: str = "OK",
    provider: str = "codex",
    replacement: str | None = None,
    name: str = "fake-cli.py",
    feature_overrides: dict[str, bool] | None = None,
    probe_path: str | None = None,
    leaked_child_record: Path | None = None,
) -> Path:
    """モードを焼き込んだ偽 CLI 実行体を書く。

    本体は **同じ Directory へ複製する。** Harness が掛ける Landlock 境界の内側から
    Repository は読めないので、`tests/support` を直接 `runpy` できない。実際の CLI と
    同じく「導入先の下だけで完結する実行体」にしておく。
    """
    directory.mkdir(parents=True, exist_ok=True)
    body = directory / "fake_cli_body.py"
    body.write_text(FAKE_CLI_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    target = directory / name
    settings: dict[str, str] = {"FAKE_CLI_MODE": mode, "FAKE_CLI_PROVIDER": provider}
    if replacement is not None:
        settings["FAKE_CLI_REPLACEMENT"] = replacement
    if feature_overrides is not None:
        settings["FAKE_CLI_FEATURE_OVERRIDE"] = json.dumps(feature_overrides)
    if probe_path is not None:
        settings["FAKE_CLI_PROBE_PATH"] = probe_path
    if leaked_child_record is not None:
        settings["FAKE_CLI_CHILD_RECORD"] = str(leaked_child_record)
    target.write_text(
        "import os, runpy\n"
        f"os.environ.update({settings!r})\n"
        f"runpy.run_path({str(body)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return target


def write_runtime_profile(
    path: Path,
    *,
    executables: dict[str, list[str]],
    home: Path,
    neutral_workdir: Path,
    state_dir: Path,
    cli_runtime_root: Path,
    suggested_models: dict[str, list[str]] | None = None,
) -> Path:
    document: dict[str, Any] = {
        "contract": "cli-runtime-profile/1",
        "home": str(home),
        "neutral_workdir": str(neutral_workdir),
        "state_dir": str(state_dir),
        "cli_runtime_root": str(cli_runtime_root),
        "providers": {
            provider_id: {
                "display_name": f"fake-{provider_id}",
                "package": f"fake/{provider_id}",
                "package_version": "0.0.0-fake",
                "argv_prefix": argv,
                "suggested_models": (suggested_models or {}).get(provider_id, []),
            }
            for provider_id, argv in executables.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def fake_argv_prefix(script: Path) -> list[str]:
    return [os.path.realpath(sys.executable), str(script)]


def sandbox_policy_for(*, read_execute: list[Path], read_write: list[Path]) -> dict[str, Any]:
    """試験用の Landlock Policy。**本番と同じ Launcher へ同じ形で渡す。**"""
    from harness.infrastructure.provider import sandbox_launcher

    return {
        "contract": sandbox_launcher.POLICY_CONTRACT,
        "read_execute": [
            "/usr",
            "/lib",
            "/lib64",
            "/bin",
            "/sbin",
            os.path.dirname(os.path.realpath(sys.executable)),
            *(str(item) for item in read_execute),
        ],
        "read_only": ["/etc", "/dev/urandom", "/dev/random"],
        "read_write": ["/dev/null", "/dev/zero", *(str(item) for item in read_write)],
        "optional_paths": ["/lib64", "/sbin", "/dev/random", "/dev/zero"],
    }


def contained_argv_prefix(script: Path, policy: dict[str, Any]) -> list[str]:
    """Launcher（PID namespace + Landlock）を通して偽 CLI を起動する argv。

    Runner が子孫の全数を数えられるのは、この経路で起動したときだけである。
    """
    from harness.infrastructure.provider import sandbox_launcher

    return [
        os.path.realpath(sys.executable),
        str(Path(sandbox_launcher.__file__).resolve()),
        json.dumps(policy, separators=(",", ":"), sort_keys=True),
        "--",
        os.path.realpath(sys.executable),
        str(script),
    ]


def build_demo_worktree(root: Path, *, files: dict[str, str] | None = None) -> Path:
    """隔離 Git Worktree を作る。**通常 repo の `.git` Directory は使えない。**

    既にあるものは作り直さない。再起動を模す試験は同じ Path をもう一度渡す。
    """
    repo = root / "repo"
    worktree = root / "worktree"
    if (worktree / ".git").is_file():
        return worktree
    repo.mkdir(parents=True, exist_ok=True)
    payload = files or {
        "hello.py": 'def greet(name):\n    return "Hello, " + name\n',
        "notes.md": "# メモ\n\n合成デモ用。\n",
    }
    _git(repo, ["init", "-q", "-b", "main"])
    _git(repo, ["config", "user.email", "workbench@example.invalid"])
    _git(repo, ["config", "user.name", "workbench"])
    for name, text in payload.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, ["add", *sorted(payload)])
    _git(repo, ["commit", "-q", "-m", "synthetic"])
    _git(repo, ["worktree", "add", "-q", "-B", "workbench", str(worktree), "main"])
    return worktree


def _git(cwd: Path, args: list[str]) -> None:
    subprocess.run(  # noqa: S603 - fixed executable, argv list, no shell
        ["/usr/bin/git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=60,
        env={
            "PATH": os.defpath,
            "HOME": str(cwd),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "workbench",
            "GIT_AUTHOR_EMAIL": "workbench@example.invalid",
            "GIT_COMMITTER_NAME": "workbench",
            "GIT_COMMITTER_EMAIL": "workbench@example.invalid",
        },
    )
