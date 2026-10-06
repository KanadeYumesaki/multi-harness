"""単一SQLite DBによる状態Store（ADR-001、不変条件#14）。

Artifact BytesだけがDB外のCASにあり、他の全状態は同一DBの同一Transaction境界へ置く。
"""

from __future__ import annotations
