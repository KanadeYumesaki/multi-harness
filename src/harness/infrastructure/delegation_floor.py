"""非委任床Registryの読込み。**Domain層はFilesystemへ触れない。**

`design-source/registries/delegation-floor.yaml` が正本である（ADR-006）。
Domain側の `resolve_delegation` は読み込み済みの `FloorPolicy` を受け取るだけで、
Pathもyamlも知らない。層の依存規則をここで守る。

読めない・壊れている場合は例外で止める。床が読めないまま委任を評価すると、
床が空だったのか読めなかったのかを区別できない（不変条件#9）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml

from harness.domain.delegation import FloorPolicy

__all__ = ["DEFAULT_FLOOR_PATH", "load_floor_policy"]

DEFAULT_FLOOR_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "design-source" / "registries" / "delegation-floor.yaml"
)


def load_floor_policy(path: Path | None = None) -> FloorPolicy:
    """床RegistryをDomainの値オブジェクトへ読み込む。

    `rules` が空のRegistryは受け付けない。床が無いことと
    「床を読めていないこと」を同じ状態にしない。
    """
    target = path or DEFAULT_FLOOR_PATH
    document: dict[str, Any] = yaml.safe_load(target.read_text(encoding="utf-8"))

    rules = document.get("rules")
    if not rules:
        raise ValueError(f"{target}: rules が空である。床の無い委任評価を許さない")
    if "fail_closed_on_unknown" not in document:
        raise ValueError(f"{target}: fail_closed_on_unknown が無い")

    return FloorPolicy(
        rules=tuple(dict(rule) for rule in rules),
        fail_closed_on_unknown=bool(document["fail_closed_on_unknown"]),
        evaluation_order=str(document["evaluation_order"]),
    )
