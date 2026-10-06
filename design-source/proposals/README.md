# 仕様変更提案（未承認・未適用）

このディレクトリは**提案置き場**である。設計書v1.7、`design-source/registries/`、
`spec/`、`registry-snapshot.json` のいずれも変更していない。承認後に§6の手順で正本へ取り込む。

## 0. 状態

| ADR | 状態 |
|---|---|
| ADR-006 段階的委任 | **v4**。Event名をRegistryへ統一。v1.8反映待ち |
| ADR-007 LLM補助マスキング | **v5**。UCD14割当済みGuard＋標準NFC、依存なしGrapheme guard、LLM追加ratio、Containment Expansionを反映。v1.8反映待ち |

## 1. ファイル

| ファイル | 内容 |
|---|---|
| `ADR-006-progressive-delegation.md` | 段階的委任。確認のたびに「次回以降どうするか」を決め徐々に自動化する |
| `ADR-007-llm-assisted-masking.md` | LLM補助マスキング。**LLMはSpanだけ返し、置換は決定論Rewriterが行う** |
| `delegation-floor.draft.yaml` | 非委任床11規則の正本案（ADR-006 §6） |
| `masking-policy.draft.yaml` | REJECT／MASKABLE境界とSpan制約の正本案（ADR-007 §6） |
| `registry-additions.draft.yaml` | Event／Error／State／Schema／Case／Gateの追加案 |

## 2. レビュー条件への対応

### ADR-006（条件付きGO → 全条件反映済み）

| # | 条件 | 反映箇所 |
|---|---|---|
| 1 | Predicateを宣言型Canonical JSON Schemaにする | §4.2。任意コード・正規表現・Wildcard・OR・否定を禁止 |
| 2 | §3.8.2の無効化項目を全て照合対象に含める | §4.3。**16項目**を必須化（Scan結果を含む） |
| 3 | DelegationGrantを一回限りApprovalGrantと分離 | §3、§4。同一SQLite内の別Schema・別論理Store・別Lifecycle |
| 4 | 実行ごとに一回限りの派生Approvalを発行 | §5.2。`DerivedApprovalGrant`を毎回新規発行。Grant使い回し禁止 |
| 5 | 失効・期限・回数・署名不正・不一致を全てFail-Closed | §5.3。判定不能も不成立 |
| 6 | 失効とEffect開始の競合を最終Effect GateとCASで検証 | §5.4。**v1に欠けていた**。不変条件#3と同じ扱い |
| 7 | 非委任床をRegistry化 | §6＋`delegation-floor.draft.yaml`（11規則） |
| 8 | 署名鍵のTrust Anchor／Rotation／Revocation／Audience／Tenant／Issuer | §7 |

### ADR-007（v1破棄・全面改訂）

**破棄理由**：v1の「Mask Tokenで分割した断片が原文に同順で部分文字列として現れる」検査は
**欠落を検出できない**。断片を原文の短い部分文字列に縮めても条件が成立し続けるため、
本文の一部が消えても合格する。重複・並べ替えも同様。

| 指摘 | 反映 |
|---|---|
| LLMに本文を返させない。Span提案方式 | §1〜§4。LLMは`{start,end,category}`のみ返す。置換は決定論Rewriter |
| Secret／Credential／NATIONAL_IDは即Reject、LLMへ送らない | §6＋`masking-policy.draft.yaml`の`reject_categories`（13種） |
| MASKABLE／REJECTをRegistryで明文化 | `masking-policy.draft.yaml` |
| 停止・Timeout・形式不正・Span不正をPassにしない | §3、`failure_handling.never_pass_unmasked: true` |
| Local LLMのNetwork／Telemetry／ログ／Swap／Core Dump対策 | §7＋`masker_isolation` |
| Prompt Injectionでマスク方針を変更できない構造 | §5。方針はControl Planeのみ由来。LLMはSpanしか返せない |
| 元Bytes非保存。Hash・Version・Span証跡のみ | §8 |
| Unicode／Python Matrix／risky Grapheme／重複Span／比率境界／Isolation／LLM未使用の試験 | §11。RegistryのCase定義を正とする |

## 3. 要求と設計の対応

| 要求 | 対応 |
|---|---|
| 最初は全て確認 | DelegationGrantが無い状態が既定。全Actionが人間承認 |
| 確認時に次回以降を決める | 承認画面で選択。委任作成は**独立した第2のDecision** |
| 徐々に自動化される | 一致するGrantがあるActionだけ`POLICY_DELEGATED`で自動承認 |
| 後で確認できる | `harness delegation list / show / audit` |
| 後で変更できる | `revoke`／`narrow`は承認不要・即時。**拡大は不可** |
| 安全ゲート | 非委任床11規則＋強制再確認（既定30日／20回）＋最終Effect Gateでの失効再検証 |
| 個人情報・機密のマスキング | 決定論スキャン#1 → LLMがSpan提案 → Span検証 → 決定論Rewriter → スキャン#2 |
| Python 3.11/3.12の正規化決定性 | UCD14割当済み符号位置Guard後に標準`unicodedata.normalize('NFC')`を使用 |
| Grapheme依存 | MVP0-Aはrisky codepoint guardのみ。UAX #29完全実装はMVP0-B以降 |
| Mask ratio | Scan#1候補を分子から除外し、LLM追加Unionだけを0.60 Circuit Breakerで検査 |

## 4. 設計書の変更点

### 4.1 §3.8.5「バイパス禁止」の1行を改訂

| 現行 | 改訂案 |
|---|---|
| 前回と同じ場合の自動承認 | Predicateを持たない自動承認。および「前回と同じ」を根拠とする自動承認 |

