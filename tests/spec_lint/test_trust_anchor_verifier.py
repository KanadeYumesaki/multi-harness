"""Release Tag署名の検証（`tools/verify_trust_anchor.py`）。

## なぜ検証側を試験するのか

署名を作るだけなら誰でもできる。攻撃者は自分の鍵で署名したTagを付けられる。
**どの鍵を信じるか**を決めて、そこから外れた署名を落とさなければ、署名は
飾りにしかならない。だから試験の主役は正常系ではなく、通さない側である。

## 中間レビューで指摘された穴（2026-08-15）

1. `allowed-signers` が無いとき、設定せずに `git tag -v` を呼んでいた。
   隔離環境では落ちるが、**Global Git設定に別のTrust Storeがある環境**
   では通りうる。「通常は落ちる」は保証ではない。
2. GPG署名も受け付けていた。`gpg.ssh.allowedSignersFile` はGPGには
   効かないので、GPGモードではローカルkeyringの任意の鍵で通る。
3. 検証器と許可鍵がTagと同じCommitに入っている。Repositoryを書き換え
   られる者は両方を書き換えられるので、外部固定が無ければ
   Trust Anchor は成立しない。

本Fileはこの3点を含めて固定する。

## 鍵は使い捨てにする

各試験が `tmp_path` に鍵とRepositoryを作る。実鍵に触れないので、
CIでもローカルでも同じように走る。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "verify_trust_anchor.py"

EXIT_OK = 0
EXIT_UNSIGNED = 2
EXIT_UNTRUSTED = 3
EXIT_INPUT_INVALID = 4


def _run(argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 固定argv、shell不使用（不変条件#8）
        argv, capture_output=True, text=True, timeout=120, check=False, env=env
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    assert git is not None, "gitが見つからない"
    return _run([git, "-C", str(repo), *args])


def _verify(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, str(TOOL), "--repo", str(repo), *args])


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _keygen(target: Path, comment: str) -> Path:
    keygen = shutil.which("ssh-keygen")
    assert keygen is not None, (
        "ssh-keygen が無い。署名検証を実測できないため、この試験を"
        "飛ばすとTrust Anchorが未検証のまま残る（不変条件#16）。"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    made = _run([keygen, "-t", "ed25519", "-f", str(target), "-N", "", "-C", comment, "-q"])
    assert made.returncode == 0, made.stderr
    return target


@pytest.fixture
def signed_repo(tmp_path: Path) -> Path:
    """使い捨て鍵で署名できるRepositoryを作る。"""
    key = _keygen(tmp_path / "keys" / "signing", "trial@invalid")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Trial")
    _git(repo, "config", "user.email", "trial@invalid")
    _git(repo, "config", "gpg.format", "ssh")
    _git(repo, "config", "user.signingkey", str(key.with_suffix(".pub")))

    (repo / "ci").mkdir()
    public = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    (repo / "ci" / "allowed-signers").write_text(f"trial@invalid {public}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _sign_tag(repo: Path, name: str, *, key: Path | None = None) -> None:
    arguments = ["-c", "gpg.ssh.allowedSignersFile=ci/allowed-signers"]
    if key is not None:
        arguments += ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}"]
    result = _git(repo, *arguments, "tag", "-s", name, "-m", name)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 許可鍵が無いときのFail-Closed（中間レビュー指摘 4）
# ---------------------------------------------------------------------------


def test_missing_allowed_signers_is_refused_before_any_verification(signed_repo: Path) -> None:
    """**許可鍵Fileが無ければ、署名を読む前に拒否する。**

    以前は設定せずに `git tag -v` を呼んでいた。隔離環境では
    `gpg.ssh.allowedSignersFile needs to be configured` で落ちるが、
    Global Git設定に別のTrust Storeがある環境では通りうる。
    環境に結果を委ねているので、保証になっていない。
    """
    _sign_tag(signed_repo, "release")
    (signed_repo / "ci" / "allowed-signers").unlink()

    result = _verify(signed_repo, "--tag", "release")
    assert result.returncode == EXIT_UNTRUSTED
    assert "allowed signers file is missing" in result.stderr


def test_allowed_signers_with_only_comments_is_refused_as_unconfigured(
    signed_repo: Path,
) -> None:
    """注釈だけの許可鍵Fileは「信頼する鍵が無い」初期状態として拒否する。

    正しく署名されたTagでも通さない。`No principal matched` へ任せると、
    未設定と鍵の不一致が同じ見た目になるので、理由を別に出す。
    """
    _sign_tag(signed_repo, "release")
    (signed_repo / "ci" / "allowed-signers").write_text(
        "# no trusted signer yet\n\n   # indented comment\n", encoding="utf-8"
    )

    result = _verify(signed_repo, "--tag", "release")
    assert result.returncode == EXIT_UNTRUSTED
    assert "declares no trusted signer" in result.stderr
    assert "UNTRUSTED_REVIEW_ONLY" in result.stderr
    assert "trust anchor ok" not in result.stdout


def test_the_distributed_allowed_signers_trusts_no_key() -> None:
    """配布する許可鍵Fileは、初期状態でどの鍵も信頼しない。

    以前の所有者の鍵を既定で信頼させない。鍵を登録するのは利用者自身の操作である。
    """
    sys.path.insert(0, str(TOOL.parent))
    try:
        import verify_trust_anchor
    finally:
        sys.path.pop(0)

    distributed = REPO_ROOT / "ci" / "allowed-signers"
    assert distributed.is_file()
    assert verify_trust_anchor.declared_signers(distributed) == []


def test_global_trust_store_cannot_substitute_for_the_declared_one(
    signed_repo: Path, tmp_path: Path
) -> None:
    """Global設定に許可鍵があっても、Repositoryの宣言が無ければ通さない。

    「Global設定が無い環境では落ちる」ことしか確かめていないと、
    設定がある環境で何が起きるかは未検証のまま残る。ここを実際に作る。
    """
    _sign_tag(signed_repo, "release")

    # Repository側の宣言を消し、代わりにGlobal相当のTrust Storeを用意する
    declared = signed_repo / "ci" / "allowed-signers"
    global_store = tmp_path / "global-allowed-signers"
    global_store.write_text(declared.read_text(encoding="utf-8"), encoding="utf-8")
    declared.unlink()
    _git(signed_repo, "config", "--local", "gpg.ssh.allowedSignersFile", str(global_store))

    # 前提の確認: この状態なら git 単体では検証が通ってしまう
    raw = _git(signed_repo, "tag", "-v", "release")
    assert raw.returncode == 0, "前提が崩れている（git単体で通らない）"

    result = _verify(signed_repo, "--tag", "release")
    assert result.returncode == EXIT_UNTRUSTED
    assert "allowed signers file is missing" in result.stderr


# ---------------------------------------------------------------------------
# 外部固定（中間レビュー指摘 1）
# ---------------------------------------------------------------------------


def test_release_mode_requires_an_external_anchor(signed_repo: Path) -> None:
    """**外部固定が無ければRelease判定を通さない。**

    検証器と許可鍵はTagと同じCommitの中にある。Repositoryを書き換え
    られる者は両方を書き換えられるので、Repository内のFileだけを見て
    「正しい」と言っても、その相手には何も言えない。
    """
    _sign_tag(signed_repo, "release")

    result = _verify(signed_repo, "--tag", "release", "--require-external-anchor")
    assert result.returncode == EXIT_UNTRUSTED
    assert "UNTRUSTED_REVIEW_ONLY" in result.stderr


def test_external_anchor_matching_the_measured_hashes_is_accepted(signed_repo: Path) -> None:
    _sign_tag(signed_repo, "release")

    result = _verify(
        signed_repo,
        "--tag",
        "release",
        "--require-external-anchor",
        "--trusted-verifier-hash",
        _sha256(TOOL),
        "--trusted-signers-hash",
        _sha256(signed_repo / "ci" / "allowed-signers"),
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert "trust_level=EXTERNALLY_ANCHORED" in result.stdout


def test_tampered_allowed_signers_is_detected_by_the_external_hash(signed_repo: Path) -> None:
    """**許可鍵へ鍵を1本足した改変を、外部固定Hashが捕まえる。**

    これが本Toolの中心にある攻撃である。攻撃者は自分の鍵を
    `ci/allowed-signers` へ足し、そのCommitへ署名してTagを打つ。
    Repository内の整合性は保たれているので、内部検査では気付けない。
    """
    original = _sha256(signed_repo / "ci" / "allowed-signers")
    rogue = _keygen(signed_repo.parent / "keys" / "rogue", "rogue@invalid")
    with (signed_repo / "ci" / "allowed-signers").open("a", encoding="utf-8") as handle:
        handle.write(f"rogue@invalid {rogue.with_suffix('.pub').read_text().strip()}\n")
    _git(signed_repo, "add", "-A")
    _git(signed_repo, "commit", "-qm", "add rogue key")
    _sign_tag(signed_repo, "release", key=rogue.with_suffix(".pub"))

    result = _verify(
        signed_repo,
        "--tag",
        "release",
        "--require-external-anchor",
        "--trusted-verifier-hash",
        _sha256(TOOL),
        "--trusted-signers-hash",
        original,
    )
    assert result.returncode == EXIT_UNTRUSTED
    assert "EXTERNAL_TRUST_ANCHOR_MISMATCH for allowed-signers" in result.stderr


def test_tampered_verifier_is_detected_by_the_external_hash(
    signed_repo: Path, tmp_path: Path
) -> None:
    """検証器そのものを書き換えた場合。

    「常に成功する検証器」へ差し替えられると、他の全ての検査が無意味に
    なる。だから検証器自身のHashも外部で固定する。
    """
    _sign_tag(signed_repo, "release")

    result = _verify(
        signed_repo,
        "--tag",
        "release",
        "--require-external-anchor",
        "--trusted-verifier-hash",
        "sha256:" + "0" * 64,
        "--trusted-signers-hash",
        _sha256(signed_repo / "ci" / "allowed-signers"),
    )
    assert result.returncode == EXIT_UNTRUSTED
    assert "EXTERNAL_TRUST_ANCHOR_MISMATCH for verifier" in result.stderr


def test_malformed_external_hash_is_input_invalid(signed_repo: Path) -> None:
    _sign_tag(signed_repo, "release")
    result = _verify(signed_repo, "--tag", "release", "--trusted-verifier-hash", "not-a-hash")
    assert result.returncode == EXIT_INPUT_INVALID


def test_without_external_anchor_the_trust_level_is_review_only(signed_repo: Path) -> None:
    """外部固定を求めない呼び方でも、結果に格付けを書く。

    同じ「PASS」でも意味が違う。区別できないと、開発中の確認結果を
    Release GO の根拠として持ち出せてしまう。
    """
    _sign_tag(signed_repo, "release")
    result = _verify(signed_repo, "--tag", "release")
    assert result.returncode == EXIT_OK
    assert "trust_level=UNTRUSTED_REVIEW_ONLY" in result.stdout


# ---------------------------------------------------------------------------
# 署名方式の固定（中間レビュー指摘 5）
# ---------------------------------------------------------------------------


def test_gpg_signed_tag_is_refused(signed_repo: Path, tmp_path: Path) -> None:
    """GPG署名を受け付けない。

    `gpg.ssh.allowedSignersFile` はGPGには効かない。同じ関数で両方を
    受けると、片方だけ信頼制御が無い状態になる。
    実鍵を作らず、Tag objectへGPG署名の見出しを持たせて判定を確かめる。
    """
    _git(signed_repo, "tag", "-a", "gpgish", "-m", "body")
    # annotated tag の中身をGPG署名付きの形へ差し替える
    raw = _git(signed_repo, "cat-file", "tag", "gpgish").stdout
    forged = raw + "-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n"
    written = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [
            shutil.which("git") or "git",
            "-C",
            str(signed_repo),
            "hash-object",
            "-t",
            "tag",
            "-w",
            "--stdin",
        ],
        input=forged,
        capture_output=True,
        text=True,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    _git(signed_repo, "update-ref", "refs/tags/gpgish", written.stdout.strip())

    result = _verify(signed_repo, "--tag", "gpgish")
    assert result.returncode == EXIT_UNTRUSTED
    assert "SSH signatures only" in result.stderr


# ---------------------------------------------------------------------------
# 保護Branch由来（中間レビュー指摘 3）
# ---------------------------------------------------------------------------


def test_tag_outside_the_protected_branch_is_refused(signed_repo: Path) -> None:
    """**未レビューBranchのCommitへ署名しても通さない。**

    署名はCommitの出所を保証しない。「誰が承認したか」は分かるが、
    「レビューを経たか」は別の話である。
    """
    _git(signed_repo, "branch", "protected")
    _git(signed_repo, "checkout", "-q", "-b", "side")
    (signed_repo / "unreviewed.txt").write_text("bypass\n", encoding="utf-8")
    _git(signed_repo, "add", "-A")
    _git(signed_repo, "commit", "-qm", "unreviewed change")
    _sign_tag(signed_repo, "release")

    result = _verify(signed_repo, "--tag", "release", "--require-ancestor-of", "protected")
    assert result.returncode == EXIT_UNTRUSTED
    assert "not an ancestor of protected" in result.stderr


def test_tag_on_the_protected_branch_is_accepted(signed_repo: Path) -> None:
    _git(signed_repo, "branch", "protected")
    _sign_tag(signed_repo, "release")

    result = _verify(signed_repo, "--tag", "release", "--require-ancestor-of", "protected")
    assert result.returncode == EXIT_OK, result.stderr


# ---------------------------------------------------------------------------
# 署名そのもの
# ---------------------------------------------------------------------------


def test_missing_tag_is_reported_as_unestablished(signed_repo: Path) -> None:
    """Tagが無いのは「未確立」であって「異常」ではない。

    Trust Anchorをまだ張っていない状態は、Release前の正常な途中経過である。
    ここを異常終了にすると、未確立と改ざんが同じ見た目になる。
    """
    result = _verify(signed_repo, "--tag", "absent")
    assert result.returncode == EXIT_UNSIGNED
    assert "tag not found" in result.stderr


def test_lightweight_tag_cannot_stand_in_for_a_signature(signed_repo: Path) -> None:
    """`git tag v1.0` と `git tag -s v1.0` は同じ「Tagがある」に見える。"""
    _git(signed_repo, "tag", "lightweight")
    result = _verify(signed_repo, "--tag", "lightweight")
    assert result.returncode == EXIT_UNSIGNED
    assert "lightweight" in result.stderr


def test_unsigned_annotated_tag_is_rejected(signed_repo: Path) -> None:
    _git(signed_repo, "tag", "-a", "plain", "-m", "unsigned")
    result = _verify(signed_repo, "--tag", "plain")
    assert result.returncode == EXIT_UNSIGNED
    assert "not signed" in result.stderr


def test_signature_from_an_unlisted_key_is_rejected(signed_repo: Path, tmp_path: Path) -> None:
    """署名が「ある」ことと「信じてよい」ことは別である。"""
    rogue = _keygen(tmp_path / "keys" / "rogue", "rogue@invalid")
    _sign_tag(signed_repo, "rogue", key=rogue.with_suffix(".pub"))

    result = _verify(signed_repo, "--tag", "rogue")
    assert result.returncode == EXIT_UNTRUSTED


def test_tag_pointing_at_another_commit_is_rejected(signed_repo: Path) -> None:
    """Releaseの中身はCommitで決まる。別のCommitを指すTagは裏付けにならない。"""
    _sign_tag(signed_repo, "release")
    (signed_repo / "file.txt").write_text("change\n", encoding="utf-8")
    _git(signed_repo, "add", "-A")
    _git(signed_repo, "commit", "-qm", "second")
    head = _git(signed_repo, "rev-parse", "HEAD").stdout.strip()

    result = _verify(signed_repo, "--tag", "release", "--expect-commit", head)
    assert result.returncode == EXIT_UNTRUSTED
    assert "points at" in result.stderr


# ---------------------------------------------------------------------------
# 通す側（これが無いと上の試験は「常に落ちる実装」でも通る）
# ---------------------------------------------------------------------------


def test_signed_tag_records_key_fingerprint_and_hashes(signed_repo: Path, tmp_path: Path) -> None:
    """Evidenceに何を残すか。

    署名者をメールアドレスだけで記録すると、鍵をローテーションした後に
    **同一人物・別鍵**を区別できない。Fingerprintまで残す。
    """
    _sign_tag(signed_repo, "release")
    evidence = tmp_path / "trust-anchor.json"
    result = _verify(signed_repo, "--tag", "release", "--emit-evidence", str(evidence))
    assert result.returncode == EXIT_OK, result.stderr

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    summary = payload["summary"]
    assert payload["area"] == "trust_anchor"
    assert payload["trust_level"] == "UNTRUSTED_REVIEW_ONLY"
    # Runtime GO の Evidence Area にはまだ登録していない。
    assert payload["runtime_go_evidence_area"] is False
    assert summary["signer"] == "trial@invalid"
    assert summary["key_fingerprint"].startswith("SHA256:")
    assert summary["key_type"]
    assert summary["tag_object_id"]
    assert summary["commit"]
    assert summary["allowed_signers_sha256"].startswith("sha256:")
    assert summary["verifier_sha256"] == _sha256(TOOL)
    assert summary["external_anchor_supplied"] is False
