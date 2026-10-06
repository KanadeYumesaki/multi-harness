"""回答時点 Canon の束縛と、版上げ時の影響判定を固定する試験。

## 何を守るか

Owner は CPB-1-A〜CPB-7-A を選んだ。要するに次の 4 つである。

1. 回答済み Package は **回答時点の Canon** へ束縛したままにする
2. その Canon の信頼根は **Git 履歴** である
3. `schema_catalog_hash` を持たない旧 Package は **持たないまま** 保つ
4. 後継 Package は **機械判定と Owner の明示承認** が揃ったときだけ資格を持つ

## 通ることではなく通らないことを測る

* 現行 Snapshot を束縛の根拠へ戻したら落ちる
* 欠けた `schema_catalog_hash` を現行値で埋めたら落ちる
* 影響を判定できないのに `false` と言ったら落ちる
* 承認が無いのに後継可と言ったら落ちる
* 判定 Tool が Package を書いたら落ちる

## Docstring を根拠にしない

文言の一致では何も守れない。ここで見るのは **実際の Git の出力**、**JSON の
構造**、**構文木** の 3 つだけである。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import chat_canon_binding

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISION_DIR = REPO_ROOT / "docs/decision"
AUDIT_JSON = REPO_ROOT / "docs/audit/codex-chat-canon-binding-review.json"
DCR_JSON = DECISION_DIR / "DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE.json"
DCR_MD = DECISION_DIR / "DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE.md"
BUILDER = REPO_ROOT / "docs/audit/build_codex_chat_canon_binding_review.py"
COMPAT_TOOL = REPO_ROOT / "tools/check_codex_chat_canon_compatibility.py"
SNAPSHOT = REPO_ROOT / "registry-snapshot.json"

#: 現行 Canon 束縛から回答時点束縛へ移した試験。**戻っていないことを見る。**
CONVERTED_TESTS = (
    "tests/spec_lint/test_chat_provider_values_dcr.py",
    "tests/spec_lint/test_chat_provider_readiness.py",
    "tests/spec_lint/test_chat_provider_value_input.py",
)

#: 束縛の根拠にしてはならない Field。作業ツリーの Snapshot から読ませない。
CANON_HASH_KEYS = frozenset({"design_sha256", "registry_snapshot_hash", "schema_catalog_hash"})

#: 設計正本の表題。版番号を読ませるために Fixture でも同じ形にする。
DESIGN_TITLE = "## フェーズ別詳細設計書 v{version}（Fixture）"

#: Task 指示書が宣言した Owner の選択。**これが正である。**
#:
#: 回答 Hash は回答から導くので、回答を書き換えて Hash も付け直されると
#: 気づけない。指示書の宣言値をここに置いて、自己整合な書き換えを捕まえる。
DECLARED_ANSWERS = {
    "CPB-1": "CPB-1-A",
    "CPB-2": "CPB-2-A",
    "CPB-3": "CPB-3-A",
    "CPB-4": "CPB-4-A",
    "CPB-5": "CPB-5-A",
    "CPB-6": "CPB-6-A",
    "CPB-7": "CPB-7-A",
}
DECLARED_ANSWER_SHA256 = "sha256:828ce42bcef65acb33a47bf36420409b82816c18e65686e987ad59ca8a93f7e9"


@pytest.fixture(autouse=True)
def synthetic_canon(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    from synthetic_history_fixture import build_canon_history

    fixture = build_canon_history(tmp_path_factory.mktemp("canon-contract") / "history")
    original = REPO_ROOT
    for name in (
        "DECISION_DIR",
        "AUDIT_JSON",
        "DCR_JSON",
        "DCR_MD",
        "BUILDER",
        "COMPAT_TOOL",
        "SNAPSHOT",
    ):
        monkeypatch.setattr(
            request.module, name, fixture.root / getattr(request.module, name).relative_to(original)
        )
    monkeypatch.setattr(request.module, "REPO_ROOT", fixture.root)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _historical() -> list[dict[str, Any]]:
    return list(_json(AUDIT_JSON)["historical_design_hash_matches"])


def _builder_module() -> Any:
    """Builder を Path から読み込む。`docs/audit` は Package ではない。"""
    spec = importlib.util.spec_from_file_location("_cpb_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _decision_bytes() -> dict[str, bytes]:
    """`docs/decision` の全 File の Bytes。**後継の勝手な発行を見張る。**"""
    return {
        str(path.relative_to(REPO_ROOT)): path.read_bytes()
        for path in sorted(DECISION_DIR.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# Fixture の Git Repository
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        ["git", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"git {args}: {completed.stderr}"


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    out = subprocess.run(  # noqa: S603 - 固定argv、shell不使用
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _write_design(repo: Path, version: str, body: str, *, subdir: str = "") -> Path:
    path = repo / subdir / chat_canon_binding.design_filename(version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DESIGN_TITLE.format(version=version) + "\n\n" + body + "\n", encoding="utf-8")
    return path


def _write_snapshot(repo: Path, version: str, design: Path, *, extra: dict[str, str]) -> None:
    document = {
        "design_version": version,
        "design_sha256": _sha(design.read_bytes()),
        **extra,
    }
    (repo / chat_canon_binding.SNAPSHOT_NAME).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 1: Git 履歴 Resolver
# ---------------------------------------------------------------------------


def test_every_recorded_history_item_resolves_to_one_path() -> None:
    """v1.19〜v1.23 の 5 件が、記録 commit から一意に解決すること。

    **記録に無い値を根拠にしない。** commit も版も Hash も、凍結 Audit が持って
    いるものだけを渡す。
    """
    items = _historical()
    assert [item["design_version"] for item in items] == ["1.19", "1.20", "1.21", "1.22", "1.23"]
    for item in items:
        resolved = chat_canon_binding.resolve_historical_design(REPO_ROOT, item)
        assert resolved["commit"] == item["commit"]
        assert resolved["resolved_hash"] == item["package_design_sha256"]
        assert resolved["matches"] is True
        assert resolved["path"].endswith(
            chat_canon_binding.design_filename(str(item["design_version"]))
        )


def test_resolution_uses_the_recorded_commit_not_the_lookup_table() -> None:
    """`HISTORY_COMMITS` を壊しても、記録 commit で解決すること。

    固定表が権威なら、表を書き換えるだけで検証を素通りできる。**表を壊して**
    それでも通ることで、権威が記録側にあることを示す。
    """
    builder = _builder_module()
    original = dict(builder.HISTORY_COMMITS)
    broken = {version: original["1.23"] for version in original}
    builder.HISTORY_COMMITS = broken
    try:
        builder._validate_answered_package()
    finally:
        builder.HISTORY_COMMITS = original


def test_validation_does_not_read_the_working_tree_design() -> None:
    """作業ツリーの設計 Path を消しても、回答済みの検証が通ること。

    現行 v1.24 の Bytes を回答時点検証の代わりに使っていたら、ここで落ちる。
    """
    builder = _builder_module()
    original_design, original_snapshot = builder.DESIGN, builder.SNAPSHOT
    builder.DESIGN = REPO_ROOT / "no-such-design-file.md"
    builder.SNAPSHOT = REPO_ROOT / "no-such-snapshot.json"
    try:
        builder._validate_answered_package()
    finally:
        builder.DESIGN, builder.SNAPSHOT = original_design, original_snapshot


def test_a_missing_commit_field_is_rejected() -> None:
    item = {k: v for k, v in _historical()[0].items() if k != "commit"}
    with pytest.raises(chat_canon_binding.CanonResolutionError) as caught:
        chat_canon_binding.resolve_historical_design(REPO_ROOT, item)
    assert caught.value.code == "BINDING_FIELD_MISSING"


def test_an_unknown_commit_fails_closed() -> None:
    """Git が失敗したら止まること。**空の結果を「一致」にしない。**"""
    item = dict(_historical()[0])
    item["commit"] = "0" * 40
    with pytest.raises(chat_canon_binding.CanonResolutionError) as caught:
        chat_canon_binding.resolve_historical_design(REPO_ROOT, item)
    assert caught.value.code == "GIT_HISTORY_UNAVAILABLE"


def test_a_broken_git_directory_fails_closed(tmp_path: Path) -> None:
    """Git Repository ですらない場所を渡したら止まること。"""
    with pytest.raises(chat_canon_binding.CanonResolutionError) as caught:
        chat_canon_binding.resolve_historical_design(tmp_path, _historical()[0])
    assert caught.value.code == "GIT_HISTORY_UNAVAILABLE"


def test_an_absent_design_path_fails_closed(tmp_path: Path) -> None:
    """その commit にその版の設計が無ければ止まること。"""
    repo = tmp_path / "absent"
    _init_repo(repo)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    commit = _commit(repo, "no design")
    with pytest.raises(chat_canon_binding.CanonResolutionError) as caught:
        chat_canon_binding.resolve_historical_design(
            repo,
            {"commit": commit, "design_version": "9.1", "package_design_sha256": _sha(b"x")},
        )
    assert caught.value.code == "GIT_HISTORY_DESIGN_PATH_ABSENT"


def test_two_paths_with_the_same_name_fail_closed(tmp_path: Path) -> None:
    """同名の設計が 2 箇所にあれば止まること。**片方を選ばない。**"""
    repo = tmp_path / "ambiguous"
    _init_repo(repo)
    left = _write_design(repo, "9.1", "left", subdir="a")
    _write_design(repo, "9.1", "right", subdir="b")
    commit = _commit(repo, "two designs")
    with pytest.raises(chat_canon_binding.CanonResolutionError) as caught:
        chat_canon_binding.resolve_historical_design(
            repo,
            {
                "commit": commit,
                "design_version": "9.1",
                "package_design_sha256": _sha(left.read_bytes()),
            },
        )
    assert caught.value.code == "GIT_HISTORY_DESIGN_PATH_AMBIGUOUS"


def test_a_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "mismatch"
    _init_repo(repo)
    _write_design(repo, "9.1", "body")
    commit = _commit(repo, "design")
    with pytest.raises(chat_canon_binding.CanonResolutionError) as caught:
        chat_canon_binding.resolve_historical_design(
            repo,
            {
                "commit": commit,
                "design_version": "9.1",
                "package_design_sha256": _sha(b"different bytes"),
            },
        )
    assert caught.value.code == "GIT_HISTORY_DESIGN_HASH_MISMATCH"


def test_every_failure_code_is_declared() -> None:
    """送出しうる分類が :data:`FAILURE_CODES` に揃っていること。

    Code を増やしたのに表へ足し忘れると、呼び手が分岐できない。**構文木で**
    実際に送出している Literal を集めて突き合わせる。
    """
    tree = ast.parse(
        (REPO_ROOT / "tools/chat_canon_binding.py").read_text(encoding="utf-8"),
        filename="chat_canon_binding.py",
    )
    raised: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "CanonResolutionError" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            raised.add(first.value)
    assert raised, "送出箇所を 1 つも拾えていない"
    assert raised <= set(chat_canon_binding.FAILURE_CODES), sorted(
        raised - set(chat_canon_binding.FAILURE_CODES)
    )


# ---------------------------------------------------------------------------
# 2: 現行 Canon を代用しない
# ---------------------------------------------------------------------------


def test_the_resolver_never_falls_back_to_the_working_tree(tmp_path: Path) -> None:
    """作業ツリーにだけ在る設計を、履歴の代わりに使わないこと。

    **これが一番効く試験である。** Bytes は記録 Hash と一致するのに、Git 履歴に
    無い。ここで通ってしまう実装は「回答時点 Canon」を名乗りながら現行を見て
    いる。
    """
    repo = tmp_path / "uncommitted"
    _init_repo(repo)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _commit(repo, "only readme")
    design = _write_design(repo, "9.1", "uncommitted body")
    _write_snapshot(repo, "9.1", design, extra={"registry_snapshot_hash": _sha(b"r")})

    resolved = chat_canon_binding.resolve_answer_time_canon(
        repo,
        {
            "design_version": "9.1",
            "design_sha256": _sha(design.read_bytes()),
            "registry_snapshot_hash": _sha(b"r"),
        },
    )
    assert resolved["matches"] is False
    assert resolved["failure"]["code"] == "GIT_HISTORY_DESIGN_VERSION_ABSENT"


def test_a_working_tree_edit_does_not_satisfy_the_recorded_hash(tmp_path: Path) -> None:
    """履歴に版が在っても、Bytes が合わなければ作業ツリーで代用しないこと。

    前の試験は「履歴にその版が 1 件も無い」場合を見る。こちらは **履歴に在るが
    Bytes が違う** 場合を見る。Resolver の枝が違うので、両方要る。片方だけだと、
    もう一方の枝へ作業ツリー Fallback を書き足しても誰も気づかない。
    """
    repo = tmp_path / "edited"
    _init_repo(repo)
    design = _write_design(repo, "9.1", "committed body")
    _write_snapshot(repo, "9.1", design, extra={"registry_snapshot_hash": _sha(b"r")})
    _commit(repo, "v9.1")

    # 作業ツリーだけを書き換える。**この Bytes は履歴に無い。**
    design.write_text(design.read_text(encoding="utf-8") + "\nworking tree only\n", "utf-8")
    edited = _sha(design.read_bytes())

    resolved = chat_canon_binding.resolve_answer_time_canon(
        repo,
        {
            "design_version": "9.1",
            "design_sha256": edited,
            "registry_snapshot_hash": _sha(b"r"),
        },
    )
    assert resolved["matches"] is False
    assert resolved["failure"]["code"] == "GIT_HISTORY_DESIGN_HASH_MISMATCH"


def test_a_newer_design_version_does_not_move_the_answer_time_canon(tmp_path: Path) -> None:
    """版を上げても、旧版を名乗る Package は旧版へ解決し続けること。

    版上げを **隔離した Fixture で** 起こす。実 Repository の凍結 File は 1 Byte
    も触らない。
    """
    repo = tmp_path / "bumped"
    _init_repo(repo)
    old_design = _write_design(repo, "9.1", "old body")
    old_hash = _sha(old_design.read_bytes())
    _write_snapshot(repo, "9.1", old_design, extra={"registry_snapshot_hash": _sha(b"old")})
    old_commit = _commit(repo, "v9.1")

    old_design.unlink()
    new_design = _write_design(repo, "9.2", "new body")
    _write_snapshot(repo, "9.2", new_design, extra={"registry_snapshot_hash": _sha(b"new")})
    _commit(repo, "v9.2")

    binding = {
        "design_version": "9.1",
        "design_sha256": old_hash,
        "registry_snapshot_hash": _sha(b"old"),
    }
    resolved = chat_canon_binding.resolve_answer_time_canon(repo, binding)
    assert resolved["matches"] is True
    assert resolved["design"]["commit"] == old_commit
    assert resolved["design"]["resolved_hash"] == old_hash
    assert resolved["snapshot"]["commit"] == old_commit
    # いまの作業ツリーは v9.2 である。**それでも解決先は v9.1 のままである。**
    assert chat_canon_binding.design_filename("9.2") in {
        path.name for path in repo.iterdir() if path.is_file()
    }


def test_the_converted_tests_do_not_read_canon_hashes_from_the_snapshot() -> None:
    """置換した 3 試験が、作業ツリーの Snapshot から束縛 Hash を読まないこと。

    文字列検索では自分の needle を拾ってしまう。**構文木で追う。**
    `SNAPSHOT` から作った名前を辿り、その名前へ Canon Hash の添字が付いていたら
    現行追随へ戻ったと判じる。語彙や件数のために Snapshot を読むのは違反にしない。
    """
    offenders: list[str] = []
    for relative in CONVERTED_TESTS:
        offenders.extend(snapshot_canon_hash_reads(REPO_ROOT / relative, relative))
    assert not offenders, f"現行 Snapshot を束縛の根拠へ戻している: {offenders}"


def snapshot_canon_hash_reads(path: Path, relative: str) -> list[str]:
    """`SNAPSHOT` から作った名前へ Canon Hash の添字を付けた箇所を返す。

    非公開側へ移した試験にも同じ検査を当てるため、関数として切り出している。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    derived: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or node.value is None:
            continue
        names = {child.id for child in ast.walk(node.value) if isinstance(child, ast.Name)}
        if "SNAPSHOT" not in names:
            continue
        derived |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        value, index = node.value, node.slice
        if not isinstance(value, ast.Name) or value.id not in derived:
            continue
        if isinstance(index, ast.Constant) and index.value in CANON_HASH_KEYS:
            found.append(f"{relative}:{node.lineno}: {index.value}")
    return found


