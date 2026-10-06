#!/usr/bin/env python3
"""MVP0-A配布ZIPを決定的に生成し、Git来歴を含むRelease Bindingを同梱する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Final

EXCLUDED_TOP_LEVEL: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "evidence",
        "release",
        "slides",
    }
)
EXCLUDED_NAMES: Final[frozenset[str]] = frozenset({"__pycache__", ".coverage"})
REQUIRED_ASCII_DESIGN: Final[str] = "design-v1.25-runtime-go.md"
REPORT_PATH: Final[str] = "MVP0-A_実装・検証報告.md"
REGISTRY_SNAPSHOT_PATH: Final[str] = "registry-snapshot.json"
BINDING_PATH: Final[str] = "release-binding.json"
# 受領者が署名Tagを検証するのに要る。ZIPへ必ず入る位置にある。
ALLOWED_SIGNERS_PATH: Final[str] = "ci/allowed-signers"
TRUST_ANCHOR_VERIFIER_PATH: Final[str] = "tools/verify_trust_anchor.py"
FIXED_ZIP_DATETIME: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)
COMMIT_SHA: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")


def _archive_entry_is_excluded(name: str) -> bool:
    """Archive内Pathが除外対象か。`should_include` の規則をArchive側で読み直す。

    Archive名は `<root>/<relative...>` である。Top Level 除外は `<relative>` の
    先頭要素だけへ効く。深さを問わず一致させると、`schemas/evidence/` のように
    除外語を途中に含む正当なPathまで撥ねる。
    """
    parts = Path(name).parts
    if any(part in EXCLUDED_NAMES for part in parts):
        return True
    return len(parts) >= 2 and parts[1] in EXCLUDED_TOP_LEVEL


def should_include(relative: Path) -> bool:
    """配布対象を決定する。以前のBindingは新しいBindingで必ず置換する。"""
    if relative.as_posix() == BINDING_PATH:
        return False
    if relative.parts[0] in EXCLUDED_TOP_LEVEL:
        return False
    if any(part in EXCLUDED_NAMES for part in relative.parts):
        return False
    return not relative.name.endswith((".pyc", ".pyo"))


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        raise ValueError(
            "release packaging requires Git; unpacked release payloads are verification-only"
        )
    return git


def _git(source_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and arguments, shell disabled
        [_git_executable(), "-C", str(source_root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def resolve_commit_sha(source_root: Path, supplied: str | None) -> str:
    """実在するHEADと入力SHAを照合し、dirty treeをFail-Closedで拒否する。"""
    head_result = _git(source_root, "rev-parse", "--verify", "HEAD^{commit}")
    if head_result.returncode != 0:
        raise ValueError("release packaging requires a source tree with a resolvable Git HEAD")
    head = head_result.stdout.strip()
    if not COMMIT_SHA.fullmatch(head):
        raise ValueError("Git HEAD is not a 40-character commit SHA")
    if supplied is not None and supplied != head:
        raise ValueError("--commit-sha must exactly match the resolved Git HEAD")

    status = _git(source_root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        raise ValueError("cannot determine Git source tree cleanliness")
    if status.stdout:
        raise ValueError("release packaging requires a clean Git source tree")
    return head


def verify_required_ancestors(
    source_root: Path, head: str, required_ancestors: Iterable[str]
) -> tuple[str, ...]:
    """指定された修正commitがRelease HEADへ到達することをGit実体で確認する。"""
    supplied = tuple(required_ancestors)
    ancestors = tuple(sorted(set(supplied)))
    if len(ancestors) != len(supplied):
        raise ValueError("--required-ancestor must not contain duplicates")
    for ancestor in ancestors:
        if not COMMIT_SHA.fullmatch(ancestor):
            raise ValueError("--required-ancestor must be a 40-character commit SHA")
        result = _git(source_root, "merge-base", "--is-ancestor", ancestor, head)
        if result.returncode != 0:
            raise ValueError(f"required ancestor is not reachable from Git HEAD: {ancestor}")
    return ancestors


def _source_tree_hash(entries: Iterable[tuple[str, bytes]]) -> str:
    """ZIPへ収録する全ソースファイルを決定的manifestとしてHash化する。"""
    manifest = [
        {"path": path, "sha256": sha256_bytes(content)} for path, content in sorted(entries)
    ]
    canonical = json.dumps(
        {"source_tree_hash_version": "1.0", "files": manifest},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(canonical)


def resolve_trust_anchor(
    source_root: Path, tag: str | None, commit_sha: str, sources: dict[str, bytes]
) -> dict[str, str]:
    """署名付きTagを Trust Anchor として記録できる形にする。

    Tagが与えられなければ空を返す。**署名の有無で `external_trust_anchor_required`
    を切り替えない。** ZIPはGit objectを含まないので、署名があっても
    archive-only では祖先を証明できない。変わるのは「証明できる」ことでは
    なく「どこを見れば証明できるか」が分かることである。

    ここで記録するのは受領者への案内であり、証明そのものではない。
    証明は受領者が bundle と `ci/allowed-signers` で行う。

    ## 何を残すか

    署名者をメールアドレスだけで記録すると、鍵をローテーションした後に
    **同一人物・別鍵**を区別できない。数年後の監査では、当時どの鍵で
    署名されたかが要点になる。Fingerprint・Tag object ID・許可鍵Fileの
    Hash・検証器のHashまで残す。

    ## 実行ごとに変わる値は入れない

    一時 `GITHUB_RUN_ID` を環境変数から読んで記録していたが、これは
    **同一Commitから作ったZIPが実行ごとに別Byte列になる**ことを意味する。
    決定性（`test_archive_is_byte_reproducible_*`）は受領者が
    「同じCommitから同じZIPが出る」ことを確かめる土台なので、監査用の
    付加情報のために崩してよいものではない。

    どの実行が作ったかは、Bindingではなく `trust-anchor.json` と
    CI Artifact 側に残る。Bindingは**Commitの内容だけ**から決まる。
    """
    if tag is None:
        return {}

    tag_object = _git(source_root, "rev-parse", "--verify", f"refs/tags/{tag}")
    if tag_object.returncode != 0:
        raise ValueError(f"trust anchor tag not found: {tag}")

    kind = _git(source_root, "cat-file", "-t", f"refs/tags/{tag}")
    if kind.stdout.strip() != "tag":
        raise ValueError(f"trust anchor tag {tag} is lightweight and carries no signature")

    pointed = _git(source_root, "rev-list", "-n", "1", f"refs/tags/{tag}")
    if pointed.returncode != 0:
        raise ValueError(f"cannot resolve the commit behind {tag}")
    if pointed.stdout.strip() != commit_sha:
        raise ValueError(
            f"trust anchor tag {tag} points at {pointed.stdout.strip()}, "
            f"not the release commit {commit_sha}"
        )

    if ALLOWED_SIGNERS_PATH not in sources:
        raise ValueError(f"trust anchor requires {ALLOWED_SIGNERS_PATH} to be present")

    # SSH署名だけを受ける。`gpg.ssh.allowedSignersFile` はGPGに効かないため、
    # GPGを通すとローカルkeyringの任意の鍵で検証が成立しうる。
    raw = _git(source_root, "cat-file", "tag", tag)
    if raw.returncode != 0:
        raise ValueError(f"cannot read tag object for {tag}")
    if "-----BEGIN PGP SIGNATURE-----" in raw.stdout:
        raise ValueError(f"trust anchor tag {tag} is GPG-signed; only SSH signatures are accepted")
    if "-----BEGIN SSH SIGNATURE-----" not in raw.stdout:
        raise ValueError(f"trust anchor tag {tag} is not signed")

    # 検証鍵も収録するCommitのBytesだけを使う。作業木の同名Fileを信用しない。
    with tempfile.TemporaryDirectory(prefix="release-signers-") as directory:
        allowed = Path(directory) / "allowed-signers"
        allowed.write_bytes(sources[ALLOWED_SIGNERS_PATH])
        verified = _git(
            source_root,
            "-c",
            f"gpg.ssh.allowedSignersFile={allowed}",
            "tag",
            "-v",
            tag,
        )
    output = verified.stdout + verified.stderr
    if verified.returncode != 0:
        raise ValueError(f"trust anchor tag {tag} did not verify: {output.strip()}")

    match = re.search(r'Good "git" signature for (\S+) with (\S+) key (SHA256:\S+)', output)
    if match is None:
        raise ValueError(f"cannot determine the signer of {tag}: {output.strip()}")

    anchor = {
        "tag": tag,
        "tag_object_id": tag_object.stdout.strip(),
        "commit": commit_sha,
        "signer": match.group(1),
        "key_type": match.group(2),
        "key_fingerprint": match.group(3),
        "verification_method": "git tag -v (ssh)",
        "allowed_signers_path": ALLOWED_SIGNERS_PATH,
        "allowed_signers_sha256": sha256_bytes(sources[ALLOWED_SIGNERS_PATH]),
        "verifier_path": TRUST_ANCHOR_VERIFIER_PATH,
        "verifier_sha256": sha256_bytes(sources[TRUST_ANCHOR_VERIFIER_PATH]),
    }
    return anchor


def _release_binding(
    sources: dict[str, bytes],
    commit_sha: str,
    source_tree_hash: str,
    required_ancestors: tuple[str, ...],
    trust_anchor: dict[str, str] | None = None,
) -> bytes:
    for required in (REQUIRED_ASCII_DESIGN, REPORT_PATH, REGISTRY_SNAPSHOT_PATH):
        if required not in sources:
            raise ValueError(f"required committed release source missing: {required}")
    try:
        snapshot = json.loads(sources[REGISTRY_SNAPSHOT_PATH])
        registry_snapshot_hash = snapshot["registry_snapshot_hash"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid registry snapshot for release binding") from exc
    if not isinstance(registry_snapshot_hash, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", registry_snapshot_hash
    ):
        raise ValueError("registry_snapshot_hash is missing or invalid")

    binding = {
        # 1.3 で trust_anchor を追加した。1.2 のZIPも検証できる形にしてある
        # （既に配布済みのものを検証不能にしない）。
        "binding_version": "1.3",
        "implementation_commit_sha": commit_sha,
        "source_tree_hash": source_tree_hash,
        "commit_ancestry": {
            "verified_ancestors": list(required_ancestors),
            "verification_method": "git merge-base --is-ancestor",
            # 署名付きTagがあっても true のままにする。ZIPはGit objectを
            # 含まないので、archive-only では祖先を証明できない。
            # 変わるのは「証明できる」ことではなく、
            # 「どこを見れば証明できるか」が分かることである。
            "external_trust_anchor_required": True,
            "note": (
                "ZIP omits Git objects; archive-only verification checks this record "
                "but cannot prove ancestry."
            ),
        },
        # 署名付きTagが無ければ null。受領者は「Anchorが無い」ことを
        # 見分けられる必要がある。欄ごと省くと、古い生成器なのか
        # Anchorが無いのかが区別できない。
        "trust_anchor": trust_anchor or None,
        "design": {
            "path": REQUIRED_ASCII_DESIGN,
            "sha256": sha256_bytes(sources[REQUIRED_ASCII_DESIGN]),
        },
        "registry_snapshot": {
            "path": REGISTRY_SNAPSHOT_PATH,
            "registry_snapshot_hash": registry_snapshot_hash,
            "file_sha256": sha256_bytes(sources[REGISTRY_SNAPSHOT_PATH]),
        },
        "implementation_report": {
            "path": REPORT_PATH,
            "sha256": sha256_bytes(sources[REPORT_PATH]),
        },
    }
    return (json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _zip_info(archive_name: str) -> zipfile.ZipInfo:
    """ホストmtime・permission・圧縮実装差を持ち込まないEntry metadata。"""
    info = zipfile.ZipInfo(filename=archive_name, date_time=FIXED_ZIP_DATETIME)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_STORED
    info.flag_bits |= 0x800
    return info


def _source_entries(source_root: Path, commit_sha: str) -> list[tuple[str, bytes]]:
    """固定CommitのBlobだけを収録する。無視File・作業木の更新を持ち込まない。"""
    tree = _git(source_root, "ls-tree", "-r", "-z", "--full-tree", commit_sha)
    if tree.returncode != 0:
        raise ValueError("cannot enumerate committed release source")
    selected: list[tuple[str, str]] = []
    for record in tree.stdout.split("\0"):
        if not record:
            continue
        metadata, name = record.split("\t", 1)
        mode, kind, oid = metadata.split()
        if not should_include(Path(name)):
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("release source contains a non-regular Git entry")
        if not COMMIT_SHA.fullmatch(oid):
            raise ValueError("invalid committed source object ID")
        selected.append((name, oid))
    objects = subprocess.run(  # noqa: S603 - Git Blob IDs from a fixed Commit, no shell
        [_git_executable(), "-C", str(source_root), "cat-file", "--batch"],
        input="".join(oid + "\n" for _, oid in selected).encode("ascii"),
        capture_output=True,
        check=False,
        timeout=60,
    )
    if objects.returncode != 0:
        raise ValueError("cannot read committed release source objects")
    entries: list[tuple[str, bytes]] = []
    offset = 0
    for name, oid in selected:
        end = objects.stdout.find(b"\n", offset)
        if end < 0:
            raise ValueError("truncated Git object response")
        header = objects.stdout[offset:end].split()
        if len(header) != 3 or header[:2] != [oid.encode("ascii"), b"blob"]:
            raise ValueError("unexpected Git object response")
        if not header[2].isdigit():
            raise ValueError("invalid Git object size")
        size = int(header[2])
        offset = end + 1
        content = objects.stdout[offset : offset + size]
        offset += size
        if len(content) != size or objects.stdout[offset : offset + 1] != b"\n":
            raise ValueError("truncated Git object content")
        offset += 1
        entries.append((name, content))
    if offset != len(objects.stdout):
        raise ValueError("unexpected trailing Git object content")
    return sorted(entries)


def create_archive(
    source_root: Path,
    out: Path,
    *,
    commit_sha: str | None = None,
    required_ancestors: Iterable[str] = (),
    trust_anchor_tag: str | None = None,
) -> list[str]:
    source_root = source_root.resolve()
    resolved_commit_sha = resolve_commit_sha(source_root, commit_sha)
    verified_ancestors = verify_required_ancestors(
        source_root, resolved_commit_sha, required_ancestors
    )
    source_entries = _source_entries(source_root, resolved_commit_sha)
    sources = dict(source_entries)
    trust_anchor = resolve_trust_anchor(source_root, trust_anchor_tag, resolved_commit_sha, sources)
    source_tree_hash = _source_tree_hash(source_entries)
    out.parent.mkdir(parents=True, exist_ok=True)
    root_name = source_root.name
    entries = [(f"{root_name}/{relative}", content) for relative, content in source_entries]
    entries.append(
        (
            f"{root_name}/{BINDING_PATH}",
            _release_binding(
                sources,
                resolved_commit_sha,
                source_tree_hash,
                verified_ancestors,
                trust_anchor,
            ),
        )
    )
    entries.sort(key=lambda entry: entry[0])

    with zipfile.ZipFile(
        out, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True
    ) as archive:
        for archive_name, content in entries:
            archive.writestr(_zip_info(archive_name), content)
    return [name for name, _content in entries]


def verify_archive(out: Path, *, allow_legacy: bool = False) -> None:
    """配布ZIPを検証する。

    `allow_legacy` は `binding_version: 1.2` を許す明示Modeである。
    1.2 には `trust_anchor` 欄が無いので、既定で受け入れると
    「Anchorの有無を書いていない配布物」がRelease検証を通ってしまう。
    後方互換は必要だが、既定であってはならない。
    """
    with zipfile.ZipFile(out) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("archive contains duplicate paths")
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise ValueError("archive contains a traversal path")
        if not any(name.endswith("/" + REQUIRED_ASCII_DESIGN) for name in names):
            raise ValueError("archive missing ASCII design")
        if not any(name.endswith("/" + BINDING_PATH) for name in names):
            raise ValueError("archive missing release binding")
        # `should_include` と同じ規則で見る。片方だけが深さを問わず一致すると、
        # 詰める側は入れたのに検証側が撥ねる。`schemas/evidence/` がこれで落ちた。
        # `EXCLUDED_TOP_LEVEL` は Top Level だけ、`EXCLUDED_NAMES` は全深さである。
        forbidden = [name for name in names if _archive_entry_is_excluded(name)]
        if forbidden:
            raise ValueError(f"archive includes excluded generated content: {forbidden[:3]}")
        non_utf8 = [
            info.filename
            for info in infos
            if not info.filename.isascii() and not (info.flag_bits & 0x800)
        ]
        if non_utf8:
            raise ValueError(f"non-ASCII ZIP paths without UTF-8 flag: {non_utf8[:3]}")
        unstable_metadata = [
            info.filename
            for info in infos
            if info.date_time != FIXED_ZIP_DATETIME
            or info.compress_type != zipfile.ZIP_STORED
            or info.external_attr != (0o100644 << 16)
        ]
        if unstable_metadata:
            raise ValueError(f"archive metadata is not deterministic: {unstable_metadata[:3]}")

        binding_name = next(name for name in names if name.endswith("/" + BINDING_PATH))
        root_name = binding_name.removesuffix("/" + BINDING_PATH)
        try:
            binding = json.loads(archive.read(binding_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid release binding: {exc}") from exc
        binding_version = binding.get("binding_version")
        accepted = {"1.2", "1.3"} if allow_legacy else {"1.3"}
        if binding_version not in accepted:
            if binding_version == "1.2":
                # 1.2 には trust_anchor 欄が無い。既定で受け入れると、
                # Anchorの有無を書いていない配布物がRelease検証を通る。
                raise ValueError(
                    "release binding 1.2 predates the trust_anchor field; "
                    "pass --allow-legacy-binding to verify it as a legacy archive"
                )
            raise ValueError("release binding has unsupported binding_version")
        if not COMMIT_SHA.fullmatch(str(binding.get("implementation_commit_sha", ""))):
            raise ValueError("release binding has invalid implementation_commit_sha")
        ancestry = binding.get("commit_ancestry")
        if not isinstance(ancestry, dict):
            raise ValueError("release binding has no commit_ancestry")
        ancestors = ancestry.get("verified_ancestors")
        if not isinstance(ancestors, list) or len(ancestors) != len(set(ancestors)):
            raise ValueError("release binding has invalid verified_ancestors")
        if not all(
            isinstance(ancestor, str) and COMMIT_SHA.fullmatch(ancestor) for ancestor in ancestors
        ):
            raise ValueError("release binding has invalid verified ancestor SHA")
        if ancestry.get("verification_method") != "git merge-base --is-ancestor":
            raise ValueError("release binding has invalid ancestry verification method")
        if ancestry.get("external_trust_anchor_required") is not True:
            raise ValueError("release binding must require an external trust anchor")
        if binding_version == "1.3":
            # 1.3 は trust_anchor 欄を**必ず持つ**。null でもよいが、
            # 欄ごと無いのは許さない。無いと「Anchorが無い」のか
            # 「古い生成器で欄そのものが無い」のかを区別できない。
            if "trust_anchor" not in binding:
                raise ValueError("release binding 1.3 must carry a trust_anchor field")
            anchor = binding["trust_anchor"]
            if anchor is not None:
                if not isinstance(anchor, dict):
                    raise ValueError("release binding trust_anchor must be an object or null")
                missing = [
                    key
                    for key in ("tag", "signer", "verification_method", "allowed_signers_path")
                    if not isinstance(anchor.get(key), str) or not anchor[key]
                ]
                if missing:
                    raise ValueError(f"release binding trust_anchor is incomplete: {missing}")
                # 受領者が検証に使うFileが、ZIPの中に実在すること。
                # Pathだけ書いてあって中身が無いと、案内として成立しない。
                if f"{root_name}/{anchor['allowed_signers_path']}" not in names:
                    raise ValueError(
                        "release binding trust_anchor points at "
                        f"{anchor['allowed_signers_path']}, which is not in the archive"
                    )
        expected_tree_hash = binding.get("source_tree_hash")
        if not isinstance(expected_tree_hash, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", expected_tree_hash
        ):
            raise ValueError("release binding has invalid source_tree_hash")
        _verify_binding_file(archive, root_name, binding, "design", "sha256")
        _verify_binding_file(archive, root_name, binding, "implementation_report", "sha256")
        registry = binding.get("registry_snapshot")
        if not isinstance(registry, dict):
            raise ValueError("release binding has no registry snapshot")
        _verify_binding_file(archive, root_name, binding, "registry_snapshot", "file_sha256")
        registry_path = registry.get("path")
        if not isinstance(registry_path, str):
            raise ValueError("release binding has invalid registry snapshot path")
        try:
            snapshot = json.loads(archive.read(f"{root_name}/{registry_path}").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"archive registry snapshot cannot be read: {exc}") from exc
        if snapshot.get("registry_snapshot_hash") != registry.get("registry_snapshot_hash"):
            raise ValueError("release binding registry_snapshot_hash mismatch")

        source_entries = [
            (name.removeprefix(root_name + "/"), archive.read(name))
            for name in names
            if name != binding_name
        ]
        if _source_tree_hash(source_entries) != expected_tree_hash:
            raise ValueError("release binding source_tree_hash mismatch")


def _verify_binding_file(
    archive: zipfile.ZipFile, root_name: str, binding: dict[str, object], key: str, hash_key: str
) -> None:
    record = binding.get(key)
    if not isinstance(record, dict):
        raise ValueError(f"release binding has no {key}")
    relative_path = record.get("path")
    expected_hash = record.get(hash_key)
    if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
        raise ValueError(f"release binding has invalid {key}")
    try:
        actual_hash = sha256_bytes(archive.read(f"{root_name}/{relative_path}"))
    except KeyError as exc:
        raise ValueError(f"archive missing bound file for {key}: {relative_path}") from exc
    if actual_hash != expected_hash:
        raise ValueError(f"release binding hash mismatch for {key}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--commit-sha")
    parser.add_argument(
        "--required-ancestor",
        action="append",
        default=[],
        metavar="SHA",
        help="Release HEADへ到達することをGit実体で検証し、Bindingへ記録する40桁SHA",
    )
    parser.add_argument(
        "--trust-anchor-tag",
        metavar="TAG",
        help=(
            "Release Commitを指す署名付きTag。署名を検証してBindingへ記録する。"
            "祖先の証明にはならない（ZIPはGit objectを含まない）が、"
            "受領者がどこを見れば検証できるかを示す"
        ),
    )
    parser.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--verify-archive",
        type=Path,
        metavar="ZIP",
        help="Gitやsource treeを必要とせず、既存配布ZIPの構造・Binding・Hashを検証する",
    )
    parser.add_argument(
        "--allow-legacy-binding",
        action="store_true",
        help=(
            "binding_version 1.2（trust_anchor欄が無い）を検証対象として許す。"
            "現行Release判定では使わない"
        ),
    )
    args = parser.parse_args(argv)

    if args.verify_archive is not None:
        if (
            any(
                value is not None
                for value in (args.source_root, args.out, args.commit_sha, args.trust_anchor_tag)
            )
            or args.required_ancestor
            or args.verify
        ):
            parser.error("--verify-archive cannot be combined with packaging options")
        verify_archive(args.verify_archive, allow_legacy=args.allow_legacy_binding)
        with zipfile.ZipFile(args.verify_archive) as archive:
            entry_count = len(archive.infolist())
        print(f"archive={args.verify_archive}")
        print(f"sha256={sha256(args.verify_archive)}")
        print(f"entry_count={entry_count}")
        if args.allow_legacy_binding:
            print("binding_mode=LEGACY_ALLOWED")
        print("verification=PASS")
        return 0

    if args.source_root is None or args.out is None:
        parser.error("--source-root and --out are required unless --verify-archive is used")
    names = create_archive(
        args.source_root,
        args.out,
        commit_sha=args.commit_sha,
        required_ancestors=args.required_ancestor,
        trust_anchor_tag=args.trust_anchor_tag,
    )
    if args.verify:
        verify_archive(args.out)
    print(f"archive={args.out}")
    print(f"sha256={sha256(args.out)}")
    print(f"entry_count={len(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
