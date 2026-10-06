# ADR-007：LLM補助マスキング（Span提案方式）

- 状態: **提案 v5（UCD14割当済みGuard＋標準NFCへ整理）**。設計書v1.7へは未反映
- 起案日: 2026-08-06 ／ 改訂: 2026-08-06（CI Matrix互換・UCD14割当済みGuard・依存なしGrapheme guard・ratio/Span境界を反映）
- 対象Phase: MVP0-A（Port＋決定論スキャナ＋Rewriter＋Mock Masker）→ MVP0-B（ローカルLLM）
- 関連: §1.13、§1.16.3、不変条件#7、ADR-006

## 0. v1を破棄した理由

v1は「LLMにマスク済み本文を返させ、Mask Tokenで分割した各リテラル片が原文に同順で
部分文字列として現れることを検証する」方式だった。**この検証は欠落を検出できない。**

```text
原文    : "契約者は山田太郎、金額は1,200,000円、担当は佐藤"
LLM出力 : "契約者は[MASKED:PERSON]"
検証     : 断片 "契約者は" は原文に存在し順序も正しい → 合格してしまう
実際     : 金額と担当が丸ごと消えている
```

同様に重複・並べ替え・Unicode正規化差・意味改変も完全には防げない。
**LLMに本文を生成させる限り、出力の完全性を決定論的に保証できない。**

v2以降はLLMに本文を返させない。**座標（Span）だけを返させ、置換は決定論Rewriterが行う。**

## 1. パイプライン（§1.16.3の置換）

```text
Bytes読込（SafeInputReader）
  → UCD 14.0割当済み符号位置Guard → Python `unicodedata.normalize('NFC')`
       （固定`normalization_profile`）。source_normalized_hash を確定
  → Deterministic Scan #1                      ← 権限を持つのはここと#2だけ
       Media Type／Encoding／Size検証
       Secret／Credential Scan
       Data Classification判定
       PII／Regulated Data Scan
       Binary／Executable／Archive判定
       → 決定論スキャナがMASKABLE候補Spanを生成
  → 判定（境界は masking-policy.yaml が正本）
       REJECT   : 保存・候補化を拒否。**LLMへ渡さない**
       CLEAN    : マスク不要。そのままCAS保存
       MASKABLE : 以下へ
  → LLM Masker（ローカルのみ）へ正規化済みテキストを渡す
  → LLMは **Spanだけ** を返す（本文を返さない）
  → Span検証（決定論・§3）
       決定論候補SpanとLLM追加SpanをUnionし、重複排除後に検証
  → 決定論Rewriterが原文へ置換（§4）
  → Deterministic Scan #2                      ← 実質のGate
       合格   : MaskedArtifactとしてCAS保存。ContextFragment候補化
       不合格 : REJECT。原本もマスク版も候補化しない
```

`source_normalized_hash`はUCD 14.0未割当符号位置を事前Rejectしたうえで、次のDomain Hashで求める。

```text
SHA-256(
  "FDE-HARNESS/normalized-source/3/" ||
  normalization_profile || normalization_profile_artifact_hash ||
  normalized_utf8_bytes
)
```

UCD 14.0割当済み符号位置集合Artifactの欠落・Hash不一致・Profile不一致、または未割当符号位置の入力は、
正規化前に一律Fail-Closedとする。Artifactは正規化テーブルではなく、UCD 14.0の割当済み符号位置集合だけを含む。
Artifactはヘッダなしの`UCD14_ASSIGNED_BITMAP_V1`固定バイナリ形式（0x110000 bit、139,264 bytes、符号位置昇順のLSB-first）とし、
余分なBytes・短いBytes・PolicyのFormat／Version不一致はRejectする。HashはArtifactの生Bytesに対するSHA-256（`sha256:`接頭辞）で固定し、
生成時は公式UCD 14.0入力のSource Hashも記録する。実行時の`unicodedata.unidata_version`は14.0.0以上を要求する。
割当済み符号位置だけに入力を限定すれば、Unicode Normalization Stabilityにより、
Python 3.11／3.12の標準NFC正規化結果は一致する。NFCアルゴリズムを自前実装しない。