def test_the_shared_resolver_has_no_working_tree_path_constant() -> None:
    """共通 Resolver が作業ツリーの設計 Path を持たないこと。

    Path 定数を 1 つ置いた瞬間、そこから現行 Canon へ落ちる経路ができる。
    """
    source = (REPO_ROOT / "tools/chat_canon_binding.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="chat_canon_binding.py")
    # Docstring は説明であって指し先ではない。Code の Literal だけを見る。
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    # 版番号まで含んだ完全な File 名だけを違反とする。f-string の断片
    # （`"-runtime-go.md"`）は名前を組み立てる部品であって、指し先ではない。
    whole_name = re.compile(r"design-v\d+\.\d+-runtime-go\.md")
    assert not [text for text in literals if whole_name.search(text)], (
        "設計 File 名を Literal で持っている"
    )


# ---------------------------------------------------------------------------
# 3: 回答済み Package を動かさない
# ---------------------------------------------------------------------------


def test_the_check_mode_passes_and_writes_nothing() -> None:
    before = _decision_bytes()
    audit_before = AUDIT_JSON.read_bytes()
    result = _run(str(BUILDER), "--check")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert _decision_bytes() == before, "--check が Package を書いた"
    assert AUDIT_JSON.read_bytes() == audit_before, "--check が監査を書いた"


def test_a_plain_rebuild_is_refused() -> None:
    """回答済みの Package を、通常の Builder が上書きできないこと。"""
    before = _decision_bytes()
    result = _run(str(BUILDER))
    assert result.returncode != 0, "回答済みなのに生成器が通った"
    assert "ALREADY_ANSWERED" in result.stdout + result.stderr
    assert _decision_bytes() == before, "生成器が回答済み Package を書き換えた"


def test_the_recorded_answers_match_the_declared_contract() -> None:
    """記録された選択と回答 Hash が、宣言された契約と一致すること。

    **回答 Hash だけでは足りない。** 回答を書き換えて Hash を付け直せば、Hash
    検査は通る。宣言値と突き合わせて初めて、自己整合な書き換えを捕まえられる。
    """
    package = _json(DCR_JSON)
    recorded = {qid: answer["choice_id"] for qid, answer in package["answers"].items()}
    assert recorded == DECLARED_ANSWERS
    assert package["answer_sha256"] == DECLARED_ANSWER_SHA256
    assert chat_canon_binding.recorded_answer_digest(DECLARED_ANSWERS) == DECLARED_ANSWER_SHA256


def _reanswer(doc: dict[str, Any], qid: str, choice_id: str, *, keep_text: bool) -> None:
    """選択を差し替え、回答 Hash も付け直す。**自己整合な改ざんを作る。**"""
    answer = doc["answers"][qid]
    answer["choice_id"] = choice_id
    if keep_text:
        question = next(q for q in doc["questions"] if q["id"] == qid)
        option = next(o for o in question["options"] if o["id"] == choice_id)
        answer["label"] = option["label"]
        answer["detail"] = option["detail"]
    else:
        answer.pop("label", None)
        answer.pop("detail", None)
    doc["answer_sha256"] = chat_canon_binding.recorded_answer_digest(
        {q: a["choice_id"] for q, a in doc["answers"].items()}
    )


def test_a_self_consistent_reanswer_is_caught_by_the_declared_contract() -> None:
    """回答も Hash も辻褄を合わせた書き換えを、宣言値との突合が捕まえること。

    Builder の `--check` はこれを捕まえられない。Package の中だけを見ても矛盾が
    無いからである。**捕まえられないことも含めて測る。** 捕まえるのは
    `test_the_recorded_answers_match_the_declared_contract` である。

    その試験を子 Process で走らせ、落ちることを確かめる。
    """
    original_json = DCR_JSON.read_bytes()
    original_md = DCR_MD.read_bytes()
    document = json.loads(original_json.decode("utf-8"))
    _reanswer(document, "CPB-1", "CPB-1-B", keep_text=True)
    mutated = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert mutated != original_json

    node = f"{Path(__file__).name}::test_the_recorded_answers_match_the_declared_contract"
    try:
        DCR_JSON.write_bytes(mutated)
        # Package の中は辻褄が合っている。だから Builder は通ってしまう。
        builder = _run(str(BUILDER), "--check")
        contract = _run(
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1])[sys.argv[2]]()",
            str(REPO_ROOT / "tests/spec_lint" / node.split("::")[0]),
            node.split("::")[1],
        )
    finally:
        DCR_JSON.write_bytes(original_json)
        DCR_MD.write_bytes(original_md)
    assert DCR_JSON.read_bytes() == original_json
    assert builder.returncode == 0, "Builder が捕まえたなら、この試験の前提が変わった"
    assert contract.returncode != 0, "宣言値との突合が自己整合な書き換えを見逃した"


