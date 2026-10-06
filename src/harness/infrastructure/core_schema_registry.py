"""旧import経路の互換Facade。

Schema Catalog、Schema Set Hash、保存前検証は
`harness.infrastructure.schema.registry.CoreSchemaRegistry`へ一元化した。
"""

from __future__ import annotations

from harness.infrastructure.schema.registry import CoreSchemaRegistry

__all__ = ["CoreSchemaRegistry"]
