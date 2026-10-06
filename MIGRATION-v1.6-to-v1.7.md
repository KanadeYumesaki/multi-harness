# v1.6 → v1.7 移行記録

v1.6キットのIT専門家レビューで検出した欠陥と、その是正内容の対応表。
すべての是正は再現可能なツールとして同梱しており、手作業の書き換えを含まない。

## 1. なぜ改訂が必要だったか

v1.6の設計内容そのものの品質は高い。問題は**設計を検証するはずの判定基盤**にあった。

設計書§0.6と§28は次を宣言していた。

> Runtime GOは……同梱する`verify_runtime_go.py`が終了Code 0を返した場合に限り宣言できる。
> VerifierはGOを付与する装置であると同時に、GOの手動偽装を防止する境界である。

この宣言が成立していなかった。実測結果：

```text
入力: 42バイトのダミーJSON 1個
      全86 Case + 36 Gate の evidence_path をそれへ向ける
      required_evidence_areas を []
      python_version を "2.7.0"
      input_fixture_hash を全て sha256:000...0

v1.6 出力: "decision": "RUNTIME_GO", "error_count": 0
           終了Code 0
```

同時に、MVP0-AのRuntime GOは**そもそも到達不能**だった。§26.1が判定単位をMVP0-Aとしながら、§0.6とVerifierが全86 CaseのPASSを要求しており、その86 Caseには§0.3が「実装しない」と宣言した参照仕様Phase専属のCaseが含まれていた。

## 2. 是正内容

### Blocker 1：Verifierの非偽装性

| 穴 | v1.0の実装 | v1.1の是正 | 検出試験 |
|---|---|---|---|
| Evidence領域の必須集合が未検査 | `for a in areas:` のみ。空配列で全要件消滅 | Registry由来の必須集合と過不足を突合 | `AT-VERIFIER-002` |
| Evidence Fileの流用 | 検査なし | `evidence_manifest_hash`の一意性を検査 | `AT-VERIFIER-003` |
| Path脱出検査の非対称 | `test_cases`のみ実装。`gates`／`areas`は素通し | 3箇所で統一。絶対Pathと`..`を拒否 | `AT-VERIFIER-004/005` |
| Evidence内容の未検証 | Bytes Hashのみ | JSONをパースし、Registry期待値（`observed_state`／`observed_error_code`／`observed_event_sequence`／`assertions`）と突合 | `AT-VERIFIER-009/010/011` |
| Python版数の形骸化 | 真偽値判定のみ | 3.11以上を数値比較（ADR-003） | `AT-VERIFIER-008` |
| `test_manifest_hash`の未束縛 | 形式検査のみ | Registry導出のScope別値と突合 | `AT-VERIFIER-012` |
| Registryの改変 | Registry概念なし | Snapshotの自己Hashを再計算して検証 | `AT-VERIFIER-013` |
| Report Hashが判定内容を束縛しない | `decision`と件数のみ。別Manifest 2件が同一Hashを出していた | `manifest_sha256`／`registry_snapshot_hash`／`verifier_source_sha256`／`implementation_commit_sha`／`release_scope`を含む本体全体を覆う | `AT-VERIFIER-015` |
| Gateの根拠が不明 | 検査なし | `case_refs`を必須化し、全てPASS Caseであることを検証。`ALL_IN_SCOPE` Gateは全Scope内Caseの列挙を要求 | `AT-VERIFIER-016` |
| 判定器自身が無検証 | テスト0件 | `AT-VERIFIER-*` 22 Case。`verifier_self_test`をEvidence必須領域化 | — |

### Blocker 2：Release Scope

Case単位の`phase_scope`を導入し、Verifierに`--release-scope`を必須化した。

```text
全Phase合計      : 86 Case / 37 Test ID
MVP0-A           : 70 Case / 29 Test ID / 36 Gate  ← 初回判定対象
MVP0-B           : 42 Case / 21 Test ID
MVP1-A           : 66 Case / 25 Test ID
MVP0-C           : 53 Case / 24 Test ID
MVP1-D           : 70 Case / 26 Test ID
```