@pytest.mark.parametrize(
    ("pointer", "mutate"),
    [
        (
            "answers/CPB-1/choice_id",
            lambda doc: doc["answers"]["CPB-1"].update({"choice_id": "CPB-1-B"}),
        ),
        ("answer_sha256", lambda doc: doc.update({"answer_sha256": _sha(b"tampered")})),
        ("unanswered", lambda doc: doc.update({"unanswered": ["CPB-1"]})),
        ("questions", lambda doc: doc["questions"].pop()),
        ("options", lambda doc: doc["questions"][0]["options"].pop()),
        ("bound_to/audit_sha256", lambda doc: doc["bound_to"].update({"audit_sha256": _sha(b"x")})),
        ("status", lambda doc: doc.update({"status": "DECISION_REQUIRED"})),
        (
            "self_consistent_unknown_choice",
            lambda doc: _reanswer(doc, "CPB-1", "CPB-1-Z", keep_text=False),
        ),
    ],
)
def test_tampering_with_the_answered_package_is_detected(
    tmp_path: Path, pointer: str, mutate: Any
) -> None:
    """回答 ID・回答 Hash・設問・選択肢・未回答・監査 Hash の改変を見つけること。

    **実 Repository の凍結 File を書き換えない。** 元 Bytes を退避し、検査後に
    必ず戻す。戻ったことも測る。
    """
    original_json = DCR_JSON.read_bytes()
    original_md = DCR_MD.read_bytes()
    (tmp_path / "backup.json").write_bytes(original_json)

    document = json.loads(original_json.decode("utf-8"))
    mutate(document)
    mutated = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert mutated != original_json, f"{pointer}: 変異が Bytes を動かしていない"

    try:
        DCR_JSON.write_bytes(mutated)
        result = _run(str(BUILDER), "--check")
        assert result.returncode != 0, f"{pointer}: 改変を見逃した"
    finally:
        DCR_JSON.write_bytes(original_json)
        DCR_MD.write_bytes(original_md)
    assert DCR_JSON.read_bytes() == original_json
    assert DCR_MD.read_bytes() == original_md


