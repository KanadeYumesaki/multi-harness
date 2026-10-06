"""Core Schema の読込みと検証（具象層）。

Schema本体はFilesystem上のJSON Fileであり、Domain層は`SchemaRef`だけを扱う。
"""

from __future__ import annotations
