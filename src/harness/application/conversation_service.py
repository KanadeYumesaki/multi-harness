"""会話の保存・取得を束ねる Application Service（設計書 §4.11）。

## Transaction 境界を持つのはここ

不変条件#15 のとおり、Repository は `commit()` しない。`UnitOfWorkPort` が開いた
`BEGIN IMMEDIATE` の内側で Repository を呼ぶ。

## 保存の順序は固定である

    1. 決定論的 Scanner を実行
    2. Reject 分類があれば保存・送信を停止
    3. 許可された本文だけ Artifact CAS へ保存
    4. CAS の content_artifact_hash を検証
    5. ConversationMessage Record を保存
    6. Record と Artifact の参照整合性を検証

**Scanner 未実行の本文を保存しない。** Gate を呼ばずに CAS へ書く経路を作らない。
Reject された本文は CAS へも SQLite へも書かない。**例外に本文を載せない。**
分類名だけを述べる。

## CAS 保存と SQLite 保存の間で落ちたら

CAS だけが書かれた状態が残りうる。**そのとき Message Record を作らない。**
未参照 Artifact は既存の GC 規則（`ArtifactGcPort`）が扱う。成功 Result を返さない。
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.domain.artifact import ArtifactMetadata
from harness.domain.context_budget import MessageRole
from harness.domain.conversation import (
    Conversation,
    ConversationMessage,
    derive_conversation_hash,
    derive_message_content_hash,
)
from harness.domain.conversation_snapshot import (
    ConversationSnapshot,
    derive_message_set_hash,
    derive_snapshot_hash,
)
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import ContentHash, hash_bytes
from harness.domain.knowledge_reference import KnowledgeReference
from harness.domain.schema_set import compute_schema_set_hash
from harness.ports.artifact_store import ArtifactStorePort
from harness.ports.conversation_store import (
    ConversationMessageStorePort,
    ConversationSnapshotStorePort,
    ConversationStorePort,
)
from harness.ports.masking import MaskingPolicyGatePort
from harness.ports.schema_catalog import SchemaCatalogPort
from harness.ports.unit_of_work import UnitOfWorkPort

__all__ = ["ConversationService", "StoredMessage"]


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """追加できた Message と、その本文の Artifact 参照。

    **本文そのものを持たない。** 読み戻しは `read_body` で行う。
    """

    message: ConversationMessage
    artifact_id: str


class ConversationService:
    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkPort,
        conversations: ConversationStorePort,
        messages: ConversationMessageStorePort,
        artifacts: ArtifactStorePort,
        masking_gate: MaskingPolicyGatePort,
        snapshots: ConversationSnapshotStorePort | None = None,
        schema_catalog: SchemaCatalogPort | None = None,
        design_sha256: ContentHash | None = None,
    ) -> None:
        self._uow = unit_of_work
        self._conversations = conversations
        self._messages = messages
        self._artifacts = artifacts
        self._gate = masking_gate
        self._snapshots = snapshots
        self._schema_catalog = schema_catalog
        self._design_sha256 = design_sha256

    # -- Conversation --------------------------------------------------------

    def create_conversation(
        self, *, conversation_id: str, record_id: str, created_at: str, producer: str
    ) -> Conversation:
        """`conversation_hash` は作成時の不変メタから導出する（CMC-4-A）。

        `conversation_id` は呼出側が UUIDv4 を Port 経由で採って渡す。
        Domain の中で乱数も時刻も引かない。
        """
        conversation_hash = derive_conversation_hash(
            conversation_id=conversation_id, created_at=created_at, producer=producer
        )
        conversation = Conversation(
            conversation_id=conversation_id,
            conversation_hash=conversation_hash,
            record_id=record_id,
            created_at=created_at,
            producer=producer,
            # Record 自身の内容 Hash。識別内容と同じ入力から導く。
            content_hash=conversation_hash,
        )
        with self._uow.begin_immediate():
            self._conversations.add(conversation)
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    def list_conversations(self) -> tuple[Conversation, ...]:
        return self._conversations.list_all()

    def verify_conversation_hash(self, conversation: Conversation) -> bool:
        """記録された Hash が識別内容から導けることを確かめる。"""
        expected = derive_conversation_hash(
            conversation_id=conversation.conversation_id,
            created_at=conversation.created_at,
            producer=conversation.producer,
        )
        return expected == conversation.conversation_hash

    # -- User-selected knowledge ---------------------------------------------

    def preview_knowledge(self, reference: KnowledgeReference) -> bytes:
        """検査済みの完全な保存Bytesを返す。DB/CASへの書込みや外部送信はない。"""
        body = reference.body()
        denial = self._gate.evaluate(body, "INTERNAL")
        if denial is not None:
            raise HarnessError(
                ErrorCode.MASKING_VERIFICATION_FAILED,
                "knowledge rejected before preview or storage: "
                f"categories={sorted(denial.rejected_categories)}",
            )
        return body

    def import_knowledge(
        self,
        *,
        reference: KnowledgeReference,
        expected_preview_hash: ContentHash,
        conversation_id: str,
        conversation_record_id: str,
        message_id: str,
        message_record_id: str,
        artifact_id: str,
        created_at: str,
        producer: str,
    ) -> tuple[Conversation, StoredMessage]:
        """資料と新会話を保存する。会話とMessageは同じTransactionで確定する。

        Gate/Hash照合はCAS書込みより先。DB失敗では新会話もMessageも残さない。
        CASだけ残った場合は通常の未参照Artifactであり、成功を返さない。
        この操作は送信Approvalではなく、CLI/HTTPS推論へは到達しない。
        """
        body = self.preview_knowledge(reference)
        if hash_bytes(body) != expected_preview_hash:
            raise HarnessError(
                ErrorCode.APPROVAL_INVALIDATED,
                "knowledge changed after preview; review the current content before saving",
            )
        manifest = self._artifacts.put(
            body,
            ArtifactMetadata(
                media_type="application/json",
                size_bytes=len(body),
                data_classification="INTERNAL",
                trust_level="UNTRUSTED_EXTERNAL_INPUT",
            ),
            artifact_id=artifact_id,
            stored_at=created_at,
        )
        self._require_verified(manifest.content_hash, "knowledge artifact after put")
        conversation_hash = derive_conversation_hash(
            conversation_id=conversation_id, created_at=created_at, producer=producer
        )
        conversation = Conversation(
            conversation_id=conversation_id,
            conversation_hash=conversation_hash,
            record_id=conversation_record_id,
            created_at=created_at,
            producer=producer,
            content_hash=conversation_hash,
        )
        with self._uow.begin_immediate():
            self._conversations.add(conversation)
            sequence_number = self._messages.next_sequence_number(conversation_id)
            message = ConversationMessage(
                message_id=message_id,
                conversation_id=conversation_id,
                role=MessageRole.USER_TASK,
                sequence_number=sequence_number,
                content_artifact_hash=manifest.content_hash,
                record_id=message_record_id,
                created_at=created_at,
                producer=producer,
                content_hash=derive_message_content_hash(
                    content_artifact_hash=manifest.content_hash,
                    role=MessageRole.USER_TASK,
                    sequence_number=sequence_number,
                ),
            )
            self._messages.add(message)
        self._require_verified(message.content_artifact_hash, "knowledge artifact after commit")
        return conversation, StoredMessage(message=message, artifact_id=manifest.artifact_id)

    # -- Message -------------------------------------------------------------

    def append_message(
        self,
        *,
        conversation_id: str,
        role: MessageRole,
        body: bytes,
        message_id: str,
        record_id: str,
        created_at: str,
        producer: str,
        artifact_id: str,
        media_type: str = "text/plain",
        data_classification: str = "INTERNAL",
        trust_level: str = "UNTRUSTED_EXTERNAL_INPUT",
    ) -> StoredMessage:
        """本文を CAS へ置き、Message Record を追加する。

        順序は module docstring のとおりで、入れ替えない。
        """
        # 1. 決定論的 Scanner。**先に呼ぶ。** 呼ばずに CAS へ書く経路を作らない。
        denial = self._gate.evaluate(body, data_classification)

        # 2. Reject 分類があれば止める。マスクして保存しない（不変条件#21）。
        if denial is not None:
            # 本文も抜粋も例外へ載せない。分類名だけを述べる。
            raise HarnessError(
                ErrorCode.MASKING_VERIFICATION_FAILED,
                "conversation body rejected before storage: "
                f"categories={sorted(denial.rejected_categories)}",
            )

        # 3. 許可された本文だけ CAS へ保存する。
        metadata = ArtifactMetadata(
            media_type=media_type,
            size_bytes=len(body),
            data_classification=data_classification,
            trust_level=trust_level,
        )
        manifest = self._artifacts.put(
            body, metadata, artifact_id=artifact_id, stored_at=created_at
        )

        # 4. CAS の content_artifact_hash を検証する。
        self._require_verified(manifest.content_hash, f"artifact {artifact_id} after put")

        # 5. Record を保存する。順序キーは Transaction の内側で採る。
        #    `BEGIN IMMEDIATE` が書込みロックを取るので、同時追加でも重複しない。
        with self._uow.begin_immediate():
            sequence_number = self._messages.next_sequence_number(conversation_id)
            message = ConversationMessage(
                message_id=message_id,
                conversation_id=conversation_id,
                role=role,
                sequence_number=sequence_number,
                content_artifact_hash=manifest.content_hash,
                record_id=record_id,
                created_at=created_at,
                producer=producer,
                content_hash=derive_message_content_hash(
                    content_artifact_hash=manifest.content_hash,
                    role=role,
                    sequence_number=sequence_number,
                ),
            )
            self._messages.add(message)

        # 6. Record と Artifact の参照整合性を確かめる。
        self._require_verified(
            message.content_artifact_hash, f"artifact referenced by message {message_id}"
        )
        return StoredMessage(message=message, artifact_id=manifest.artifact_id)

    def list_messages(self, conversation_id: str) -> tuple[ConversationMessage, ...]:
        """`sequence_number` 昇順で返す。"""
        return self._messages.list_for_conversation(conversation_id)

    def read_body(self, message: ConversationMessage) -> bytes:
        """`content_artifact_hash` から本文 Bytes を読み戻す。

        読んだ Bytes を再 Hash して参照と一致することを確かめる。
        **一致しなければ返さない。**
        """
        body = self._artifacts.get(message.content_artifact_hash)
        if hash_bytes(body) != message.content_artifact_hash:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"artifact bytes for message {message.message_id} do not match the reference",
            )
        return body

    # -- Snapshot ------------------------------------------------------------

    def build_snapshot(
        self,
        *,
        conversation_id: str,
        snapshot_id: str,
        record_id: str,
        created_at: str,
        producer: str,
        design_sha256: ContentHash,
    ) -> ConversationSnapshot:
        """Message 集合から Snapshot を作って保存する。

        Message は読むだけで書き換えない。不在 Artifact・Hash 不一致・
        Design Hash 不一致では作らずに止める。
        """
        if self._snapshots is None or self._schema_catalog is None:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                "snapshot store and schema catalog are required to build a snapshot",
            )
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise HarnessError(
                ErrorCode.STORAGE_WRITE_FAILED, f"conversation {conversation_id} does not exist"
            )

        # 現行 Design Hash と一致しない Snapshot を作らない。
        if self._design_sha256 is not None and design_sha256 != self._design_sha256:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "design_sha256 does not match the current design document",
            )

        messages = self._messages.list_for_conversation(conversation_id)
        self._require_ascending(messages)
        for message in messages:
            self._verify_message(message)

        message_set_hash = derive_message_set_hash([m.content_hash for m in messages])
        schema_set_hash = self._used_schema_set_hash()
        snapshot_hash = derive_snapshot_hash(
            conversation_hash=conversation.conversation_hash,
            message_set_hash=message_set_hash,
            schema_set_hash=schema_set_hash,
            design_sha256=design_sha256,
        )
        snapshot = ConversationSnapshot(
            snapshot_id=snapshot_id,
            conversation_id=conversation_id,
            snapshot_hash=snapshot_hash,
            message_set_hash=message_set_hash,
            schema_set_hash=schema_set_hash,
            design_sha256=design_sha256,
            record_id=record_id,
            created_at=created_at,
            producer=producer,
            # Record 自身の内容 Hash。束縛先から導いた値と同じ入力を使う。
            content_hash=snapshot_hash,
        )
        with self._uow.begin_immediate():
            self._snapshots.add(snapshot)
        return snapshot

    def list_snapshots(self, conversation_id: str) -> tuple[ConversationSnapshot, ...]:
        if self._snapshots is None:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "snapshot store is not configured"
            )
        return self._snapshots.list_for_conversation(conversation_id)

    def verify_snapshot(self, snapshot: ConversationSnapshot) -> bool:
        """記録された束縛先から `snapshot_hash` を導き直して照合する。"""
        conversation = self._conversations.get(snapshot.conversation_id)
        if conversation is None:
            return False
        recomputed = snapshot.recompute_hash(conversation_hash=conversation.conversation_hash)
        return recomputed == snapshot.snapshot_hash

    def _used_schema_set_hash(self) -> ContentHash:
        """**実際に使用した** Schema の Version 集合から導く（§15.2）。

        名前は使っている Domain 型が持つ `SCHEMA_NAME` から採り、Version は
        Registry の active write version から採る。ここに名前も版も書かない。
        Catalog 全体を無条件に含めない。
        """
        if self._schema_catalog is None:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                "schema catalog is not configured",
            )
        used = (
            Conversation.SCHEMA_NAME,
            ConversationMessage.SCHEMA_NAME,
            ConversationSnapshot.SCHEMA_NAME,
        )
        refs = [self._schema_catalog.active_ref(name) for name in used]
        return compute_schema_set_hash(refs)

    def _require_ascending(self, messages: tuple[ConversationMessage, ...]) -> None:
        """順序キーが厳密に昇順であること。

        重複や逆順は Repository の並べ替えでは直らない壊れ方である。
        **推測で並べ直さず止める。**
        """
        numbers = [m.sequence_number for m in messages]
        if numbers != sorted(set(numbers)):
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"sequence_number is not strictly ascending: {numbers}",
            )

    def _verify_message(self, message: ConversationMessage) -> None:
        """本文 Artifact の実在と Hash 一致、Record の内容 Hash を確かめる。"""
        self._require_verified(
            message.content_artifact_hash,
            f"artifact referenced by message {message.message_id}",
        )
        expected = derive_message_content_hash(
            content_artifact_hash=message.content_artifact_hash,
            role=message.role,
            sequence_number=message.sequence_number,
        )
        if expected != message.content_hash:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"message {message.message_id} content_hash does not match "
                "body/role/sequence_number",
            )

    def _require_verified(self, content_hash: object, what: str) -> None:
        verification = self._artifacts.verify(content_hash)  # type: ignore[arg-type]
        # 実物が用意した判定を使う。自前で outcome を比べ直さない。
        if not verification.ok:
            raise HarnessError(
                ErrorCode.ARTIFACT_CONTENT_CONFLICT,
                f"{what} did not verify: {verification.outcome.name}",
            )