# ---------------------------------------------------------------------------
# 4: 欠けた schema_catalog_hash を保つ
# ---------------------------------------------------------------------------


def test_the_recorded_schema_hash_distribution_is_unchanged() -> None:
    """合成Packageの有り／無しを個別に確認する。実Ownerの在庫数は証明しない。

    **5 件をひとまとめに扱わない。** File ごとに Key の有無を見る。
    """
    distribution = _json(AUDIT_JSON)["schema_hash_distribution"]
    assert distribution["with_hash"]
    assert distribution["without_hash"]
    assert not set(distribution["with_hash"]) & set(distribution["without_hash"])
    for relative in distribution["with_hash"]:
        assert "schema_catalog_hash" in _json(REPO_ROOT / relative), relative
    for relative in distribution["without_hash"]:
        assert "schema_catalog_hash" not in _json(REPO_ROOT / relative), relative


def test_the_binding_keeps_a_missing_field_missing() -> None:
    """Key が無い Package から作った束縛に、Key が生えないこと。

    `None` も空文字も現行 Snapshot の値も入れない（CPB-5-A）。
    """
    live_schema_hash = _json(SNAPSHOT)["schema_catalog_hash"]
    distribution = _json(AUDIT_JSON)["schema_hash_distribution"]
    for relative in distribution["without_hash"]:
        package = _json(REPO_ROOT / relative)
        binding = chat_canon_binding.answer_time_binding(package)
        assert "schema_catalog_hash" not in binding, relative
        assert live_schema_hash not in binding.values(), f"{relative}: 現行値で埋めている"
        resolved = chat_canon_binding.resolve_answer_time_canon(REPO_ROOT, binding)
        assert resolved["schema_catalog_hash_present"] is False, relative
        assert resolved["matches"] is True, f"{relative}: {resolved['failure']}"
        assert "schema_catalog_hash" not in resolved["snapshot"]["compared_fields"], relative


