"""design-source/registries/chat-context-policy.yaml からの生成物。直接編集しない。

再生成: python tools/generate_chat_context_policy_code.py
整合検査: python tools/generate_chat_context_policy_code.py --check

source registry:
  chat-context-policy.yaml  sha256:e621e6986aec2ff7f7797dbc6b2850a4b2dd5befc476d3e991b6f604ee363733
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Final

CHAT_CONTEXT_POLICY_SOURCE_HASH: Final[str] = (
    "sha256:e621e6986aec2ff7f7797dbc6b2850a4b2dd5befc476d3e991b6f604ee363733"
)
CHAT_CONTEXT_POLICY_VERSION: Final[int] = 1


class TrustTier(Enum):
    """信頼境界の段（Owner Decision CPM-2-A）。正本は chat-context-policy.yaml。"""

    CONTROL = "CONTROL"
    INTERMEDIATE = "INTERMEDIATE"
    UNTRUSTED = "UNTRUSTED"


# 段の順位。**大小だけが意味を持つ。** 絶対値に意味を持たせない。
TIER_PRIORITY: Final[Mapping[TrustTier, int]] = MappingProxyType(
    {
        TrustTier.CONTROL: 3,
        TrustTier.INTERMEDIATE: 2,
        TrustTier.UNTRUSTED: 1,
    }
)

# 段が mandatory の**必要条件**を満たすか。十分条件ではない。
# MANDATORY_PREREQUISITES の検証済み Artifact が別途要る。
TIER_MANDATORY_CAPABLE: Final[Mapping[TrustTier, bool]] = MappingProxyType(
    {
        TrustTier.CONTROL: True,
        TrustTier.INTERMEDIATE: False,
        TrustTier.UNTRUSTED: False,
    }
)

# §1.16.4 の 7 Role と段の写像。Role名は ContextFragment の Schema enum と一致する。
MESSAGE_ROLE_TIER: Final[Mapping[str, TrustTier]] = MappingProxyType(
    {
        "SYSTEM_CONTROL": TrustTier.CONTROL,
        "DEVELOPER_CONTROL": TrustTier.CONTROL,
        "USER_TASK": TrustTier.INTERMEDIATE,
        "TOOL_DEFINITION": TrustTier.INTERMEDIATE,
        "VERIFIED_REFERENCE_DATA": TrustTier.INTERMEDIATE,
        "UNTRUSTED_ARTIFACT_DATA": TrustTier.UNTRUSTED,
        "UNTRUSTED_PROVIDER_DATA": TrustTier.UNTRUSTED,
    }
)

INTRA_TIER_ORDER: Final[str] = "SEQUENCE_NUMBER_DESC"
FINAL_MESSAGE_ORDER: Final[str] = "SEQUENCE_NUMBER_ASC"
UNRESOLVED_SELECTION_INPUT: Final[str] = "REJECT_SEND"
UNKNOWN_MESSAGE_ROLE: Final[str] = "REJECT_SEND"
CONTROL_AUTHORITY_GRANT: Final[str] = "NONE"
PROVIDER_OUTPUT_ROLE: Final[str] = "UNTRUSTED_PROVIDER_DATA"

# §1.16.4 が Control Role へ要求する検証項目。
MANDATORY_PREREQUISITES: Final[tuple[str, ...]] = (
    "VERIFIED_SIGNATURE",
    "VERIFIED_POLICY_HASH",
    "VERIFIED_ISSUER",
    "VERIFIED_EXPIRY",
)
