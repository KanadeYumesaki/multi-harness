"""Conversation / Message 保存基盤の試験（設計書 §4.11）。

実物の SQLite と実物の CAS を使う。Repository も Service も本物である。
偽物にするのは「決定論 Scanner が何を Reject と言うか」だけで、そこは
Port の契約どおりに振る舞う小さな代役を置く。

## 何を測っているか

* 保存して取り出せること、本文を CAS 経由で再表示できること
* Reject 本文が CAS にも SQLite にも**残らない**こと
* Append-only が守られること（UPDATE 経路が無いこと）
* Hash が決定論で、role と順序の差し替えを検出すること
* 外部 Provider が呼ばれないこと
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from harness.application.conversation_service import ConversationService
from harness.domain.artifact import ArtifactMetadata, VerificationOutcome
from harness.domain.context_budget import MessageRole
from harness.domain.conversation import (
    Conversation,
    ConversationMessage,
    derive_conversation_hash,
    derive_message_content_hash,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes
from harness.infrastructure.sqlite.conversation_repository import (
    SqliteConversationMessageRepository,
    SqliteConversationRepository,
)
from harness.ports.masking import MaskingPolicyDenial

REPO_ROOT = Path(__file__).resolve().parents[3]

_TS = "2026-08-28T00:00:00Z"
_PRODUCER = "test"

#: 保存を拒否する分類（不変条件#21 / CMC-5-A）。
_REJECT = ("SECRET_GENERIC", "API_KEY", "PRIVATE_KEY", "NATIONAL_ID", "SPECIAL_CATEGORY_DATA")


# ---------------------------------------------------------------------------
# 代役
# ---------------------------------------------------------------------------


class _Gate:
    """決定論 Scanner の代役。呼ばれた回数を数える。

    **素通しにしない。** 何を Reject と言うかを試験が決め、Service がその答えに
    従うかを見る。
    """

    def __init__(self, rejects: tuple[str, ...] = ()) -> None:
        self._rejects = rejects
        self.calls = 0
        self.seen: list[bytes] = []

    def evaluate(self, payload: bytes, classification: str | None) -> MaskingPolicyDenial | None:
        self.calls += 1
        self.seen.append(payload)
        if self._rejects:
            return MaskingPolicyDenial(rejected_categories=self._rejects, masker_invocation_count=0)
        return None


class _Cas:
    """Artifact CAS の代役。Bytes を辞書へ持つ。

    `put` は §1.14 と同じく、同一 Hash へ別内容が来たら停止する。
    """

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.put_calls = 0

    def put(
        self, data: bytes, metadata: ArtifactMetadata, *, artifact_id: str, stored_at: str
    ) -> Any:
        self.put_calls += 1
        digest = hash_bytes(data)
        key = str(digest)
        if key in self.blobs and self.blobs[key] != data:
            raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "content conflict")
        self.blobs[key] = data
        from harness.ports.artifact_store import ArtifactManifestRecord

        return ArtifactManifestRecord(
            artifact_id=artifact_id,
            content_hash=digest,
            media_type=metadata.media_type,
            size_bytes=metadata.size_bytes,
            data_classification=metadata.data_classification,
            trust_level=metadata.trust_level,
            stored_at=stored_at,
            storage_path=f"cas/{digest.hexdigest[:2]}/{digest.hexdigest}",
        )

    def get(self, content_hash: ContentHash) -> bytes:
        key = str(content_hash)
        if key not in self.blobs:
            raise HarnessError(ErrorCode.ARTIFACT_CONTENT_CONFLICT, "bytes missing")
        return self.blobs[key]

    def verify(self, content_hash: ContentHash) -> Any:
        from harness.domain.artifact import ArtifactVerification

        key = str(content_hash)
        if key not in self.blobs:
            return ArtifactVerification(
                outcome=VerificationOutcome.BYTES_MISSING, content_hash=content_hash
            )
        observed = hash_bytes(self.blobs[key])
        if observed != content_hash:
            return ArtifactVerification(
                outcome=VerificationOutcome.CONTENT_MISMATCH,
                content_hash=content_hash,
                observed_hash=observed,
            )
        return ArtifactVerification(
            outcome=VerificationOutcome.OK, content_hash=content_hash, observed_hash=observed
        )


class _FailingMessages:
    """SQLite 保存だけが落ちる代役。CAS 保存後の障害を作る。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def next_sequence_number(self, conversation_id: str) -> int:
        return self._inner.next_sequence_number(conversation_id)

    def add(self, message: ConversationMessage) -> None:
        raise HarnessError(ErrorCode.STORAGE_WRITE_FAILED, "simulated sqlite failure")

    def list_for_conversation(self, conversation_id: str) -> tuple[ConversationMessage, ...]:
        return self._inner.list_for_conversation(conversation_id)


