"""抽象Portの定義のみを置く層（CLAUDE.md §2）。

具象実装を持たず、`infrastructure/`・`adapters/`が実装する。
Application層はこの層の抽象だけへ依存する。
"""

from __future__ import annotations
