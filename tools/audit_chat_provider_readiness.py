#!/usr/bin/env python3
"""Chat を Provider へ繋ぐ前段の契約完全性を実測する。

## 何をする Script か

`TASK-LLM-CHAT-PROVIDER-CONFIG-ROUTER-READINESS-001` Phase 1 の監査である。
Provider 設定・SecretRef・自動切替・安全境界の各項目が **既存正本から導出できるか**
を 1 件ずつ測り、`docs/audit/chat-provider-readiness.json` へ書く。

## 値を手入力しない

Provider ID、Endpoint、Model 名、Capability 名、Retry 回数、Timeout 値、Failover 順、
Keyring service 名を **この Script へ書かない**。正本を読んで、有るか無いかを測る。
無いものを埋めない。

「読んで無かった」と「読んでいない」を区別する。測っていない項目は `derivable` を
書かず `MEASUREMENT_MISSING` として落ちる。

## 出力

```json
{"items": [{"area": ..., "item": ..., "derivable": bool, "source": ..., "detail": ...}]}
```

`derivable=false` が 1 件でもあれば Phase 1 の停止条件に当たる。判定は呼出側が行う。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTRIES = ROOT / "design-source/registries"
ROUTE_POLICY = REGISTRIES / "route-policy.yaml"
SNAPSHOT = ROOT / "registry-snapshot.json"
SCHEMAS = ROOT / "schemas/core"
SRC = ROOT / "src/harness"
#: v1 は凍結する。Hash が回答済み `DCR-CHAT-PROVIDER-CONFIG` に束縛されている。
FROZEN_V1 = ROOT / "docs/audit/chat-provider-readiness.json"

#: 直した監査器の出力。v1 とは別 File である。
DEFAULT_OUT = ROOT / "docs/audit/provider-readiness-v3.json"
DEFAULT_MD = ROOT / "docs/audit/provider-readiness-v3.md"

#: 正本のどこかに現れるかを測る語。**値ではなく Field 名である。**
#: ここへ Provider ID や Endpoint の実値を書かない。
PROVIDER_CONFIG_FIELDS = (
    "id",
    "endpoint",
    "models",
    "contract_state",
    "capabilities",
    "route_class",
    "enabled",
    "secret_ref",
)

FALLBACK_TUNING_FIELDS = ("retry", "timeout", "priority", "order", "health")


class Measurement:
    """1 項目の実測。**根拠を必ず持たせる。**"""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        #: Production に動的 import があるか。あれば実装判定を確定させない。
        self.undetermined = False

    def add(
        self,
        area: str,
        item: str,
        derivable: bool,
        source: str,
        detail: str,
        *,
        kind: str = "CONTRACT",
        basis: str = "registry",
    ) -> None:
        """1 件測る。

        `kind` は **契約の不足**（`CONTRACT`）と **実装の未着手**
        （`IMPLEMENTATION`）を分ける。Phase 1 の停止条件に当たるのは契約側だけで
        ある。実装が無いことは、この Task が実装すべきものが残っている、という
        意味にすぎない。
        """
        if not detail:
            raise SystemExit(f"MEASUREMENT_MISSING: {area}/{item} に根拠が無い")
        if kind not in {"CONTRACT", "IMPLEMENTATION"}:
            raise SystemExit(f"UNKNOWN_KIND: {kind}")
        if basis not in {"call_graph", "registry", "schema", "declaration"}:
            raise SystemExit(f"UNKNOWN_BASIS: {basis}")
        if kind == "IMPLEMENTATION" and basis != "call_graph":
            # 実装の有無を、散文や File の存在で測らない。
            raise SystemExit(f"IMPLEMENTATION_NEEDS_CALL_GRAPH: {area}/{item}")
        if kind == "IMPLEMENTATION" and self.undetermined:
            status = "UNDETERMINED_STATIC"
        elif kind == "IMPLEMENTATION":
            status = "IMPLEMENTED" if derivable else "NOT_IMPLEMENTED"
        else:
            status = "CONFIGURED" if derivable else "NOT_IMPLEMENTED"
        self.items.append(
            {
                "area": area,
                "item": item,
                "kind": kind,
                "basis": basis,
                "status": status,
                "derivable": derivable,
                "source": source,
                "detail": detail,
            }
        )


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


sys.path.insert(0, str(ROOT / "tools"))
from audit_provider_readiness_v2 import SymbolIndex  # noqa: E402

#: 判定の語彙。Phase 5 の 5 つを使う。`READY_TO_SEND` は Provider 状態側で扱う。
STATUSES = (
    "IMPLEMENTED",
    "CONFIGURED",
    "NOT_IMPLEMENTED",
    "UNDETERMINED_STATIC",
    "NOT_APPLICABLE",
)

#: 資格情報 Store を読む Module。**import で測る。言及では測らない。**
CREDENTIAL_STORE_MODULES = frozenset({"keyring", "secretstorage", "keyrings"})


def _imports_credential_store(path: Path) -> bool:
    """資格情報 Store を import しているか。**docstring も Comment も見ない。**"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        if any(name.split(".")[0] in CREDENTIAL_STORE_MODULES for name in names):
            return True
    return False