class _Uow:
    """`BEGIN IMMEDIATE` を実際に開く Unit of Work。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def begin_immediate(self) -> Any:
        return _Scope(self._connection)

    def in_transaction(self) -> bool:
        return self._connection.in_transaction


class _Scope:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> _Scope:
        self._connection.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self._connection.execute("COMMIT")
        else:
            self._connection.execute("ROLLBACK")
        return False


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    from harness.infrastructure.sqlite.migrations import _MIGRATION_0006

    conn = sqlite3.connect(tmp_path / "chat.db", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    for statement in _MIGRATION_0006:
        conn.execute(statement)
    return conn


@pytest.fixture
def service(connection: sqlite3.Connection) -> ConversationService:
    return _service(connection, _Gate())


def _service(
    connection: sqlite3.Connection, gate: _Gate, cas: _Cas | None = None, messages: Any = None
) -> ConversationService:
    inner = SqliteConversationMessageRepository(connection)
    return ConversationService(
        unit_of_work=_Uow(connection),
        conversations=SqliteConversationRepository(connection),
        messages=messages if messages is not None else inner,
        artifacts=cas or _Cas(),
        masking_gate=gate,
    )


def _new_conversation(service: ConversationService, cid: str | None = None) -> Conversation:
    return service.create_conversation(
        conversation_id=cid or str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
    )


def _append(
    service: ConversationService,
    conversation_id: str,
    body: bytes,
    role: MessageRole = MessageRole.USER_TASK,
) -> Any:
    return service.append_message(
        conversation_id=conversation_id,
        role=role,
        body=body,
        message_id=str(uuid.uuid4()),
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
        artifact_id=str(uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


def test_conversation_is_created_and_read_back(service: ConversationService) -> None:
    created = _new_conversation(service)
    fetched = service.get_conversation(created.conversation_id)
    assert fetched == created


def test_conversation_hash_verifies(service: ConversationService) -> None:
    created = _new_conversation(service)
    assert service.verify_conversation_hash(created)


def test_conversation_id_is_uuid4(service: ConversationService) -> None:
    created = _new_conversation(service)
    assert uuid.UUID(created.conversation_id).version == 4


def test_conversation_list_is_ordered_not_by_filesystem(service: ConversationService) -> None:
    made = [_new_conversation(service) for _ in range(3)]
    listed = service.list_conversations()
    assert sorted(c.conversation_id for c in made) == sorted(c.conversation_id for c in listed)
    assert list(listed) == sorted(listed, key=lambda c: (c.created_at, c.conversation_id))


def test_duplicate_conversation_is_rejected(service: ConversationService) -> None:
    created = _new_conversation(service)
    with pytest.raises(HarnessError) as caught:
        service.create_conversation(
            conversation_id=created.conversation_id,
            record_id=str(uuid.uuid4()),
            created_at=_TS,
            producer=_PRODUCER,
        )
    assert caught.value.code is ErrorCode.STORAGE_WRITE_FAILED


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


def test_message_is_stored_and_read_back(service: ConversationService) -> None:
    conversation = _new_conversation(service)
    stored = _append(service, conversation.conversation_id, b"hello")
    listed = service.list_messages(conversation.conversation_id)
    assert [m.message_id for m in listed] == [stored.message.message_id]


def test_body_is_restored_from_the_artifact_reference(service: ConversationService) -> None:
    conversation = _new_conversation(service)
    stored = _append(service, conversation.conversation_id, b"hello body")
    assert service.read_body(stored.message) == b"hello body"


def test_artifact_hash_matches_the_bytes(service: ConversationService) -> None:
    conversation = _new_conversation(service)
    stored = _append(service, conversation.conversation_id, b"payload")
    assert stored.message.content_artifact_hash == hash_bytes(b"payload")


def test_message_id_is_uuid4(service: ConversationService) -> None:
    conversation = _new_conversation(service)
    stored = _append(service, conversation.conversation_id, b"x")
    assert uuid.UUID(stored.message.message_id).version == 4


def test_messages_come_back_in_sequence_order(service: ConversationService) -> None:
    conversation = _new_conversation(service)
    for i in range(5):
        _append(service, conversation.conversation_id, f"body-{i}".encode())
    listed = service.list_messages(conversation.conversation_id)
    assert [m.sequence_number for m in listed] == [1, 2, 3, 4, 5]


def test_sequence_number_is_unique_within_a_conversation(
    connection: sqlite3.Connection, service: ConversationService
) -> None:
    """DB 側の制約が効くこと。Application の採番だけに頼らない。"""
    conversation = _new_conversation(service)
    stored = _append(service, conversation.conversation_id, b"first")
    duplicate = ConversationMessage(
        message_id=str(uuid.uuid4()),
        conversation_id=conversation.conversation_id,
        role=MessageRole.USER_TASK,
        sequence_number=stored.message.sequence_number,
        content_artifact_hash=stored.message.content_artifact_hash,
        record_id=str(uuid.uuid4()),
        created_at=_TS,
        producer=_PRODUCER,
        content_hash=stored.message.content_hash,
    )
    repository = SqliteConversationMessageRepository(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(HarnessError) as caught:
            repository.add(duplicate)
    finally:
        connection.execute("ROLLBACK")
    assert caught.value.code is ErrorCode.STORAGE_WRITE_FAILED


def test_conversations_are_isolated_from_each_other(service: ConversationService) -> None:
    first, second = _new_conversation(service), _new_conversation(service)
    _append(service, first.conversation_id, b"a")
    _append(service, first.conversation_id, b"b")
    _append(service, second.conversation_id, b"c")
    assert len(service.list_messages(first.conversation_id)) == 2
    assert len(service.list_messages(second.conversation_id)) == 1
    # 順序キーは Conversation ごとに 1 から始まる
    assert [m.sequence_number for m in service.list_messages(second.conversation_id)] == [1]


def test_message_for_unknown_conversation_is_rejected(service: ConversationService) -> None:
    with pytest.raises(HarnessError) as caught:
        _append(service, str(uuid.uuid4()), b"orphan")
    assert caught.value.code is ErrorCode.STORAGE_WRITE_FAILED


@pytest.mark.parametrize("role", list(MessageRole))
def test_all_seven_roles_are_accepted(service: ConversationService, role: MessageRole) -> None:
    conversation = _new_conversation(service)
    stored = _append(service, conversation.conversation_id, b"x", role=role)
    assert stored.message.role is role


def test_role_vocabulary_has_exactly_seven_values() -> None:
    assert len(list(MessageRole)) == 7


def test_invalid_role_is_rejected_by_the_database(connection: sqlite3.Connection) -> None:
    """正本に無い role を DB が拒む。"""
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO conversation (conversation_id, conversation_hash, record_id,"
            " created_at, producer, content_hash, store_version)"
            " VALUES ('c', 'sha256:" + "0" * 64 + "', 'r', ?, 'p', 'sha256:" + "0" * 64 + "', 1)",
            (_TS,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO conversation_message (message_id, conversation_id, role,"
                " sequence_number, content_artifact_hash, record_id, created_at, producer,"
                " content_hash, store_version)"
                " VALUES ('m', 'c', 'assistant', 1, 'sha256:" + "0" * 64 + "', 'r', ?, 'p',"
                " 'sha256:" + "0" * 64 + "', 1)",
                (_TS,),
            )
    finally:
        connection.execute("ROLLBACK")


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------


def test_message_hash_is_deterministic() -> None:
    args = {
        "content_artifact_hash": hash_bytes(b"body"),
        "role": MessageRole.USER_TASK,
        "sequence_number": 3,
    }
    assert derive_message_content_hash(**args) == derive_message_content_hash(**args)


def test_message_hash_detects_role_change() -> None:
    body = hash_bytes(b"body")
    a = derive_message_content_hash(
        content_artifact_hash=body, role=MessageRole.USER_TASK, sequence_number=1
    )
    b = derive_message_content_hash(
        content_artifact_hash=body, role=MessageRole.UNTRUSTED_PROVIDER_DATA, sequence_number=1
    )
    assert a != b


def test_message_hash_detects_sequence_change() -> None:
    body = hash_bytes(b"body")
    a = derive_message_content_hash(
        content_artifact_hash=body, role=MessageRole.USER_TASK, sequence_number=1
    )
    b = derive_message_content_hash(
        content_artifact_hash=body, role=MessageRole.USER_TASK, sequence_number=2
    )
    assert a != b


def test_message_hash_detects_body_change() -> None:
    a = derive_message_content_hash(
        content_artifact_hash=hash_bytes(b"one"),
        role=MessageRole.USER_TASK,
        sequence_number=1,
    )
    b = derive_message_content_hash(
        content_artifact_hash=hash_bytes(b"two"),
        role=MessageRole.USER_TASK,
        sequence_number=1,
    )
    assert a != b


def test_hash_inputs_exclude_ids_times_and_pid() -> None:
    """引数に ID・時刻・PID が無いことが根拠である（不変条件#4）。"""
    import inspect

    params = set(inspect.signature(derive_message_content_hash).parameters)
    assert params == {"content_artifact_hash", "role", "sequence_number"}
    conv = set(inspect.signature(derive_conversation_hash).parameters)
    assert conv == {"conversation_id", "created_at", "producer"}
    for forbidden in ("message_id", "conversation_id", "pid", "content_hash"):
        assert forbidden not in params


