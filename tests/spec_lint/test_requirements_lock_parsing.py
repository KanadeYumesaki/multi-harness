"""requirements.txt の解釈がCIと一致していることの静的検査。

CIの`license-allowlist`は`tools/check_licenses.py`を実行する。この解析が
壊れるとCIだけが落ち、ローカルのpytest／mypy／ruffは全て通る。実際に
Hash固定を導入した時点から、Push 3回分のCIが継続して落ちていた。

同じ解析器をここでも走らせ、ローカルの試験で捕まるようにする。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_licenses  # noqa: E402
from check_licenses import parse_requirement_names  # noqa: E402

REQUIREMENTS = REPO_ROOT / "requirements.txt"
DEV_REQUIREMENTS = REPO_ROOT / "requirements-dev.txt"
EXCEPTIONS = REPO_ROOT / "ci" / "license-exceptions.yaml"


@pytest.fixture(scope="module")
def names() -> list[str]:
    return parse_requirement_names(REQUIREMENTS.read_text(encoding="utf-8"))


def test_no_option_line_is_taken_as_a_package_name(names: list[str]) -> None:
    """`--hash=...` を名前として拾わない。CIが落ちていた直接の原因。"""
    assert names
    offenders = [name for name in names if name.startswith("-")]
    assert not offenders, f"Option行をPackage名として拾っている: {offenders}"


def test_every_name_resolves_to_an_installed_distribution(names: list[str]) -> None:
    """CIと同じ経路で解決できることを確かめる。

    `check_licenses.py` は `metadata.distribution(name)` を呼ぶ。ここで
    解決できない名前があれば、CIでは`PackageNotFoundError`になる。
    """
    from importlib import metadata

    missing = []
    for name in names:
        try:
            metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
    assert not missing, f"解決できないDistribution: {missing}"


def test_platform_specific_dependency_does_not_crash_the_checker() -> None:
    """回帰対象（2026-08-15検出・修正済み）: 環境Markerで検査器が落ちていた。

    `colorama==0.4.6 ; sys_platform == 'win32'` はLinuxへ入らない。
    Markerを読み飛ばして名前だけ拾うと、入っていないDistributionの
    metadataを引きに行って `PackageNotFoundError` で**検査全体が止まる**。

    止まった位置より後ろは一度も検査されない。実際この不具合のせいで、
    後ろにあった GPLv3+ の `rfc3987` へ到達したことが一度も無かった。
    検査器が落ちることは「違反が無い」ことではない。
    """
    text = "colorama==0.4.6 ; sys_platform == 'win32'\nattrs==26.1.0\n"
    entries = check_licenses.parse_requirements(text)
    assert [entry.name for entry in entries] == ["colorama", "attrs"]
    assert entries[0].marker == "sys_platform == 'win32'"
    assert entries[1].marker == ""

    assert check_licenses.applies_here("sys_platform == 'win32'") is (sys.platform == "win32")
    assert check_licenses.applies_here("") is True


def test_unparseable_marker_is_treated_as_applicable() -> None:
    """読めないMarkerを「関係ない」と決めつけない。

    決めつけると、Markerを1文字壊すだけで検査対象から静かに外れる。
    """
    assert check_licenses.applies_here("this is not a marker") is True


def test_hash_pinned_entries_are_still_counted(names: list[str]) -> None:
    """継続行を除いた結果、Package自体まで消えていないこと。

    Option行を落とす処理が広すぎると、検査対象が0件でも成功してしまう。
    """
    declared = sum(
        1
        for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.strip().startswith("#") and not raw.strip().startswith("-")
    )
    assert len(names) == declared
    assert len(names) >= 2  # 直接依存の jsonschema と PyYAML は必ずある


def test_direct_dependencies_are_present(names: list[str]) -> None:
    lowered = {name.lower() for name in names}
    assert "jsonschema" in lowered
    assert "pyyaml" in lowered


def test_dev_and_sbom_lock_has_hash_for_every_resolved_distribution() -> None:
    """CIが導入する開発・SBOM依存を、未Hashの追加installへ戻さない。"""
    lines = DEV_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    pins = [
        index
        for index, raw in enumerate(lines)
        if raw.strip() and not raw.lstrip().startswith(("#", "-"))
    ]
    assert pins
    for position, start in enumerate(pins):
        end = pins[position + 1] if position + 1 < len(pins) else len(lines)
        declaration = lines[start].strip()
        window = lines[start + 1 : end]
        assert "==" in declaration, f"unresolved dev dependency: {declaration}"
        assert any("--hash=sha256:" in line for line in window), (
            f"hash missing for dev dependency: {declaration}"
        )


# ---------------------------------------------------------------------------
# Copyleft の例外登録
# ---------------------------------------------------------------------------


def test_runtime_lock_has_no_copyleft_dependency() -> None:
    """**実行時ロックにCopyleftを入れない。** ここは例外を認めない。

    実行時依存は import され、配布物の一部になる。Build時に実行するだけの
    Toolとは伝播の仕方が違う。
    """
    from importlib import metadata

    offenders = []
    for entry in check_licenses.parse_requirements(REQUIREMENTS.read_text(encoding="utf-8")):
        try:
            meta = metadata.distribution(entry.name).metadata
        except metadata.PackageNotFoundError:
            continue
        _, values = check_licenses.license_fields(meta)
        if any(check_licenses.DENIED.search(value) for value in values):
            offenders.append(entry.name)
    assert not offenders, f"実行時ロックにCopyleftがある: {offenders}"


def test_every_exception_carries_a_reason() -> None:
    """理由の無い例外を通さない。読込み時に落ちることを確かめる。"""
    entries = check_licenses.load_exceptions(EXCEPTIONS)
    assert entries, "例外登録が空。全て解消したなら本試験は消してよい"
    for package, row in entries.items():
        assert str(row.get("reason", "")).strip(), f"理由が無い: {package}"
        assert row["scope"] == "BUILD_TOOLING"


def test_exception_without_a_reason_is_rejected(tmp_path: Path) -> None:
    """理由欄を空にした登録は読込みで落ちる。"""
    import yaml

    path = tmp_path / "license-exceptions.yaml"
    path.write_text(
        yaml.safe_dump(
            {"exceptions": [{"package": "x", "scope": "BUILD_TOOLING", "reason": "  "}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="without a reason"):
        check_licenses.load_exceptions(path)


def test_runtime_scope_exception_is_rejected(tmp_path: Path) -> None:
    """`BUILD_TOOLING` 以外のScopeを受け付けない。

    Scopeを自由記述にすると、`RUNTIME` と書いた例外が通ってしまう。
    それは例外ではなく、方針の放棄である。
    """
    import yaml

    path = tmp_path / "license-exceptions.yaml"
    path.write_text(
        yaml.safe_dump(
            {"exceptions": [{"package": "x", "scope": "RUNTIME", "reason": "使いたいので"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="BUILD_TOOLING"):
        check_licenses.load_exceptions(path)


def test_exception_is_void_once_the_package_enters_the_runtime_lock(tmp_path: Path) -> None:
    """**例外の成立条件を機械が確かめる。**

    「Build時だけのつもり」が「いつの間にか配布物へ入っていた」に変わる
    のを、宣言ではなく構造で防ぐ。実行時ロックへ入った瞬間、例外は
    無効になって検査が落ちなければならない。
    """
    exceptions = tmp_path / "exceptions.yaml"
    exceptions.write_text(EXCEPTIONS.read_text(encoding="utf-8"), encoding="utf-8")
    # rfc3987 を実行時ロックへ紛れ込ませた状態を作る
    runtime = tmp_path / "requirements.txt"
    runtime.write_text("rfc3987==1.3.8\n", encoding="utf-8")
    dev = tmp_path / "requirements-dev.txt"
    dev.write_text("rfc3987==1.3.8\n", encoding="utf-8")

    code = check_licenses.main(
        [
            "--requirements",
            str(dev),
            "--exceptions",
            str(exceptions),
            "--runtime-lock",
            str(runtime),
        ]
    )
    assert code == 1, "実行時ロックへ入っても例外が通ってしまっている"


def test_exception_applies_when_the_package_stays_out_of_the_runtime_lock(
    tmp_path: Path,
) -> None:
    """例外が正しく効く側も確かめる。常に落ちる実装でも上の試験は通る。"""
    exceptions = tmp_path / "exceptions.yaml"
    exceptions.write_text(EXCEPTIONS.read_text(encoding="utf-8"), encoding="utf-8")
    runtime = tmp_path / "requirements.txt"
    runtime.write_text("attrs==26.1.0\n", encoding="utf-8")
    dev = tmp_path / "requirements-dev.txt"
    dev.write_text("rfc3987==1.3.8\n", encoding="utf-8")

    code = check_licenses.main(
        [
            "--requirements",
            str(dev),
            "--exceptions",
            str(exceptions),
            "--runtime-lock",
            str(runtime),
        ]
    )
    assert code == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("attrs==26.1.0 \\\n    --hash=sha256:abc \\\n    --hash=sha256:def", ["attrs"]),
        ("# comment only\n", []),
        ("PyYAML==6.0.2  # 行末コメント", ["PyYAML"]),
        ("foo>=1.0 ; python_version < '3.12'", ["foo"]),
        ("bar[extra]==2.0", ["bar"]),
        ("-r other.txt\n--index-url https://example.invalid\nbaz==1", ["baz"]),
    ],
    ids=["hash_pinned", "comment", "trailing_comment", "marker", "extra", "options"],
)
def test_parser_shapes(text: str, expected: list[str]) -> None:
    assert parse_requirement_names(text) == expected


@pytest.mark.parametrize(
    ("expression", "legacy", "classifiers", "version", "expected"),
    [
        ("GPL-3.0-only", None, ["Programming Language :: Python"], "1.0", 1),
        ("AGPL-3.0-or-later", None, [], "1.0", 1),
        ("MIT OR GPL-2.0-only", None, [], "1.0", 1),
        ("not a valid SPDX expression", "MIT", [], "1.0", 1),
        (None, None, ["Programming Language :: Python"], "1.0", 1),
        (None, "UNKNOWN", ["License :: OSI Approved"], "1.0", 1),
        (None, "GPLv3+", [], "1.0", 1),
        (None, "GNU General Public License", [], "1.0", 1),
        (None, "GNU Affero General Public License", [], "1.0", 1),
        ("MIT", None, [], "2.0", 1),
        ("MIT", None, [], "1.0", 0),
        ("Apache-2.0 OR BSD-3-Clause", None, [], "1.0", 0),
        (None, "UNKNOWN", ["License :: OSI Approved :: MIT License"], "1.0", 0),
        (
            None,
            "LGPL",
            ["License :: OSI Approved :: GNU Lesser General Public License v2 or later (LGPLv2+)"],
            "1.0",
            0,
        ),
    ],
)
def test_metadata_identity_and_license_are_bound_to_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expression: str | None,
    legacy: str | None,
    classifiers: list[str],
    version: str,
    expected: int,
) -> None:
    from email.message import Message
    from types import SimpleNamespace

    meta = Message()
    meta["Name"] = "demo"
    meta["Version"] = version
    if expression is not None:
        meta["License-Expression"] = expression
    if legacy is not None:
        meta["License"] = legacy
    for value in classifiers:
        meta["Classifier"] = value
    monkeypatch.setattr(
        check_licenses.metadata,
        "distribution",
        lambda _: SimpleNamespace(metadata=meta, version=version),
    )
    lock = tmp_path / "req.txt"
    lock.write_text("demo==1.0\n", encoding="utf-8")
    exceptions = tmp_path / "exceptions.yaml"
    exceptions.write_text("exceptions: []\n", encoding="utf-8")
    assert (
        check_licenses.main(
            [
                "--requirements",
                str(lock),
                "--runtime-lock",
                str(lock),
                "--exceptions",
                str(exceptions),
            ]
        )
        == expected
    )


def test_runtime_license_exception_cannot_use_name_spelling_to_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from email.message import Message
    from types import SimpleNamespace

    meta = Message()
    meta["Name"] = "Demo_Pkg"
    meta["License-Expression"] = "GPL-3.0-only"
    monkeypatch.setattr(
        check_licenses.metadata,
        "distribution",
        lambda _: SimpleNamespace(metadata=meta, version="1.0"),
    )
    dev = tmp_path / "dev.txt"
    dev.write_text("Demo_Pkg==1.0\n", encoding="utf-8")
    runtime = tmp_path / "runtime.txt"
    runtime.write_text("demo-pkg==1.0\n", encoding="utf-8")
    exceptions = tmp_path / "exceptions.yaml"
    exceptions.write_text(
        "exceptions:\n - package: DEMO.pkg\n   scope: BUILD_TOOLING\n   reason: fixture\n",
        encoding="utf-8",
    )
    assert (
        check_licenses.main(
            [
                "--requirements",
                str(dev),
                "--runtime-lock",
                str(runtime),
                "--exceptions",
                str(exceptions),
            ]
        )
        == 1
    )
    runtime.write_text("other==1.0\n", encoding="utf-8")
    assert (
        check_licenses.main(
            [
                "--requirements",
                str(dev),
                "--runtime-lock",
                str(runtime),
                "--exceptions",
                str(exceptions),
            ]
        )
        == 0
    )
    runtime.unlink()
    assert (
        check_licenses.main(
            [
                "--requirements",
                str(dev),
                "--runtime-lock",
                str(runtime),
                "--exceptions",
                str(exceptions),
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "demo>=1",
        "demo==1.*",
        "demo @ https://example.invalid/demo.whl",
        "-r other.txt\ndemo==1",
        "demo==1 ; broken marker",
        "demo==1\nDEMO==2",
        "demo==1\n --hash=sha256:abc",
    ],
)
def test_audit_lock_rejects_ambiguous_or_uncovered_input(text: str) -> None:
    with pytest.raises(ValueError):
        check_licenses.locked_requirements(text)


def test_license_expression_duplicates_are_not_silently_selected() -> None:
    from email.message import Message

    meta = Message()
    meta["License-Expression"] = "MIT"
    meta["License-Expression"] = "GPL-3.0-only"
    with pytest.raises(ValueError, match="multiple"):
        check_licenses.license_fields(meta)


def test_duplicate_exception_aliases_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "exceptions.yaml"
    path.write_text(
        "exceptions:\n"
        " - {package: Demo_Pkg, scope: BUILD_TOOLING, reason: fixture}\n"
        " - {package: demo-pkg, scope: BUILD_TOOLING, reason: fixture}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        check_licenses.load_exceptions(path)


def test_license_report_is_fresh_and_hash_bound(tmp_path: Path) -> None:
    import hashlib
    import json

    report = tmp_path / "licenses.json"
    argv = [
        "--requirements",
        str(REQUIREMENTS),
        "--runtime-lock",
        str(REQUIREMENTS),
        "--exceptions",
        str(EXCEPTIONS),
        "--out",
        str(report),
    ]
    assert check_licenses.main(argv) == 0
    value = json.loads(report.read_text())
    assert value["requirements_sha256"] == hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    assert all(row["locked_version"] == row["installed_version"] for row in value["dependencies"])
    before = report.read_bytes()
    assert check_licenses.main(argv) == 1
    assert report.read_bytes() == before
