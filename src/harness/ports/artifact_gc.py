"""§1.18.1 GC Executor が要る抽象。

`ArtifactStorePort` は Put／Get／Verify の契約であり、削除を持たない。GC は
Bytes だけを消して Manifest を残すという別の操作なので、必要な分だけを別 Port
として置く。既存 Port の契約を広げない。

Application 層が Filesystem を知らずに済むよう、Path を露出しない。Payload が
残っているかは `verify` の判定（`BYTES_MISSING`）で分かる。
"""

from __future__ import annotations

from typing import Protocol

from harness.domain.artifact import ArtifactVerification
from harness.domain.hashing import ContentHash
from harness.ports.artifact_store import ArtifactManifestRecord

__all__ = ["ArtifactManifestPort", "ArtifactPayloadPort"]


class ArtifactPayloadPort(Protocol):
    """CAS 上の Bytes に対する GC 操作。"""

    def verify(self, content_hash: ContentHash, *, manifest_exists: bool) -> ArtifactVerification:
        """Bytes と記録の突き合わせ。例外を投げず判定だけを返す（§1.14）。"""
        ...

    def delete_payload(self, content_hash: ContentHash) -> bool:
        """Bytes だけを消す。**Manifest は呼出側が保持する。**

        既に無い場合は `False` を返す。存在しないことを例外にしない。
        """
        ...


class ArtifactManifestPort(Protocol):
    """Manifest の参照と削除印。Manifest 自体は消さない。"""

    def find_by_content_hash(self, content_hash: ContentHash) -> ArtifactManifestRecord | None: ...

    def mark_payload_deleted(self, content_hash: ContentHash) -> None:
        """Bytes を消したことを記録する。**Metadata は残す。**"""
        ...