def _hash_in_child(expression: str, seed: str) -> str:
    """別 Process・別 `PYTHONHASHSEED` で Hash を計算させる。

    PID が混ざっていれば値が変わる。引数名の検査では捕まえられない混入を、
    振る舞いで捕まえる。
    """
    import os
    import subprocess
    import sys

    script = (
        "from harness.domain.conversation import "
        "derive_conversation_hash, derive_message_content_hash\n"
        "from harness.domain.context_budget import MessageRole\n"
        "from harness.domain.hashing import hash_bytes\n"
        f"print({expression})\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
    )
    return result.stdout.strip()


def test_conversation_hash_is_stable_across_processes() -> None:
    """PID や時刻が混ざっていないこと。**別 Process で同じ値になる。**"""
    expression = (
        "derive_conversation_hash(conversation_id='c-1',"
        " created_at='2026-08-28T00:00:00Z', producer='test')"
    )
    seen = {_hash_in_child(expression, seed) for seed in ("0", "1", "4242")}
    assert len(seen) == 1, f"Process ごとに Hash が変わった: {seen}"
    local = str(
        derive_conversation_hash(
            conversation_id="c-1", created_at="2026-08-28T00:00:00Z", producer="test"
        )
    )
    assert seen.pop() == local, "子 Process と親 Process で Hash が違う"