## 2. LLMの出力契約

LLMは次のJSONだけを返す。`additionalProperties: false`のJSON Schemaで厳格検証する。

```json
{
  "source_normalized_hash": "sha256:...",
  "normalization_profile": "NFC_CODEPOINT_V3",
  "normalization_profile_artifact_hash": "sha256:...",
  "spans": [
    {"start": 120, "end": 132, "category": "EMAIL"},
    {"start": 200, "end": 205, "category": "PERSON_NAME"}
  ]
}
```

* `start` / `end` は**NFC正規化済みテキスト上のUnicode scalar value列のindex**（半開区間 `[start, end)`）。
  Byte offsetおよびUTF-16 code unit indexは使わない。実装言語に依存しないよう、
  UTF-8をUnicode scalar value列へ変換した後の位置として定義する。
* `normalization_profile`と`normalization_profile_artifact_hash`は入力とMasker出力で完全一致しなければならない。
* `category` は`masking-policy.yaml`の`categories`に登録された値のみ。
* 本文、置換文字列、説明文、理由は**受け取らない**。Schemaに存在しないため混入し得ない。

## 3. Span検証（決定論・全てFail-Closed）

| # | 検証 | 不合格時 |
|---|---|---|
| 1 | JSON Schema厳格適合（未知Field拒否） | `MASKER_OUTPUT_MALFORMED` |
| 2 | `source_normalized_hash`が入力と一致 | `MASKER_OUTPUT_MALFORMED` |
| 3 | `0 <= start < end <= len(text)` | `MASKING_SPAN_INVALID` |
| 4 | `category`がRegistry登録値 | `MASKING_CATEGORY_UNKNOWN` |
| 5 | Candidate／LLMの重複は同一カテゴリContainmentだけを解決し、最終Spanを`start`昇順・**非重複・非入れ子**にする | `MASKING_SPAN_CONFLICT`／`MASKING_SPAN_INVALID` |
| 6 | Span数が`max_spans`以下 | `MASKING_SPAN_INVALID` |
| 7 | LLM追加SpanのUnion長が`max_mask_ratio`以下（Scan#1候補は分母へ含めるが分子から除外） | `MASKING_RATIO_EXCEEDED` |
| 8 | Span境界がUnicode scalar value境界（indexの性質上自明。表明として検証） | `MASKING_SPAN_INVALID` |
| 9 | Spanが保守的risky codepoint guardに該当しない | `MASKING_GRAPHEME_SPLIT` |

MVP0-Aではruntime依存を追加せず、UCD 14.0割当済み符号位置集合Artifactに基づく
`CONSERVATIVE_RISKY_CODEPOINT_GUARD_V1`を使う。結合文字・Spacing Mark・Hangul Jamo・
Variation Selector・ZWJ・Extended Pictographic・Emoji Modifier・Regional Indicator・Tag・
Format Joinerを含むSpanはRejectし、その他の
SpanはUnicode scalar value境界を検証する。UAX #29完全実装はMVP0-B以降の拡張とする。
Grapheme判定器がない場合は、危険文字を含むSpanだけをRejectし、通常のASCII／安全文字列まで
一律Rejectしない。一方、UCD 14.0割当済み符号位置集合Artifactがない場合、またはHashが一致しない場合は、
ASCIIを含む全入力を正規化段階で一律Rejectする。この2つの欠落条件を混同しない。

