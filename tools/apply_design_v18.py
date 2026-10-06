#!/usr/bin/env python3
"""設計書 v1.7 → v1.8 の適用（ADR-006 段階的委任 / ADR-007 LLM補助マスキング）。

表（Test Manifest、State名前空間、受入Gate）は**Registryから生成**して置換する。
手で写すと`lint_spec.py`の`MANIFEST_COUNT_MISMATCH`／`STATE_REGISTRY_DESIGN_MISMATCH`
に落ちるうえ、件数の手入力（不変条件#18違反）になる。

本文の改訂箇所は定数として保持し、適用を再現可能にする。冪等である。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# 本文の改訂
# ---------------------------------------------------------------------------

ADR_INSERT_BEFORE = "## 0.5 非機能目標"

ADR_006_007 = """### ADR-006：段階的委任を宣言型Predicateで行う

確認のたびに「次回以降どうするか」を決め、徐々に自動化したい運用要求がある。
§3.8.5は「前回と同じ場合の自動承認」を禁止しており、これは§0.1（開発体制1名）と
ADR-004に由来する意図的な判断である。

本ADRは禁止条項を撤廃せず、**判定根拠を置き換える**。

| | 引き続き禁止 | 本ADRが導入するもの |
|---|---|---|
| 判定根拠 | 過去の実行履歴との一致 | 人間が事前に承認した**宣言型Predicate**と、**いま解決されたPlan**の照合 |

`DelegationGrant`はFlagではなく、人間が承認して発行する署名付きArtifactである。
作成そのものが独立した承認Decisionであり、委任範囲を見ずに成立する経路を作らない。
成立時はその実行専用の`DerivedApprovalGrant`を毎回新規発行し、`DelegationGrant`を
実行権限として使い回さない。消費は§1.10のCAS経路をそのまま通る。

`--yes`、`--auto-approve`、`--force`、`--skip-approval`の禁止は維持する。

Predicateは§3.8.2の無効化項目を全て照合対象へ含める。任意コード、正規表現、Wildcard、
OR結合、否定条件を禁止した宣言型Schemaとし、Path指定は完全一致またはDirectory prefixの
明示列挙だけとする。

非委任床は`design-source/registries/delegation-floor.yaml`を正本とし、Resolverは本Registryを
読んで評価する。条件をコードへ直書きしない。**委任による委任の作成・拡大を禁止する**規則を
含み、これが無いと委任範囲が無限に拡大する。

失効とEffect開始の競合は、不変条件#3（Fencing Tokenの最終Storage書込み直前の再検証）と
同じ扱いとする。外部FSのAtomic ReplaceはSQLite Transactionへ含められないため、
`BEGIN IMMEDIATE`とCAS更新をDB内の**Effect線形化点**とし、Commit後にのみ外部Effectを開始する。
線形化点より後の失効はEffectを止められないため、`DELEGATION_REVOKED_AFTER_EFFECT_START`として
監査記録しReconciliation対象とする。「失効競合時は常にEffect 0件」とは主張しない。

**単独開発においてDelegationGrantは安全統制ではない。** Maker／Approver／Operatorが同一人物で
ある以上、委任は独立した第三者統制を提供しない。提供するのは確認操作の削減、自動承認された
実行の完全な監査証跡、委任範囲の明示化と即時失効の3点だけである（§22）。

### ADR-007：マスキングはLLMにSpanだけを提案させ、置換は決定論Rewriterが行う

個人情報・機密情報のマスキングにLLMを用いる。ただしLLMに本文を生成させると、
欠落・重複・並べ替え・意味改変を決定論的に検出できない。断片の順序と部分文字列一致を
検査する方式では、断片を原文の短い部分文字列へ縮める改変が検出できない。

したがってLLMには座標（Span）だけを返させる。

```text
Bytes読込
  → UCD 14.0割当済み符号位置Guard → 標準 unicodedata.normalize('NFC')
  → Deterministic Scan #1（権限を持つのはここと#2だけ）
       REJECT   : 保存・候補化を拒否。LLMへ渡さない
       CLEAN    : マスク不要
       MASKABLE : 決定論候補Spanを生成してLLMへ
  → LLMは {start, end, category} だけを返す
  → Span検証（Schema厳格・範囲・昇順・非重複・非入れ子・カテゴリ・数・比率）
  → 決定論RewriterがMask Tokenへ置換（LLMの返した文字列は使わない）
  → Deterministic Scan #2 が実質のGate
```