def test_message_hash_is_stable_across_processes() -> None:
    """Message の `content_hash` にも PID・時刻が混ざっていないこと。"""
    expression = (
        "derive_message_content_hash(content_artifact_hash=hash_bytes(b'body'),"
        " role=MessageRole.USER_TASK, sequence_number=1)"
    )
    seen = {_hash_in_child(expression, seed) for seed in ("0", "1", "4242")}
    assert len(seen) == 1, f"Process ごとに Hash が変わった: {seen}"
    local = str(
        derive_message_content_hash(
            content_artifact_hash=hash_bytes(b"body"),
            role=MessageRole.USER_TASK,
            sequence_number=1,
        )
    )
    assert seen.pop() == local


def test_tampered_content_hash_is_rejected_by_the_domain() -> None:
    """本文・role・順序と食い違う `content_hash` を持つ Message を作れないこと。"""
    with pytest.raises(HarnessError) as caught:
        ConversationMessage(
            message_id="m",
            conversation_id="c",
            role=MessageRole.USER_TASK,
            sequence_number=1,
            content_artifact_hash=hash_bytes(b"body"),
            record_id="r",
            created_at=_TS,
            producer=_PRODUCER,
            content_hash=hash_bytes(b"not the right hash"),
        )
    assert caught.value.code is ErrorCode.SCHEMA_CONDITIONAL_VIOLATION