5と7が重要である。重複・入れ子を許すとRewriterの結果が順序依存になり決定性を失う。
決定論候補とLLM Spanが同一座標・同一カテゴリなら1件へDeduplicateする。
LLM Spanが同一カテゴリの決定論候補を完全に包含する場合は、安全方向のContainment Expansion
として包含Spanへ正規化する。1つのLLM Spanが複数候補を包含する場合、カテゴリが異なる場合、
または部分重複で包含関係が成立しない場合は`MASKING_SPAN_CONFLICT`でRejectする。
比率上限は、LLMが全文を1 Spanで塗り潰して「マスク成功」に見せるための
サーキットブレーカーであり、PIIが完全に除去された証明ではない。比率は非重複Spanの
Unionのうち、決定論Scan#1候補が既に覆う範囲を除いたLLM追加Unicode scalar value数を、
正規化済み本文のUnicode scalar value数で割って求める。決定論Scan#1だけで高密度になる文書は
このCircuit Breakerの対象外とし、Scan#2とPolicy境界で検証する。
Containment Expansionを含む各LLM追加Spanにも`max_single_llm_addition_ratio`を適用する。
空本文はSpanを許可せず、未設定の閾値はFail-Closedとする。`0.59`、`0.60`、`0.61`の境界を試験する。

**LLMの停止、Timeout、接続不能、空応答、非JSON応答は全て不合格**であり、
マスクなしでのPassにしない（`MASKER_UNAVAILABLE`）。

## 4. 決定論Rewriter

```text
出力 = `normalization_profile`に従うNFC正規化済み原文の [start,end) を Mask Token へ置換したもの
```

* Mask Tokenは`masking-policy.yaml`の`mask_tokens`で**カテゴリごとに固定**。
  LLMが返した文字列は一切使わない。
* 置換は`start`降順で適用し、index shiftを排除する。
* Rewriterは純粋関数とし、同一入力から常に同一出力を返す。
* CLEANも下流へ渡すCanonical Artifactは同じNFC正規化ProfileのUTF-8とする。
  原文Bytesとのバイト同一性が必要な経路は本ADRの対象外であり、別の正規化Map仕様を要する。

**LLMが影響できるのは「どこを隠すか」だけであり、「何が書かれるか」には影響できない。**
これがv1との決定的な差である。

## 5. Prompt Injectionへの耐性

入力ArtifactはZ5 / `UNTRUSTED_ARTIFACT_DATA`であり、本文に指示文を含み得る。

* マスク方針（categories、mask_tokens、上限値、REJECT境界）は
  **Control Planeの`masking-policy.yaml`だけ**から来る。入力本文から変更できない。
* LLMへ渡すInstructionは固定でHash化し、`RuntimeEnvelopeSpec.masker_instruction_hash`
  として`plan_content_hash`へ含める。
* Injectionが成功しても、LLMが返せるのはSpanだけである。
  * 過少マスク → **Scan #2 が検出**して REJECT
  * 過剰マスク → 安全側。ただし`max_mask_ratio`で検出
  * 不正Span → §3で拒否
* したがってInjectionはFail-Closedへ倒れる経路しか持たない。

## 6. REJECT境界（Registry化）

`design-source/registries/masking-policy.yaml`を正本とする。散文とコードへ直書きしない。

**次はマスクせず即Rejectとし、LLMへ送らない。**

* Secret、Password、API Key、Bearer Token、Session Cookie、Private Key、Certificate Key
* Cloud Credential、Connection String、`.env`相当
* `SPECIAL_CATEGORY_DATA`（要配慮個人情報）
* `NATIONAL_ID`（国民識別番号。日本のマイナンバー等を含む）
* 分類不能、Scan不能、Binary、暗号化済み、Archive内部

**MASKABLE**（LLMへ送ってよい）は`PERSONAL_DATA`のうち氏名・住所・電話・メール・
社員番号・取引先名など、`masking-policy.yaml`に列挙されたカテゴリだけとする。

「マスクして使う利益」より「マスクのためにLLMへ渡す危険」が大きいものは、常にRejectを選ぶ。

## 7. ローカルLLMの隔離要件

