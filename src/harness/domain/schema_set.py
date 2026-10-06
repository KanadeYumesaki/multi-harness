"""§1.11 Schema Set Hash。

> Schema Set：各Schema ID／Version／HashのCanonical配列を含める

`schema_set_hash` は `plan_content_hash` の入力である。Schemaが1件でも変われば
Plan Hashが変わり、既存Approvalが無効化される。これは意図した挙動であり、
「Schemaを差し替えたのに承認が生き続ける」ことを防ぐ。

Domain層の純粋関数とし、Filesystemへ触れない。Schema本体の読込みは
infrastructure層が担い、本Moduleは(name, version, content_hash)の列だけを受け取る。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from harness.domain.hashing import ContentHash, hash_canonical

__all__ = ["SchemaRef", "compute_schema_set_hash"]

_ARTIFACT_TYPE: Final[str] = "schema-set"
_SCHEMA_MAJOR: Final[int] = 1


@dataclass(frozen=True, slots=True)
class SchemaRef:
    """Schema Set の1要素。"""

    schema_name: str
    schema_version: str
    content_hash: ContentHash

    def __post_init__(self) -> None:
        if not self.schema_name:
            raise ValueError("schema_name must not be empty")
        if not self.schema_version:
            raise ValueError("schema_version must not be empty")


def compute_schema_set_hash(refs: Iterable[SchemaRef]) -> ContentHash:
    """Schema Set の Canonical Hash を返す。

    列挙順に依存しないよう `(schema_name, schema_version)` のCode Point昇順で
    整列してから Canonical 化する。Registryの記載順やFilesystemの列挙順が
    Hashへ漏れると、§1.11「Filesystem列挙順へ依存するPlan生成を禁止する」に反する。

    同一 `(schema_name, schema_version)` の重複は拒否する。どちらのHashが
    採用されたか判定できない状態を通さない（不変条件#9）。
    """
    ordered = sorted(refs, key=lambda ref: (ref.schema_name, ref.schema_version))
    seen: set[tuple[str, str]] = set()
    projection: list[dict[str, str]] = []
    for ref in ordered:
        key = (ref.schema_name, ref.schema_version)
        if key in seen:
            raise ValueError(f"duplicate schema in set: {ref.schema_name} {ref.schema_version}")
        seen.add(key)
        projection.append(
            {
                "schema_name": ref.schema_name,
                "schema_version": ref.schema_version,
                "content_hash": str(ref.content_hash),
            }
        )
    if not projection:
        raise ValueError("schema set must not be empty")
    return hash_canonical(projection, artifact_type=_ARTIFACT_TYPE, schema_major=_SCHEMA_MAJOR)