def test_conversation_hash_does_not_change_when_messages_are_added(
    service: ConversationService,
) -> None:
    conversation = _new_conversation(service)
    before = service.get_conversation(conversation.conversation_id)
    for i in range(3):
        _append(service, conversation.conversation_id, f"m{i}".encode())
    after = service.get_conversation(conversation.conversation_id)
    assert before == after, "Message 追加で Conversation Record が動いた"


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


def _executed_sql(relative: str) -> list[str]:
    """`.execute(...)` の第 1 引数の文字列リテラルだけを集める。

    Source を素のまま走査すると、**禁止語について書いたコメントに反応する**。
    実際に SQLite へ渡る文だけを見る。
    """
    import ast

    tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
    statements: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"execute", "executemany"} or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            statements.append(first.value)
    return statements


def test_repositories_have_no_update_or_delete_path() -> None:
    """Repository が UPDATE / DELETE を **実行しない** こと。"""
    statements = _executed_sql("src/harness/infrastructure/sqlite/conversation_repository.py")
    assert statements, "SQL を 1 つも見つけられていない。検査が何も見ていない"
    for sql in statements:
        upper = " ".join(sql.upper().split())
        for forbidden in ("UPDATE CONVERSATION", "DELETE FROM CONVERSATION", "INSERT OR REPLACE"):
            assert forbidden not in upper, f"{forbidden} を実行している: {sql[:60]}"


def test_repository_only_inserts_and_selects() -> None:
    """実行される文が INSERT と SELECT だけであること。

    禁止語の列挙は「思いついた語」しか防げない。**動詞そのものを絞る。**
    """
    statements = _executed_sql("src/harness/infrastructure/sqlite/conversation_repository.py")
    verbs = {" ".join(sql.upper().split()).split(" ", 1)[0] for sql in statements}
    assert verbs <= {"INSERT", "SELECT"}, f"想定外の動詞: {sorted(verbs - {'INSERT', 'SELECT'})}"


def test_conversation_table_has_no_message_ids_or_count(
    connection: sqlite3.Connection,
) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(conversation)").fetchall()}
    assert "message_ids" not in columns
    assert "message_count" not in columns


def test_message_table_has_no_inline_body(connection: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(conversation_message)").fetchall()
    }
    assert "content_artifact_hash" in columns
    for forbidden in ("content", "text", "body", "payload", "token_count"):
        assert forbidden not in columns


def test_repository_refuses_to_write_outside_a_transaction(
    connection: sqlite3.Connection,
) -> None:
    """Transaction の外からの書込みを拒むこと（不変条件#15）。"""
    repository = SqliteConversationRepository(connection)
    conversation_id = str(uuid.uuid4())
    conversation = Conversation(
        conversation_id=conversation_id,
        conversation_hash=derive_conversation_hash(
            conversation_id=conversation_id, created_at=_TS, producer=_PRODUCER
        ),
        record_id="r",
        created_at=_TS,
        producer=_PRODUCER,
        content_hash=derive_conversation_hash(
            conversation_id=conversation_id, created_at=_TS, producer=_PRODUCER
        ),
    )
    with pytest.raises(HarnessError):
        repository.add(conversation)