def test_a_package_with_the_field_still_compares_it() -> None:
    """持っている Package では、その Field も比べていること。

    「欠落を保つ」を口実に、有る Field まで比較から外していないかを見る。
    """
    for relative in _json(AUDIT_JSON)["schema_hash_distribution"]["with_hash"]:
        package = _json(REPO_ROOT / relative)
        binding = chat_canon_binding.answer_time_binding(package)
        resolved = chat_canon_binding.resolve_answer_time_canon(REPO_ROOT, binding)
        assert resolved["matches"] is True, f"{relative}: {resolved['failure']}"
        assert "schema_catalog_hash" in resolved["snapshot"]["compared_fields"], relative


def test_a_wrong_schema_hash_is_rejected() -> None:
    """Key を持つ Package の値を 1 文字変えたら解決できないこと。"""
    package = dict(_json(DCR_JSON))
    package["schema_catalog_hash"] = _sha(b"not the recorded schema catalog")
    binding = chat_canon_binding.answer_time_binding(package)
    resolved = chat_canon_binding.resolve_answer_time_canon(REPO_ROOT, binding)
    assert resolved["matches"] is False
    assert resolved["failure"]["code"] == "GIT_HISTORY_SNAPSHOT_HASH_MISMATCH"


# ---------------------------------------------------------------------------
# 5: 影響判定と Owner 承認
# ---------------------------------------------------------------------------


