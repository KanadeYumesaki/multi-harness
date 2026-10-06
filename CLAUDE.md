# 実装上の不変条件（マルチプロバイダーAI業務実行統制基盤）

このファイルはAIコーディングエージェントが**毎回最初に読む**正本である。
設計書 v1.8 Appendix B の実体であり、設計書側は本ファイルの要約に過ぎない。

## 0. このプロジェクトの前提

| 項目 | 値 |
|---|---|
| 開発体制 | 1名。Maker／Approver／Operatorは同一人物。組織的SoDは主張しない |
| 実装言語 | Python 3.11以上 |
| 実行環境 | WSL2のLinux側Filesystemのみ。`/mnt/*`とWindows Native FSへの書込みは拒否 |
| 状態Store | 単一SQLite DB。Artifact BytesだけがDB外のCAS |
| 脅威モデル | 単一UIDでの**改ざん検知**。改ざん耐性・否認防止は提供しない |
| Release信頼根 | Protected CIの`RUNTIME_GO_VERIFIER_SHA256`未設定時は`UNTRUSTED_REVIEW_ONLY` |
| 実装Phase | MVP0-A → MVP0-B → MVP1-A → MVP0-C → MVP1-D |
| 参照仕様Phase | MVP1-B／MVP1-C／MVP1-E／MVP2-A／MVP2-B／Blind Reviewer／Enterprise。**実装しない** |

## 1. 絶対に破ってはならない不変条件

1. **Event LedgerをUPDATE／DELETEしない。** 訂正はCompensating EventのAppendだけで行う。
2. **Effect実行前にOperationJournalを`PREPARED_DURABLE`へCommitする。** Journal未確定のままI/Oを起こさない。
3. **Fencing Tokenを最終Storage書込み直前に再検証する。** Application層の検証だけで通さない。
4. **Plan ContentへランダムID、採番ID、時刻、PID、列挙順を含めない。** SnapshotはHashだけを含める。
5. **Planは1回凍結した`PlanBuildInput`から2回Buildし、さらに別`PYTHONHASHSEED`の子プロセスで3回目をBuildしてHash一致を検証する。**
6. **Planner Packageで`set`／`frozenset`を反復しない。** Code Point昇順で`sorted`してから使う。同一プロセス内の二重Buildは`PYTHONHASHSEED`依存の非決定性を検出できない。
7. **Secret値をLog、Event、Attestation、例外、Test Evidenceへ出さない。** `SecretRef`型だけを扱う。
8. **Process起動は`list[str]`だけ。** `shell=True`とShell文字列を禁止する。
9. **例外を握り潰さない。** 判定不能は`EFFECT_UNKNOWN`または`BLOCKED_REPAIR_REQUIRED`で停止する。
10. **Approval消費は単一SQLite TransactionのCASで行う。**
11. **Approval Skip Flag／環境変数／直接実行APIを作らない。** `--yes`、`--auto-approve`、`--force`、`--skip-approval`相当を実装しない。
    自動承認は`DelegationGrant`（ADR-006）だけを経路とする。**「前回と同じ」を根拠にしない。** 判定根拠は人間が事前に承認した宣言型Predicateといま解決されたPlanの照合であり、過去の実行履歴を参照しない。成立時もApproval Managerを迂回せず、その実行専用の`DerivedApprovalGrant`を毎回新規発行する。非委任床は`design-source/registries/delegation-floor.yaml`が正本で、**委任による委任の作成・拡大を禁止する**。
