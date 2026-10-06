"""Registry正本とCore Schema Fileを束ねる単一のSchema Registry。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_bytes
from harness.domain.schema_set import SchemaRef, compute_schema_set_hash

__all__ = ["CoreSchemaRegistry", "SchemaValidationResult"]


@dataclass(frozen=True, slots=True)
class SchemaValidationResult:
    """理由を握り潰さない構造化Schema検証結果。"""

    accepted: bool
    schema_name: str
    errors: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        return "ACCEPTED" if self.accepted else "REJECTED"


def catalog_entries(rows: list[dict[str, Any]]) -> list[tuple[str, str, str, bool]]:
    """`schemas.yaml`の行を`(schema_name, schema_version, path, active_write)`へ展開する。

    §15.9の複数Version登録形式と、それ以前の1 Version形式の両方を受ける。

    生成器側の正本は`tools/schema_catalog.py`である。Toolはspec生成・Snapshot生成の
    ためにRepository Root配下でしか動かず、`src/`をimportしない。本関数はRuntimeが
    同じRegistryを読むための実装であり、両者が食い違わないことは
    `tests/spec_lint/test_schema_version_tools.py`が機械検査する。
    """
    entries: list[tuple[str, str, str, bool]] = []
    for row in sorted(rows, key=lambda item: int(item["ordinal"])):
        name = str(row["schema_name"])
        versions = row.get("versions")
        if not versions:
            entries.append((name, str(row["schema_version"]), str(row["path"]), True))
            continue
        active = row.get("active_write_version")
        if not active:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"core schema {name}: active_write_version is required",
            )
        for entry in sorted(versions, key=lambda item: str(item["version"])):
            version = str(entry["version"])
            entries.append((name, version, str(entry["path"]), version == str(active)))
    return entries


class CoreSchemaRegistry:
    """`schemas.yaml`、Schema Set Hash、Record検証を一つの経路で扱う。"""

    def __init__(self, repo_root: Path, registries: Path | None = None) -> None:
        self._root = repo_root
        self._registries = registries or (repo_root / "design-source" / "registries")
        self._catalog = yaml.safe_load(
            (self._registries / "schemas.yaml").read_text(encoding="utf-8")
        )["core_schemas"]
        self._schemas: dict[tuple[str, str], Draft202012Validator] = {}
        self._refs: list[SchemaRef] = []
        # 論理Schema名 -> 書込み可能Version。読取り専用の旧版はここへ入れない。
        self._versions: dict[str, str] = {}
        self._load()

    @classmethod
    def bundled(cls) -> CoreSchemaRegistry:
        """同梱Registryを使う通常Runtime用Factory。"""
        return cls(Path(__file__).resolve().parents[4])

    def _load(self) -> None:
        for name, version, relative, active_write in catalog_entries(self._catalog):
            path = self._root / relative
            if not path.is_file():
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                    f"core schema file missing: {relative}",
                )
            try:
                raw = path.read_bytes()
                document = json.loads(raw.decode("utf-8"))
                Draft202012Validator.check_schema(document)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                    f"core schema cannot be loaded: {relative}: {exc}",
                ) from None
            if (name, version) in self._schemas:
                raise HarnessError(
                    ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, f"duplicate schema: {name}/{version}"
                )
            self._schemas[(name, version)] = Draft202012Validator(document)
            if active_write:
                if name in self._versions:
                    raise HarnessError(
                        ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                        f"core schema {name}: more than one active write version",
                    )
                self._versions[name] = version
            self._refs.append(
                SchemaRef(schema_name=name, schema_version=version, content_hash=hash_bytes(raw))
            )
        missing = sorted(
            {name for name, _, _, _ in catalog_entries(self._catalog)} - set(self._versions)
        )
        if missing:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"core schema without an active write version: {', '.join(missing)}",
            )

    @property
    def schema_names(self) -> tuple[str, ...]:
        """論理Schema名。同名の複数Versionを1件として数える。"""
        names: list[str] = []
        for ref in self._refs:
            if ref.schema_name not in names:
                names.append(ref.schema_name)
        return tuple(names)

    @property
    def refs(self) -> tuple[SchemaRef, ...]:
        return tuple(self._refs)

    def active_ref(self, schema_name: str) -> SchemaRef:
        """`SchemaCatalogPort`。active write version の参照を返す。

        登録が無ければ停止する。**推測で版を決めない。**
        """
        version = self._versions.get(schema_name)
        if version is None:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"core schema {schema_name} has no active write version",
            )
        for ref in self._refs:
            if ref.schema_name == schema_name and ref.schema_version == version:
                return ref
        raise HarnessError(
            ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
            f"core schema {schema_name}/{version} is not loaded",
        )

    def schema_set_hash(self) -> str:
        return str(compute_schema_set_hash(self._refs))

    def validate(self, schema_name: str, record: dict[str, Any]) -> SchemaValidationResult:
        """Catalog登録済みSchemaを用いてRecordを検証する。

        Versionを指定しない呼出しは書込み可能Version（`active_write_version`）で検証する。
        旧Versionでの検証はUpcast側の関心であり、そこは版を明示する。
        """
        active = self._versions.get(schema_name)
        validator = self._schemas.get((schema_name, active)) if active else None
        if validator is None:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"unknown core schema: {schema_name}",
            )
        errors = sorted(validator.iter_errors(record), key=lambda error: list(error.absolute_path))
        if not errors:
            return SchemaValidationResult(accepted=True, schema_name=schema_name)
        messages = tuple(
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors
        )
        return SchemaValidationResult(
            accepted=False,
            schema_name=schema_name,
            errors=messages,
        )

    def validate_or_raise(
        self, schema_name: str, schema_version: str, record: dict[str, object]
    ) -> None:
        """保存境界向けFail-Closed API。

        新規Recordの書込み先は`active_write_version`だけである（§15.9）。
        読取り専用Versionを書込みに使おうとした場合、および未登録Schema／未登録Version、
        Recordの不適合は`SCHEMA_CONDITIONAL_VIOLATION`で停止する。
        """
        expected_version = self._versions.get(schema_name)
        if expected_version is None:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"unknown core schema: {schema_name}",
            )
        if schema_version != expected_version:
            registered = (schema_name, schema_version) in self._schemas
            reason = (
                "read-only core schema version" if registered else "unsupported core schema version"
            )
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"{reason}: {schema_name}/{schema_version} "
                f"(active write version is {expected_version})",
            )
        result = self.validate(schema_name, record)
        if not result.accepted:
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                f"{schema_name}/{schema_version} validation failed: {result.errors[0]}",
            )