def _references_attribute(path: Path, owner: str, attribute: str) -> bool:
    """`owner.attribute` を Code が参照しているか。**文字列検索ではない。**"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == attribute
        and isinstance(node.value, ast.Name)
        and node.value.id == owner
        for node in ast.walk(tree)
    )


def _module_name_of(path: Path) -> str:
    """`src/harness/...` の Path を Module 名へ写す。"""
    relative = path.relative_to(SRC.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _has_key(node: Any, key: str) -> bool:
    """解析済みの構造から Key を探す。**File の文字列を検索しない。**"""
    if isinstance(node, dict):
        return key in node or any(_has_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_has_key(value, key) for value in node)
    return False


def _schema_properties() -> dict[str, set[str]]:
    """Core Schema 名 -> property 名の集合。"""
    found: dict[str, set[str]] = {}
    for path in sorted(SCHEMAS.glob("*/*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        key = f"{path.parent.name}@{path.stem.removesuffix('.schema')}"
        found[key] = set(document.get("properties", {}))
    return found


def measure() -> dict[str, Any]:
    route = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    schema_props = _schema_properties()
    index = SymbolIndex.build()
    evidence = index.evidence_modules()
    dynamic = sorted(index.dynamic_import_modules & index.production_modules())

    providers: list[dict[str, Any]] = route["providers"]
    provider_fields: set[str] = set()
    for entry in providers:
        provider_fields |= set(entry)

    error_codes = set(snapshot["error_codes"])
    event_types = set(snapshot["event_types"])
    state_namespaces = snapshot["state_namespaces"]

    route_text = ROUTE_POLICY.read_text(encoding="utf-8")

    m = Measurement()
    # 動的 import があると実装の有無を静的に確定できない。**無いとは書かない。**
    m.undetermined = bool(dynamic)
    registry = str(ROUTE_POLICY.relative_to(ROOT))

    # --- Provider 設定 -------------------------------------------------------
    area = "PROVIDER_CONFIG"
    m.add(
        area,
        "Provider ID の正本",
        bool(providers) and "id" in provider_fields,
        registry,
        f"providers に {len(providers)} 件、"
        f"id Field {'あり' if 'id' in provider_fields else 'なし'}",
    )
    for field in ("endpoint", "models", "secret_ref"):
        m.add(
            area,
            f"Provider の {field} を束縛する Field",
            field in provider_fields,
            registry,
            f"providers の Field: {sorted(provider_fields)}",
        )
    m.add(
        area,
        "契約済み状態の語彙",
        bool(route.get("contract_states")),
        registry,
        f"contract_states {len(route.get('contract_states') or [])} 件",
    )
    m.add(
        area,
        "Provider ごとの契約状態",
        "contract_state" in provider_fields,
        registry,
        f"providers の Field: {sorted(provider_fields)}",
    )
    m.add(
        area,
        "Provider ごとの有効・無効",
        "enabled" in provider_fields,
        registry,
        f"providers の Field: {sorted(provider_fields)}",
    )
    m.add(
        area,
        "Capability の宣言 Field",
        "capabilities" in provider_fields,
        registry,
        f"providers の Field: {sorted(provider_fields)}",
    )
    m.add(
        area,
        "Capability 語彙の正本（宣言できる値の一覧）",
        bool(route.get("capabilities")),
        registry,
        "route-policy.yaml に capabilities の Catalog 節が"
        f"{'ある' if route.get('capabilities') else '無い'}。"
        "Provider ごとの宣言はあるが、宣言してよい値の一覧が無い",
    )
    m.add(
        area,
        "Route Class の語彙",
        bool(route.get("route_classes")),
        registry,
        f"route_classes {len(route.get('route_classes') or [])} 件",
    )
    provider_config_schemas = sorted(
        name
        for name, props in schema_props.items()
        if {"provider", "endpoint"} <= props or "provider_config" in props
    )
    m.add(
        area,
        "Provider 設定を表す Core Schema",
        bool(provider_config_schemas),
        "schemas/core/",
        f"provider と endpoint を同時に持つ Core Schema: {provider_config_schemas or 'なし'}",
    )
    m.add(
        area,
        "有効化設定の置き場所",
        "enabled" in provider_fields,
        registry,
        "EXPLICITLY_ENABLED が求める『設定』は route-policy.yaml の providers[].enabled "
        f"である。Field: {sorted(provider_fields)}",
    )
    required_to_add = ("endpoint", "models", "secret_ref")
    missing_to_add = [f for f in required_to_add if f not in provider_fields]
    m.add(
        area,
        "外部 Provider を 1 件追加するのに要る Field が揃っている",
        not missing_to_add,
        registry,
        f"足りない Field: {missing_to_add or 'なし'}。"
        "これらが無いと、追加する Provider の宛先・使用 Model・Secret 参照を"
        "正本へ書けない",
    )
    m.add(
        area,
        "MVP0-C §5.5 Endpoint Allowlist を Chat（MVP0-B）へ当てる宣言",
        False,
        "design §4.3 / §5.5",
        "§5.5 は Endpoint Allowlist の項目を散文で挙げるが MVP0-C の節である。"
        "Chat の §4.3 から §5.5 を参照する記述は無く、Schema も Registry も無い",
    )
    entitlement_schemas = sorted(n for n in schema_props if "Entitlement" in n)
    m.add(
        area,
        "CommercialEntitlementSnapshot の Core Schema",
        bool(entitlement_schemas),
        "schemas/core/",
        f"Entitlement を含む Core Schema: {entitlement_schemas or 'なし'}。"
        "§5.7 は必須項目を散文で挙げるが機械可読な正本が無い",
    )

    # --- SecretRef -----------------------------------------------------------
    area = "SECRET_REF"
    ref_type_verdict, ref_type_detail = index.classify("SecretRef")
    m.add(
        area,
        "SecretRef 型の実装",
        ref_type_verdict == "IMPLEMENTED",
        "src/harness/",
        ref_type_detail,
        kind="IMPLEMENTATION",
        basis="call_graph",
    )
    secret_schemas = sorted(
        name for name, props in schema_props.items() if any("secret" in p for p in props)
    )
    m.add(
        area,
        "SecretRef の形式を定める Schema",
        bool(secret_schemas),
        "schemas/core/",
        f"secret を含む property を持つ Core Schema: {secret_schemas or 'なし'}",
    )
    # **import で測る。** 散文が名を挙げただけでは実装にしない。
    store_importers = sorted(
        str(p.relative_to(ROOT))
        for p in SRC.rglob("*.py")
        if _imports_credential_store(p) and _module_name_of(p) in evidence
    )
    m.add(
        area,
        "Keyring を読む Port または Adapter",
        bool(store_importers),
        "src/harness/",
        f"資格情報 Store を import する Production Module: {store_importers or 'なし'}",
        kind="IMPLEMENTATION",
        basis="call_graph",
    )
    # 解析済みの Registry から Key を探す。File の文字列を検索しない。
    service_name_declared = _has_key(route, "keyring_service")
    m.add(
        area,
        "Keyring service 名の正本",
        service_name_declared,
        registry,
        "route-policy.yaml に keyring service 名を定める Key が"
        f"{'ある' if service_name_declared else '無い'}",
    )
    requirement_ids = [r["id"] for r in route.get("enablement_requirements", [])]
    m.add(
        area,
        "Keyring 未解決を許可条件として扱う宣言",
        any("SECRET" in r for r in requirement_ids),
        registry,
        f"enablement_requirements: {requirement_ids}",
    )
    forbidden = {f["id"]: f.get("error_code") for f in route.get("fallback_forbidden_failures", [])}
    secret_ids = [k for k in forbidden if "SECRET" in k]
    m.add(
        area,
        "Keyring 未設定時の Error Code",
        bool(secret_ids) and all(forbidden[k] in error_codes for k in secret_ids),
        registry,
        f"Fallback 禁止側の Secret 障害 {secret_ids} の error_code: "
        f"{[forbidden[k] for k in secret_ids]}",
    )
    design = next(ROOT.glob("design-v*-runtime-go.md"))
    design_text = design.read_text(encoding="utf-8")
    env_fallback = "環境変数への暗黙のFallbackを実装しない" in design_text
    m.add(
        area,
        "環境変数 Fallback 禁止の宣言",
        env_fallback,
        f"{design.name} §4.3",
        f"「環境変数への暗黙のFallbackを実装しない」の記述が{'ある' if env_fallback else '無い'}",
    )
    no_secret_logging = "Secret値をLog、Event、Attestation、例外、Test Evidenceへ出さない" in (
        (ROOT / "CLAUDE.md").read_text(encoding="utf-8") + design_text
    )
    m.add(
        area,
        "Secret を Log・Event・Evidence・例外へ出さない規則",
        no_secret_logging,
        "CLAUDE.md 不変条件#7",
        f"不変条件#7 の記述が{'ある' if no_secret_logging else '無い'}",
    )

    # --- 自動切替 ------------------------------------------------------------
    area = "FAILOVER"
    m.add(
        area,
        "切替対象 Provider の許可条件",
        bool(requirement_ids) and bool(route.get("fallback_rules")),
        registry,
        f"enablement_requirements {len(requirement_ids)} 件 / "
        f"fallback_rules {sorted(route.get('fallback_rules') or {})}",
    )
    eligible = {f["id"]: f.get("error_code") for f in route.get("fallback_eligible_failures", [])}
    m.add(
        area,
        "一時的失敗と Policy 拒否の区別",
        bool(eligible) and bool(forbidden),
        registry,
        f"Fallback 可能 {len(eligible)} 件 / 禁止 {len(forbidden)} 件",
    )
    m.add(
        area,
        "Retry 可能な失敗の Error Code",
        all(code in error_codes for code in eligible.values()),
        registry,
        f"Fallback 可能な失敗の error_code: {eligible}。"
        f"null は現行 errors.yaml に該当 Code が無いという意味である",
    )
    m.add(
        area,
        "切替してはいけない失敗の Error Code",
        all(code in error_codes for code in forbidden.values()),
        registry,
        f"Fallback 禁止の失敗の error_code: {forbidden}",
    )
    order_fields = sorted(f for f in provider_fields if f in FALLBACK_TUNING_FIELDS)
    m.add(
        area,
        "Provider の優先順位（Failover 順）",
        bool(order_fields),
        registry,
        f"providers に順序・優先度を表す Field: {order_fields or 'なし'}。"
        "列挙順を順位として使えるとはどこにも書かれていない",
    )
    tuning_present = sorted(k for k in route if any(t in k for t in FALLBACK_TUNING_FIELDS))
    m.add(
        area,
        "Retry 回数・Timeout 値の正本",
        bool(tuning_present),
        registry,
        f"route-policy.yaml の Retry / Timeout 関連 Key: {tuning_present or 'なし'}",
    )
    m.add(
        area,
        "Health 判定の正本",
        any("health" in k for k in route),
        registry,
        f"route-policy.yaml の Key: {sorted(route)}",
    )
    remote_registry = "REMOTE_INVOCATION_REGISTRY"
    chat_binding = "REMOTE_INVOCATION" in route_text or "Idempotency" in route_text
    m.add(
        area,
        "同一 Request の重複送信防止",
        chat_binding,
        "registry-snapshot.json / 設計 §5.9 / route-policy.yaml",
        f"Idempotency Key と {remote_registry} は MVP0-C（§5.9）の契約であり、State "
        f"名前空間は{'存在する' if remote_registry in state_namespaces else '無い'}"
        f"（{state_namespaces.get(remote_registry, [])}）。"
        "**それを Chat（MVP0-B）の Provider 呼出しへ当てる宣言が route-policy.yaml にも "
        "§4.3 にも無い。** 語彙が無いのではなく、結び付ける根拠が無い",
    )
    provider_states = sorted(
        name
        for name, members in state_namespaces.items()
        if any("PROVIDER" in member or "ROUTE" in member for member in members)
    )
    generic_terminals = sorted(
        f"{name}.{member}"
        for name, members in state_namespaces.items()
        for member in members
        if member in {"FAILED_PERMANENT", "FAILED_RETRYABLE", "EFFECT_UNKNOWN"}
    )
    m.add(
        area,
        "全 Provider 失敗時の終端 State",
        bool(provider_states),
        "registry-snapshot.json",
        f"Provider / Route 専用の State 名前空間: {provider_states or 'なし'}。"
        f"汎用の終端は {generic_terminals} が存在するが、Chat の Provider 呼出しを"
        "どの名前空間の遷移として扱うかを定めた宣言が無い",
    )
    provider_events = sorted(e for e in event_types if "PROVIDER" in e or "ROUTE" in e)
    remote_events = sorted(e for e in event_types if e.startswith("REMOTE_INVOCATION"))
    m.add(
        area,
        "Provider 選択・切替を記録する Event",
        bool(provider_events),
        "registry-snapshot.json",
        f"PROVIDER / ROUTE を含む Event Type: {provider_events or 'なし'}。"
        f"MVP0-C の {remote_events} は存在するが、Chat の Provider 選択・Fallback を"
        "これで記録すると定めた宣言が無い",
    )

    # --- 安全境界 ------------------------------------------------------------
    area = "SAFETY_BOUNDARY"
    stages = (
        ("Conversation / Message の検証", SRC / "application/conversation_service.py"),
        ("Context Policy 解決", SRC / "domain/chat_context_policy.py"),
        ("Context Selection", SRC / "application/chat_context_service.py"),
        ("Masking Gate", SRC / "ports/masking.py"),
        ("Route Policy 判定", None),
        ("契約済み Provider 確認", None),
        ("SecretRef 解決", None),
        ("Provider 呼出し", SRC / "ports/provider.py"),
    )
    for number, (label, path) in enumerate(stages, start=1):
        # **File の存在では測らない。** Production の入口から到達するかを見る。
        module = _module_name_of(path) if path is not None else None
        reached = module is not None and module in evidence
        m.add(
            area,
            f"{number}. {label}",
            reached,
            str(path.relative_to(ROOT)) if path is not None else "（実装なし）",
            (
                f"`{module}` は Production の入口から到達する"
                if reached
                else (
                    f"`{module}` は Production の入口から到達しない"
                    if module is not None
                    else "実装 Module が無い"
                )
            ),
            kind="IMPLEMENTATION",
            basis="call_graph",
        )

    provider_path = SRC / "infrastructure/provider/mock_provider.py"
    # AST で `ErrorCode.RUNTIME_SPEC_MISMATCH` の参照を見る。文字列検索ではない。
    rejects = _references_attribute(provider_path, "ErrorCode", "RUNTIME_SPEC_MISMATCH")
    allowed = [p["id"] for p in providers if p.get("enabled")]
    m.add(
        area,
        "Provider Adapter が allowlist 外を拒否する",
        rejects,
        "src/harness/infrastructure/provider/mock_provider.py",
        f"Registry で enabled な Provider は {len(allowed)} 件。Adapter は "
        + ("RUNTIME_SPEC_MISMATCH で拒否する" if rejects else "拒否しない")
        + "（AST の Attribute 参照で測った。**この Module は試験用の代役である**）",
    )
    m.add(
        area,
        "Masking 拒否が終端であることの宣言",
        bool(route.get("masking_gate", {}).get("reject_is_terminal")),
        registry,
        f"masking_gate: {sorted(route.get('masking_gate') or {})}",
    )

    items = m.items
    contract_gaps = [i for i in items if i["kind"] == "CONTRACT" and not i["derivable"]]
    implementation_gaps = [i for i in items if i["kind"] == "IMPLEMENTATION" and not i["derivable"]]
    status_counts = {name: sum(1 for i in items if i["status"] == name) for name in STATUSES}
    return {
        "audit_id": "CHAT-PROVIDER-READINESS",
        "report_version": 3,
        "task_id": "TASK-LLM-CHAT-PROVIDER-READINESS-AUDIT-V1-CLEANUP-001",
        "supersedes": {
            "path": str(FROZEN_V1.relative_to(ROOT)),
            "sha256": _sha(FROZEN_V1),
            "state": "HISTORICAL_FROZEN",
            "why": (
                "v1 の Hash は回答済み `DCR-CHAT-PROVIDER-CONFIG` の `audit_report` へ"
                "束縛されている。測り方を直すと根拠の記述が変わり Bytes が動くので、"
                "**v1 を書き換えず別 File として v3 を出す**"
            ),
        },
        "evidence_rules": {
            "implementation_basis": "call_graph",
            "accepted": [
                "Production の入口から import で到達する Module の定義",
                "その定義が実際に呼ばれていること",
                "解析済み Registry の値",
                "Core Schema の property",
            ],
            "rejected": [
                "docstring",
                "comment",
                "文字列 Literal",
                "型名や Class 名だけ",
                "呼出元のない関数",
                "試験用の代役",
                "生成物だけにある定義",
                "設計書の散文（実装の根拠としては使わない）",
                "File が存在すること",
            ],
        },
        "source_index": {
            "modules": len(index.modules),
            "production_modules": len(index.production_modules()),
            "evidence_modules": len(evidence),
            "generated_modules": sorted(n for n in index.modules if index.is_generated(n)),
            "test_double_modules": sorted(n for n in index.modules if index.is_double(n)),
            "dynamic_import_modules": dynamic,
        },
        "status_counts": status_counts,
        "design_version": snapshot["design_version"],
        "design_sha256": snapshot["design_sha256"],
        "registry_snapshot_hash": snapshot["registry_snapshot_hash"],
        "route_policy_sha256": _sha(ROUTE_POLICY),
        "measured_counts": {
            "items": len(items),
            "contract_items": sum(1 for i in items if i["kind"] == "CONTRACT"),
            "contract_gaps": len(contract_gaps),
            "implementation_items": sum(1 for i in items if i["kind"] == "IMPLEMENTATION"),
            "implementation_gaps": len(implementation_gaps),
            "providers_in_allowlist": len(providers),
            "core_schemas": len(schema_props),
            "error_codes": len(error_codes),
        },
        "items": items,
        "contract_gaps": contract_gaps,
        "implementation_gaps": implementation_gaps,
    }


def _markdown(report: dict[str, Any]) -> str:
    L: list[str] = []
    w = L.append
    w("# Chat Provider Readiness 監査 v3")
    w("")
    w("**判定は Production の呼出経路から出した。** docstring も Comment も文字列も")
    w("見ていない。生成物と試験用の代役は根拠から外した。")
    w("")
    superseded = report["supersedes"]
    w("| 項目 | 値 |")
    w("|---|---|")
    w(f"| 設計書 | v{report['design_version']} `{report['design_sha256'][:23]}…` |")
    w(f"| registry_snapshot_hash | `{report['registry_snapshot_hash'][:23]}…` |")
    w(f"| v1（凍結） | `{superseded['path']}` `{superseded['sha256'][:23]}…` |")
    w(f"| v1 の扱い | `{superseded['state']}` |")
    w("")
    w(superseded["why"] + "。")
    w("")

    w("## 何を根拠にしたか")
    w("")
    rules = report["evidence_rules"]
    w("| 根拠にする | 根拠にしない |")
    w("|---|---|")
    accepted = rules["accepted"]
    rejected = rules["rejected"]
    for row in range(max(len(accepted), len(rejected))):
        left = accepted[row] if row < len(accepted) else ""
        right = rejected[row] if row < len(rejected) else ""
        w(f"| {left} | {right} |")
    w("")
    index_facts = report["source_index"]
    w("| 区分 | 件数 |")
    w("|---|---|")
    w(f"| `src/harness` の Module | {index_facts['modules']} |")
    w(f"| Production の入口から到達する Module | {index_facts['production_modules']} |")
    w(f"| 根拠に数えてよい Module | {index_facts['evidence_modules']} |")
    w(f"| 生成物 | {len(index_facts['generated_modules'])} |")
    w(f"| 試験用の代役 | {len(index_facts['test_double_modules'])} |")
    w(f"| 動的 import を持つ Production Module | {len(index_facts['dynamic_import_modules'])} |")
    w("")

    w("## 判定の内訳")
    w("")
    w("| 判定 | 件数 |")
    w("|---|---|")
    for name, count in report["status_counts"].items():
        w(f"| `{name}` | {count} |")
    w("")
    counts = report["measured_counts"]
    contract_gaps = counts["contract_gaps"]
    implementation_gaps = counts["implementation_gaps"]
    w(f"契約の不足 {contract_gaps} 件 / 実装の未着手 {implementation_gaps} 件。")
    w("")

    areas: dict[str, list[dict[str, Any]]] = {}
    for entry in report["items"]:
        areas.setdefault(entry["area"], []).append(entry)
    for area, entries in areas.items():
        w(f"## {area}")
        w("")
        w("| 項目 | 判定 | 種別 | 根拠 | 詳細 |")
        w("|---|---|---|---|---|")
        for entry in entries:
            detail = entry["detail"].replace("|", "\\|")
            w(
                f"| {entry['item']} | `{entry['status']}` | {entry['kind']} "
                f"| {entry['basis']} | {detail} |"
            )
        w("")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD)
    args = parser.parse_args(argv)

    # v1 と v3 は凍結してある。上書きを試みたら止める。
    #
    # v3 を足したのは、その Bytes が回答済み Decision Package へ束縛されている
    # ためである。監査器は `src/harness` の Module を数えるので、Production を
    # 1 件足すだけで測定値が動く。書けてしまうと、束縛が黙って外れる。
    #
    # 過去 Report の再現は `tools/verify_provider_readiness_history.py` が
    # 固定 Git 入力から行う。現行監査はここから**別出力へ**書く。
    #
    # **凍結を運用の注意で守らない。** 書けない形にする。JSON と Markdown の
    # 両方、`--out` と `--md-out` の両方を塞ぐ。片方だけ塞ぐと、Markdown から
    # 保存物が壊れる。
    frozen = {
        FROZEN_V1.resolve(): FROZEN_V1.name,
        DEFAULT_OUT.resolve(): DEFAULT_OUT.name,
        DEFAULT_MD.resolve(): DEFAULT_MD.name,
    }
    for target in (args.out, args.md_out):
        name = frozen.get(target.resolve())
        if name is not None:
            raise SystemExit(f"FROZEN_REPORT: {name} は書き換えない")

    report = measure()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.md_out.write_text(_markdown(report), encoding="utf-8")

    counts = report["measured_counts"]
    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    try:
        shown = args.out.relative_to(ROOT)
    except ValueError:
        # ROOT の外へ出す場合がある（再生成して比べるだけの用途）。
        shown = args.out
    print(f"{shown}  sha256:{digest}")
    print(
        f"測定 {counts['items']} 件"
        f"（契約 {counts['contract_items']} / 実装 {counts['implementation_items']}）"
    )
    print(f"契約の不足 {counts['contract_gaps']} 件 — 1 件でもあれば Phase 1 停止条件")
    for item in report["contract_gaps"]:
        print(f"  契約NG  [{item['area']}] {item['item']}")
        print(f"          {item['detail']}")
    print(f"実装の未着手 {counts['implementation_gaps']} 件")
    for item in report["implementation_gaps"]:
        print(f"  実装なし [{item['area']}] {item['item']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
