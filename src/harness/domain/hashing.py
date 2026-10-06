"""§1.11 Hash規約。Domain分離付きSHA-256と`ContentHash`値オブジェクト。

    <hash> = SHA-256( "FDE-HARNESS/<artifact-type>/<schema-major>/" || CanonicalBytes )

Domain分離Prefixは、意味の異なるArtifactが同一Canonical Bytesを持った場合でも
Hashが衝突しないことを保証する。Prefixの構文を固定しないと
``artifact-type`` へ ``/`` を混ぜてDomainを詐称できるため、Token構文を強制する。

Hashは文字列のまま持ち回らず :class:`ContentHash` へ包む
（CLAUDE.md 層の依存規則「Path、Hash、Token量、Approval、Effectは値オブジェクト化する」）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Final

from harness.domain.canonical import canonicalize

__all__ = [
    "HASH_PROFILE_VERSION",
    "ContentHash",
    "InvalidContentHashError",
    "domain_separator",
    "hash_bytes",
    "hash_canonical",
]

HASH_PROFILE_VERSION: Final[int] = 1

_DOMAIN_PREFIX: Final[str] = "FDE-HARNESS"

# artifact-type は小文字英数とハイフンだけ。区切り子`/`を含められない。
_ARTIFACT_TYPE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class InvalidContentHashError(ValueError):
    """`sha256:<64 lowercase hex>` 形式でないHash文字列。"""


@dataclass(frozen=True, slots=True)
class ContentHash:
    """`sha256:<64 lowercase hex>` を表す不変値オブジェクト。"""

    algorithm: str
    hexdigest: str

    def __post_init__(self) -> None:
        if self.algorithm != "sha256":
            raise InvalidContentHashError(f"unsupported hash algorithm: {self.algorithm!r}")
        if not _SHA256_HEX_RE.match(self.hexdigest):
            raise InvalidContentHashError("digest must be 64 lowercase hexadecimal characters")

    @classmethod
    def parse(cls, value: str) -> ContentHash:
        """`sha256:<hex>` をParseする。大文字hexや前後空白は拒否する。"""
        algorithm, separator, hexdigest = value.partition(":")
        if not separator:
            raise InvalidContentHashError("missing 'sha256:' prefix")
        return cls(algorithm=algorithm, hexdigest=hexdigest)

    @classmethod
    def of_digest(cls, digest: bytes) -> ContentHash:
        if len(digest) != 32:
            raise InvalidContentHashError("sha256 digest must be 32 bytes")
        return cls(algorithm="sha256", hexdigest=digest.hex())

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.hexdigest}"


def domain_separator(artifact_type: str, schema_major: int) -> bytes:
    """`FDE-HARNESS/<artifact-type>/<schema-major>/` のUTF-8 Bytesを返す。"""
    if not _ARTIFACT_TYPE_RE.match(artifact_type):
        raise ValueError(f"artifact_type must match [a-z0-9]+(-[a-z0-9]+)*; got {artifact_type!r}")
    if schema_major < 1:
        raise ValueError(f"schema_major must be >= 1; got {schema_major}")
    return f"{_DOMAIN_PREFIX}/{artifact_type}/{schema_major}/".encode()


def hash_canonical(value: Any, *, artifact_type: str, schema_major: int) -> ContentHash:
    """Canonical化した値へDomain分離を適用してSHA-256を計算する。

    §1.11のHash（`plan_content_hash`、`execution_plan_hash`、Approval、Receipt、
    Schema Set等）はすべてこの関数を経由する。
    """
    separator = domain_separator(artifact_type, schema_major)
    digest = hashlib.sha256(separator + canonicalize(value)).digest()
    return ContentHash.of_digest(digest)


def hash_bytes(data: bytes) -> ContentHash:
    """原BytesのSHA-256。Artifact Binary Hash用でDomain分離を適用しない。

    §1.11「改行：Text Artifactの論理正規化はLF、Content Hashは保存BytesのHash」
    に従い、Artifact内容のHashは保存Bytesをそのまま入力とする。
    """
    return ContentHash.of_digest(hashlib.sha256(data).digest())
