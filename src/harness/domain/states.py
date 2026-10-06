"""§19.1 State名空間。

State名の正本は `design-source/registries/states.yaml`。
`StateNamespace` / `STATE_NAMESPACES` は生成物の再輸出である。

§1.4.2「本表をEvent／State遷移の正本とし」に基づく遷移表は
`transitions.py`（TASK-MVP0A-002以降）で定義する。本Moduleは名前空間の定義に限る。
"""

from __future__ import annotations

from harness.domain._registry_generated import STATE_NAMESPACES, StateNamespace

__all__ = [
    "STATE_NAMESPACES",
    "STATE_NAMESPACE_COUNT",
    "StateNamespace",
    "is_valid_state",
    "states_of",
]

STATE_NAMESPACE_COUNT: int = len(StateNamespace)


def states_of(namespace: StateNamespace) -> tuple[str, ...]:
    """名前空間に属するState名をRegistry記載順で返す。"""
    return STATE_NAMESPACES[namespace]


def is_valid_state(namespace: StateNamespace, state: str) -> bool:
    """`state` が当該名前空間の正規State名かどうかを返す。"""
    return state in STATE_NAMESPACES[namespace]