`--yes`／`--auto-approve`／`--force`／`--skip-approval`の禁止は**維持**する。
CLAUDE.md／AGENTS.md 不変条件#11も同じ改訂が必要。

### 4.2 §1.16.3「Classification／Secret Scan順序」を置換

現行6段をADR-007 §1の5段パイプラインへ置換する。`REJECT`の扱いは変更しない。

### 4.3 §22 残余リスクへ追加

* 単独開発におけるDelegationGrantは**安全統制ではなく監査証跡付きの効率化機構**である（ADR-004）。
* LLMマスキングは多層防御の一層であり決定論スキャナを代替しない。日本語PII検出精度は未測定。
* ローカルMaskerのSwap／メモリダンプ対策は完全でない。単一UIDでは`/proc/<pid>/mem`を防げない。

### 4.4 件数直書きの解消

Core Schemaが20→22になるため、§1.15「20 Schema」とGate 30「20 Core Schema」の
件数直書きをRegistry参照へ変更する（不変条件#18）。

## 5. 連鎖影響

```text
設計書 v1.8 → design_sha256 変化
  → registry-snapshot.json                    （build_registry_snapshot.py）
  → spec/ 33ファイル + spec-manifest          （build_spec_shards.py）
  → runtime-go-manifest.MVP0-A.template.json  （build_manifest_template.py）
  → README.md の Hash 2行                     （check_design_generation_contract.pyが検査）
  → src/harness/domain/_registry_generated.py （generate_domain_registry_code.py）
```

| Registry | 追加 |
|---|---|
| events.yaml | +15 |
| errors.yaml | +23 |
| states.yaml | +2 名前空間 |
| schemas.yaml | +2（既存3 Schemaの変更あり） |
| tests.yaml | +41 Case（AT-DELEGATION-001 × 15、AT-MASKING-001 × 26） |
| gates.yaml | +9 Gate |
| **新規Registry** | `delegation-floor.yaml`、`masking-policy.yaml`。snapshot／source hash／生成コード／Verifierへ必須統合 |

**実際の件数は再生成結果を正とし、本文・コードへ手入力しない**（不変条件#18）。
新規Registry 2件は任意扱いにせず、`registry-additions.draft.yaml`の
`snapshot_integration`契約に従ってsnapshot／source hash／Domain生成コード／Verifierへ統合する。
欠落時はFail-Closedとし、v1.8の生成契約テストで検出する。

## 6. 承認後の適用手順

```bash
# 1. 設計書 v1.8 を編集（§3.8.5、§1.16.3、§1.15、§3.13 Gate表、§22、ADR-006/007追加）
# 2. Registry正本へ registry-additions.draft.yaml を取り込み、
#    delegation-floor.yaml と masking-policy.yaml を新設
#    UCD 14.0割当済み符号位置集合Artifactを生成・Hash固定（ヘッダなし139,264-byte bitmap、欠落時はv1.8適用不可）
# 3. 再生成
python tools/build_registry_snapshot.py --registries design-source/registries \
  --design "$DESIGN" --out registry-snapshot.json
python tools/build_manifest_template.py --registry registry-snapshot.json \
  --release-scope MVP0-A --out runtime-go-manifest.MVP0-A.template.json
python tools/build_spec_shards.py --design "$DESIGN" \
  --registries design-source/registries --out spec
python tools/generate_domain_registry_code.py
# 4. README.md の Hash 2行を更新
# 5. 検証（全て exit 0、run_verification は 2）
python tools/lint_spec.py ... && python tools/check_design_generation_contract.py ... \
  && python tools/validate_test_manifest.py ... && python tests/test_verify_runtime_go.py \
  && pytest tests/unit tests/spec_lint -q && ./run_verification.sh MVP0-A
```

## 7. 実装物の所在（レビュー時の注意）

設計書ADR-002／不変条件#12によりWorkspaceはWSL2 Linux側FS限定である。
そのため実装物はWindows側キットに存在しない。

| 対象 | 場所 |
|---|---|
| 設計正本・判定器・**本proposals** | 現行v1.8リポジトリroot（`design-v1.24-runtime-go.md`、`verify_runtime_go.py`、`registry-snapshot.json`） |
| `src/`、`tests/unit`、`tests/spec_lint`、`pyproject.toml`、`.github/` | 実装Repoの論理Root `FDE_HARNESS_IMPL_ROOT`。Windows例：`\\wsl.localhost\<distro>\home\<user>\fde-harness\` |
| 同上のミラー | 各自のGit Repository（例: `<OWNER>/<REPO>`） |

キット単体で確認できるのは設計Hash不変・Spec Lint 0違反・Verifier自己試験31件・
`run_verification.sh` の`BLOCKED_EVIDENCE_MISSING`まで。実装側テスト件数は固定値を正本とせず、
実装Repoで`pytest -q`を実行した時点のCI／テストReportを正とする（2026-08-06時点は168 passed＝実装側134＋Kit同梱34）。
新しいスレッドはまず`FDE_HARNESS_IMPL_ROOT`を解決し、Windows UNC／Linux絶対Pathを環境に応じて選択する。

## 8. 未解決事項

* `max_added_lines`等のPredicate既定値（実測後）
* Predicate評価器の配置。Plan決定性§1.11.1へ影響しない位置であること
* UCD 14.0割当済み符号位置集合Artifactの生成・署名Hash確定（欠落時は全入力Fail-Closed）
* `max_spans` / `max_mask_ratio`の実測校正。`0.60`はLLM追加分だけに掛かる暫定Circuit Breakerであり安全証明ではない
* 国別National ID分類をREJECTから緩和する場合の法務・Privacy承認
* 日本語PII検出Rule Setの設計（決定論スキャナ側）
* 新規Registry 2件の実装統合は必須。任意採否は未解決事項にしない
