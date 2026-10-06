#!/usr/bin/env python3
"""Provider の実装状況を、**Production の呼出経路**から測る（v2）。

## v1 の何が偽陽性だったか

v1 は `src/` 全体の識別子を 1 つの集合へ集め、名前があるかどうかで判定していた。
その集合には次が混ざる。

* 生成物（`_registry_generated.py`）が定義する語彙
* 試験用の代役（`mock_masker.py` の Fake）
* 誰も呼んでいない関数と、型注釈にしか出ない Class
* docstring と Comment に出てくる語

どれも「Production で実際に使える」ことを示さない。実際、ローカル UI の散文が
資格情報 Store の名を書いただけで、v1 は「Keyring Adapter がある」と判定した。

## v2 が根拠にするもの

**Production の入口から import で到達できる Module** に定義され、かつ
**どこかから呼ばれている**ことだけを実装の根拠にする。

| 根拠にする | 根拠にしない |
|---|---|
| 到達可能 Module の定義 + 呼出 | 名前があるだけ |
| Registry の値 | docstring・Comment |
| Owner が入れた値 | 生成物だけにある型 |
| | 試験専用の代役 |
| | 呼出元のない関数 |
| | 設計書の記述 |

docstring と Comment は AST の Constant であり、この Script は **文字列の中身を
1 度も読まない**。読まないから、書かれていても判定が動かない。

## Provider ID を持たない

この Script に Provider ID は 1 つも無い。`route-policy.yaml` から読む。
値も 1 つも作らない。Owner の入力欄が空なら、空のまま報告する。

## 静的に決まらないものを「無い」と書かない

動的 import が経路上にあれば、その領域は `UNDETERMINED_STATIC` にする。
**「たぶん実装済み」で `IMPLEMENTED` へ倒さない。**
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src/harness"
ROUTE_POLICY = ROOT / "design-source/registries/route-policy.yaml"
SNAPSHOT = ROOT / "registry-snapshot.json"
VALUE_PACKAGE = ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json"
ANSWER_PACKAGE = ROOT / "docs/decision/DCR-CHAT-PROVIDER-VALUES.json"
METHOD_PACKAGE = ROOT / "docs/decision/DCR-CHAT-PROVIDER-CONFIG.json"
OUT_JSON = ROOT / "docs/audit/provider-readiness-v2.json"
OUT_MD = ROOT / "docs/audit/provider-readiness-v2.md"

#: Production の入口。ここから import で辿れる Module が Production である。
ENTRY_MODULES = (
    "harness.presentation.cli",
    "harness.presentation.local_ui",
    "harness.infrastructure.runtime_facade",
)

#: 生成物。**語彙を定義するだけで、振る舞いを持たない。**
GENERATED_SUFFIX = "_generated.py"

#: 試験用の代役。Production に置かれていても実装の根拠にしない。
DOUBLE_MARKERS = ("mock_", "fake_", "stub_", "_mock", "_fake", "_stub")

#: 静的に辿れなくなる呼び方。見つけたらその領域を UNDETERMINED_STATIC にする。
DYNAMIC_IMPORT_CALLS = ("import_module", "__import__", "load_module", "exec_module")

#: 判定の語彙。**この 7 つだけを使う。**
STATUSES = (
    "IMPLEMENTED",
    "CONFIGURED",
    "OWNER_VALUE_MISSING",
    "PRODUCTION_CONTRACT_MISSING",
    "NOT_IMPLEMENTED",
    "UNDETERMINED_STATIC",
    "NOT_APPLICABLE",
)

#: 実装済みと言える判定。**偽陽性の試験はここへ落ちないことを見る。**
POSITIVE_STATUSES = frozenset({"IMPLEMENTED", "CONFIGURED"})


# ---------------------------------------------------------------------------
# Source の索引
# ---------------------------------------------------------------------------


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass
class SymbolIndex:
    """`src/harness` の定義と呼出を、Module ごとに索引する。

    **文字列の中身を読まない。** docstring も Comment も AST では Constant か
    そもそも捨てられており、ここでは一度も参照しない。
    """

    modules: dict[str, Path] = field(default_factory=dict)
    imports: dict[str, set[str]] = field(default_factory=dict)
    defined: dict[str, set[str]] = field(default_factory=dict)
    called: dict[str, set[str]] = field(default_factory=dict)
    dynamic_import_modules: set[str] = field(default_factory=set)

    @classmethod
    def build(cls) -> SymbolIndex:
        index = cls()
        for path in sorted(SRC.rglob("*.py")):
            name = _module_name(path)
            index.modules[name] = path
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            index.imports[name] = _imported_modules(tree)
            index.defined[name] = _top_level_definitions(tree)
            called, dynamic = _called_names(tree)
            index.called[name] = called
            if dynamic:
                index.dynamic_import_modules.add(name)
        return index

    # -- 分類 ---------------------------------------------------------------

    def is_generated(self, module: str) -> bool:
        return self.modules[module].name.endswith(GENERATED_SUFFIX)

    def is_double(self, module: str) -> bool:
        stem = self.modules[module].stem
        return any(marker in stem for marker in DOUBLE_MARKERS)

    def production_modules(self) -> set[str]:
        """入口から import で到達できる Module。**到達しないものは Production でない。**"""
        seen: set[str] = set()
        queue = [name for name in ENTRY_MODULES if name in self.modules]
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            # Submodule を import すると親 Package も import される。
            # 親を「到達しない」と数えると、Report の一覧が実態とずれる。
            parts = current.split(".")
            for depth in range(1, len(parts)):
                ancestor = ".".join(parts[:depth])
                if ancestor in self.modules and ancestor not in seen:
                    queue.append(ancestor)
            for target in self.imports.get(current, set()):
                if target in self.modules and target not in seen:
                    queue.append(target)
        return seen

    def evidence_modules(self) -> set[str]:
        """実装の根拠として数えてよい Module。生成物と代役を外す。"""
        return {
            name
            for name in self.production_modules()
            if not self.is_generated(name) and not self.is_double(name)
        }

    # -- 判定 ---------------------------------------------------------------

    def where_defined(self, symbol: str) -> set[str]:
        return {name for name, names in self.defined.items() if symbol in names}

    def where_called(self, symbol: str) -> set[str]:
        return {name for name, names in self.called.items() if symbol in names}

    def classify(self, symbol: str) -> tuple[str, str]:
        """1 つの Symbol の実装状況。**名前があるだけでは通さない。**"""
        defined = self.where_defined(symbol)
        if not defined:
            return "NOT_IMPLEMENTED", f"`{symbol}` はどの Module にも定義されていない"

        evidence = self.evidence_modules()
        production_defs = defined & evidence
        if not production_defs:
            generated = {name for name in defined if self.is_generated(name)}
            doubles = {name for name in defined if self.is_double(name)}
            unreached = defined - self.production_modules()
            if generated:
                return (
                    "PRODUCTION_CONTRACT_MISSING",
                    f"`{symbol}` は生成物 {sorted(generated)} にしかない。語彙であって実装ではない",
                )
            if doubles:
                return (
                    "PRODUCTION_CONTRACT_MISSING",
                    f"`{symbol}` は試験用の代役 {sorted(doubles)} にしかない",
                )
            return (
                "NOT_IMPLEMENTED",
                f"`{symbol}` は {sorted(unreached)} にあるが、Production の入口から到達しない",
            )

        callers = self.where_called(symbol) & evidence
        # 自分自身の定義 File だけで呼ばれていても、外から到達していれば経路はある。
        if not callers:
            return (
                "NOT_IMPLEMENTED",
                f"`{symbol}` は {sorted(production_defs)} に定義されているが、"
                "Production から 1 度も呼ばれていない",
            )
        return (
            "IMPLEMENTED",
            f"`{symbol}` は {sorted(production_defs)} に定義され、{sorted(callers)} から呼ばれる",
        )

    def any_implemented(self, symbols: tuple[str, ...]) -> tuple[str, str]:
        """候補のうち 1 つでも実装されていればそれを採る。全滅なら最も強い理由を返す。"""
        details: list[str] = []
        for symbol in symbols:
            status, detail = self.classify(symbol)
            if status == "IMPLEMENTED":
                return status, detail
            details.append(detail)
        contract_missing = [d for d in details if "生成物" in d or "代役" in d]
        if contract_missing:
            return "PRODUCTION_CONTRACT_MISSING", "／".join(contract_missing)
        return "NOT_IMPLEMENTED", "／".join(details)


def _imported_modules(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            # `from harness.x import y` の `y` が Module のこともある。
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _top_level_definitions(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _called_names(tree: ast.Module) -> tuple[set[str], bool]:
    """呼び出されている名前と、動的 import があるか。

    **型注釈は数えない。** `x: SecretRef` は使用であって呼出ではない。
    """
    called: set[str] = set()
    dynamic = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
        if isinstance(func, ast.Attribute | ast.Name):
            name = func.attr if isinstance(func, ast.Attribute) else func.id
            if name in DYNAMIC_IMPORT_CALLS:
                dynamic = True
    return called, dynamic


# ---------------------------------------------------------------------------
# 測定
# ---------------------------------------------------------------------------


@dataclass
class Findings:
    items: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        *,
        area: str,
        item: str,
        status: str,
        basis: str,
        source: str,
        detail: str,
        subject: str | None = None,
    ) -> None:
        if status not in STATUSES:
            raise SystemExit(f"UNKNOWN_STATUS: {status}")
        if not detail:
            raise SystemExit(f"NO_EVIDENCE: {area}/{item}")
        if basis not in {"call_graph", "registry", "owner_package", "absent", "schema"}:
            raise SystemExit(f"UNKNOWN_BASIS: {basis}")
        self.items.append(
            {
                "area": area,
                "subject": subject,
                "item": item,
                "status": status,
                "basis": basis,
                "source": source,
                "detail": detail,
            }
        )


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _value_fields() -> list[dict[str, Any]]:
    package = json.loads(VALUE_PACKAGE.read_text(encoding="utf-8"))
    fields: list[dict[str, Any]] = package["fields"]
    return fields


def _fields_for(questions: tuple[str, ...]) -> list[dict[str, Any]]:
    return [entry for entry in _value_fields() if entry["question"] in questions]


def _owner_value_status(questions: tuple[str, ...]) -> tuple[str, str]:
    """該当設問の入力欄が埋まっているか。**値そのものを Report へ出さない。**"""
    relevant = _fields_for(questions)
    if not relevant:
        return "NOT_APPLICABLE", f"{list(questions)} に対応する入力欄が Package に無い"
    missing = [entry["name"] for entry in relevant if entry["value"] is None]
    if not missing:
        return "OWNER_VALUE_PRESENT", f"{len(relevant)} 欄すべてに Owner 値がある"
    return (
        "OWNER_VALUE_MISSING",
        f"{len(missing)} / {len(relevant)} 欄が未入力（{'／'.join(missing)}）",
    )


#: 領域ごとに探す Symbol。**Provider ID を含まない。** 型名でも呼出で測る。
SECRET_SYMBOLS = ("SecretRef", "SecretReference", "SecretRefPort", "SecretResolverPort")
KEYRING_SYMBOLS = ("KeyringAdapter", "KeyringPort", "KeyringSecretStore", "SecretStorePort")
NETWORK_SYMBOLS = ("HttpProviderAdapter", "RemoteProviderAdapter", "HttpClient", "HttpTransport")
RETRY_SYMBOLS = ("RetryPolicy", "RetryController", "RetryExecutor", "retry_policy")
FAILOVER_SYMBOLS = ("FailoverRouter", "ProviderRouter", "FailoverPolicy", "select_provider")
IDEMPOTENCY_SYMBOLS = ("IdempotencyKey", "derive_idempotency_key", "IdempotencyKeyPort")


def measure() -> dict[str, Any]:
    index = SymbolIndex.build()
    route = yaml.safe_load(ROUTE_POLICY.read_text(encoding="utf-8"))
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    value_package = json.loads(VALUE_PACKAGE.read_text(encoding="utf-8"))
    findings = Findings()

    dynamic_in_production = sorted(index.dynamic_import_modules & index.production_modules())

    # -- Phase 1: Provider ごと ---------------------------------------------
    providers = route["providers"]
    contract_states = {entry["id"]: entry for entry in route["contract_states"]}
    route_classes = {entry["id"]: entry for entry in route["route_classes"]}
    for entry in providers:
        provider_id = str(entry["id"])
        source = "design-source/registries/route-policy.yaml"
        findings.add(
            area="PROVIDER",
            subject=provider_id,
            item="Registry 登録",
            status="CONFIGURED",
            basis="registry",
            source=source,
            detail="route-policy.yaml の providers に載っている",
        )
        findings.add(
            area="PROVIDER",
            subject=provider_id,
            item="enabled",
            status="CONFIGURED" if entry["enabled"] else "NOT_APPLICABLE",
            basis="registry",
            source=source,
            detail=f"enabled={entry['enabled']}",
        )
        state = str(entry["contract_state"])
        findings.add(
            area="PROVIDER",
            subject=provider_id,
            item="contract_state",
            status="CONFIGURED",
            basis="registry",
            source=source,
            detail=f"{state}（may_send={contract_states[state]['may_send']}）",
        )
        klass = str(entry["route_class"])
        findings.add(
            area="PROVIDER",
            subject=provider_id,
            item="route_class",
            status="CONFIGURED",
            basis="registry",
            source=source,
            detail=f"{klass}（external_egress={route_classes[klass]['external_egress']}）",
        )
        capabilities = list(entry.get("capabilities", ()))
        findings.add(
            area="PROVIDER",
            subject=provider_id,
            item="Capability 宣言",
            status="CONFIGURED" if capabilities else "NOT_IMPLEMENTED",
            basis="registry",
            source=source,
            detail=f"capabilities={capabilities}",
        )

        external = bool(route_classes[klass]["external_egress"])
        for label, questions in (
            ("Endpoint 設定", ("CVR-2",)),
            ("Model 設定", ("CVR-3",)),
            ("SecretRef 設定", ("CVR-7", "CVR-10")),
        ):
            if not external:
                findings.add(
                    area="PROVIDER",
                    subject=provider_id,
                    item=label,
                    status="NOT_APPLICABLE",
                    basis="registry",
                    source=source,
                    detail=f"route_class={klass} は外部へ出ない。この設定を要さない",
                )
                continue
            status, detail = _owner_value_status(questions)
            findings.add(
                area="PROVIDER",
                subject=provider_id,
                item=label,
                status=status,
                basis="owner_package",
                source="docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json",
                detail=detail,
            )

        # Adapter と呼出経路
        if not external:
            findings.add(
                area="PROVIDER",
                subject=provider_id,
                item="外部 Provider Adapter",
                status="NOT_APPLICABLE",
                basis="registry",
                source=source,
                detail=(
                    f"route_class={klass} は外部へ出ない。外部 Adapter の判定対象外である。"
                    "Mock の Adapter を外部 Adapter の根拠にしない"
                ),
            )
            findings.add(
                area="PROVIDER",
                subject=provider_id,
                item="Application からの呼出経路",
                status="NOT_APPLICABLE",
                basis="registry",
                source=source,
                detail=f"route_class={klass} は外部送信を伴わない",
            )
        else:  # pragma: no cover - 外部 Provider が登録されるまで到達しない
            status, detail = index.any_implemented(NETWORK_SYMBOLS)
            findings.add(
                area="PROVIDER",
                subject=provider_id,
                item="外部 Provider Adapter",
                status=status,
                basis="call_graph",
                source="src/harness/",
                detail=detail,
            )

    # -- Phase 2: Owner Value の欄ごと ---------------------------------------
    for entry in value_package["fields"]:
        status = "OWNER_VALUE_PRESENT" if entry["value"] is not None else "OWNER_VALUE_MISSING"
        findings.add(
            area="OWNER_VALUE",
            subject=entry["question"],
            item=entry["name"],
            status=status,
            basis="owner_package",
            source="docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json",
            detail=f"分類 {entry['category']}／形 {entry['shape']}／回答 {entry['answer']}",
        )

    # -- Phase 3: SecretRef / Keyring ---------------------------------------
    secret_items: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Production から呼べる SecretRef 型", SECRET_SYMBOLS),
        ("Keyring Adapter", KEYRING_SYMBOLS),
        ("SecretRef から値を取る Port", ("SecretResolverPort", "SecretStorePort")),
    )
    for label, symbols in secret_items:
        status, detail = index.any_implemented(symbols)
        findings.add(
            area="SECRET_REF",
            item=label,
            status=status,
            basis="call_graph",
            source="src/harness/",
            detail=detail,
        )
    status, detail = _owner_value_status(("CVR-7", "CVR-10"))
    findings.add(
        area="SECRET_REF",
        item="SecretRef の形式と Keyring service 名の値",
        status=status,
        basis="owner_package",
        source="docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json",
        detail=detail,
    )
    findings.add(
        area="SECRET_REF",
        item="失敗時の Fail-Closed",
        status="NOT_IMPLEMENTED",
        basis="absent",
        source="design-source/registries/route-policy.yaml",
        detail=(
            "`SECRET_RESOLVABLE` は許可条件として宣言されているが、"
            "解決を行う実装が無いので Fail-Closed の経路も無い"
        ),
    )

    # -- Phase 4: Provider Adapter の 8 条件 ---------------------------------
    adapter_conditions: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Provider 呼出 Port の実装", NETWORK_SYMBOLS),
        ("Application 経路からの到達", NETWORK_SYMBOLS),
        ("Endpoint・Model・SecretRef を受け取る", NETWORK_SYMBOLS),
        ("Network 実装", NETWORK_SYMBOLS),
        ("Timeout の扱い", ("TimeoutPolicy", "RequestTimeout", "with_timeout")),
        ("Provider 応答の検証", ("ProviderResponseValidator", "validate_provider_response")),
    )
    for label, symbols in adapter_conditions:
        status, detail = index.any_implemented(symbols)
        findings.add(
            area="PROVIDER_ADAPTER",
            item=label,
            status=status,
            basis="call_graph",
            source="src/harness/",
            detail=detail,
        )

    # -- Phase 5: Retry と Failover を分ける ---------------------------------
    text = ROUTE_POLICY.read_text(encoding="utf-8")
    retry_words = sorted({w for w in ("retry", "Retry") if w in text})
    findings.add(
        area="RETRY",
        item="Retry 可能な障害 ID",
        status="OWNER_VALUE_MISSING" if not retry_words else "UNDETERMINED_STATIC",
        basis="registry",
        source="design-source/registries/route-policy.yaml",
        detail=(
            "route-policy.yaml は Fallback の可否だけを定める"
            f"（`fallback_eligible_failures` {len(route['fallback_eligible_failures'])} 件／"
            f"`fallback_forbidden_failures` {len(route['fallback_forbidden_failures'])} 件）。"
            "**Fallback は別 Provider への切替であり、Retry は同じ Provider への再送である。**"
            "Retry の対象を定める記述は無い"
        ),
    )
    for label, questions in (
        ("Retry 回数", ("CVR-16",)),
        ("Timeout の値", ("CVR-16",)),
        ("Provider 別上限と共通上限", ("CVR-16",)),
    ):
        status, detail = _owner_value_status(questions)
        findings.add(
            area="RETRY",
            item=label,
            status=status,
            basis="owner_package",
            source="docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json",
            detail=detail,
        )
    status, detail = index.any_implemented(RETRY_SYMBOLS)
    findings.add(
        area="RETRY",
        item="Retry の実装",
        status=status,
        basis="call_graph",
        source="src/harness/",
        detail=detail,
    )

    priority_fields = sorted({key for entry in providers for key in entry} & {"priority", "order"})
    findings.add(
        area="FAILOVER",
        item="Failover 順位の Field",
        status="NOT_IMPLEMENTED" if not priority_fields else "CONFIGURED",
        basis="registry",
        source="design-source/registries/route-policy.yaml",
        detail=(
            "providers の各 entry に順位 Field が無い"
            f"（実在 Field: {sorted({k for e in providers for k in e})}）。"
            "**列挙順を順位として扱わない。順位は未定義である**"
            if not priority_fields
            else f"順位 Field {priority_fields} がある"
        ),
    )
    status, detail = _owner_value_status(("CVR-5",))
    findings.add(
        area="FAILOVER",
        item="Provider ごとの順位の値",
        status=status,
        basis="owner_package",
        source="docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json",
        detail=detail,
    )
    status, detail = index.any_implemented(FAILOVER_SYMBOLS)
    findings.add(
        area="FAILOVER",
        item="Failover の実装",
        status=status,
        basis="call_graph",
        source="src/harness/",
        detail=detail,
    )
    findings.add(
        area="FAILOVER",
        item="Retry を尽くしてから Failover する順序",
        status="NOT_IMPLEMENTED",
        basis="absent",
        source="src/harness/",
        detail=(
            "Retry も Failover も実装が無いので、両者の順序を持つ経路も無い。"
            "**Fallback 規則があることを Retry 規則があることと読み替えない**"
        ),
    )
    findings.add(
        area="FAILOVER",
        item="全 Provider 失敗時の停止",
        status="NOT_IMPLEMENTED",
        basis="absent",
        source="src/harness/",
        detail="Failover の実装が無いので、尽きたときの停止経路も無い",
    )
    findings.add(
        area="FAILOVER",
        item="Unknown Error 時の停止",
        status="CONFIGURED",
        basis="registry",
        source="design-source/registries/route-policy.yaml",
        detail=(
            f"`fallback_rules.unknown_state_is_not_fallback="
            f"{route['fallback_rules']['unknown_state_is_not_fallback']}`。"
            "契約はある。実行する経路は無い"
        ),
    )
    status, detail = index.any_implemented(IDEMPOTENCY_SYMBOLS)
    findings.add(
        area="IDEMPOTENCY",
        item="同一 Request の重複送信防止",
        status=status,
        basis="call_graph",
        source="src/harness/",
        detail=detail,
    )
    findings.add(
        area="IDEMPOTENCY",
        item="Provider 変更時の Idempotency Key の扱い",
        status="CONFIGURED",
        basis="owner_package",
        source="docs/decision/DCR-CHAT-PROVIDER-VALUES.json",
        detail=("`CVR-22-A`（宛先ごとに別 Key）が決まっている。契約はある。実装する経路は無い"),
    )

    # -- 静的に決まらないもの -------------------------------------------------
    if dynamic_in_production:
        for entry in findings.items:
            if entry["basis"] == "call_graph" and entry["status"] in POSITIVE_STATUSES:
                entry["status"] = "UNDETERMINED_STATIC"
                entry["detail"] += (
                    f"／Production に動的 import がある（{dynamic_in_production}）ので"
                    "静的には確定できない"
                )

    counts: dict[str, int] = dict.fromkeys(STATUSES, 0)
    for entry in findings.items:
        counts[entry["status"]] += 1

    return {
        "document_version": 2,
        "supersedes": {
            "path": "docs/audit/chat-provider-readiness.json",
            "sha256": _sha(ROOT / "docs/audit/chat-provider-readiness.json"),
            "note": "v1 は上書きしない。束縛されている Hash を動かさないためである",
        },
        "task_id": "TASK-LLM-CHAT-PROVIDER-READINESS-AUDIT-CORRECTION-001",
        "commit": subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "design_version": snapshot["design_version"],
        "design_sha256": snapshot["design_sha256"],
        "registry_snapshot_hash": snapshot["registry_snapshot_hash"],
        "schema_catalog_hash": snapshot["schema_catalog_hash"],
        "provider_id_source": {
            "path": "design-source/registries/route-policy.yaml",
            "sha256": _sha(ROUTE_POLICY),
            "provider_count": len(providers),
            "note": "この Script は Provider ID を 1 つも持たない。ここから読む",
        },
        "owner_value_package": {
            "path": "docs/decision/DCR-CHAT-PROVIDER-VALUE-INPUT.json",
            "sha256": _sha(VALUE_PACKAGE),
            "fields": len(value_package["fields"]),
            "entered": sum(1 for f in value_package["fields"] if f["value"] is not None),
        },
        "answer_package": {
            "path": "docs/decision/DCR-CHAT-PROVIDER-VALUES.json",
            "sha256": _sha(ANSWER_PACKAGE),
        },
        "method_package": {
            "path": "docs/decision/DCR-CHAT-PROVIDER-CONFIG.json",
            "sha256": _sha(METHOD_PACKAGE),
        },
        "source_index": {
            "modules": len(index.modules),
            "production_modules": len(index.production_modules()),
            "evidence_modules": len(index.evidence_modules()),
            "generated_modules": sorted(name for name in index.modules if index.is_generated(name)),
            "test_double_modules": sorted(name for name in index.modules if index.is_double(name)),
            "unreachable_modules": sorted(set(index.modules) - index.production_modules()),
            "dynamic_import_modules": dynamic_in_production,
        },
        "counts": counts,
        "items": findings.items,
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _markdown(doc: dict[str, Any]) -> str:
    L: list[str] = []
    w = L.append
    w("# Provider Readiness 監査 v2")
    w("")
    w("**判定は Production の呼出経路から出した。** 名前があるだけ、docstring に")
    w("書いてあるだけ、生成物にあるだけ、試験の代役にあるだけでは実装済みにしない。")
    w("")

    w("## 調査対象")
    w("")
    w("| 項目 | 値 |")
    w("|---|---|")
    w(f"| Commit | `{doc['commit']}` |")
    w(f"| 設計書 | v{doc['design_version']} `{doc['design_sha256'][:23]}…` |")
    w(f"| registry_snapshot_hash | `{doc['registry_snapshot_hash'][:23]}…` |")
    w(f"| schema_catalog_hash | `{doc['schema_catalog_hash'][:23]}…` |")
    w(f"| v1 Report（不変） | `{doc['supersedes']['sha256'][:23]}…` |")
    w("")
    source = doc["provider_id_source"]
    w("## Provider ID の出典")
    w("")
    w(f"`{source['path']}`（`{source['sha256'][:23]}…`）に {source['provider_count']} 件。")
    w(f"{source['note']}。")
    w("")
    package = doc["owner_value_package"]
    w(f"Owner 値の Package は `{package['path']}`（`{package['sha256'][:23]}…`）。")
    w(f"**{package['entered']} / {package['fields']} 欄が入力済み**である。")
    w("")

    w("## 何を根拠にしたか")
    w("")
    index = doc["source_index"]
    w("| 区分 | 件数 |")
    w("|---|---|")
    w(f"| `src/harness` の Module | {index['modules']} |")
    w(f"| Production の入口から到達する Module | {index['production_modules']} |")
    w(f"| 根拠に数えてよい Module | {index['evidence_modules']} |")
    w(f"| 生成物（語彙であって実装ではない） | {len(index['generated_modules'])} |")
    w(f"| 試験用の代役 | {len(index['test_double_modules'])} |")
    w(f"| 入口から到達しない Module | {len(index['unreachable_modules'])} |")
    w(f"| 動的 import を持つ Production Module | {len(index['dynamic_import_modules'])} |")
    w("")
    if index["generated_modules"]:
        w("生成物:")
        w("")
        for name in index["generated_modules"]:
            w(f"* `{name}`")
        w("")
    if index["test_double_modules"]:
        w("試験用の代役:")
        w("")
        for name in index["test_double_modules"]:
            w(f"* `{name}`")
        w("")

    w("## 判定の内訳")
    w("")
    w("| 判定 | 件数 |")
    w("|---|---|")
    for status, count in doc["counts"].items():
        w(f"| `{status}` | {count} |")
    w("")

    areas: dict[str, list[dict[str, Any]]] = {}
    for entry in doc["items"]:
        areas.setdefault(entry["area"], []).append(entry)
    for area, entries in areas.items():
        w(f"## {area}")
        w("")
        w("| 対象 | 項目 | 判定 | 根拠 | 詳細 |")
        w("|---|---|---|---|---|")
        for entry in entries:
            subject = f"`{entry['subject']}`" if entry["subject"] else "—"
            detail = entry["detail"].replace("|", "\\|")
            w(
                f"| {subject} | {entry['item']} | `{entry['status']}` "
                f"| {entry['basis']} | {detail} |"
            )
        w("")

    w("## この監査がしていないこと")
    w("")
    w("* Provider へ接続していない。Network 通信は 0 回である")
    w("* Keyring を読んでいない。API Key も Secret 値も扱っていない")
    w("* Provider ID も Endpoint も Model も Retry 値も **1 つも作っていない**")
    w("* 設計書の散文を実装の根拠にしていない")
    w("* v1 の Report を上書きしていない")
    w("")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provider Readiness 監査 v2")
    parser.add_argument("--json-out", type=Path, default=OUT_JSON)
    parser.add_argument("--md-out", type=Path, default=OUT_MD)
    parser.add_argument("--check", action="store_true", help="偽陽性が無いことだけを見る")
    args = parser.parse_args(argv)

    doc = measure()
    if args.check:
        positives = [e for e in doc["items"] if e["status"] in POSITIVE_STATUSES]
        bad = [e for e in positives if e["basis"] == "call_graph"]
        if bad:
            raise SystemExit(f"UNEXPECTED_IMPLEMENTED: {[e['item'] for e in bad]}")
        print(f"CHECK_OK: 呼出経路を根拠とする実装済み判定は {len(bad)} 件")
        return 0

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.md_out.write_text(_markdown(doc), encoding="utf-8")

    print(f"項目 {len(doc['items'])} 件")
    for status, count in doc["counts"].items():
        if count:
            print(f"  {status:30} {count}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