def _fixture_canon(
    tmp_path: Path, package: dict[str, Any], *, drift: str | None
) -> tuple[Path, Path]:
    """回答時点の Canon を Git から復元し、指定した Field だけを変える。"""
    resolved = chat_canon_binding.verify_answer_time_canon(REPO_ROOT, package)
    tmp_path.mkdir(parents=True, exist_ok=True)
    design_ref = resolved["design"]
    snapshot_ref = resolved["snapshot"]
    design = tmp_path / Path(design_ref["path"]).name
    design.write_bytes(
        chat_canon_binding.git_bytes(
            REPO_ROOT, "show", f"{design_ref['commit']}:{design_ref['path']}"
        )
    )
    snapshot_bytes = chat_canon_binding.git_bytes(
        REPO_ROOT, "show", f"{snapshot_ref['commit']}:{snapshot_ref['path']}"
    )
    fixture_snapshot = tmp_path / chat_canon_binding.SNAPSHOT_NAME
    fixture_snapshot.write_bytes(snapshot_bytes)
    if drift is None:
        return design, fixture_snapshot
    snapshot = json.loads(snapshot_bytes)
    if drift == "design":
        fixture_design = tmp_path / design.name
        fixture_design.write_text(
            design.read_text(encoding="utf-8") + "\n<!-- fixture drift -->\n", encoding="utf-8"
        )
        snapshot["design_sha256"] = _sha(fixture_design.read_bytes())
        fixture_snapshot = tmp_path / chat_canon_binding.SNAPSHOT_NAME
        fixture_snapshot.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return fixture_design, fixture_snapshot
    snapshot[drift] = _sha(f"fixture drift {drift}".encode())
    fixture_snapshot = tmp_path / chat_canon_binding.SNAPSHOT_NAME
    fixture_snapshot.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return design, fixture_snapshot