Maskerは**ローカルのみ**。外部Providerへは渡さない。加えて次を要求する。

| 項目 | 要件 | 強制手段 |
|---|---|---|
| Network | Masker Processのegressを拒否 | `RuntimeEnvelopeSpec`のNetwork Policy。起動前検証 |
| Telemetry | Model Runtimeのテレメトリ送信を無効化 | 設定Hashを`plan_content_hash`へ含める |
| Prompt Log | Prompt／応答のディスク記録を無効化 | 起動前に設定検証。有効なら`MASKER_UNAVAILABLE` |
| Core Dump | `RLIMIT_CORE = 0` | Process起動時に設定 |
| Swap | Raw PII経路では`mlockall`を必須化。不能ならMaskerを使用しない | 起動時検証。不能時は`MASKER_ISOLATION_INCOMPLETE`でReject |
| 一時File | 中間データをディスクへ書かない | in-memory処理を必須化 |

**Swapとメモリダンプへの対策は完全ではない。** 単一UID環境では他Processからの
`/proc/<pid>/mem`読み出しを防げない。これは§22残余リスクへ明記する。

## 8. 保存する証跡（`MaskingReceipt`）

原本Bytesは永続化しない。保存するのは次だけとする。

`masking_receipt_id` / `source_content_hash` / `source_normalized_hash` / `normalization_profile` /
`normalization_profile_artifact_hash` /
`masked_content_hash` /
`scan1_findings`（**Finding Type・Rule ID・件数・Scanner Versionのみ。値と本文断片を含めない**）/
`scan1_decision` / `scan1_candidate_span_count` / `spans`（start・end・categoryのみ）/ `span_validation_result` /
`masker_provider` / `masker_model` / `masker_model_digest` / `masker_instruction_hash` /
`rewriter_version` / `scan2_result` / `masking_policy_version` / `policy_snapshot_hash` /
`created_at` / `store_version`

`spans`は座標であり本文を含まないため保持してよい。ただし
**Span座標と原本があればマスク前を復元できる**ため、原本Bytesを保存しない前提を崩さない。

## 9. ADR-006（委任）との関係

* マスキングが`REJECT`となった入力を含むPlanは**常に人間承認へ戻す**（非委任床11）。
* `MASKED`で通過した入力は、`classification_ceiling`の範囲内なら委任対象になり得る。
* マスクによる`data_classification`の**自動Downgradeを禁止**する（§1.13）。
  引下げが必要な場合は根拠Artifactと人間承認を持つ独立Decisionとする。

## 10. Phase配分

| Phase | 範囲 |
|---|---|
| **MVP0-A** | `ContentScannerPort` / `MaskerPort`、UCD 14.0割当済みGuard、標準NFC、決定論スキャナ、risky codepoint guard、Span検証、決定論Rewriter、二重スキャン、`MaskingReceipt`、`masking-policy.yaml`、Mock Masker、Secret Canary試験 |
| **MVP0-B** | Ollama Masker Adapter。隔離要件（§7）の実測 |
| 参照仕様 | 外部LLMによるマスキングは採らない |

MVP0-Aは外部通信も実Providerも無いため、Mock Maskerが決定論的にSpanを返す。
**LLMが1つも無い状態でパイプライン全体と全否定系Caseを検証できる**構成とする。

## 11. 必要な試験（レビュー指摘を反映）

