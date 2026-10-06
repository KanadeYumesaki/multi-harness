"""ADR-007 マスキングPipelineの具象層。

判定権限を持つのは決定論スキャナ（Scan#1／#2）だけであり、
LLMはマスク範囲の提案しか行えない。
"""

from __future__ import annotations