LLMが影響できるのは「どこを隠すか」だけであり、「何が書かれるか」には影響できない。
Prompt Injectionが成功しても返せるのはSpanだけであり、過少マスクはScan #2が検出する。

Secret、Password、API Key、Bearer Token、Session Cookie、Private Key、Cloud Credential、
`SPECIAL_CATEGORY_DATA`、`NATIONAL_ID`は**マスクせず即Reject**しLLMへ渡さない。
マスクして使う利益より、マスクのためにLLMへ渡す危険が大きい。境界は
`design-source/registries/masking-policy.yaml`を正本とする。

Python 3.11の`unicodedata`はUCD 14.0.0、3.12は15.0.0であり、15.0で追加された符号位置を
含む文字列はNFC結果が処理系間で食い違い得る。`source_normalized_hash`が処理系依存になると
ADR-006のPredicate照合まで処理系依存になる。Unicode Normalization Stability Policyは
「あるVersionで割当済みの文字だけから成る文字列の正規化形は以降のVersionでも変化しない」と
保証するため、**入力をUCD 14.0割当済み符号位置へ限定したうえで標準NFCを使う**。
NFCアルゴリズムを自前実装しない。割当済み集合は139,264 byteの固定Bitmap Artifactとして
同梱し、公式UCD入力Hashとともに由来を記録する。Artifact欠落・Hash不一致・未割当符号位置は
正規化前にFail-Closedとする。

Maskerはローカル実行のみとし、Network egress拒否、Telemetry無効、Prompt Log無効、
Core Dump無効、一時File禁止を起動前に検証する。Swapとメモリダンプへの対策は完全ではなく、
単一UIDでは`/proc/<pid>/mem`の読出しを防げない（§22）。

"""

SECTION_1_16_3_OLD = """### 1.16.3 Classification／Secret Scan順序

Artifact StoreへのPut前に、少なくとも以下を実行する。

1. Media Type／Encoding／Size検証
2. Data Classification判定
3. Secret Scan
4. PII／Regulated Data Scan。Phase Policyで必要な場合
5. Binary／Executable／Archive判定
6. Retention／Encryption Policy判定

`RESTRICTED`以上、Secret検出、分類不能、Scan不能はMVP0-Aでは保存・候補化を拒否する。Scan結果自体にSecret本文を含めず、Finding Type、位置の安全な要約、Rule ID、Scanner Version、Evidence Hashだけを保存する。"""

SECTION_1_16_3_NEW = """### 1.16.3 Classification／Secret Scan順序とマスキング

Artifact StoreへのPut前に、次の順序で処理する。判定権限を持つのは決定論スキャナだけであり、
LLMはマスク範囲の提案しか行えない（ADR-007）。

```text
1. UCD 14.0割当済み符号位置Guard → 標準 unicodedata.normalize('NFC')
   Artifact欠落・Hash不一致・未割当符号位置は正規化前にReject
2. Deterministic Scan #1
     Media Type／Encoding／Size検証
     Secret／Credential Scan
     Data Classification判定
     PII／Regulated Data Scan
     Binary／Executable／Archive判定
     → MASKABLE候補Spanを生成
3. 判定（境界は masking-policy.yaml が正本）
     REJECT   : 保存・候補化を拒否。LLMへ渡さない
     CLEAN    : そのままCAS保存
     MASKABLE : 4へ
4. ローカルLLM Maskerへ正規化済みテキストを渡し、Spanだけを受け取る
5. Span検証（決定論）。Scan #1候補とUnionし重複排除
6. 決定論RewriterがMask Tokenへ置換
7. Deterministic Scan #2  ← 実質のGate
     合格   : MaskedArtifactとしてCAS保存
     不合格 : Reject。原本もマスク版も候補化しない
```

`RESTRICTED`以上、Secret／Credential検出、`NATIONAL_ID`、`SPECIAL_CATEGORY_DATA`、分類不能、
Scan不能はマスクせずRejectする。停止、Timeout、形式不正、Span不正、Hash不一致も全てRejectとし、
マスクなしでの通過を一切許さない。

Scan結果自体にSecret本文を含めず、Finding Type、位置の安全な要約、Rule ID、Scanner Version、
Evidence Hashだけを保存する。マスクによる`data_classification`の自動Downgradeを禁止する（§1.13）。"""

BYPASS_OLD = "* 前回と同じ場合の自動承認"
BYPASS_NEW = (
    "* Predicateを持たない自動承認。および「前回と同じ」を根拠とする自動承認\n"
    "  （人間が事前に承認した宣言型Predicateといま解決されたPlanの照合による\n"
    "  `DelegationGrant`はADR-006で許可する。過去の実行履歴は判定根拠にしない）"
)

RESIDUAL_ANCHOR = "# 22. 残余リスク"

RESIDUAL_ADD = """
## 22.1 v1.8で追加した残余リスク

