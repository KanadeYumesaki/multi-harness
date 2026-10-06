"""§1.6 SafeInputReaderPort。

| Operation | 入力 | 出力 |
|---|---|---|
| `open_read` | Capability、Relative Path | Verified File Handle／Read Evidence |
| `enumerate` | Capability、Relative Directory | Verified Candidate Entries |

拒否は例外ではなく`ReadDenial`として返す。§1.16.2が拒否ごとに
Capability ID・要求Path・Reason Code・検証時刻の記録を要求するため、
呼出側が`INPUT_READ_DENIED`をLedgerへ書けるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from harness.domain.hashing import ContentHash
from harness.domain.input_read import ReadDecision, ReadDenial

__all__ = ["FileReadPort", "ReadEvidence", "SafeInputReaderPort"]


@dataclass(frozen=True, slots=True)
class ReadEvidence:
    """読取りが成立したことの証跡。

    `file_identity` は device／inode／サイズ／mtime を束ねた文字列。
    読取り前後で一致しない場合は§1.16.2「読取り中にDevice／Inode／Generation／
    Mount IDが変化した対象」に該当し拒否する。
    """

    capability_id: str
    requested_path: str
    canonical_path: str
    content_hash: ContentHash
    size_bytes: int
    file_identity: str
    mount_id: str

    @property
    def decision(self) -> ReadDecision:
        return ReadDecision.ALLOWED


class FileReadPort(Protocol):
    """1 File を読む側だけの契約。

    ## なぜ `SafeInputReaderPort` と分けるか

    §1.6 は `open_read` と `enumerate` の2 Operation を定める。しかし
    `InputReadOrchestrator` が呼ぶのは `open_read` だけである。にもかかわらず
    Orchestrator が広いほうの Port を要求していると、**列挙を実装していない
    Reader を Production から渡せない**。

    実際、具象の `SafeInputReader` は `enumerate` を持っていない（CC-03 で
    strict 型検査が最初の Production 呼出し元を型付けしたときに判明した）。
    ここで契約を狭めるのは、その事実を**隠さずに型へ書く**ためである。

    `SafeInputReaderPort` は §1.6 の形のまま残す。**要求を弱めていない。**
    列挙が要る呼出側は引き続き広いほうを要求する。
    """

    def open_read(
        self, capability_id: str, relative_path: str
    ) -> tuple[bytes, ReadEvidence] | ReadDenial: ...


class SafeInputReaderPort(FileReadPort, Protocol):
    """§1.6 の2 Operation を揃えた Reader。

    SafeInputReader は両操作に同じ Lease・Scope・失効再確認を適用する。
    """

    def enumerate(self, capability_id: str, relative_directory: str) -> list[str] | ReadDenial: ...


class CandidateEnumerationPort(Protocol):
    def enumerate(self, capability_id: str, relative_directory: str) -> list[str] | ReadDenial: ...
