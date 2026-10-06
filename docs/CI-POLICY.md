# CI 方針：ローカル優先の 3 層

GitHub Actions の月枠を使い切ったことを受けて、**通常開発はローカル検証を基本**とし、
Actions は「安全性に直結する最小 PR チェック」と「Release 時の完全検査」に限定する。

**この方針は検査を消すものではない。** どこで走らせるかを決めるものである。

---

## 1. ローカル検証（通常開発の基本）

```bash
./run-checks.sh          # 全部
./run-checks.sh fast     # 型・lint・test だけ
```

必ず通すもの。

| 検査 | 中身 |
|---|---|
| `lint_spec` | 設計書・Registry・Snapshot・spec の整合 |
| generation contract | 設計 Hash・snapshot・spec-manifest・README の同時整合 |
| design mirror / reference currency | 英語正本と同期ミラー、旧名参照 |
| 生成物の `--check` | Registry codegen、Chat Context Policy、Core Schema、期待値 Hash |
| schema contract / blocked records / case coverage | 契約と Block Record と Case 網羅 |
| `ruff format --check .` / `ruff check .` | 整形と lint |
| `mypy --strict` | `domain`／`ports`／`infrastructure`／`application` |
| `pytest -q` | 全 Suite |

### ローカルの PASS が意味しないこと

* **GitHub CI の PASS ではない。** Runner で走らせた結果ではない
* **Release Evidence ではない。** Runtime GO は WSL2 実機の Release Gate だけが生成する
  （不変条件#19）
* Log の文言も `LOCAL_OK` にしてある。緑と書かない

---

## 2. PR チェック層

判定は `tools/ci_scope.py` が行い、`scope-plan` Job が出力する。
**YAML の式で判定しない。** 判定は試験できる場所に置く。

### Draft PR

| 実行するもの |
|---|
| policy gate（許可 Path・禁止パターン・Secret 走査） |
| 変更範囲の判定 |
| 変更範囲の最小テスト |
| Python 1 版のみ |
| 正本が動いていれば spec・reference も回す |

**正本の壊れは Ready まで持ち越さない。** `design-source/`／`schemas/`／`spec/`／
`ci/`／`registry-snapshot.json`／設計書が動いたら Draft でも spec を回す。

### Ready for Review

| 実行するもの |
|---|
| Draft の全検査 |
| spec / generation / reference currency |
| 変更対象に対応する Integration test |
| 安全境界テスト |
| 全 pytest |

### Full 検査

次のときだけ走る。

| 条件 |
|---|
| `ci:full` ラベル |
| `workflow_dispatch` |
| main への push |
| Release tag |
| Release 候補 Branch（`release/`・`hotfix/`） |
| **影響範囲が不明なとき（Fail-Closed）** |

内容は Python 全版・全 pytest・license／SBOM・deep 検査。

---

## 3. Path 判定

| 分類 | 対象 | 効果 |
|---|---|---|
| `safety` | `src/`・`tests/`・`tools/`・`.github/` | 最小テストの対象 |
| `canon` | `design-source/`・`schemas/`・`spec/`・`ci/`・`registry-snapshot.json`・設計書 | spec・generation・reference を回す |
| `dependency` | `requirements*.txt`・`pyproject.toml`・`constraints.txt` | license／SBOM を回す |
| `docs` | `docs/`・`runtime-evidence/`・`blocked/`・`release/`・`tasks/`・README 等 | 最小のまま |
| `unknown` | 上のどれでもない | **完全検査へ回す** |

### Fail-Closed

* 分類できない Path が 1 つでもあれば完全検査
* 差分を読めなければ完全検査
* 変更が 0 件でも完全検査（**読めていないことを「影響なし」と書かない**）

新しい置き場所が増えたら、`tools/ci_scope.py` の分類表を更新するまで重い側へ倒れる。
**推測で検査を省略しない。**

---

## 4. Workflow の制約

| 制約 | 実装 |
|---|---|
| required check 名を維持 | Job 名を変えない。**Job を skip しない** |
| 対象外の Job | 短い not-applicable の成功で終える |
| Workflow 全体の `paths-ignore` | 使わない。check ごと消えるため |
| PR 単位の concurrency | 新しい Run が古い Run を置き換える。main と Tag は止めない |
| feature branch の重複起動 | `push` を main と Tag に限定。PR 側だけが走る |
| `pull_request_target` | **追加しない** |
| API Key・Keyring・Provider 通信 | 行わない |
| 安全検査そのもの | 削除しない |

### check 名

| Job | check 名 |
|---|---|
| `plan` | `scope-plan` |
| `spec` | `spec-foundation` |
| `quality` | `quality (py…)` |
| `supply-chain` | `license-and-sbom` |
| `nightly` | `nightly-deep` |
| `release-tag` | `release-tag-signature` |

`scope-plan` は**足しただけ**である。既存の名前は 1 つも消していない。

### Job を skip しない理由

skip された Job は check として現れない。required にしている名前が消えると、
**その検査が無いまま Merge できる状態**になる。だから Job は必ず走らせ、
対象外なら先頭の判定 step で `not applicable` と述べて成功で終える。

---

## 5. 何が試験で守られているか

| File | 測っていること |
|---|---|
| `tests/spec_lint/test_ci_scope.py` | 判定そのもの。Fail-Closed を含む 30 件 |
| `tests/spec_lint/test_workflow_minutes_guard.py` | Workflow の形。check 名・concurrency・timeout・not-applicable・安全検査の残存 |

どちらも壊すと落ちる。**設定は消しても静かに壊れる**ので、試験で固定している。