def _compat(
    package: Path,
    design: Path,
    snapshot: Path,
    *,
    approval: str | None = None,
    out: Path | None = None,
) -> tuple[int, dict[str, Any], str]:
    args = [
        str(COMPAT_TOOL),
        "--package",
        str(package),
        "--against-design",
        str(design),
        "--against-snapshot",
        str(snapshot),
    ]
    if approval is not None:
        args += ["--owner-approval", approval]
    if out is not None:
        args += ["--out", str(out)]
    result = _run(*args)
    report = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
    return result.returncode, report, result.stderr


def test_the_same_canon_needs_no_successor(tmp_path: Path) -> None:
    """差分が無ければ影響なし、後継も不要と判定すること。"""
    design, snapshot = _fixture_canon(tmp_path, _json(DCR_JSON), drift=None)
    code, report, _ = _compat(DCR_JSON, design, snapshot)
    assert code == 0
    assert report["impact"] is False
    assert report["changed"] == []
    assert report["blockers"] == []
    assert report["owner_approval_required"] is False
    assert report["successor_eligible"] is False


@pytest.mark.parametrize("drift", ["design", "registry_snapshot_hash", "schema_catalog_hash"])
def test_a_changed_canon_requires_owner_approval(tmp_path: Path, drift: str) -> None:
    """Canon が動いたら影響ありとし、承認が要ると言うこと。

    **どの Field が動いたかを挙げる。** 「なにか変わった」で済ませない。
    """
    design, snapshot = _fixture_canon(tmp_path, _json(DCR_JSON), drift=drift)
    code, report, stderr = _compat(DCR_JSON, design, snapshot)
    assert report["impact"] is True, report
    assert report["changed"], report
    assert report["owner_approval_required"] is True
    assert report["approval_present"] is False
    assert report["successor_eligible"] is False
    assert code != 0, "承認が無いのに 0 で終わった"
    assert "OWNER_APPROVAL_REQUIRED" in stderr


def test_an_unresolvable_binding_is_unknown_not_false(tmp_path: Path) -> None:
    """回答時点 Canon を解決できないときは `unknown` で止まること。

    **`false` に倒さない。** 比べられなかったものを「影響なし」にしたら、統治
    が黙って外れる。
    """
    broken = tmp_path / "broken-package.json"
    package = _json(DCR_JSON)
    package["design_sha256"] = _sha(b"never committed")
    broken.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    design, snapshot = _fixture_canon(tmp_path, _json(DCR_JSON), drift=None)
    code, report, _ = _compat(broken, design, snapshot)
    assert report["impact"] == "unknown"
    assert any(b.startswith("ANSWER_TIME_CANON_UNRESOLVED") for b in report["blockers"]), report
    assert report["owner_approval_required"] is True
    assert report["successor_eligible"] is False
    assert code != 0


def test_a_tampered_choice_is_unknown(tmp_path: Path) -> None:
    """選択肢と回答が食い違う Package を `unknown` にすること。"""
    broken = tmp_path / "tampered-package.json"
    package = _json(DCR_JSON)
    package["answers"]["CPB-1"]["label"] = "書き換えた文面"
    broken.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    design, snapshot = _fixture_canon(tmp_path, package, drift=None)
    _, report, _ = _compat(broken, design, snapshot)
    assert report["impact"] == "unknown"
    assert any("PACKAGE_CHOICE_INCONSISTENT" in b for b in report["blockers"]), report


def test_only_an_explicit_matching_approval_grants_successor_eligibility(tmp_path: Path) -> None:
    """承認があって初めて後継の資格が立つこと。**Package は作らない。**"""
    design, snapshot = _fixture_canon(tmp_path, _json(DCR_JSON), drift="registry_snapshot_hash")
    _, report, _ = _compat(DCR_JSON, design, snapshot)
    digest = report["compared_canon"]["digest"]
    package_id = report["package_id"]

    before = _decision_bytes()
    code, approved, _ = _compat(DCR_JSON, design, snapshot, approval=f"{package_id}:{digest}")
    assert code == 0
    assert approved["approval_present"] is True
    assert approved["successor_eligible"] is True
    assert _decision_bytes() == before, "承認したら Package を書いてしまった"