Test ID単位ではなくCase単位にした理由は、1つのTest IDが複数Phaseの関心事を束ねていたためである。`AT-EVENT-MAPPING-001`の4 Caseは実際には別Phaseに属していた。

| Case | v1.7 phase_scope | 根拠 |
|---|---|---|
| `LEDGER_EVENT_MISSING` | MVP0-A以降 | Phase Store先行／Ledger Event欠落はLocal File Commitでも起きる汎用不整合。§3.13 Gate 13が要求する |
| `REMOTE_UNCERTAIN` | MVP0-C以降 | Remote Invocation Registryが存在するPhaseのみ |
| `BUDGET_UNKNOWN` | MVP1-D | Budget Reservation Storeが存在するPhaseのみ |
| `OUTBOX_UNKNOWN` | MVP1-E | Transactional Outboxが存在するPhaseのみ（参照仕様） |

`AT-CONFIG-001/DRIFT`はMVP0-Aへ追加した。Mock AdapterもRuntimeEnvelopeSpecとRuntime Attestationを持つため、Config DriftはMVP0-Aから検証できる。これにより§3.13 Gate 4の参照整合が解消した。

参照仕様Phaseは`--release-scope`として指定できない（終了Code 4）。

### 追加で見つかった defect（Spec Lint導入により顕在化）

| 症状 | 内容 | 是正 |
|---|---|---|
| §19.1のState表が不完全 | 「State例」の部分列挙であり、規範Manifestが使う`BLOCKED_APPROVAL`／`FAILED_RETRYABLE`／`WAITING_APPROVAL`／`BLOCKED_CONFLICT`を含まなかった。§19.1本文が要求する「Subject型のState Enumに対して解釈する」検査が実装不能だった | §1.4.2／§1.4.3／§1.5／§3.8.1から完全Enumを導出して固定 |
| §3.13 Gate 2の参照が機械可読でない | 条件が「Manifest全体」で、Gateの根拠Caseを検証できなかった | `test_refs_mode=ALL_IN_SCOPE`を導入し、Scope内全Caseの列挙を必須化 |
| Appendix Cのファイル名ドリフト | `integrated-design-v1.5.md`を指していた。AIエージェントが探すファイル名が§0.2と矛盾 | v1.7へ統一。Spec Linterが再発を検出 |
| 規範Fixtureの`producer` | v1.6文書中に`harness-core/1.5.0`が10箇所。実Fixtureへコピペされる | 1.7.0へ統一。Spec Linterが検出 |
| Code Fence内のH1 | Appendix Bの`# 実装上の不変条件`がフェンス内にあり、shard分割ツールが誤爆する | Appendixを実ファイル参照へ変更。Linterが`HEADING_INSIDE_CODE_FENCE`で検出 |

### 保守性の是正

| 項目 | v1.6 | v1.7 |
|---|---|---|
| 再認定 | Tupleの1項目変化でGO失効。手順未定義。Commit毎に人手計測Approval UXが必要 | §26.6の3階層（`TIER_1_SMOKE`／`TIER_2_AFFECTED`／`TIER_3_FULL`）。人手計測EvidenceはSubtree Hashへ束縛して再利用可 |
| CI | 「初期CI Matrix 3.11/3.12」の記述のみ | §16.5でTrigger別実行内容、必須Check、依存保守、Coverage方針を定義。`ci/github-actions-ci.yml`を同梱 |
| Plan決定性 | 同一プロセス二重Build。`PYTHONHASHSEED`依存の`set`反復を検出不能 | 別プロセス三重Build＋AST検査＋Locale／TZ固定（§1.11.1） |
| 耐久性の主張 | Gate文言がfsync境界の検証を含意するが、`os._exit`では到達不能 | Tier 3段階を定義し`durability_tier`をEvidence必須項目化（§3.10.2） |
| 新スレッド再現性 | Registry・Linter・`CLAUDE.md`が実ファイルとして存在しない | すべて実ファイルとして同梱。読み込み順序をAppendix Cへ明記 |

## 3. 再現手順

v1.6から本キットを再構築する場合：

