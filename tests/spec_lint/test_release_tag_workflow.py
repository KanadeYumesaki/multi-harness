"""Release Tag Job の構造検査（中間レビュー指摘 1・2・3・7）。

## なぜ構造を試験するのか

Trust Anchor の強さは、検証器の正しさだけでは決まらない。**どういう
条件でその検証器が呼ばれるか**で決まる。呼び出し側が緩ければ、検証器が
どれだけ厳格でも意味がない。

指摘されたのは次の形である。

* `needs` が無く、Spec・Quality・Supply-chain が落ちていても
  署名検証だけがPASSできた
* 保護Branch由来かを確かめておらず、未レビューBranchのCommitへ
  署名すれば通った
* 外部固定Hashを渡していないので、Repositoryごと書き換えた相手に
  対しては何も言えなかった

いずれもWorkflowの書き方の問題で、Pythonの試験では捕まらない。
YAMLを読んで確かめる。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
    REPO_ROOT / "ci" / "github-actions-ci.yml",
)
JOB_ID = "release-tag"


def _jobs(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs: dict[str, Any] = document["jobs"]
    return jobs


def _release_job(path: Path) -> dict[str, Any]:
    jobs = _jobs(path)
    assert JOB_ID in jobs, f"{path.name} に {JOB_ID} Job が無い"
    job: dict[str, Any] = jobs[JOB_ID]
    return job


def _script(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_release_tag_job_depends_on_the_other_checks(path: Path) -> None:
    """**他の検査が通っていないTagをReleaseとして認めない。**

    `needs` が無いと、Spec・Quality・Supply-chain が落ちていても署名検証
    だけがPASSでき、「CIが緑のTag」に見えてしまう。署名はコードの正しさを
    保証しない。誰が承認したかを示すだけである。
    """
    job = _release_job(path)
    needs = job.get("needs") or []
    jobs = _jobs(path)

    for required in ("spec", "quality", "supply-chain"):
        assert required in needs, f"{path.name}: {JOB_ID} が {required} に依存していない"
    missing = [name for name in needs if name not in jobs]
    assert not missing, f"{path.name}: 存在しないJobへ依存している: {missing}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_release_tag_job_runs_only_for_tags(path: Path) -> None:
    """Tagのときだけ走る。

    毎回走らせると「Tagが無い」で常に同じ結果になり、何も見ていない
    工程が1つ増える。緑が増えるだけで、判定材料は増えない。
    """
    job = _release_job(path)
    assert "refs/tags/" in str(job.get("if", "")), f"{path.name}: Tag限定になっていない"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_release_tag_job_requires_an_external_anchor(path: Path) -> None:
    """**外部固定を必須にする。**

    検証器も許可鍵もTagが指すCommitの中にある。Repositoryを書き換え
    られる者は両方を書き換えられるので、Repository内のFileだけを見て
    「正しい」と言っても、その相手には何も言えない。
    """
    job = _release_job(path)
    script = _script(job)
    assert "--require-external-anchor" in script, f"{path.name}: 外部固定が必須になっていない"
    assert "--trusted-verifier-hash" in script
    assert "--trusted-signers-hash" in script
    # Protected Variable から渡していること。Repository内のFileから
    # 読んでは、外部固定にならない。
    joined = "\n".join(
        str(step.get("env", {})) for step in job["steps"] if isinstance(step.get("env"), dict)
    )
    assert "vars." in joined, f"{path.name}: 期待Hashを Protected Variable から渡していない"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_release_tag_job_requires_a_protected_branch_ancestor(path: Path) -> None:
    """署名はCommitの出所を保証しない。

    未レビューBranchのCommitでも署名付きTagは打てる。保護Branchの
    祖先であることを別途確かめる。
    """
    job = _release_job(path)
    script = _script(job)
    assert "--require-ancestor-of" in script, f"{path.name}: 祖先検証が無い"
    assert "origin/main" in script


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_release_tag_job_fetches_full_history(path: Path) -> None:
    """浅いcloneだと `git tag -v` も祖先判定も対象を読めない。"""
    job = _release_job(path)
    checkouts = [
        step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts, f"{path.name}: checkout が無い"
    assert all(step.get("with", {}).get("fetch-depth") == 0 for step in checkouts), (
        f"{path.name}: fetch-depth: 0 が無い"
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_release_tag_job_fetches_the_annotated_tag_object(path: Path) -> None:
    """Runner上で軽量Tagに化けた場合でも署名付きTag objectを再取得する。"""
    job = _release_job(path)
    script = _script(job)
    assert "refs/tags/${GITHUB_REF_NAME}:refs/tags/${GITHUB_REF_NAME}" in script, (
        f"{path.name}: annotated Tag objectの明示取得が無い"
    )
    assert 'git cat-file -t "refs/tags/${GITHUB_REF_NAME}"' in script, (
        f"{path.name}: Tag object種別のFail-Closed検査が無い"
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_release_zip_is_built_and_cross_checked_in_the_same_workflow(path: Path) -> None:
    """検証したTagと配布するZIPが同じであることを、同一Workflowで確かめる。

    手元で別に作ると、検証したものと配ったものが違っていても誰も
    気付かない。Bindingを読み直してTagとCommitを突き合わせる。
    """
    job = _release_job(path)
    script = _script(job)
    assert "package_release_zip.py" in script, f"{path.name}: ZIP生成が無い"
    assert "--trust-anchor-tag" in script
    assert "release-binding.json" in script, f"{path.name}: Bindingの突き合わせが無い"
    assert "GITHUB_REF_NAME" in script
    assert "GITHUB_SHA" in script


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_does_not_write_artifacts_into_the_repository(path: Path) -> None:
    """**成果物をRepositoryの中へ置かない。**

    回帰対象（2026-08-16 レビュー指摘・修正済み）: Evidenceを
    `--emit-evidence trust-anchor.json` とRepository直下へ出していた。
    未追跡Fileが1つ増えるだけで、後段の `package_release_zip.py` が
    clean tree 違反で止まる。

    手順書には「鍵と配布物をRepositoryの中へ置かない」と落とし穴として
    書いてあった。**書いた本人がWorkflow側で踏んでいた。** 散文の注意書きは
    自分にも効かない。
    """
    job = _release_job(path)
    script = _script(job)

    for option, value in (("--emit-evidence", None), ("--out", None)):
        for line in script.splitlines():
            stripped = line.strip()
            if not stripped.startswith(option):
                continue
            target = stripped.removeprefix(option).strip().strip('"')
            assert target.startswith("${RUNNER_TEMP}") or target.startswith("evidence/"), (
                f"{path.name}: {option} の出力先がRepository内にある: {target}"
            )
            assert value is None  # 形をそろえるためのダミー

    # 出力先を変えても、汚れたまま先へ進まないことを別途確かめる工程を置く。
    assert "git status --porcelain" in script, (
        f"{path.name}: packaging前に作業ツリーの清浄を確かめる工程が無い"
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_uploaded_artifacts_come_from_the_runner_temp(path: Path) -> None:
    """Artifactの収集元も同じ場所を指していること。

    出力先だけ変えてUpload側が古いPathのままだと、Artifactが空になる。
    失敗はせず、成果物だけが消える。気付きにくい壊れ方である。
    """
    job = _release_job(path)
    uploads = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert uploads, f"{path.name}: Artifact出力が無い"
    for step in uploads:
        paths = str(step.get("with", {}).get("path", ""))
        for line in paths.splitlines():
            entry = line.strip()
            if entry:
                assert "runner.temp" in entry or entry.startswith("evidence/"), (
                    f"{path.name}: Artifact収集元がRepository内: {entry}"
                )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_artifact_retention_is_not_mistaken_for_archival(path: Path) -> None:
    """CI Artifactは保持期限で消える。**長期保管ではない。**

    ここに残っているから大丈夫、と思い込む余地を消すため、
    保持期限を明示し、恒久保管の手順を指し示す。
    """
    job = _release_job(path)
    uploads = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert uploads, f"{path.name}: Artifact出力が無い"
    assert all("retention-days" in step.get("with", {}) for step in uploads), (
        f"{path.name}: retention-days を明示していない"
    )
    text = path.read_text(encoding="utf-8")
    assert "長期保管ではない" in text, f"{path.name}: 保持期限の注意書きが無い"
