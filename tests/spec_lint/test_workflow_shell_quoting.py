r"""GitHub Actions workflow の `run:` が壊れた形で書かれていないことの検査。

回帰対象の不具合（2026-08-06検出・修正済み）:
`ci/github-actions-ci.yml` の2ステップが、YAMLの**折り畳みスカラー**へ
Shellの行継続 `\` を書いていた。

    run: python tools/check_design_generation_contract.py \
      --design "$DESIGN" --snapshot registry-snapshot.json \
      --spec-manifest spec/spec-manifest.json --readme README.md

折り畳みスカラーではYAMLが改行を空白へ畳むため、行末の `\` は「改行のエスケープ」
ではなく「空白のエスケープ」になる。結果として `\ --design` が単独の引数として
渡され、CIが `error: the following arguments are required: --design` で落ちる。

Shellの行継続を使う `run:` は必ずブロックスカラー `run: |` にする。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_FILES = [
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
    REPO_ROOT / "ci" / "github-actions-ci.yml",
]


def _run_commands(path: Path) -> list[tuple[str, str]]:
    """workflow中の (step名, run文字列) を列挙する。"""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for job_name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            command = step.get("run")
            if isinstance(command, str):
                found.append((f"{job_name}[{index}] {step.get('name', '')}", command))
    return found


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_workflow_exists_and_parses(path: Path) -> None:
    assert path.is_file(), f"workflow が存在しない: {path}"
    assert _run_commands(path), f"run: を持つstepが1つも無い: {path}"


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_no_folded_scalar_line_continuation(path: Path) -> None:
    """Parse後の `run` に「バックスラッシュ＋空白」が現れないこと。

    ブロックスカラーなら行継続は `\\` の直後が改行として保たれる。
    折り畳みスカラーだと改行が空白へ畳まれ `\\ ` になる。この差で判別できる。
    """
    violations = []
    for label, command in _run_commands(path):
        if "\\ " in command:
            snippet = command[: command.index("\\ ") + 20].replace("\n", "⏎")
            violations.append(f"{path.name}: {label}\n    ...{snippet}...")
    assert not violations, (
        "折り畳みスカラーへShellの行継続が書かれている。`run: |` へ変更すること:\n"
        + "\n".join(violations)
    )


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_ci_does_not_generate_runtime_evidence(path: Path) -> None:
    """不変条件#19「CIのPASSをRuntime Evidenceへ流用しない」。

    CIがRelease Manifestを生成する経路を作らない。
    """
    forbidden = ("run_verification.sh", "--emit-release-manifest", "runtime-go-release-manifest")
    violations = []
    for label, command in _run_commands(path):
        for token in forbidden:
            if token in command:
                violations.append(f"{path.name}: {label} が {token} を実行している")
    assert not violations, "CIがRuntime Evidenceを生成している:\n" + "\n".join(violations)