12. **WorkspaceをWindows側Filesystem、`/mnt/*`、未知Network／FUSE／Overlayへ置かない。**
13. **ArtifactはTemp write → File fsync → Atomic Rename → Directory fsync → Manifest登録の順にする。**
14. **全状態Storeを単一SQLite DBへ置き、物理分離しない。**
15. **DB接続は`ConnectionFactory`だけから生成し、Repositoryは`commit()`しない。** Transaction所有者はApplication層のUnit of Work。
16. **実行していないTestをPASSと書かない。** 証跡なしは`UNVERIFIED`。
17. **Testを削除、Skip、期待値緩和してGateを通さない。**
18. **件数を本文・コードへ手入力しない。** 正本は`design-source/registries/`。
19. **CIのPASSをRuntime Evidenceへ流用しない。** CIはStatic／Self-test専用で、Release Manifestを生成しない。
20. **同一TaskにOPENのBLOCKED Recordがある場合は反復しない。** `BLOCKED-RECOVERY.md`の復帰条件とOwnerのDecisionを先に処理する。
21. **マスキングでLLMに本文を返させない。** LLMは`{start, end, category}`のSpanだけを返し、置換は決定論Rewriterが行う（ADR-007）。合否を決めるのは決定論スキャナであり、LLMはマスクを増やせるが通せない。Secret／Credential／`NATIONAL_ID`／`SPECIAL_CATEGORY_DATA`はマスクせずRejectし、LLMへ渡さない。
22. **正規化はUCD 14.0割当済み符号位置Guardの後に標準`unicodedata.normalize('NFC')`を使う。** NFCを自前実装しない。Guardは正規化の**前**に置く。Bitmap Artifactの欠落・Hash不一致・未割当符号位置はFail-Closed。Python 3.11(UCD 14.0)と3.12(UCD 15.0)の差異はこのGuardだけが吸収する。

## 2. 層の依存規則

```
domain/       … sqlite3, os, subprocess, Provider SDK を import しない
application/  … ports/ の抽象だけへ依存。Transaction境界を所有する
ports/        … 抽象Portの定義のみ
infrastructure/, adapters/, presentation/ … 具象実装
```

* CLIはApplication Serviceだけを呼ぶ。Repositoryへ直接アクセスしない。
* 時刻、UUID、乱数、Fault InjectionはPort経由で注入する。Plan Content生成中に直接参照しない。
* Path、Hash、Token量、Approval、Effectは値オブジェクト化する。文字列のまま受け渡さない。

## 3. 作業前後に必ず実行する

```bash
# 着手前
python tools/lint_spec.py --design <設計書> --registries design-source/registries \
  --snapshot registry-snapshot.json --spec-dir spec

# 生成後
python tools/lint_spec.py ...            # 同上
python tools/validate_test_manifest.py --registries design-source/registries
python tools/build_expectation_hashes.py --check   # Case期待値Hashの導出一致
python tools/generate_domain_registry_code.py --check
python tools/build_ucd_assigned_bitmap.py --version 14.0.0 \
  --out src/harness/masking/ucd/14.0.0-assigned-codepoints --check
python tests/test_verify_runtime_go.py   # Verifierに触れた場合は必須
# 存在するPackageだけを対象にする。未作成の層を渡すとmypyは終了Code 2で落ちる
targets=(); for p in src/harness/domain src/harness/ports \
  src/harness/infrastructure src/harness/application; do
  [ -d "$p" ] && targets+=("$p")
done
mypy --strict "${targets[@]}"
ruff format --check . && ruff check .
pytest tests/ -q
```

Spec Lintが失敗した状態でCodeを生成・採用してはならない。

## 4. 与えられた文脈の扱い

* 設計書全文（約82,000 tokens）をPromptへ入れない。`spec/`の該当shardとRegistryの該当部分だけを使う。
* 新規スレッドは`THREAD-START.md`の順序と`TASK-BRIEF.template.md`を使う。
* `spec/99-reference/`（参照仕様Phase）を実装Taskの文脈へ入れない。該当PhaseのCode、Schema、Feature Flagを生成しない。
* `spec-manifest.json`の`source_hash`が設計書と一致しない場合、Code生成を停止して報告する。

## 5. 反復の上限

* 1 Taskあたり8反復まで。
* 直近2反復で試験結果が改善しない場合は停止し、原因を報告する。
* 各反復前にCommitし、失敗時は直前Commitへ戻す。
* 採用条件はSpec Lint、型検査、Unit、関連Integration、Security Checkの**全合格**。

## 6. 迷ったときの既定

* 安全側に倒す。可用性より安全停止を優先する。
* 状態が判定できないなら、推測して進めず`EFFECT_UNKNOWN`で止める。
* 「たぶん大丈夫」でPASSにしない。証跡がないなら`UNVERIFIED`と書く。
