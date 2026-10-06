"""配布ZIPのGit来歴拘束・決定性・Release Bindingの回帰試験。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGER = ROOT / "tools" / "package_release_zip.py"
GIT = shutil.which("git")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert GIT is not None, "Git is required for release package tests"
    return subprocess.run(  # noqa: S603 - fixed executable and arguments, shell disabled
        [GIT, "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def _commit(root: Path) -> str:
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "release-test@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Release Test").returncode == 0
    assert _git(root, "add", "-A").returncode == 0
    committed = _git(root, "commit", "-qm", "release fixture")
    assert committed.returncode == 0, committed.stderr
    head = _git(root, "rev-parse", "HEAD")
    assert head.returncode == 0, head.stderr
    return head.stdout.strip()


def _run(
    source: Path,
    archive: Path,
    *,
    commit_sha: str | None = None,
    required_ancestors: tuple[str, ...] = (),
    trust_anchor_tag: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(PACKAGER),
        "--source-root",
        str(source),
        "--out",
        str(archive),
        "--verify",
    ]
    if commit_sha is not None:
        command.extend(("--commit-sha", commit_sha))
    for ancestor in required_ancestors:
        command.extend(("--required-ancestor", ancestor))
    if trust_anchor_tag is not None:
        command.extend(("--trust-anchor-tag", trust_anchor_tag))
    return subprocess.run(  # noqa: S603 - fixed argv, shell disabled
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _binding(archive: Path) -> dict[str, object]:
    with zipfile.ZipFile(archive) as opened:
        name = next(n for n in opened.namelist() if n.endswith("/release-binding.json"))
        loaded: dict[str, object] = json.loads(opened.read(name))
        return loaded


def _verify_archive(archive: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, shell disabled
        [sys.executable, str(PACKAGER), "--verify-archive", str(archive)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_release_source(root: Path) -> None:
    (root / "design-v1.25-runtime-go.md").write_text("canonical design\n", encoding="utf-8")
    (root / "MVP0-A_実装・検証報告.md").write_text("final report\n", encoding="utf-8")
    (root / "registry-snapshot.json").write_text(
        json.dumps({"registry_snapshot_hash": "sha256:" + "b" * 64}) + "\n",
        encoding="utf-8",
    )
    (root / "release-binding.json").write_text("obsolete binding\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "日本語.md").write_text("UTF-8 path\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "excluded.pyc").write_bytes(b"cache")
    # Top Level の除外Directory。Runtime の出力先であり配布しない。
    (root / "evidence").mkdir()
    (root / "evidence" / "runtime.json").write_text("{}\n", encoding="utf-8")
    # 除外語を途中に含む**正当な**Path。配布対象である。
    (root / "schemas" / "evidence" / "CaseEvidence").mkdir(parents=True)
    (root / "schemas" / "evidence" / "CaseEvidence" / "2.0.schema.json").write_text(
        "{}\n", encoding="utf-8"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_archive_is_byte_reproducible_when_input_mtime_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    head = _commit(source)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    initial = _run(source, first, commit_sha=head)
    assert initial.returncode == 0, initial.stdout + initial.stderr

    design = source / "design-v1.25-runtime-go.md"
    stat = design.stat()
    os.utime(design, (stat.st_atime + 86_400, stat.st_mtime + 86_400))
    repeated = _run(source, second, commit_sha=head)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert _sha256(first) == _sha256(second)


def test_archive_binds_report_design_registry_commit_and_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    head = _commit(source)
    archive_path = tmp_path / "release.zip"

    result = _run(source, archive_path, commit_sha=head)
    assert result.returncode == 0, result.stdout + result.stderr

    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        root = source.name
        binding = json.loads(archive.read(f"{root}/release-binding.json"))
        # 1.3 で trust_anchor 欄を足した。1.2 のZIPも検証はできる。
        assert binding["binding_version"] == "1.3"
        assert binding["implementation_commit_sha"] == head
        assert binding["source_tree_hash"].startswith("sha256:")
        assert binding["commit_ancestry"] == {
            "verified_ancestors": [],
            "verification_method": "git merge-base --is-ancestor",
            "external_trust_anchor_required": True,
            "note": (
                "ZIP omits Git objects; archive-only verification checks this record "
                "but cannot prove ancestry."
            ),
        }
        assert (
            binding["design"]["sha256"]
            == "sha256:"
            + hashlib.sha256((source / "design-v1.25-runtime-go.md").read_bytes()).hexdigest()
        )
        assert (
            binding["implementation_report"]["sha256"]
            == "sha256:"
            + hashlib.sha256((source / "MVP0-A_実装・検証報告.md").read_bytes()).hexdigest()
        )
        assert binding["registry_snapshot"]["registry_snapshot_hash"] == "sha256:" + "b" * 64
        assert names.count(f"{root}/release-binding.json") == 1
        assert f"{root}/__pycache__/excluded.pyc" not in names
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.external_attr == (0o100644 << 16)


def test_archive_records_a_verified_required_ancestor(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    head = _commit(source)
    archive_path = tmp_path / "release.zip"

    result = _run(source, archive_path, commit_sha=head, required_ancestors=(head,))
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(archive_path) as archive:
        binding = json.loads(archive.read(f"{source.name}/release-binding.json"))
    assert binding["commit_ancestry"]["verified_ancestors"] == [head]


def test_archive_rejects_required_ancestor_that_is_not_reachable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    head = _commit(source)

    result = _run(
        source,
        tmp_path / "release.zip",
        commit_sha=head,
        required_ancestors=("a" * 40,),
    )
    assert result.returncode != 0
    assert "not reachable from Git HEAD" in result.stderr


def test_archive_only_verification_does_not_require_a_git_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    head = _commit(source)
    archive_path = tmp_path / "release.zip"
    packaged = _run(source, archive_path, commit_sha=head)
    assert packaged.returncode == 0, packaged.stdout + packaged.stderr

    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(unpacked)
    shutil.rmtree(unpacked / source.name / ".git", ignore_errors=True)

    verified = _verify_archive(archive_path)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "verification=PASS" in verified.stdout
    assert f"entry_count={len(zipfile.ZipFile(archive_path).infolist())}" in verified.stdout


def test_archive_only_verification_rejects_tampered_commit_ancestry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    head = _commit(source)
    archive_path = tmp_path / "release.zip"
    packaged = _run(source, archive_path, commit_sha=head)
    assert packaged.returncode == 0, packaged.stdout + packaged.stderr

    with zipfile.ZipFile(archive_path) as archive:
        entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
    binding_name = f"{source.name}/release-binding.json"
    tampered_entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, content in entries:
        if info.filename == binding_name:
            binding = json.loads(content.decode("utf-8"))
            binding["commit_ancestry"]["verification_method"] = "untrusted"
            content = (
                json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
        tampered_entries.append((info, content))
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for info, content in tampered_entries:
            archive.writestr(info, content)

    verified = _verify_archive(archive_path)
    assert verified.returncode != 0
    assert "invalid ancestry verification method" in verified.stderr


def test_archive_only_verification_rejects_packaging_option_mix(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, shell disabled
        [
            sys.executable,
            str(PACKAGER),
            "--verify-archive",
            str(tmp_path / "release.zip"),
            "--source-root",
            str(tmp_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "cannot be combined" in result.stderr


def test_archive_rejects_supplied_commit_that_is_not_git_head(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    _commit(source)

    result = _run(source, tmp_path / "release.zip", commit_sha="a" * 40)

    assert result.returncode != 0
    assert "must exactly match the resolved Git HEAD" in result.stderr


def test_archive_rejects_dirty_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    head = _commit(source)
    (source / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

    result = _run(source, tmp_path / "release.zip", commit_sha=head)

    assert result.returncode != 0
    assert "requires a clean Git source tree" in result.stderr


# ---------------------------------------------------------------------------
# Trust Anchor（署名付きTag）の記録
#
# 記録するのは受領者への**案内**であり、証明ではない。ZIPはGit objectを
# 含まないので、署名があっても archive-only では祖先を証明できない。
# したがって `external_trust_anchor_required` は true のままにする。
# ここが false へ倒れると、ZIP単体で完結したかのような誤解を生む。
# ---------------------------------------------------------------------------


def _signing_key(tmp_path: Path) -> Path:
    keygen = shutil.which("ssh-keygen")
    assert keygen is not None, (
        "ssh-keygen が無い。署名Tagの記録を実測できないため、この試験を"
        "飛ばすとTrust Anchorの経路が未検証のまま残る（不変条件#16）。"
    )
    key = tmp_path / "keys" / "signing"
    key.parent.mkdir(parents=True, exist_ok=True)
    made = subprocess.run(  # noqa: S603 - fixed argv, shell disabled
        [keygen, "-t", "ed25519", "-f", str(key), "-N", "", "-C", "trial@invalid", "-q"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert made.returncode == 0, made.stderr
    return key


def _sign_tag(source: Path, key: Path, name: str) -> None:
    assert (
        _git(
            source,
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={key}.pub",
            "-c",
            "gpg.ssh.allowedSignersFile=ci/allowed-signers",
            "tag",
            "-s",
            name,
            "-m",
            name,
        ).returncode
        == 0
    )


def _release_source_with_signing(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    key = _signing_key(tmp_path)
    (source / "ci").mkdir()
    public = (key.with_suffix(".pub")).read_text(encoding="utf-8").strip()
    (source / "ci" / "allowed-signers").write_text(f"trial@invalid {public}\n", encoding="utf-8")
    # Bindingは検証器のHashも記録する。実物を置いて、記録される値が
    # 実在Fileから採られていることを確かめられるようにする。
    (source / "tools").mkdir()
    shutil.copy2(ROOT / "tools" / "verify_trust_anchor.py", source / "tools")
    head = _commit(source)
    return source, key, head


def test_binding_without_a_signed_tag_records_a_null_trust_anchor(tmp_path: Path) -> None:
    """Tagを渡さないときは `null` を書く。**欄ごと省かない。**

    省くと、受領者は「Anchorが無い」のか「古い生成器で欄そのものが無い」
    のかを区別できない。区別できない記録は判断材料にならない。
    """
    source, _key, _head = _release_source_with_signing(tmp_path)
    archive = tmp_path / "release.zip"

    assert _run(source, archive).returncode == 0
    binding = _binding(archive)

    assert binding["binding_version"] == "1.3"
    assert "trust_anchor" in binding
    assert binding["trust_anchor"] is None


def test_binding_records_a_verified_signed_tag(tmp_path: Path) -> None:
    source, key, _head = _release_source_with_signing(tmp_path)
    _sign_tag(source, key, "rel-1")
    archive = tmp_path / "release.zip"

    result = _run(source, archive, trust_anchor_tag="rel-1")
    assert result.returncode == 0, result.stderr

    anchor = _binding(archive)["trust_anchor"]
    assert isinstance(anchor, dict)
    assert anchor["tag"] == "rel-1"
    assert anchor["signer"] == "trial@invalid"
    assert anchor["allowed_signers_path"] == "ci/allowed-signers"
    # 署名者をメールアドレスだけで記録すると、鍵ローテーション後に
    # 同一人物・別鍵を区別できない。数年後の監査ではそこが要点になる。
    assert anchor["key_fingerprint"].startswith("SHA256:")
    assert anchor["key_type"]
    assert anchor["tag_object_id"]
    assert anchor["commit"] == _git(source, "rev-parse", "HEAD").stdout.strip()
    assert (
        anchor["allowed_signers_sha256"]
        == "sha256:" + hashlib.sha256((source / "ci" / "allowed-signers").read_bytes()).hexdigest()
    )
    assert anchor["verifier_path"] == "tools/verify_trust_anchor.py"
    assert (
        anchor["verifier_sha256"]
        == "sha256:"
        + hashlib.sha256((source / "tools" / "verify_trust_anchor.py").read_bytes()).hexdigest()
    )


def test_signed_tag_does_not_clear_the_external_trust_anchor_requirement(
    tmp_path: Path,
) -> None:
    """**署名があっても要件は下がらない。**

    ZIPはGit objectを含まない。署名付きTagは「誰が承認したか」を示すが、
    ZIPの中身だけで祖先を再証明できるようにはならない。ここを false へ
    倒すと、archive-only 検証がPASSしたことを「祖先まで確かめた」と
    読み違える余地ができる。
    """
    source, key, _head = _release_source_with_signing(tmp_path)
    _sign_tag(source, key, "rel-1")
    archive = tmp_path / "release.zip"
    assert _run(source, archive, trust_anchor_tag="rel-1").returncode == 0

    ancestry = _binding(archive)["commit_ancestry"]
    assert isinstance(ancestry, dict)
    assert ancestry["external_trust_anchor_required"] is True


def test_unsigned_tag_is_refused_as_a_trust_anchor(tmp_path: Path) -> None:
    source, _key, _head = _release_source_with_signing(tmp_path)
    assert _git(source, "tag", "-a", "plain", "-m", "unsigned").returncode == 0

    result = _run(source, tmp_path / "release.zip", trust_anchor_tag="plain")
    assert result.returncode != 0
    # 署名の見出しが無い時点で落とす。`git tag -v` を呼ぶ前に判る。
    assert "is not signed" in result.stderr


def test_trust_anchor_tag_must_point_at_the_release_commit(tmp_path: Path) -> None:
    """別のCommitを指すTagは、正しく署名されていても裏付けにならない。"""
    source, key, _head = _release_source_with_signing(tmp_path)
    _sign_tag(source, key, "rel-1")
    (source / "later.txt").write_text("later\n", encoding="utf-8")
    assert _git(source, "add", "-A").returncode == 0
    assert _git(source, "commit", "-qm", "second").returncode == 0

    result = _run(source, tmp_path / "release.zip", trust_anchor_tag="rel-1")
    assert result.returncode != 0
    assert "not the release commit" in result.stderr


def test_missing_trust_anchor_tag_is_refused(tmp_path: Path) -> None:
    source, _key, _head = _release_source_with_signing(tmp_path)
    result = _run(source, tmp_path / "release.zip", trust_anchor_tag="absent")
    assert result.returncode != 0
    assert "trust anchor tag not found" in result.stderr


def test_archive_only_verification_rejects_a_1_3_binding_without_the_field(
    tmp_path: Path,
) -> None:
    """1.3 を名乗りながら `trust_anchor` 欄を落としたZIPを通さない。

    欄を削るだけで「Anchorの有無を書かなくてよい」状態に戻せてしまうと、
    版数を上げた意味が無くなる。
    """
    source, key, _head = _release_source_with_signing(tmp_path)
    _sign_tag(source, key, "rel-1")
    archive = tmp_path / "release.zip"
    assert _run(source, archive, trust_anchor_tag="rel-1").returncode == 0

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive) as opened:
        items = [(info, opened.read(info.filename)) for info in opened.infolist()]
    with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as out:
        for info, data in items:
            if info.filename.endswith("/release-binding.json"):
                binding = json.loads(data)
                del binding["trust_anchor"]
                data = (
                    json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8")
            out.writestr(info, data)

    result = _verify_archive(tampered)
    assert result.returncode != 0
    assert "must carry a trust_anchor field" in result.stderr


def _downgrade_binding_to_1_2(archive: Path, out: Path) -> None:
    """1.2 相当（trust_anchor 欄なし）のZIPを作る。"""
    with zipfile.ZipFile(archive) as opened:
        items = [(info, opened.read(info.filename)) for info in opened.infolist()]
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as writer:
        for info, data in items:
            if info.filename.endswith("/release-binding.json"):
                binding = json.loads(data)
                binding["binding_version"] = "1.2"
                del binding["trust_anchor"]
                data = (
                    json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8")
            writer.writestr(info, data)


def test_legacy_1_2_binding_is_refused_by_default(tmp_path: Path) -> None:
    """**1.2 を既定で受け入れない。**

    1.2 には `trust_anchor` 欄が無い。既定で通すと「Anchorの有無を
    書いていない配布物」がRelease検証を通ってしまう。後方互換は
    必要だが、既定であってはならない。
    """
    source, key, _head = _release_source_with_signing(tmp_path)
    _sign_tag(source, key, "rel-1")
    archive = tmp_path / "release.zip"
    assert _run(source, archive, trust_anchor_tag="rel-1").returncode == 0

    legacy = tmp_path / "legacy.zip"
    _downgrade_binding_to_1_2(archive, legacy)

    result = _verify_archive(legacy)
    assert result.returncode != 0
    assert "--allow-legacy-binding" in result.stderr


def test_legacy_1_2_binding_is_accepted_in_the_explicit_mode(tmp_path: Path) -> None:
    """明示Modeでのみ通す。通ったことが出力に残る。

    「Legacyを許した検証」と「現行の検証」が同じ見た目のPASSだと、
    記録を読んだ側が区別できない。
    """
    source, key, _head = _release_source_with_signing(tmp_path)
    _sign_tag(source, key, "rel-1")
    archive = tmp_path / "release.zip"
    assert _run(source, archive, trust_anchor_tag="rel-1").returncode == 0

    legacy = tmp_path / "legacy.zip"
    _downgrade_binding_to_1_2(archive, legacy)

    result = subprocess.run(  # noqa: S603 - fixed argv, shell disabled
        [
            sys.executable,
            str(PACKAGER),
            "--verify-archive",
            str(legacy),
            "--allow-legacy-binding",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "binding_mode=LEGACY_ALLOWED" in result.stdout


def test_binding_does_not_depend_on_the_ci_environment(tmp_path: Path) -> None:
    """**同じCommitから、いつどこで作っても同じZIPが出る。**

    一時 `GITHUB_RUN_ID` をBindingへ記録していた。監査には有用に見えるが、
    同一Commitから作ったZIPが実行ごとに別Byte列になる。決定性は受領者が
    「そのCommitからそのZIPが出る」ことを確かめる土台なので、付加情報の
    ために崩してよいものではない。

    どの実行が作ったかは `trust-anchor.json` とCI Artifactに残る。
    Bindingは Commit の内容だけから決まる。
    """
    source, key, _head = _release_source_with_signing(tmp_path)
    _sign_tag(source, key, "rel-1")

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    def build(target: Path, environment: dict[str, str]) -> None:
        command = [
            sys.executable,
            str(PACKAGER),
            "--source-root",
            str(source),
            "--out",
            str(target),
            "--trust-anchor-tag",
            "rel-1",
            "--verify",
        ]
        result = subprocess.run(  # noqa: S603 - fixed argv, shell disabled
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, **environment},
        )
        assert result.returncode == 0, result.stderr

    build(first, {"GITHUB_RUN_ID": "111", "GITHUB_SHA": "a" * 40})
    build(second, {"GITHUB_RUN_ID": "999", "GITHUB_SHA": "b" * 40})

    assert _sha256(first) == _sha256(second), (
        "CIの環境変数でZIPが変わっている。同一Commitの再現性が崩れる"
    )
    anchor = _binding(first)["trust_anchor"]
    assert isinstance(anchor, dict)
    assert "workflow_run_id" not in anchor
    assert "workflow_commit_sha" not in anchor


def test_gpg_signed_trust_anchor_tag_is_refused(tmp_path: Path) -> None:
    """GPG署名Tagを Trust Anchor として受け付けない。

    `gpg.ssh.allowedSignersFile` はGPGに効かない。両方を受けると、
    片方だけ信頼制御が無い状態になる。
    """
    source, _key, _head = _release_source_with_signing(tmp_path)
    assert _git(source, "tag", "-a", "gpgish", "-m", "body").returncode == 0
    raw = _git(source, "cat-file", "tag", "gpgish").stdout
    forged = raw + "-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n"
    assert GIT is not None
    written = subprocess.run(  # noqa: S603 - fixed argv, shell disabled
        [GIT, "-C", str(source), "hash-object", "-t", "tag", "-w", "--stdin"],
        input=forged,
        text=True,
        capture_output=True,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    _git(source, "update-ref", "refs/tags/gpgish", written.stdout.strip())

    result = _run(source, tmp_path / "release.zip", trust_anchor_tag="gpgish")
    assert result.returncode != 0
    assert "only SSH signatures are accepted" in result.stderr


def test_excluded_names_match_at_top_level_only(tmp_path: Path) -> None:
    """除外語を途中に含む正当なPathを撥ねない。

    `evidence/` は Runtime の出力先として Top Level で除外する。深さを問わず
    一致させると `schemas/evidence/` の Evidence Schema まで落ちる。詰める側と
    検証側で規則がずれていると、詰めたものを自分で撥ねる配布物ができる。
    """
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    commit = _commit(source)
    archive = tmp_path / "release.zip"

    result = _run(source, archive, commit_sha=commit)
    assert result.returncode == 0, result.stderr

    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()

    assert any(n.endswith("/schemas/evidence/CaseEvidence/2.0.schema.json") for n in names), names
    # Top Level の除外は効いたままであること。
    assert not any("/evidence/runtime.json" in n for n in names), names
    assert not any("__pycache__" in n for n in names), names

    verified = _verify_archive(archive)
    assert verified.returncode == 0, verified.stderr


def test_archive_excludes_ignored_local_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    (source / ".gitignore").write_text(".env\n")
    head = _commit(source)
    (source / ".env").write_text("SYNTHETIC_LOCAL_ONLY=not-for-distribution\n")
    assert not _git(source, "status", "--porcelain").stdout
    archive = tmp_path / "release.zip"
    result = _run(source, archive, commit_sha=head)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(archive) as opened:
        assert "source/.env" not in opened.namelist()
        assert all(b"SYNTHETIC_LOCAL_ONLY" not in opened.read(n) for n in opened.namelist())


def test_archive_uses_commit_bytes_when_git_ignores_worktree_updates(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    head = _commit(source)
    first = tmp_path / "first.zip"
    result = _run(source, first, commit_sha=head)
    assert result.returncode == 0, result.stderr
    for name in ("design-v1.25-runtime-go.md", "registry-snapshot.json", "docs/日本語.md"):
        assert _git(source, "update-index", "--assume-unchanged", name).returncode == 0
        (source / name).write_text("LOCAL_UNCOMMITTED_BYTES\n")
    assert not _git(source, "status", "--porcelain").stdout
    second = tmp_path / "second.zip"
    result = _run(source, second, commit_sha=head)
    assert result.returncode == 0, result.stderr
    assert first.read_bytes() == second.read_bytes()


def test_archive_rejects_committed_symlink_without_reading_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    outside = tmp_path / "private-local.txt"
    outside.write_text("SYNTHETIC_PRIVATE_LOCAL\n")
    (source / "linked.txt").symlink_to(outside)
    head = _commit(source)
    archive = tmp_path / "release.zip"
    result = _run(source, archive, commit_sha=head)
    assert result.returncode != 0
    assert "non-regular Git entry" in result.stderr
    assert not archive.exists()
    assert "SYNTHETIC_PRIVATE_LOCAL" not in result.stderr + result.stdout


def test_archive_rejects_committed_submodule(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_release_source(source)
    initial = _commit(source)
    (source / "vendor").mkdir()
    assert (
        _git(source, "update-index", "--add", "--cacheinfo", f"160000,{initial},vendor").returncode
        == 0
    )
    assert _git(source, "commit", "-qm", "synthetic gitlink").returncode == 0
    head = _git(source, "rev-parse", "HEAD").stdout.strip()
    archive = tmp_path / "release.zip"
    result = _run(source, archive, commit_sha=head)
    assert result.returncode != 0
    assert "non-regular Git entry" in result.stderr
    assert not archive.exists()


def test_trust_anchor_uses_committed_signers_when_worktree_bytes_differ(tmp_path: Path) -> None:
    source, key, head = _release_source_with_signing(tmp_path)
    _sign_tag(source, key, "release-test")
    first = tmp_path / "first.zip"
    result = _run(source, first, commit_sha=head, trust_anchor_tag="release-test")
    assert result.returncode == 0, result.stderr
    assert _git(source, "update-index", "--assume-unchanged", "ci/allowed-signers").returncode == 0
    (source / "ci/allowed-signers").write_text("LOCAL_UNTRUSTED_SIGNER\n")
    second = tmp_path / "second.zip"
    result = _run(source, second, commit_sha=head, trust_anchor_tag="release-test")
    assert result.returncode == 0, result.stderr
    assert first.read_bytes() == second.read_bytes()