@pytest.mark.parametrize(
    "approval",
    ["CPB-6-A", "DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE:sha256:0000", "yes", ""],
)
def test_a_wrong_approval_is_not_an_approval(tmp_path: Path, approval: str) -> None:
    """比較に紐づかない承認を受け付けないこと。

    既存統治の選択肢 ID をそのまま承認へ流用する経路も塞ぐ。
    """
    design, snapshot = _fixture_canon(tmp_path, _json(DCR_JSON), drift="registry_snapshot_hash")
    code, report, _ = _compat(DCR_JSON, design, snapshot, approval=approval)
    assert report["approval_present"] is False
    assert report["approval_rejected_because"] is not None
    assert report["successor_eligible"] is False
    assert code != 0


def test_an_approval_does_not_carry_to_another_comparison(tmp_path: Path) -> None:
    """ある比較への承認を、別の比較へ使い回せないこと。"""
    left_design, left_snapshot = _fixture_canon(
        tmp_path / "left", _json(DCR_JSON), drift="registry_snapshot_hash"
    )
    right_design, right_snapshot = _fixture_canon(
        tmp_path / "right", _json(DCR_JSON), drift="schema_catalog_hash"
    )
    _, left_report, _ = _compat(DCR_JSON, left_design, left_snapshot)
    stolen = f"{left_report['package_id']}:{left_report['compared_canon']['digest']}"
    code, report, _ = _compat(DCR_JSON, right_design, right_snapshot, approval=stolen)
    assert report["approval_present"] is False
    assert report["approval_rejected_because"] == "APPROVAL_DOES_NOT_MATCH_COMPARISON"
    assert code != 0


def test_the_tool_refuses_to_write_into_the_decision_directory(tmp_path: Path) -> None:
    """判定 Tool が Package 置き場へ書かないこと。**後継の勝手な発行を塞ぐ。**"""
    design, snapshot = _fixture_canon(tmp_path, _json(DCR_JSON), drift=None)
    target = DECISION_DIR / "DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE-V2.json"
    before = _decision_bytes()
    try:
        code, _, stderr = _compat(DCR_JSON, design, snapshot, out=target)
        wrote = target.exists()
    finally:
        # **門が壊れていても Repository を汚さない。** 変異検査で実際に残った。
        target.unlink(missing_ok=True)
    assert code == 2, stderr
    assert "OUTPUT_PATH_FORBIDDEN" in stderr
    assert not wrote, "Package 置き場へ書き出した"
    assert _decision_bytes() == before


def test_a_missing_schema_field_is_not_filled_by_the_tool(tmp_path: Path) -> None:
    """Key を持たない Package の判定に、現行 Schema Hash が出てこないこと。

    **報告 JSON 全体を見る。** どこか 1 箇所でも現行値を差し込んだら落ちる。
    """
    live_schema_hash = _json(SNAPSHOT)["schema_catalog_hash"]
    for relative in _json(AUDIT_JSON)["schema_hash_distribution"]["without_hash"]:
        package_path = REPO_ROOT / relative
        design, snapshot = _fixture_canon(tmp_path, _json(package_path), drift=None)
        _, report, _ = _compat(package_path, design, snapshot)
        assert report["bound_canon"]["schema_catalog_hash_present"] is False, relative
        assert "schema_catalog_hash" not in report["compared_fields"], relative
        bound = json.dumps(report["bound_canon"], ensure_ascii=False)
        assert live_schema_hash not in bound, f"{relative}: 現行値で埋めている"
        assert report["impact"] is False, report


def test_the_tool_never_writes_a_successor_package(tmp_path: Path) -> None:
    """どの経路を通っても `docs/decision` の Bytes が動かないこと。"""
    before = _decision_bytes()
    same_design, same_snapshot = _fixture_canon(tmp_path / "same", _json(DCR_JSON), drift=None)
    drifted_design, drifted_snapshot = _fixture_canon(
        tmp_path / "drift", _json(DCR_JSON), drift="registry_snapshot_hash"
    )
    _compat(DCR_JSON, same_design, same_snapshot)
    _compat(DCR_JSON, drifted_design, drifted_snapshot)
    _, report, _ = _compat(DCR_JSON, drifted_design, drifted_snapshot)
    digest = report["compared_canon"]["digest"]
    _compat(
        DCR_JSON,
        drifted_design,
        drifted_snapshot,
        approval=f"{report['package_id']}:{digest}",
        out=tmp_path / "judgement.json",
    )
    assert _decision_bytes() == before
    assert (tmp_path / "judgement.json").exists(), "--out へは書けるはずである"


def test_the_tool_declares_the_three_impact_values() -> None:
    """`impact` が 3 値であることを、構文木で確かめる。

    Docstring の文言ではなく、実際に代入している値を集める。
    """
    tree = ast.parse(COMPAT_TOOL.read_text(encoding="utf-8"), filename=COMPAT_TOOL.name)
    assigned: set[Any] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "impact" for t in node.targets):
            continue
        if isinstance(node.value, ast.Constant):
            assigned.add(node.value.value)
    assert assigned == {True, False, "unknown"}, assigned