# ---------------------------------------------------------------------------
# 保存境界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", _REJECT)
def test_rejected_body_reaches_neither_cas_nor_sqlite(
    connection: sqlite3.Connection, category: str
) -> None:
    gate = _Gate(rejects=(category,))
    cas = _Cas()
    service = _service(connection, gate, cas)
    conversation = _new_conversation(service)

    with pytest.raises(HarnessError) as caught:
        _append(service, conversation.conversation_id, b"secret material")

    assert caught.value.code is ErrorCode.MASKING_VERIFICATION_FAILED
    assert cas.put_calls == 0, "Reject なのに CAS へ書いている"
    assert cas.blobs == {}, "Reject 本文が CAS に残っている"
    assert service.list_messages(conversation.conversation_id) == ()
    rows = connection.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
    assert rows[0] == 0, "Reject なのに SQLite へ書いている"


@pytest.mark.parametrize("category", _REJECT)
def test_rejection_message_does_not_leak_the_body(
    connection: sqlite3.Connection, category: str
) -> None:
    """例外文へ本文を載せないこと。分類名だけを述べる。"""
    service = _service(connection, _Gate(rejects=(category,)), _Cas())
    conversation = _new_conversation(service)
    secret = b"AKIAIOSFODNN7EXAMPLE super secret value"
    with pytest.raises(HarnessError) as caught:
        _append(service, conversation.conversation_id, secret)
    rendered = str(caught.value)
    assert "AKIAIOSFODNN7EXAMPLE" not in rendered
    assert "super secret" not in rendered
    assert category in rendered


def test_scanner_runs_before_any_write(connection: sqlite3.Connection) -> None:
    """Gate を呼ばずに CAS へ書く経路が無いこと。"""
    gate = _Gate()
    cas = _Cas()
    service = _service(connection, gate, cas)
    conversation = _new_conversation(service)
    assert gate.calls == 0 and cas.put_calls == 0
    _append(service, conversation.conversation_id, b"clean")
    assert gate.calls == 1
    assert cas.put_calls == 1


def test_sqlite_failure_after_cas_leaves_no_message_record(
    connection: sqlite3.Connection,
) -> None:
    """CAS 保存後に SQLite が落ちても、偽の Record や成功 Result を作らないこと。"""
    cas = _Cas()
    service = _service(connection, _Gate(), cas)
    conversation = _new_conversation(service)

    failing = _service(
        connection,
        _Gate(),
        cas,
        messages=_FailingMessages(SqliteConversationMessageRepository(connection)),
    )
    with pytest.raises(HarnessError) as caught:
        _append(failing, conversation.conversation_id, b"body that reaches cas")

    assert caught.value.code is ErrorCode.STORAGE_WRITE_FAILED
    # CAS には残りうる。未参照 Artifact は既存の GC 規則が扱う。
    assert cas.put_calls == 1
    # **Message Record は作られていない。**
    assert service.list_messages(conversation.conversation_id) == ()
    rows = connection.execute("SELECT COUNT(*) FROM conversation_message").fetchone()
    assert rows[0] == 0


# ---------------------------------------------------------------------------
# 境界
# ---------------------------------------------------------------------------


def test_no_provider_is_reachable_from_the_service() -> None:
    """Service が Provider を import していないこと。"""
    source = (REPO_ROOT / "src/harness/application/conversation_service.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("provider", "Provider", "requests", "urllib", "socket", "http"):
        assert forbidden not in source, f"{forbidden} を参照している"


def test_domain_does_not_touch_sqlite_or_filesystem() -> None:
    """Domain が SQLite・Filesystem・Process を触らないこと（CLAUDE.md §2）。"""
    source = (REPO_ROOT / "src/harness/domain/conversation.py").read_text(encoding="utf-8")
    for forbidden in ("import sqlite3", "import os", "import subprocess", "from pathlib"):
        assert forbidden not in source, f"{forbidden} がある"


def test_repository_does_not_commit() -> None:
    """Repository が commit しないこと（不変条件#15）。"""
    source = (REPO_ROOT / "src/harness/infrastructure/sqlite/conversation_repository.py").read_text(
        encoding="utf-8"
    )
    assert ".commit()" not in source
