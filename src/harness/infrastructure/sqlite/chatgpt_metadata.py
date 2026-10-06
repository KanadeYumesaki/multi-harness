"""Nonsecret OAuth registration metadata, in the existing single SQLite DB."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping


class SqliteChatGptMetadata:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def load(self) -> dict[str, str]:
        return {
            str(row["name"]): str(row["value"])
            for row in self.connection.execute(
                "SELECT name, value FROM chatgpt_registration ORDER BY name"
            )
        }

    def save(self, metadata: Mapping[str, str]) -> None:
        if not set(metadata) <= {"host_id", "client_id", "subject_hash"}:
            raise ValueError("credential values cannot be persisted")
        for name, value in sorted(metadata.items()):
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError("invalid registration metadata")
            self.connection.execute(
                "INSERT INTO chatgpt_registration(name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, value),
            )