| Case | 内容 |
|---|---|
| `SECRET_REJECTED_NOT_MASKED` | 認証情報がMaskerへ渡らず即Reject。`masker_invocation_count == 0` |
| `SECOND_SCAN_IS_THE_GATE` | 過少マスクをScan#2が検出しREJECT |
| `SPAN_OUT_OF_RANGE` | `end > len(text)`、負値、`start >= end` を拒否 |
| `SPAN_OVERLAP` | 部分重複・カテゴリ衝突・複数候補包含などContainment外を拒否 |
| `SPAN_UNSORTED` | 昇順でないSpanを拒否 |
| `SPAN_CATEGORY_UNKNOWN` | 未登録カテゴリを拒否 |
| `MASK_RATIO_EXCEEDED` | 全文塗り潰しを拒否 |
| `MASKER_OUTPUT_MALFORMED` | 非JSON・未知Field・Hash不一致を拒否 |
| `MASKER_UNAVAILABLE_FAIL_CLOSED` | 停止・Timeout時にマスクなしで通さない |
| `UNICODE_NORMALIZATION` | NFC正規化前後でSpan座標がずれないこと。結合文字・非BMPを含む |
| `NORMALIZATION_PROFILE_MISMATCH` | Profile／Unicode data version不一致をReject |
| `UNICODE_PROFILE_CROSS_PYTHON` | Python 3.11／3.12でUCD14 Profileと正規化Hashが一致 |
| `UNICODE_PROFILE_ARTIFACT_MISSING` | UCD 14.0割当済み符号位置集合Artifact欠落・Hash不一致を全入力Reject |
| `UNICODE_UCD14_ASSIGNED_GUARD` | UCD 14.0未割当符号位置を正規化前にReject |
| `GRAPHEME_SPLIT` | risky codepoint guardに該当するSpanをReject。runtime依存なし |
| `MASK_RATIO_BOUNDARY` | LLM追加Union長の0.59／0.60／0.61境界とScan#1高密度を検証 |
| `SCAN1_CANDIDATE_UNION` | 決定論候補SpanとLLM追加SpanをUnionしてRewriterへ渡す |
| `SPAN_CONTAINMENT_EXPANSION` | 同一カテゴリで1候補を完全包含するLLM Spanを正規化 |
| `SPAN_CONFLICT_PARTIAL` | 部分重複・カテゴリ衝突・複数候補包含をReject |
| `NATIONAL_ID_REJECTED_NOT_MASKED` | NATIONAL_IDをMaskerへ渡さず即Reject |
| `MASKER_ISOLATION_INCOMPLETE` | egress／telemetry／log／core dump／mlock等の強制条件未成立をReject |
| `REWRITER_DETERMINISM` | 同一入力・同一Spanから常に同一出力 |
| `NO_LLM_PATH` | Maskerを一切呼ばない経路（CLEAN／REJECT）が正しく動作 |
| `NO_SECRET_IN_RECEIPT` | Receipt／Ledger／LogへSecret Canary漏えい0件 |
| `NO_AUTO_DOWNGRADE` | マスク後もclassificationが下がらない |
| `PROMPT_INJECTION` | 入力本文の指示でマスク方針・カテゴリ・Tokenが変化しない |

## 12. 検討したが採らなかった案

| 案 | 不採用理由 |
|---|---|
| **LLMにマスク済み本文を返させる（v1）** | **欠落・重複・並べ替え・意味改変を決定論的に検出できない（§0）** |
| 外部LLMでマスク | 守る対象を外部へ送ることになる |
| LLMの判断で分類をDowngrade | §1.13が自動Downgradeを禁止 |
| 原本を隔離領域へ保存 | 不変条件#7の適用範囲がGC／Backup／Exportへ拡大 |
| 認証情報もマスクして利用 | マスク漏れ時の損害が大きい。Rejectで足りる |
| LLMに置換文字列も決めさせる | 本文生成能力を与えることになりv1と同じ穴が開く |

## 13. 未解決事項

* UCD 14.0割当済み符号位置集合Artifactの生成・署名Hash確定（欠落時は全入力Fail-Closed）
* `masking-policy.yaml`の日本語PII Rule Setと`max_spans`／`max_mask_ratio`の実測校正
* 国別のNational ID分類をREJECTから緩和する場合の法務・Privacy承認
* ローカルLLMのSwap／メモリダンプ対策の実効性（§7）。未成立時はFail-Closedを維持する
