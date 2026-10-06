"""Application層。Transaction境界とPortの組合せを持つ。

Domainは純粋、Infrastructureは具象、ここはその接続である。
不変条件#15「Transaction所有者はApplication層のUnit of Work」に従い、
`BEGIN IMMEDIATE` を開くのはこの層だけである。
"""

from __future__ import annotations
