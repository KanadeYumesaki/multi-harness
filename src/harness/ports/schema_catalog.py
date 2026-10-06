"""Schema Catalog の Port（CLAUDE.md §2）。

`schema_set_hash` は「その Record が**実際に使用した** Version 集合」である
（設計書 §15.2）。Application は使った Schema の名前だけを知っており、その
Version と Hash は Registry が持つ。**Application 側で版を書かない。**
"""

from __future__ import annotations

from typing import Protocol

from harness.domain.schema_set import SchemaRef

__all__ = ["SchemaCatalogPort"]


class SchemaCatalogPort(Protocol):
    def active_ref(self, schema_name: str) -> SchemaRef:
        """その Schema の active write version の参照を返す。

        登録が無ければ停止する。推測で版を決めない（不変条件#9）。
        """
        ...