| # | 残余リスク | 内容 |
|---|---|---|
| R-06 | 単独開発における委任は安全統制ではない | Maker／Approver／Operatorが同一人物のため、`DelegationGrant`は独立した第三者統制を提供しない。提供するのは確認操作の削減、監査証跡、範囲の明示化と即時失効の3点だけである（ADR-004、ADR-006） |
| R-07 | 委任失効とEffectの競合は完全には防げない | DB Commitを線形化点とするため、線形化点より後の失効は進行中の外部Effectを止められない。`DELEGATION_REVOKED_AFTER_EFFECT_START`として監査しReconciliation対象とする |
| R-08 | LLMマスキングは決定論スキャナを代替しない | 多層防御の一層である。日本語PII検出精度は未測定であり、Scan #1／#2のRule Set品質が上限を決める |
| R-09 | ローカルMaskerのSwap／メモリダンプ対策は不完全 | 単一UIDでは`/proc/<pid>/mem`の読出しを防げない。`mlockall`はBest Effortであり保証ではない |
| R-10 | 正規化決定性はUnicodeの外部保証へ依存する | Unicode Normalization Stability Policyが「割当済み文字の正規化形は将来Versionでも不変」と保証することに依拠する。この保証が覆る場合、UCD 14.0 Guardだけでは処理系差異を吸収できない |
"""


# ---------------------------------------------------------------------------
# 表の生成
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "`null`"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, list):
        return f"`{'; '.join(str(v) for v in value)}`" if value else "`NONE`"
    return f"`{value}`"


def manifest_rows(cases: list[dict[str, Any]]) -> list[str]:
    rows = []
    for case in cases:
        sequence = case["expected_event_sequence"]
        sequence_cell = f"`{' → '.join(sequence)}`" if sequence else "`NONE`"
        rows.append(
            "| "
            + " | ".join(
                [
                    _cell(case["test_id"]),
                    _cell(case["case_id"]),
                    _cell(case["scenario"]),
                    _cell(case["expectation_descriptor_hash"]),
                    _cell(case["input_fixture_hash"]),
                    sequence_cell,
                    _cell(case["expected_subject_type"]),
                    _cell(case["expected_subject_id"]),
                    _cell(case["expected_state"]),
                    _cell(case["expected_error_code"]),
                    _cell(case["trace_scope"]),
                    _cell(case["assertions"]),
                    _cell(case["auto_reexecution_prohibited"]),
                    _cell(case["release_allowed"]),
                    _cell(case["manual_queue_expected"]),
                    _cell(case["fault_point"]),
                    _cell(case["evidence_status"]),
                    _cell(case["evidence_manifest_hash"]),
                ]
            )
            + " |"
        )
    return rows


def state_rows(namespaces: dict[str, list[str]]) -> list[str]:
    """同一State集合を持つ名前空間を1行へまとめる。

    §19.1の表は`lint_spec.load_state_namespaces`が見出し後40行しか読まないため、
    行数を抑える必要がある。
    """
    grouped: dict[tuple[str, ...], list[str]] = {}
    for name, members in namespaces.items():
        grouped.setdefault(tuple(members), []).append(name)
    rows = []
    for members, names in grouped.items():
        subject = "／".join(f"`{n}`" for n in names)
        rows.append(f"| {subject} | `{', '.join(members)}` |")
    return rows


def gate_rows(gates: list[dict[str, Any]], phase: str) -> list[str]:
    rows = []
    for gate in gates:
        if gate["phase"] != phase:
            continue
        number = int(gate["gate_id"].rsplit("-", 1)[-1])
        if gate["test_refs_mode"] == "ALL_IN_SCOPE":
            refs = "`Scope内Manifest全体`"
        else:
            refs = "`" + ", ".join(gate["test_refs"]) + "`"
        rows.append(f"| {number} | {gate['condition']} | {refs} |")
    return rows


def replace_block(text: str, header_line: str, new_rows: list[str], label: str) -> str:
    """表ヘッダ直後の連続する`|`行を丸ごと差し替える。"""
    index = text.find(header_line)
    if index < 0:
        raise ValueError(f"{label}: header not found")
    start = text.index("\n", index) + 1
    separator_end = text.index("\n", start) + 1  # |---|---| 行
    cursor = separator_end
    while cursor < len(text):
        line_end = text.find("\n", cursor)
        if line_end < 0:
            break
        line = text[cursor:line_end]
        if not line.startswith("|"):
            break
        cursor = line_end + 1
    return text[:separator_end] + "\n".join(new_rows) + "\n" + text[cursor:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", required=True, type=Path)
    parser.add_argument("--registries", type=Path, default=Path("design-source/registries"))
    parser.add_argument("--out", type=Path, help="未指定なら--designを上書き")
    args = parser.parse_args(argv)

    def load(name: str) -> Any:
        return yaml.safe_load((args.registries / name).read_text(encoding="utf-8"))

    cases = load("tests.yaml")["test_cases"]
    gates = load("gates.yaml")["gates"]
    namespaces = load("states.yaml")["state_namespaces"]
    schemas = load("schemas.yaml")["core_schemas"]

    text = args.design.read_text(encoding="utf-8")
    applied: list[str] = []

    # ---- ADR-006 / ADR-007 --------------------------------------------
    if "### ADR-006" not in text:
        text = text.replace(ADR_INSERT_BEFORE, ADR_006_007 + ADR_INSERT_BEFORE, 1)
        applied.append("ADR-006/007 を §0.4 へ追加")

    # ---- §1.16.3 -------------------------------------------------------
    if SECTION_1_16_3_OLD in text:
        text = text.replace(SECTION_1_16_3_OLD, SECTION_1_16_3_NEW, 1)
        applied.append("§1.16.3 をマスキングPipelineへ置換")

    # ---- §3.8.5 --------------------------------------------------------
    if BYPASS_OLD in text and "Predicateを持たない自動承認" not in text:
        text = text.replace(BYPASS_OLD, BYPASS_NEW, 1)
        applied.append("§3.8.5 バイパス禁止の1項を改訂")

    # ---- §22 -----------------------------------------------------------
    if "## 22.1 v1.8で追加した残余リスク" not in text:
        index = text.find(RESIDUAL_ANCHOR)
        if index < 0:
            raise ValueError("§22 not found")
        end = text.find("\n# ", index + len(RESIDUAL_ANCHOR))
        end = len(text) if end < 0 else end
        text = text[:end] + "\n" + RESIDUAL_ADD + text[end:]
        applied.append("§22 へ残余リスク R-06..R-10 を追加")

    # ---- 表の再生成 -----------------------------------------------------
    text = replace_block(
        text,
        "| Test ID | Case ID | Scenario | Expectation Descriptor Hash |",
        manifest_rows(cases),
        "Test Manifest",
    )
    applied.append(f"§19.1 Test Manifest表を再生成（{len(cases)}行）")

    text = replace_block(
        text,
        "| Expected Subject Type | State Enum（完全） |",
        state_rows(namespaces),
        "State namespace",
    )
    applied.append(f"§19.1 State名前空間表を再生成（{len(namespaces)}名前空間）")

    text = replace_block(text, "| # | Gate条件 | Test ID |", gate_rows(gates, "MVP0-A"), "Gate")
    mvp0a_gates = [g for g in gates if g["phase"] == "MVP0-A"]
    applied.append(f"§3.13 受入Gate表を再生成（{len(mvp0a_gates)}件）")

    # ---- 件数の直書きを解消 ---------------------------------------------
    scope_cases = [c for c in cases if "MVP0-A" in c["phase_scope"]]
    scope_test_ids = {c["test_id"] for c in scope_cases}
    counts = {
        r"MVP0-Aは次の\*\*36 Gate\*\*を全て満たす": (
            "MVP0-Aは`registries/gates.yaml`が定めるMVP0-A Gateを全て満たす"
        ),
        r"必要Gate      : 36": f"必要Gate      : {len(mvp0a_gates)}",
        r"必要Test ID   : 29": f"必要Test ID   : {len(scope_test_ids)}",
        r"必要Case      : 70": f"必要Case      : {len(scope_cases)}",
        r"規範Manifest全体は\*\*37 Test ID／86 Case\*\*である。": (
            f"規範Manifest全体は**{len({c['test_id'] for c in cases})} Test ID／"
            f"{len(cases)} Case**である。"
        ),
        r"MVP0-A Scope 70 Cases": f"MVP0-A Scope {len(scope_cases)} Cases",
        r"MVP0-A 36 Gates": f"MVP0-A {len(mvp0a_gates)} Gates",
        r"MVP0-A開始時点で次の20 SchemaをJSON Schemaとして同梱する。": (
            f"MVP0-A開始時点で`registries/schemas.yaml`が定める"
            f"{len(schemas)} SchemaをJSON Schemaとして同梱する。"
        ),
    }
    for pattern, replacement in counts.items():
        plain = pattern.replace("\\*", "*")
        if plain in text:
            text = text.replace(plain, replacement)
            applied.append(f"件数の直書きを解消: {plain[:36]}…")

    # ---- 旧件数の残存を解消 ----------------------------------------------
    # 歴史記述（v1.6の状態説明）は数値を残す。現行仕様としての主張だけを
    # Registry参照または導出値へ置換する。
    n_schema = len(schemas)
    stale = {
        # §0.4 冒頭サマリ。前半はv1.6の歴史、後半が現行仕様の主張
        "MVP0-Aの必要数は**70 Case／29 Test ID／36 Gate**である。": (
            f"MVP0-Aの必要数は`registries/`から導出する"
            f"（v1.8時点で{len(scope_cases)} Case／{len(scope_test_ids)} Test ID／"
            f"{len(mvp0a_gates)} Gate）。"
        ),
        # §1.9.1 Metric表
        "| Core 20 SchemaのValid／Invalid Suite失敗 |": (
            "| Core SchemaのValid／Invalid Suite失敗（件数は`registries/schemas.yaml`） |"
        ),
        # §15 共通Envelope制約
        "`AT-SCHEMA-COMPLETE-001`で20 Schema全件を検証する。": (
            "`AT-SCHEMA-COMPLETE-001`で`registries/schemas.yaml`のCore Schema全件を検証する。"
        ),
        # Schema Delivery Gate
        "* 20 Schema全てにValid／Invalid Exampleが存在。": (
            "* `registries/schemas.yaml`のCore Schema全てにValid／Invalid Exampleが存在。"
        ),
        "`AT-SCHEMA-COMPLETE-001`で20 SchemaのValid／Invalid Fixture、": (
            "`AT-SCHEMA-COMPLETE-001`でCore SchemaのValid／Invalid Fixture、"
        ),
        # §16.4 推定作業量
        "| Schema Fixture | 20 Schema × Valid／境界／拒否の各1件以上": (
            f"| Schema Fixture | Core Schema（v1.8時点{n_schema}件）× Valid／境界／拒否の各1件以上"
        ),
        # §17 段階別成果物
        "| MVP0-A | Domain Model、20 Schema、Migration、": (
            "| MVP0-A | Domain Model、Core Schema一式、Migration、"
        ),
        # §23.3 是正内容表。v1.7時点の値であることを明示する
        "| Release Scope単位判定。MVP0-Aは70 Case／29 Test ID |": (
            "| Release Scope単位判定。必要数はRegistryから導出する"
            f"（v1.7時点70 Case／29 Test ID、v1.8時点{len(scope_cases)} Case／"
            f"{len(scope_test_ids)} Test ID） |"
        ),
        # §26.1 判定単位
        "および全Phase合計の86 Caseを1回の判定へ要求する運用を禁止する。": (
            "および全Phase合計のCase数を1回の判定へ要求する運用を禁止する。"
        ),
    }
    for old, new in stale.items():
        if old in text:
            text = text.replace(old, new)
            applied.append(f"旧件数を解消: {old[:34]}…")

    # ---- 版数 -----------------------------------------------------------
    text = re.sub(r"(?<![0-9.])v1\.7(?![0-9])", "v1.8", text)
    legacy_kit = f"runtime-go-v{1}.{7}-kit"
    current_kit = f"runtime-go-v{1}.{8}-kit"
    for legacy, current in (
        (f"{legacy_kit}/verify_runtime_go.py", "verify_runtime_go.py"),
        (f"{legacy_kit}/registry-snapshot.json", "registry-snapshot.json"),
        (f"{current_kit}/verify_runtime_go.py", "verify_runtime_go.py"),
        (f"{current_kit}/registry-snapshot.json", "registry-snapshot.json"),
    ):
        text = text.replace(legacy, current)
    applied.append("本文の版数表記をv1.8へ更新し、Runtime GO参照をroot直下へ正規化")

    target = args.out or args.design
    target.write_text(text, encoding="utf-8")

    print(f"applied to {target}:")
    for item in applied:
        print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
