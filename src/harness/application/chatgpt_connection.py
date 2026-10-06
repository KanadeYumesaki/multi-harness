"""Login operations do not grant generation or file-write approval."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from harness.ports.chatgpt import ChatGptAuthPort, ChatGptMetadataPort
from harness.ports.cli_workbench import IdSourcePort
from harness.ports.unit_of_work import UnitOfWorkPort


class ChatGptConnectionService:
    def __init__(
        self,
        *,
        auth: ChatGptAuthPort,
        metadata: ChatGptMetadataPort,
        uow: UnitOfWorkPort,
        ids: IdSourcePort,
    ) -> None:
        self.auth = auth
        self.metadata = metadata
        self.uow = uow
        self._lock = threading.RLock()
        with self._lock, self.uow.begin_immediate():
            saved = self.metadata.load()
            if not saved:
                saved = {"host_id": "urn:uuid:" + ids.new_id()}
                self.metadata.save(saved)
        auth.bind(saved)

    def remember(self, metadata: Mapping[str, str]) -> None:
        # Callback has its own serialized connection; tokens are absent by contract.
        with self._lock, self.uow.begin_immediate():
            self.metadata.save(metadata)

    def status(self) -> dict[str, Any]:
        return self.auth.status()

    def start(self, *, new_account: bool = False) -> dict[str, Any]:
        return self.auth.start(new_account=new_account)

    def disconnect(self) -> dict[str, Any]:
        return self.auth.disconnect()

    def close(self) -> None:
        self.auth.close()
