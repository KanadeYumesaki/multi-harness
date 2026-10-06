"""§1.8 共通監査イベント。

Event Typeの正本は `design-source/registries/events.yaml` であり、
`EventType` は `tools/generate_domain_registry_code.py` の生成物を再輸出する。
本Moduleへ Event 名を手書きしてはならない（不変条件#18）。
"""

from __future__ import annotations

from harness.domain._registry_generated import EventType

__all__ = ["EVENT_TYPE_COUNT", "EventType"]

# 件数はRegistry由来。定数へ数値を手入力しない。
EVENT_TYPE_COUNT: int = len(EventType)
