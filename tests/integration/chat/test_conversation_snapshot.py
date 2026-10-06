"""`ConversationSnapshot` 生成の試験（設計書 §4.11）。

実物の SQLite と実物の `CoreSchemaRegistry` を使う。代役にするのは決定論
Scanner と Artifact CAS の格納先だけで、どちらも Port の契約どおりに振る舞う。

## 何を測っているか

* 同じ履歴から同じ `message_set_hash` と `snapshot_hash` が出ること
* `snapshot_id` を変えても `snapshot_hash` が動かないこと
* 本文・`role`・順序の改変、Artifact 不在、Hash 不一致で **止まる** こと
* `schema_set_hash` が実使用 Version だけを含み、未使用 Schema で動かないこと
* Snapshot へ本文が入らないこと
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from chat.test_conversation_store import _Cas, _Gate, _Uow
from harness.application.conversation_service import ConversationService
from harness.domain.context_budget import MessageRole
from harness.domain.conversation import Conversation, ConversationMessage
from harness.domain.conversation_snapshot import (
    ConversationSnapshot,
    derive_message_set_hash,
    derive_snapshot_hash,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes
from harness.domain.schema_set import SchemaRef, compute_schema_set_hash
from harness.infrastructure.schema.registry import CoreSchemaRegistry
from harness.infrastructure.sqlite.conversation_repository import (
    SqliteConversationMessageRepository,
    SqliteConversationRepository,
    SqliteConversationSnapshotRepository,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
_TS = "2026-08-28T00:00:00Z"
_PRODUCER = "test"


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    from harness.infrastructure.sqlite.migrations import _MIGRATION_0006, _MIGRATION_0007

    conn = sqlite3.connect(tmp_path / "chat.db", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    for statement in (*_MIGRATION_0006, *_MIGRATION_0007):
        conn.execute(statement)
    return conn


@pytest.fixture
def catalog() -> CoreSchemaRegistry:
    return CoreSchemaRegistry.bundled()


@pytest.fixture
def design_hash() -> ContentHash:
    """現行 Design 本文の実測 Hash。**手入力しない。**"""
    import json

    snapshot = json.loads((REPO_ROOT / "registry-snapshot.json").read_text(encoding="utf-8"))
    design = REPO_ROOT / f"design-v{snapshot['design_version']}-runtime-go.md"
    return hash_bytes(design.read_bytes())


def _build(
    connection: sqlite3.Connection,
    catalog: CoreSchemaRegistry,
    design_hash: ContentHash,
    *,
    cas: _Cas | None = None,
    gate: _Gate | None = None,
) -> ConversationService:
    return ConversationService(
        unit_of_work=_Uow(connection),
        conversations=SqliteConversationRepository(connection),
        messages=SqliteConversationMessageRepository(connection),
        artifacts=cas or _Cas(),
        masking_gate=gate or _Gate(),
        snapshots=SqliteConversationSnapshotRepository(connection),
        schema_catalog=catalog,
        design_sha256=design_hash,
    )


@pytest.fixture
def service(
    connection: sqlite3.Connection, catalog: CoreSchemaRegistry, design_hash: ContentHash
) -> ConversationService:
    return _build(connection, catalog, design_hash)


def _conversation(service: ConversationService) -> Conversation:
    return service.create_conversation(
        conversation_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
    )


def _append(
    service: ConversationService, cid: str, body: bytes, role: MessageRole = MessageRole.USER_TASK
) -> Any:
    return service.append_message(
        conversation_id=cid,
        role=role,
        body=body,
        message_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
        artifact_id=str(uuid.uuid4()),
    )


def _snapshot(
    service: ConversationService, cid: str, design_hash: ContentHash, snapshot_id: str | None = None
) -> ConversationSnapshot:
    return service.build_snapshot(
        conversation_id=cid,
        snapshot_id=snapshot_id or str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
        design_sha256=design_hash,
    )


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def test_empty_conversation_produces_a_snapshot(
    service: ConversationService, design_hash: ContentHash
) -> None:
    """空の Conversation でも Snapshot は作れること。"""
    conversation = _conversation(service)
    snapshot = _snapshot(service, conversation.conversation_id, design_hash)
    assert snapshot.message_set_hash == derive_message_set_hash([])
    assert service.verify_snapshot(snapshot)


def test_empty_and_non_empty_differ(service: ConversationService, design_hash: ContentHash) -> None:
    """空集合の Hash が、どの非空集合とも一致しないこと。"""
    empty = _conversation(service)
    filled = _conversation(service)
    _append(service, filled.conversation_id, b"one")
    a = _snapshot(service, empty.conversation_id, design_hash)
    b = _snapshot(service, filled.conversation_id, design_hash)
    assert a.message_set_hash != b.message_set_hash


def test_single_message_snapshot(service: ConversationService, design_hash: ContentHash) -> None:
    conversation = _conversation(service)
    stored = _append(service, conversation.conversation_id, b"only")
    snapshot = _snapshot(service, conversation.conversation_id, design_hash)
    assert snapshot.message_set_hash == derive_message_set_hash([stored.message.content_hash])


def test_message_set_hash_follows_sequence_order(
    service: ConversationService, design_hash: ContentHash
) -> None:
    conversation = _conversation(service)
    stored = [_append(service, conversation.conversation_id, f"m{i}".encode()) for i in range(4)]
    snapshot = _snapshot(service, conversation.conversation_id, design_hash)
    ordered = sorted(stored, key=lambda s: s.message.sequence_number)
    assert snapshot.message_set_hash == derive_message_set_hash(
        [s.message.content_hash for s in ordered]
    )
    # 逆順で並べれば違う値になる。順序が Hash に効いている。
    assert snapshot.message_set_hash != derive_message_set_hash(
        [s.message.content_hash for s in reversed(ordered)]
    )


def test_same_history_yields_same_hashes(
    service: ConversationService, design_hash: ContentHash
) -> None:
    conversation = _conversation(service)
    for i in range(3):
        _append(service, conversation.conversation_id, f"m{i}".encode())
    first = _snapshot(service, conversation.conversation_id, design_hash)
    second = _snapshot(service, conversation.conversation_id, design_hash)
    assert first.message_set_hash == second.message_set_hash
    assert first.snapshot_hash == second.snapshot_hash


def test_snapshot_id_does_not_change_the_hash(
    service: ConversationService, design_hash: ContentHash
) -> None:
    """**`snapshot_id` は `snapshot_hash` の入力ではない。**"""
    conversation = _conversation(service)
    _append(service, conversation.conversation_id, b"x")
    a = _snapshot(service, conversation.conversation_id, design_hash, snapshot_id="snap-a")
    b = _snapshot(service, conversation.conversation_id, design_hash, snapshot_id="snap-b")
    assert a.snapshot_id != b.snapshot_id
    assert a.snapshot_hash == b.snapshot_hash


def test_snapshot_hash_excludes_ids_and_time() -> None:
    """参照実装の引数に ID も時刻も無いこと。"""
    import inspect

    params = set(inspect.signature(derive_snapshot_hash).parameters)
    assert params == {
        "conversation_hash",
        "message_set_hash",
        "schema_set_hash",
        "design_sha256",
    }
    for forbidden in ("snapshot_id", "created_at", "record_id", "snapshot_hash"):
        assert forbidden not in params


def test_every_binding_moves_the_snapshot_hash() -> None:
    """束縛先が 1 つでも動けば `snapshot_hash` が動くこと。

    引数に並んでいるだけでは足りない。**入力として効いていること**を見る。
    payload から落としても引数は残るので、この検査が無いと気付けない。
    """
    base_args = {
        "conversation_hash": hash_bytes(b"conversation"),
        "message_set_hash": hash_bytes(b"messages"),
        "schema_set_hash": hash_bytes(b"schemas"),
        "design_sha256": hash_bytes(b"design"),
    }
    baseline = derive_snapshot_hash(**base_args)
    for name in base_args:
        moved = {**base_args, name: hash_bytes(b"moved " + name.encode())}
        assert derive_snapshot_hash(**moved) != baseline, f"{name} を変えても Hash が動かない"


def test_message_set_hash_reacts_to_every_element() -> None:
    """要素が 1 つでも変われば `message_set_hash` が動くこと。"""
    elements = [hash_bytes(b"a"), hash_bytes(b"b"), hash_bytes(b"c")]
    baseline = derive_message_set_hash(elements)
    for index in range(len(elements)):
        moved = list(elements)
        moved[index] = hash_bytes(b"changed")
        assert derive_message_set_hash(moved) != baseline, f"{index} 番目を変えても動かない"
    # 件数が変わっても動く
    assert derive_message_set_hash(elements[:-1]) != baseline


def test_snapshot_uses_all_four_bindings(
    service: ConversationService, design_hash: ContentHash, catalog: CoreSchemaRegistry
) -> None:
    """生成された Snapshot の Hash が、4 つの束縛先から導けること。

    実装が payload から Field を落としていれば、ここで一致しなくなる。
    """
    conversation = _conversation(service)
    _append(service, conversation.conversation_id, b"body")
    snapshot = _snapshot(service, conversation.conversation_id, design_hash)
    expected = derive_snapshot_hash(
        conversation_hash=conversation.conversation_hash,
        message_set_hash=snapshot.message_set_hash,
        schema_set_hash=snapshot.schema_set_hash,
        design_sha256=snapshot.design_sha256,
    )
    assert snapshot.snapshot_hash == expected
    # 束縛先を差し替えれば違う値になる。何も見ていない検査にしない。
    assert snapshot.snapshot_hash != derive_snapshot_hash(
        conversation_hash=conversation.conversation_hash,
        message_set_hash=snapshot.message_set_hash,
        schema_set_hash=hash_bytes(b"other schemas"),
        design_sha256=snapshot.design_sha256,
    )


def test_adding_a_message_changes_the_message_set_hash(
    service: ConversationService, design_hash: ContentHash
) -> None:
    conversation = _conversation(service)
    _append(service, conversation.conversation_id, b"first")
    before = _snapshot(service, conversation.conversation_id, design_hash)
    _append(service, conversation.conversation_id, b"second")
    after = _snapshot(service, conversation.conversation_id, design_hash)
    assert before.message_set_hash != after.message_set_hash
    assert before.snapshot_hash != after.snapshot_hash


def test_conversation_hash_is_unchanged_by_messages(
    service: ConversationService, design_hash: ContentHash
) -> None:
    conversation = _conversation(service)
    before = service.get_conversation(conversation.conversation_id)
    for i in range(3):
        _append(service, conversation.conversation_id, f"m{i}".encode())
    _snapshot(service, conversation.conversation_id, design_hash)
    assert service.get_conversation(conversation.conversation_id) == before


# ---------------------------------------------------------------------------
# Fail-Closed
# ---------------------------------------------------------------------------


def test_missing_artifact_is_rejected(
    connection: sqlite3.Connection, catalog: CoreSchemaRegistry, design_hash: ContentHash
) -> None:
    cas = _Cas()
    service = _build(connection, catalog, design_hash, cas=cas)
    conversation = _conversation(service)
    _append(service, conversation.conversation_id, b"body")
    cas.blobs.clear()  # CAS から本文が消えた
    with pytest.raises(HarnessError) as caught:
        _snapshot(service, conversation.conversation_id, design_hash)
    assert caught.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT


def test_artifact_hash_mismatch_is_rejected(
    connection: sqlite3.Connection, catalog: CoreSchemaRegistry, design_hash: ContentHash
) -> None:
    cas = _Cas()
    service = _build(connection, catalog, design_hash, cas=cas)
    conversation = _conversation(service)
    stored = _append(service, conversation.conversation_id, b"body")
    # 同じ Key に別の Bytes を置く。Hash と中身が食い違う状態。
    cas.blobs[str(stored.message.content_artifact_hash)] = b"tampered"
    with pytest.raises(HarnessError) as caught:
        _snapshot(service, conversation.conversation_id, design_hash)
    assert caught.value.code is ErrorCode.ARTIFACT_CONTENT_CONFLICT


@pytest.mark.parametrize("column,value", [("role", "SYSTEM_CONTROL"), ("sequence_number", 99)])
def test_tampered_message_row_is_rejected(
    connection: sqlite3.Connection,
    catalog: CoreSchemaRegistry,
    design_hash: ContentHash,
    column: str,
    value: object,
) -> None:
    """`role` や `sequence_number` を書き換えた行を Snapshot が受け付けないこと。

    `content_hash` はこの 2 つを入力に取るので、書き換えれば導出値と食い違う。
    """
    service = _build(connection, catalog, design_hash)
    conversation = _conversation(service)
    stored = _append(service, conversation.conversation_id, b"body")
    connection.execute(
        f"UPDATE conversation_message SET {column} = ? WHERE message_id = ?",  # noqa: S608
        (value, stored.message.message_id),
    )
    with pytest.raises(HarnessError) as caught:
        _snapshot(service, conversation.conversation_id, design_hash)
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_tampered_content_hash_is_rejected(
    connection: sqlite3.Connection, catalog: CoreSchemaRegistry, design_hash: ContentHash
) -> None:
    """本文 Hash だけ差し替えた行も止まること。"""
    service = _build(connection, catalog, design_hash)
    conversation = _conversation(service)
    stored = _append(service, conversation.conversation_id, b"body")
    connection.execute(
        "UPDATE conversation_message SET content_hash = ? WHERE message_id = ?",
        ("sha256:" + "f" * 64, stored.message.message_id),
    )
    with pytest.raises(HarnessError):
        _snapshot(service, conversation.conversation_id, design_hash)


def test_design_hash_mismatch_is_rejected(
    service: ConversationService, design_hash: ContentHash
) -> None:
    """現行と違う Design Hash では Snapshot を作らないこと。"""
    conversation = _conversation(service)
    with pytest.raises(HarnessError) as caught:
        _snapshot(service, conversation.conversation_id, hash_bytes(b"not the design"))
    assert caught.value.code is ErrorCode.RUNTIME_SPEC_MISMATCH


def test_unknown_conversation_is_rejected(
    service: ConversationService, design_hash: ContentHash
) -> None:
    with pytest.raises(HarnessError):
        _snapshot(service, str(uuid.uuid4()), design_hash)


def test_database_refuses_duplicate_sequence_numbers(
    connection: sqlite3.Connection, catalog: CoreSchemaRegistry, design_hash: ContentHash
) -> None:
    """順序キーの重複は DB が拒む。**表の制約なので Index を外しても入らない。**"""
    service = _build(connection, catalog, design_hash)
    conversation = _conversation(service)
    first = _append(service, conversation.conversation_id, b"one")
    _append(service, conversation.conversation_id, b"two")
    connection.execute("DROP INDEX IF EXISTS conversation_message_by_sequence")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE conversation_message SET sequence_number = ? WHERE message_id != ?",
            (first.message.sequence_number, first.message.message_id),
        )


def test_non_ascending_message_set_is_rejected(
    service: ConversationService,
) -> None:
    """壊れた集合を渡されたら **並べ直さずに止める** こと。

    DB は重複を入れさせないので、Guard を直接呼んで確かめる。
    Repository の並べ替えでは直らない壊れ方である。
    """
    from harness.domain.conversation import derive_message_content_hash

    def _message(sequence_number: int) -> ConversationMessage:
        body = hash_bytes(f"b{sequence_number}".encode())
        return ConversationMessage(
            message_id=str(uuid.uuid4()),
            conversation_id="c-1",
            role=MessageRole.USER_TASK,
            sequence_number=sequence_number,
            content_artifact_hash=body,
            record_id="r",
            created_at=_TS,
            producer=_PRODUCER,
            content_hash=derive_message_content_hash(
                content_artifact_hash=body,
                role=MessageRole.USER_TASK,
                sequence_number=sequence_number,
            ),
        )

    service._require_ascending((_message(1), _message(2)))  # 正常な並びは通る
    for broken in ((_message(2), _message(1)), (_message(1), _message(1))):
        with pytest.raises(HarnessError) as caught:
            service._require_ascending(broken)
        assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


# ---------------------------------------------------------------------------
# schema_set_hash
# ---------------------------------------------------------------------------


def test_schema_set_hash_covers_only_the_used_schemas(
    service: ConversationService, catalog: CoreSchemaRegistry, design_hash: ContentHash
) -> None:
    """実際に使った 3 Schema の active version だけを含むこと。"""
    conversation = _conversation(service)
    snapshot = _snapshot(service, conversation.conversation_id, design_hash)
    expected = compute_schema_set_hash(
        [
            catalog.active_ref("Conversation"),
            catalog.active_ref("ConversationMessage"),
            catalog.active_ref("ConversationSnapshot"),
        ]
    )
    assert snapshot.schema_set_hash == expected
    # Catalog 全体とは違う。無条件に含めていない。
    assert snapshot.schema_set_hash != compute_schema_set_hash(catalog.refs)


def test_schema_set_hash_uses_the_active_write_version(catalog: CoreSchemaRegistry) -> None:
    """`ConversationMessage` は `2.0.0` を使うこと。版を手入力していない。"""
    assert catalog.active_ref("ConversationMessage").schema_version == "2.0.0"
    assert catalog.active_ref("Conversation").schema_version == "1.0.0"
    assert catalog.active_ref("ConversationSnapshot").schema_version == "1.0.0"


def test_unused_schema_addition_does_not_move_the_hash(catalog: CoreSchemaRegistry) -> None:
    """未使用 Schema が増えても `schema_set_hash` が動かないこと。"""
    used = [
        catalog.active_ref("Conversation"),
        catalog.active_ref("ConversationMessage"),
        catalog.active_ref("ConversationSnapshot"),
    ]
    baseline = compute_schema_set_hash(used)
    unrelated = SchemaRef(
        schema_name="ZZZUnusedSchema",
        schema_version="1.0.0",
        content_hash=hash_bytes(b"unused"),
    )
    # 使用集合は変わらないので同じ値のまま
    assert compute_schema_set_hash(used) == baseline
    # 使った Schema が増えれば動く。何も見ていない検査にしない。
    assert compute_schema_set_hash([*used, unrelated]) != baseline


# ---------------------------------------------------------------------------
# Append-only と漏洩
# ---------------------------------------------------------------------------


def test_snapshot_repository_only_inserts_and_selects() -> None:
    """Snapshot Repository が UPDATE / DELETE を実行しないこと。"""
    import ast

    tree = ast.parse(
        (REPO_ROOT / "src/harness/infrastructure/sqlite/conversation_repository.py").read_text(
            encoding="utf-8"
        )
    )
    statements = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "executemany"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    verbs = {" ".join(sql.upper().split()).split(" ", 1)[0] for sql in statements}
    assert verbs <= {"INSERT", "SELECT"}, f"想定外の動詞: {sorted(verbs)}"


def test_regeneration_adds_a_new_record(
    service: ConversationService, design_hash: ContentHash
) -> None:
    """再生成は新しい Record として足すこと。既存を書き換えない。"""
    conversation = _conversation(service)
    _append(service, conversation.conversation_id, b"x")
    first = _snapshot(service, conversation.conversation_id, design_hash)
    second = _snapshot(service, conversation.conversation_id, design_hash)
    listed = service.list_snapshots(conversation.conversation_id)
    assert len(listed) == 2
    assert {s.snapshot_id for s in listed} == {first.snapshot_id, second.snapshot_id}
    # 中身は同じ履歴なので Hash は一致する
    assert first.snapshot_hash == second.snapshot_hash


def test_snapshot_table_has_no_body_column(connection: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(conversation_snapshot)").fetchall()
    }
    for forbidden in ("content", "text", "body", "payload", "messages", "summary"):
        assert forbidden not in columns
    assert "message_set_hash" in columns


def test_snapshot_record_carries_no_body(
    service: ConversationService, design_hash: ContentHash
) -> None:
    """Snapshot の値に本文が現れないこと。"""
    conversation = _conversation(service)
    _append(service, conversation.conversation_id, b"SECRET BODY TEXT")
    snapshot = _snapshot(service, conversation.conversation_id, design_hash)
    rendered = repr(snapshot)
    assert "SECRET BODY TEXT" not in rendered


def test_messages_are_not_modified_by_snapshot_building(
    service: ConversationService, design_hash: ContentHash
) -> None:
    """Snapshot を作っても Message が動かないこと。"""
    conversation = _conversation(service)
    for i in range(3):
        _append(service, conversation.conversation_id, f"m{i}".encode())
    before = service.list_messages(conversation.conversation_id)
    _snapshot(service, conversation.conversation_id, design_hash)
    assert service.list_messages(conversation.conversation_id) == before


# ---------------------------------------------------------------------------
# 決定論
# ---------------------------------------------------------------------------


def test_hashes_are_stable_across_processes() -> None:
    """別 Process・別 `PYTHONHASHSEED` で同じ値になること。

    引数名の検査だけでは PID の混入を捕まえられない。振る舞いで見る。
    """
    import os
    import subprocess
    import sys

    script = (
        "from harness.domain.conversation_snapshot import "
        "derive_message_set_hash, derive_snapshot_hash\n"
        "from harness.domain.hashing import hash_bytes\n"
        "ms = derive_message_set_hash([hash_bytes(b'a'), hash_bytes(b'b')])\n"
        "print(ms)\n"
        "print(derive_snapshot_hash(conversation_hash=hash_bytes(b'c'), message_set_hash=ms,"
        " schema_set_hash=hash_bytes(b's'), design_sha256=hash_bytes(b'd')))\n"
    )
    seen = set()
    for seed in ("0", "1", "31337"):
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        seen.add(out.stdout.strip())
    assert len(seen) == 1, f"Process ごとに Hash が変わった: {seen}"

    local_ms = derive_message_set_hash([hash_bytes(b"a"), hash_bytes(b"b")])
    _local_snapshot = derive_snapshot_hash(
        conversation_hash=hash_bytes(b"c"),
        message_set_hash=local_ms,
        schema_set_hash=hash_bytes(b"s"),
        design_sha256=hash_bytes(b"d"),
    )
    local = f"{local_ms}\n{_local_snapshot}"
    assert seen.pop() == local


def _imported_modules(relative: str) -> set[str]:
    """実際の import 文だけを集める。

    Source を素のまま走査すると、**「`sqlite3` を import しない」と書いた
    docstring に反応する**。禁止について書いた文が、違反と同じに見えてしまう。
    """
    import ast

    tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_domain_snapshot_imports_nothing_forbidden() -> None:
    """Domain が SQLite・Filesystem・Network・Provider を import しないこと。"""
    modules = _imported_modules("src/harness/domain/conversation_snapshot.py")
    assert modules, "import を 1 つも見つけられていない。検査が何も見ていない"
    for forbidden in ("sqlite3", "os", "pathlib", "subprocess", "socket", "urllib", "requests"):
        assert forbidden not in modules, f"{forbidden} を import している"
    assert modules <= {"__future__", "collections", "dataclasses", "typing", "harness"}


def test_application_does_not_import_a_provider() -> None:
    """Application が Provider Port を掴んでいないこと。"""
    modules = _imported_modules("src/harness/application/conversation_service.py")
    for forbidden in ("socket", "urllib", "requests", "http"):
        assert forbidden not in modules
    source = (REPO_ROOT / "src/harness/application/conversation_service.py").read_text(
        encoding="utf-8"
    )
    assert "ProviderPort" not in source