```bash
# 1. v1.6本文からRegistryを一次抽出（移行時のみ）
python tools/extract_registries.py \
  --design "../runtime-go-v1.6-kit/…v1.6_Runtime_GO判定実行版.md" \
  --out design-source/registries

# 2. v1.8正本からRegistry Snapshot生成
python tools/build_registry_snapshot.py \
  --registries design-source/registries \
  --design "design-v1.8-runtime-go.md" \
  --out registry-snapshot.json

# 3. Manifest Template生成
python tools/build_manifest_template.py \
  --registry registry-snapshot.json --release-scope MVP0-A \
  --out runtime-go-manifest.MVP0-A.template.json

# 4. Spec shard生成と全検証
python tools/build_spec_shards.py \
  --design "design-v1.8-runtime-go.md" \
  --registries design-source/registries --out spec
./run_verification.sh MVP0-A
```

旧bootstrapパッチ生成器はv1.8 Releaseから廃止した。現行の再生成は、編集可能な正本`design-v1.8-runtime-go.md`、Registry Snapshot、Spec shard、`tools/check_design_generation_contract.py`の順で行う。レビュー済み正本へ移行用パッチを直接適用してはならない。

**以後、Registryが正本である。** `extract_registries.py`を再実行して`design-source/registries/`を上書きしてはならない（移行の再現検証を除く）。

## 4. 移行後も残る制約

* 単一UID構成は改ざん検知であり、改ざん耐性・否認防止を提供しない（ADR-004。v1.6から変更なし）。
* デバイス電源断耐久性（`T3`）は未検証のまま。`T2_CACHE_DROP`までを検証範囲とする。
* Spec Lintは参照整合を検査するが、設計の意味的正しさは検査しない。
* Verifierの`AT-VERIFIER-*`は**既知の**偽装手口を検出する。未知の手口への網羅性は主張しない。新しい手口が判明した場合はCaseを追加する。
* MVP0-B以降のGateはRegistryへ未登録である（`gates.yaml`はMVP0-Aの36 Gateのみ）。各Phase着手時に追加する。

## 5. v1.7専門家レビュー後のVerifier v1.2是正

v1.1の22試験は合格していたが、契約外の入力で次の誤GOを再現できた。

* Case／Gate／Areaの区分をまたぐ同一Evidence流用。
* Areaの`summary`／`test_run_id`／時刻欠落。
* `release_decision=RUNTIME_NO_GO`とVerifier出力GOの矛盾。
* Crash Caseの`durability_tier`欠落。
* FixtureとRaw Resultの実Fileがない自己申告Evidence。
* `/mnt/*`をLinux native filesystemとするEnvironment自己申告。
* Gate 0件のMVP0-B以降をRelease Scopeに指定可能。

Verifier v1.2はこれらを`AT-VERIFIER-023`〜`031`で固定し、Fixture／Raw Result／Runner／Commit／Environment／Schema／Migrationの束縛、Release-enabled Phase、Release Manifest生成を追加した。また、`states.yaml`、生成spec shard、`spec-manifest.json`、新規スレッド開始契約を実ファイルとして追加した。

なお、単一UID構成のVerifierはEvidence相互の整合性を検証するものであり、Evidence ProducerとVerifierを同時に改変できる攻撃者に対する改ざん耐性・否認防止は主張しない。

## 6. Geminiレビューを受けた横断是正

* `ci/verifier-source.sha256`とCI／Wrapperの照合を追加した。ただし期待Hashも同一UIDで変更できるため、Protected Branch、Required Check、変更者と承認者の分離が無い環境は`UNTRUSTED_REVIEW_ONLY`とする。
* CI RunnerとWSL2 Release Gateの責務を分離し、`tools/check_ci_runtime_boundary.py`でRuntime Evidence／Release ManifestのCI生成を拒否する。CIのPASSをRuntime GOへ流用しない。
* `blocked-record.schema.json`、`create_blocked_record.py`、`check_blocked_records.py`、`BLOCKED-RECOVERY.md`を追加した。OPEN RecordがあるTaskの反復を禁止し、OwnerのDecision／環境変更、Hash再生成、受入条件、Verifier自己試験を経てから再開する。
* `tests/test_hardening_contracts.py`はVerifier改ざん、CI Release Manifest誤生成、BLOCKED Record往復を否定系で検証する。
