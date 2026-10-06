#!/usr/bin/env python3
"""Release Tag の署名を検証し、Trust Anchor Evidence を出力する。

## なぜ署名が要るのか

`release-binding.json` の `commit_ancestry` は、生成時に
`git merge-base --is-ancestor` で確かめた結果を**記録**する。だがZIPは
Git object databaseを含まないため、受領者はその記録を再証明できない。
記録を書いたのが誰かも分からない。つまり `commit_ancestry` は
**自己申告**であり、`external_trust_anchor_required: true` はそれを
正直に申告している。

署名付きTagは、この自己申告へ「誰が」を付ける。

## 署名だけでは Trust Anchor にならない

**Tagが指すCommitの中に、検証器も許可鍵も入っている。**
悪意あるCommitは次を同時に行える。

* `ci/allowed-signers` へ自分の公開鍵を足す
* 本File（検証器）を「常に成功」へ書き換える
* Workflowから検証工程を消す

そのCommitへ自分の鍵で署名したTagを打てば、検証は通る。
署名は「誰が」を示すが、**その誰かを誰が決めるのか**は署名の外にある。

したがって Trust Anchor が成立するには、次の2つがRepositoryの外側で
固定されている必要がある。

    --trusted-verifier-hash   本Fileの期待Hash
    --trusted-signers-hash    許可鍵Fileの期待Hash

CI では Protected Variable から渡す。Repositoryを書き換えられる者でも
Variableは変えられない、という前提を置いて初めて外部固定になる。

`--require-external-anchor` を付けると、この2つが無いとき失敗する。
Release判定ではこれを必ず付ける。付けずに通した結果は
`UNTRUSTED_REVIEW_ONLY` であり、Release GO の根拠にならない。

## SSH署名のみを受け付ける

GPG署名は受け付けない。`gpg.ssh.allowedSignersFile` はSSH署名にしか
効かないため、GPGモードでは**ローカルkeyringにある任意の鍵**で検証が
通りうる。同じ関数で両方を受けると、片方だけ信頼制御が無い状態になる。
GPGを正式対応するなら、許可Fingerprint・専用GNUPGHOME・keyring Hashの
検証が別途要る。MVP0-Aではその設計を持たないので拒否する。

## 終了Code

    0  署名を検証できた
    2  署名が無い（Trust Anchor未確立。BLOCKEDに相当する正常な未達）
    3  署名はあるが信頼できない／Tagが別のCommitを指す／外部固定が無い／
       許可鍵Fileに信頼する鍵が1件も無い（配布時の初期状態）
    4  入力不正
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256_FIELD = re.compile(r"^sha256:[0-9a-f]{64}$")

# `git tag -v` のSSH署名成功時の文言。署名者・鍵種別・Fingerprintを取る。
SSH_GOOD = re.compile(r'Good "git" signature for (\S+) with (\S+) key (SHA256:\S+)')

EXIT_OK = 0
EXIT_UNSIGNED = 2
EXIT_UNTRUSTED = 3
EXIT_INPUT_INVALID = 4

SELF_PATH = Path(__file__).resolve()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise SystemExit(EXIT_INPUT_INVALID)
    return subprocess.run(  # noqa: S603 - 固定executable、shell不使用（不変条件#8）
        [git, "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def declared_signers(path: Path) -> list[str]:
    """許可鍵Fileのうち、注釈と空行を除いた宣言行を返す（OpenSSH allowed signers形式）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fail(message: str, code: int) -> int:
    print(message, file=sys.stderr)
    return code


