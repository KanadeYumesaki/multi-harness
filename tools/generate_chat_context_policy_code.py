#!/usr/bin/env python3
"""design-source/registries/chat-context-policy.yaml から Domain層のCodeを生成する。

不変条件#18「件数を本文・コードへ手入力しない」を機構で強制する。Role別priorityと
選択順序の正本はRegistry YAMLだけであり、
`src/harness/domain/_chat_context_policy_generated.py` は本Scriptの出力に過ぎない。

## Domain層へ置く

`domain/context_budget.py` の `ContextFragment` は「Domainで推測して写像を作らない」と
書いている。**その文はいまも真である。** 生成物が持つのは推測した写像ではなく、
Hash対象の正本から生成した写像である。

生成物はI/Oを持たない純粋なデータであり、CLAUDE.md §2 のDomain層の条件を満たす。
Application層へ置くと、`chat_context_service` からの参照が
「application層は ports/ と domain/ だけを参照する」に反する。既存の検査を緩めない。

## Role語彙をここで手入力しない

7 Roleの正本は §1.16.4 であり、機械可読な形は `ContextFragment` のJSON Schemaである。
Schema側のenumを読んで突き合わせる。Registryが1 Roleでも欠けば、余分に持てば、
綴りを間違えれば停止する。

## 生成物のヘッダにSource Hashを埋める

`--check` は再生成結果とチェックイン済みFileをBytes比較する。Registryを編集して
再生成を忘れた状態を検出できて初めて機構になる。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema_catalog import active_write_versions, normalize_core_schemas

__all__ = ["main", "render"]

POLICY_FILE = "chat-context-policy.yaml"
ROLE_SCHEMA_NAME = "ContextFragment"
ROLE_FIELD = "message_role"

DEFAULT_REGISTRIES = Path("design-source/registries")
DEFAULT_ROOT = Path(".")
DEFAULT_OUT = Path("src/harness/domain/_chat_context_policy_generated.py")

#: Policyが必ず持つScalar項目。欠けたら停止する。**既定値で補完しない。**
REQUIRED_SCALARS = (
    "intra_tier_order",
    "final_message_order",
    "unresolved_selection_input",
    "unknown_message_role",
    "control_authority_grant",
    "provider_output_role",
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _py(value: object) -> str:
    """Python source literal。`ruff format` の既定に合わせて二重引用符で出す。"""
    return json.dumps(value, ensure_ascii=False)


def _role_vocabulary(registries: Path, root: Path) -> tuple[str, ...]:
    """§1.16.4 の Message Role を JSON Schema から読む。

    ここを定数で持つと、Registryと定数の**両方**を同じ間違いで書いたときに
    検査が素通りする。正本を1つに保つ。
    """
    doc = yaml.safe_load((registries / "schemas.yaml").read_text(encoding="utf-8"))
    entries = normalize_core_schemas(doc["core_schemas"])
    version = active_write_versions(entries).get(ROLE_SCHEMA_NAME)
    if version is None:
        raise SystemExit(f"{ROLE_SCHEMA_NAME} が schemas.yaml に無い")
    matched = [e for e in entries if e["schema_name"] == ROLE_SCHEMA_NAME]
    matched = [e for e in matched if e["schema_version"] == version]
    if len(matched) != 1:
        raise SystemExit(f"{ROLE_SCHEMA_NAME}@{version} の登録が {len(matched)} 件である")
    path = Path(matched[0]["path"])
    schema_path = path if path.is_absolute() else root / path
    if not schema_path.is_file():
        raise SystemExit(f"Schema Fileが無い: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    field = schema.get("properties", {}).get(ROLE_FIELD)
    if not isinstance(field, dict) or not field.get("enum"):
        raise SystemExit(f"{schema_path} に {ROLE_FIELD} の enum が無い")
    roles = tuple(str(value) for value in field["enum"])
    if len(set(roles)) != len(roles):
        raise SystemExit(f"{schema_path} の {ROLE_FIELD} enum が重複している")
    return roles


def _validate(policy: dict[str, Any], roles: tuple[str, ...]) -> None:
    """Fail-Closed。壊れたPolicyから生成しない。"""
    if policy.get("chat_context_policy_version") != 1:
        raise SystemExit(
            "chat_context_policy_version が 1 でない: "
            f"{policy.get('chat_context_policy_version')!r}"
        )

    tiers = policy.get("trust_tiers")
    if not isinstance(tiers, list) or not tiers:
        raise SystemExit("trust_tiers が空である")
    tier_ids = [t["id"] for t in tiers]
    if len(set(tier_ids)) != len(tier_ids):
        raise SystemExit(f"trust_tiers の id が重複している: {tier_ids}")
    priorities = [t["priority"] for t in tiers]
    for value in priorities:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SystemExit(f"priority が 1 以上の整数でない: {value!r}")
    if len(set(priorities)) != len(priorities):
        # 段が区別できなければ CPM-2-A の「3段」が成立しない。
        raise SystemExit(f"trust_tiers の priority が重複している: {priorities}")
    for tier in tiers:
        if not isinstance(tier.get("mandatory_capable"), bool):
            raise SystemExit(f"{tier['id']} の mandatory_capable が真偽値でない")

    mapping = policy.get("message_role_tiers")
    if not isinstance(mapping, list):
        raise SystemExit("message_role_tiers が無い")
    listed = [row["role"] for row in mapping]
    if len(listed) != len(set(listed)):
        raise SystemExit(f"message_role_tiers に重複した role がある: {listed}")
    missing = [role for role in roles if role not in set(listed)]
    if missing:
        raise SystemExit(f"message_role_tiers に無い Role がある: {missing}")
    unknown = [role for role in listed if role not in set(roles)]
    if unknown:
        raise SystemExit(f"§1.16.4 に無い role を書いている: {unknown}")
    for row in mapping:
        if row["tier"] not in set(tier_ids):
            raise SystemExit(f"{row['role']} の tier {row['tier']} が trust_tiers に無い")

    # mandatory を許す段が「いちばん信頼できる段」であること。Role 名をここへ
    # 手入力しない。段と `_CONTROL_ROLES` / `_UNTRUSTED_ROLES` の一致は
    # `tests/unit/application/test_chat_context_policy.py` が測る。
    capable = [t for t in tiers if t["mandatory_capable"]]
    top = max(tiers, key=lambda t: t["priority"])
    if [t["id"] for t in capable] not in ([], [top["id"]]):
        raise SystemExit(
            "mandatory_capable は最上位の段だけが取れる: "
            f"{[t['id'] for t in capable]} / top={top['id']}"
        )

    for key in REQUIRED_SCALARS:
        if not policy.get(key):
            raise SystemExit(f"{key} が無い。既定値で補完しない")
    if policy["control_authority_grant"] != "NONE":
        # Chat に Control Authority の検証経路は無い。あると書かせない。
        raise SystemExit("control_authority_grant は NONE 以外を取れない")
    if policy["provider_output_role"] not in set(roles):
        raise SystemExit(
            f"provider_output_role が §1.16.4 に無い: {policy['provider_output_role']}"
        )

    prerequisites = policy.get("mandatory_prerequisites")
    if not isinstance(prerequisites, list) or not prerequisites:
        raise SystemExit("mandatory_prerequisites が空である")
    if len(set(prerequisites)) != len(prerequisites):
        raise SystemExit(f"mandatory_prerequisites が重複している: {prerequisites}")


def render(registries: Path, root: Path) -> str:
    raw = (registries / POLICY_FILE).read_bytes()
    policy: dict[str, Any] = yaml.safe_load(raw.decode("utf-8"))
    policy_hash = f"sha256:{_sha256_hex(raw)}"
    roles = _role_vocabulary(registries, root)
    _validate(policy, roles)

    tiers = policy["trust_tiers"]
    role_tier = {row["role"]: row["tier"] for row in policy["message_role_tiers"]}

    lines: list[str] = []
    add = lines.append

    add('"""design-source/registries/chat-context-policy.yaml からの生成物。直接編集しない。')
    add("")
    add("再生成: python tools/generate_chat_context_policy_code.py")
    add("整合検査: python tools/generate_chat_context_policy_code.py --check")
    add("")
    add("source registry:")
    add(f"  {POLICY_FILE}  {policy_hash}")
    add('"""')
    add("")
    add("from __future__ import annotations")
    add("")
    add("from collections.abc import Mapping")
    add("from enum import Enum")
    add("from types import MappingProxyType")
    add("from typing import Final")
    add("")
    add("CHAT_CONTEXT_POLICY_SOURCE_HASH: Final[str] = (")
    add(f"    {_py(policy_hash)}")
    add(")")
    add(f"CHAT_CONTEXT_POLICY_VERSION: Final[int] = {policy['chat_context_policy_version']}")
    add("")
    add("")
    add("class TrustTier(Enum):")
    add('    """信頼境界の段（Owner Decision CPM-2-A）。正本は chat-context-policy.yaml。"""')
    add("")
    for tier in tiers:
        add(f"    {tier['id']} = {_py(tier['id'])}")
    add("")
    add("")
    add("# 段の順位。**大小だけが意味を持つ。** 絶対値に意味を持たせない。")
    add("TIER_PRIORITY: Final[Mapping[TrustTier, int]] = MappingProxyType(")
    add("    {")
    for tier in tiers:
        add(f"        TrustTier.{tier['id']}: {tier['priority']},")
    add("    }")
    add(")")
    add("")
    add("# 段が mandatory の**必要条件**を満たすか。十分条件ではない。")
    add("# MANDATORY_PREREQUISITES の検証済み Artifact が別途要る。")
    add("TIER_MANDATORY_CAPABLE: Final[Mapping[TrustTier, bool]] = MappingProxyType(")
    add("    {")
    for tier in tiers:
        add(f"        TrustTier.{tier['id']}: {tier['mandatory_capable']},")
    add("    }")
    add(")")
    add("")
    add("# §1.16.4 の 7 Role と段の写像。Role名は ContextFragment の Schema enum と一致する。")
    add("MESSAGE_ROLE_TIER: Final[Mapping[str, TrustTier]] = MappingProxyType(")
    add("    {")
    for role in roles:
        add(f"        {_py(role)}: TrustTier.{role_tier[role]},")
    add("    }")
    add(")")
    add("")
    for key in REQUIRED_SCALARS:
        add(f"{key.upper()}: Final[str] = {_py(policy[key])}")
    add("")
    add("# §1.16.4 が Control Role へ要求する検証項目。")
    add("MANDATORY_PREREQUISITES: Final[tuple[str, ...]] = (")
    for item in policy["mandatory_prerequisites"]:
        add(f"    {_py(item)},")
    add(")")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat Context Policy の Code を生成する")
    parser.add_argument("--registries", type=Path, default=DEFAULT_REGISTRIES)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="生成せず、チェックイン済みFileとの一致だけを検査する",
    )
    args = parser.parse_args(argv)

    rendered = render(args.registries, args.root)

    if args.check:
        if not args.out.exists():
            print(f"generated file missing: {args.out}", file=sys.stderr)
            return 1
        if args.out.read_text(encoding="utf-8") != rendered:
            print(
                f"generated file is stale: {args.out}\n"
                "run: python tools/generate_chat_context_policy_code.py",
                file=sys.stderr,
            )
            return 1
        print(f"{args.out}: up to date")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"generated: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