def _check_external_anchor(
    args: argparse.Namespace, allowed: Path
) -> tuple[int | None, dict[str, str]]:
    """Repository外部で固定された期待Hashと照合する。

    ここが本Toolの要である。Repository内のFileだけを見て「正しい」と
    言っても、そのRepositoryごと書き換えた相手には何も言えない。
    """
    verifier_hash = sha256_file(SELF_PATH)
    signers_hash = sha256_file(allowed)
    measured = {"verifier_sha256": verifier_hash, "allowed_signers_sha256": signers_hash}

    for label, supplied, actual in (
        ("verifier", args.trusted_verifier_hash, verifier_hash),
        ("allowed-signers", args.trusted_signers_hash, signers_hash),
    ):
        if supplied is None:
            if args.require_external_anchor:
                option = "verifier" if label == "verifier" else "signers"
                return (
                    _fail(
                        f"external trust anchor for {label} is required but was not supplied; "
                        f"pass --trusted-{option}-hash from a protected CI variable. "
                        "Without it this check is UNTRUSTED_REVIEW_ONLY.",
                        EXIT_UNTRUSTED,
                    ),
                    measured,
                )
            continue
        if not SHA256_FIELD.fullmatch(supplied):
            option = "verifier" if label == "verifier" else "signers"
            return (
                _fail(f"--trusted-{option}-hash must be sha256:<64hex>", EXIT_INPUT_INVALID),
                measured,
            )
        if supplied != actual:
            return (
                _fail(
                    f"EXTERNAL_TRUST_ANCHOR_MISMATCH for {label}: "
                    f"expected={supplied} actual={actual}",
                    EXIT_UNTRUSTED,
                ),
                measured,
            )
    return None, measured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--tag", required=True, help="検証するRelease Tag名")
    parser.add_argument("--expect-commit", help="Tagが指すべきCommit SHA（40桁）。既定はHEAD")
    parser.add_argument(
        "--allowed-signers",
        type=Path,
        default=Path("ci/allowed-signers"),
        help="SSH署名で信頼する鍵の一覧。**無ければ拒否する**",
    )
    parser.add_argument(
        "--require-ancestor-of",
        metavar="REF",
        help="Tag対象CommitがこのRefの祖先であることを要求する（例 origin/main）",
    )
    parser.add_argument(
        "--trusted-verifier-hash",
        metavar="SHA256",
        help="本Fileの期待Hash。Repository外（Protected Variable）から渡す",
    )
    parser.add_argument(
        "--trusted-signers-hash",
        metavar="SHA256",
        help="許可鍵Fileの期待Hash。Repository外から渡す",
    )
    parser.add_argument(
        "--require-external-anchor",
        action="store_true",
        help="外部固定Hashが無ければ失敗する。Release判定では必ず付ける",
    )
    parser.add_argument("--emit-evidence", type=Path)
    args = parser.parse_args(argv)

    repo: Path = args.repo

    # --- 許可鍵。**最初に**確かめる ---------------------------------------
    # 無いまま `git tag -v` を呼ぶと、Global設定に別のTrust Storeがある
    # 環境では署名が通りうる。「隔離環境では落ちる」は保証ではない。
    allowed = repo / args.allowed_signers
    if not allowed.is_file():
        return _fail(
            f"allowed signers file is missing: {allowed}. "
            "Signature verification without a declared trust store is not verification.",
            EXIT_UNTRUSTED,
        )
    # 注釈だけのFileは「信頼する鍵が無い」初期状態である。`git tag -v` の
    # `No principal matched` へ任せると、未設定と鍵の不一致が同じ見た目になる。
    if not declared_signers(allowed):
        return _fail(
            f"allowed signers file declares no trusted signer: {allowed}. "
            "The trust anchor is not configured; the result stays UNTRUSTED_REVIEW_ONLY. "
            "See docs/TRUST-ANCHOR-SETUP.md.",
            EXIT_UNTRUSTED,
        )

    anchor_failure, measured = _check_external_anchor(args, allowed)
    if anchor_failure is not None:
        return anchor_failure

    # --- Tagの存在と指す先 -------------------------------------------------
    tag_object = _git(repo, "rev-parse", "--verify", f"refs/tags/{args.tag}")
    if tag_object.returncode != 0:
        return _fail(f"tag not found: {args.tag}", EXIT_UNSIGNED)
    tag_object_id = tag_object.stdout.strip()

    kind = _git(repo, "cat-file", "-t", f"refs/tags/{args.tag}")
    if kind.stdout.strip() != "tag":
        return _fail(
            f"{args.tag} is a lightweight tag and cannot carry a signature; "
            "create it with `git tag -s`",
            EXIT_UNSIGNED,
        )

    pointed = _git(repo, "rev-list", "-n", "1", f"refs/tags/{args.tag}")
    if pointed.returncode != 0:
        return _fail(f"cannot resolve the commit behind {args.tag}", EXIT_INPUT_INVALID)
    commit = pointed.stdout.strip()

    expected = args.expect_commit
    if expected is None:
        head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
        if head.returncode != 0:
            return _fail("cannot resolve HEAD", EXIT_INPUT_INVALID)
        expected = head.stdout.strip()
    if not COMMIT_SHA.fullmatch(expected):
        return _fail("--expect-commit must be a 40-character commit SHA", EXIT_INPUT_INVALID)
    if commit != expected:
        # 署名の検証以前の問題。別のCommitを指すTagは、いくら正しく署名
        # されていてもこのReleaseの裏付けにならない。
        return _fail(f"tag {args.tag} points at {commit}, expected {expected}", EXIT_UNTRUSTED)

    # --- 保護Branch由来であること -----------------------------------------
    # 署名はCommitの出所を保証しない。未レビューBranchのCommitでも
    # 署名付きTagは打てる。
    if args.require_ancestor_of:
        reachable = _git(repo, "merge-base", "--is-ancestor", commit, args.require_ancestor_of)
        if reachable.returncode != 0:
            return _fail(
                f"tagged commit {commit} is not an ancestor of {args.require_ancestor_of}; "
                "release tags must come from the protected branch",
                EXIT_UNTRUSTED,
            )

    # --- 署名の種別。SSH以外は受け付けない ---------------------------------
    raw = _git(repo, "cat-file", "tag", args.tag)
    if raw.returncode != 0:
        return _fail(f"cannot read tag object for {args.tag}", EXIT_INPUT_INVALID)
    body = raw.stdout
    if "-----BEGIN PGP SIGNATURE-----" in body:
        return _fail(
            f"{args.tag} carries a GPG signature. MVP0-A accepts SSH signatures only, "
            "because gpg.ssh.allowedSignersFile does not constrain GPG keys and any key in "
            "the local keyring would verify.",
            EXIT_UNTRUSTED,
        )
    if "-----BEGIN SSH SIGNATURE-----" not in body:
        return _fail(f"tag {args.tag} is not signed", EXIT_UNSIGNED)

    # --- 署名の検証 --------------------------------------------------------
    verified = _git(repo, "-c", f"gpg.ssh.allowedSignersFile={allowed}", "tag", "-v", args.tag)
    output = verified.stdout + verified.stderr
    if verified.returncode != 0:
        return _fail(f"signature on {args.tag} did not verify:\n{output.strip()}", EXIT_UNTRUSTED)

    match = SSH_GOOD.search(output)
    if match is None:
        # 検証は通ったが誰の署名か読み取れない。「検証できた」と
        # 記録できないので通さない。
        return _fail(
            f"cannot determine the signer of {args.tag}:\n{output.strip()}", EXIT_UNTRUSTED
        )
    signer, key_type, fingerprint = match.group(1), match.group(2), match.group(3)

    external = args.trusted_verifier_hash is not None and args.trusted_signers_hash is not None
    trust_level = "EXTERNALLY_ANCHORED" if external else "UNTRUSTED_REVIEW_ONLY"

    print(
        f"trust anchor ok: tag={args.tag} commit={commit} "
        f"signer={signer} key={fingerprint} trust_level={trust_level}"
    )

    if args.emit_evidence:
        payload = {
            "evidence_schema_version": "1.2",
            "area": "trust_anchor",
            "status": "PASS",
            # Runtime GO の Evidence Area にはまだ登録していない。
            # これは Release 補助記録であり、Runtime GO の要件は満たさない。
            "runtime_go_evidence_area": False,
            "trust_level": trust_level,
            "summary": {
                "tag": args.tag,
                "tag_object_id": tag_object_id,
                "commit": commit,
                "signer": signer,
                "key_type": key_type,
                "key_fingerprint": fingerprint,
                "allowed_signers_path": str(args.allowed_signers),
                "allowed_signers_sha256": measured["allowed_signers_sha256"],
                "verifier_sha256": measured["verifier_sha256"],
                "external_anchor_supplied": external,
                "ancestor_of": args.require_ancestor_of,
                "verification_method": "git tag -v (ssh)",
            },
            "producer": "tools/verify_trust_anchor.py",
            "test_run_id": f"trust-anchor-{commit[:12]}",
            "recorded_at": _now(),
        }
        args.emit_evidence.parent.mkdir(parents=True, exist_ok=True)
        args.emit_evidence.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.emit_evidence}")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
