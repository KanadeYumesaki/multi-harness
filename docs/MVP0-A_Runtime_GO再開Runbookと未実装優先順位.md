# MVP0-A Runtime GO再開Runbookと未実装Case優先順位

## 1. 現状とGO再開条件

現時点のRuntime Verifierは、MVP0-Aを **`BLOCKED_EVIDENCE_MISSING`** と判定しています。静的検査・テストは成功している一方、Runtime GOは実証跡を伴う別のRelease Gateです。現在のブロッカーは、WSL2条件を満たさない実行環境、Environment Manifest未生成、110件すべてのMVP0-A Case Evidence未生成です。

> **重要**：`pytest`の成功やCoverage台帳の更新は、Runtime GOの代替ではありません。Verifierが終了Code `0`を返すまで、GOを宣言しません。

| GO要件 | 現状 | 再開条件 |
|---|---:|---|
| WSL2 | 未充足 | WSL2のLinux環境で実行する |
| Linux native filesystem | 未充足 | `/mnt/c`等を避け、WSL ext4上の`$HOME`配下へ配置する |
| ソースツリー | 要再確認 | 対象commitで`git status --short`が空であること |
| Environment Manifest | 未生成 | 実測Collectorを実行し、HashをManifestへ反映する |
| Case Evidence | 0 / 110 | 全Caseを実行し、入力Hash・Evidence Hash・PASSを生成する |
| Gate Evidence | 0 / 45 | 実行時GateごとのEvidenceを生成する |

## 2. 推奨環境

| 項目 | 必須条件 | 実施例 |
|---|---|---|
| ホスト | Windows 11またはWindows 10上のWSL2 | `wsl --status`で既定Versionが2であることを確認 |
| Linux Distribution | Ubuntu 24.04 LTS相当 | `cat /etc/os-release` |
| Workspace | WSLのext4 filesystem | `/home/<user>/src/fde-harness` |
| Evidence出力先 | **Repository外**のWSL ext4 | `$HOME/runtime-evidence/<release-id>`（§2.2） |
| 禁止配置 | Windows共有Mount | `/mnt/c/...`、`drvfs`、`9p`、`cifs`、`nfs`、`fuseblk`、`overlay`は不可 |
| Python | `requirements*.txt`と整合するVersion | `python3 --version`と依存関係Hashを記録 |
| Git | 固定commit、クリーンツリー | `git rev-parse HEAD`、`git status --short` |
| 実行権限 | `/proc`・mountinfoを読める一般ユーザー | rootでの常用実行は避ける |

### 2.1 WSL2 Workspaceの準備

Windows PowerShellでは、WSL2を導入・確認します。ディストリビューション名は環境に合わせて読み替えてください。

```powershell
wsl --install -d Ubuntu-24.04
wsl --set-default-version 2
wsl --status
```

WSL側では、リポジトリをWindows Mountではなくホームディレクトリ配下へ置きます。

```bash
mkdir -p ~/src
cd ~/src
git clone <repository-url> fde-harness
cd fde-harness

# /mnt配下ではないこと、Gitツリーがクリーンであることを確認
pwd
git status --short
git rev-parse HEAD
findmnt -T "$PWD"
cat /proc/version
```

`findmnt`のfilesystem typeが`9p`、`drvfs`、`cifs`、`nfs`、`nfs4`、`fuseblk`、`overlay`の場合は、そのWorkspaceを使用しません。`/home/<user>`配下のWSL ext4へ移動してください。

### 2.2 Release IDとEvidence出力先を最初に固定する

**Evidenceの出力先はRepositoryの外へ置きます。** Repository内（`runtime-evidence/<release-id>`等）へ
出力すると、Evidenceを作る行為そのものが`git status --short`を汚し、
**同じ手順が要求する「ソースツリーがクリーンであること」を壊します。**
Evidenceは観測結果であり、観測対象のソースツリーの一部ではありません。

`RELEASE_ID`と`EVIDENCE_DIR`は、**Evidenceを1件でも出力する前に**定義します。
以降の手順（§2.2のPython freezeを含む）はすべて`$EVIDENCE_DIR`配下へ書きます。

```bash
cd ~/src/fde-harness

# 出力先はRepository外。$HOME配下のWSL ext4であること。
export RELEASE_ID="mvp0a-$(date -u +%Y%m%d)-$(git rev-parse --short HEAD)"
export EVIDENCE_DIR="$HOME/runtime-evidence/$RELEASE_ID"
mkdir -p "$EVIDENCE_DIR"

# 出力先がRepository外であることと、Windows共有Mount上でないことを機械検査する。
python tools/check_evidence_destination.py --evidence-dir "$EVIDENCE_DIR" --workspace "$PWD"
```

`check_evidence_destination.py`は次を拒否します。いずれも終了Code `3`で停止します。

* `$EVIDENCE_DIR`がWorkspace（Repository）配下にある
* `$EVIDENCE_DIR`または Workspace が`/mnt/*`配下にある
* filesystem typeが`drvfs`、`9p`、`cifs`、`nfs`、`nfs4`、`fuseblk`、`overlay`のいずれか

### 2.3 Python実行環境の固定

**導入は最初からHash強制で行います。** 後段（§5）だけを厳格にしても、
Evidenceを生成する環境がここで作られる以上、意味がありません。
緩い手順で作った環境の上で走らせたTestの結果は、再現できない環境の
観測値でしかなく、Evidenceとして使えません。

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git

cd ~/src/fde-harness
python3 -m venv .venv
source .venv/bin/activate

# `--upgrade pip` を使わない。pip 自身が requirements-dev.txt に
# Hash固定で載っている。Lockの外から最新のpipを取ってくると、
# 「固定した」と言いながらToolchainだけが毎回変わる。
python -m pip install --require-hashes -r requirements-dev.txt

# requirements-dev.txt は requirements.txt を解決済みで含む。
# 両方渡すと `--require-hashes` 下で二重指定になる。CIと同じ1本にする。
python -m pip install --no-deps -e .

# 選択したPythonからpip Moduleを呼ぶ。コピーしたExecutableのShebangは元の導入先を保持し得る。

python --version

# 出力先は §2.2 で定義済みの $EVIDENCE_DIR（Repository外）。
# Repository内へ書くと git status が汚れ、同じ手順が要求する
# 「ソースツリーがクリーンであること」を自分で壊す。
python -m pip freeze --all | LC_ALL=C sort > "$EVIDENCE_DIR/python-freeze.txt"
sha256sum requirements.txt requirements-dev.txt "$EVIDENCE_DIR/python-freeze.txt" \
  > "$EVIDENCE_DIR/python-freeze.sha256"

# freeze 直後にソースツリーが汚れていないことを確かめる。
git status --short    # 出力なしであること
```

`$RELEASE_ID`は`mvp0a-<UTC日付>-<short-commit>`の形です（§2.2で生成）。
Release中にPythonや依存関係を変更した場合は、Environment Manifestおよび
全Runtime Evidenceを失効として扱い、再実行します。

## 3. Environment Manifestの作成手順

### 3.1 Release用ディレクトリとManifest雛形を作る

`RELEASE_ID`と`EVIDENCE_DIR`は§2.2で定義済みです。ここでは雛形を配置するだけです。

```bash
cd ~/src/fde-harness
# $RELEASE_ID / $EVIDENCE_DIR が未定義なら §2.2 へ戻る。ここで定義し直さない。
: "${RELEASE_ID:?§2.2 を先に実行すること}"
: "${EVIDENCE_DIR:?§2.2 を先に実行すること}"

cp runtime-go-manifest.MVP0-A.template.json "$EVIDENCE_DIR/runtime-go-manifest.json"
cp requirements.txt "$EVIDENCE_DIR/python-lock.txt"
```

Manifestの`implementation_repository`、`implementation_commit_sha`、`source_tree_clean`、`schema_set_hash`、`migration_head`は、**手計算で転記せず、Release Harnessが実測して更新する**値です。少なくとも以下の前提値を実測します。

```bash
git status --short                     # 出力なしであること
git rev-parse HEAD                     # implementation_commit_sha
sha256sum registry-snapshot.json       # Registry束縛の照合
python tools/check_design_generation_contract.py \
  --design 'design-v1.25-runtime-go.md' \
  --snapshot registry-snapshot.json \
  --spec-manifest spec/spec-manifest.json \
  --readme README.md
```

### 3.2 実測Environment Manifestを生成する

Collectorは、WSL2判定、Workspace Mount、Python Version、Mountinfo Hash、Python lock Hashを`/proc`から実測します。WSL2以外またはforeign filesystem上では、意図的に終了Code `3`で停止します。

```bash
python tools/collect_runtime_environment.py \
  --workspace "$PWD" \
  --manifest "$EVIDENCE_DIR/runtime-go-manifest.json" \
  --python-lock "$EVIDENCE_DIR/python-lock.txt" \
  --out-root "$EVIDENCE_DIR"

jq . "$EVIDENCE_DIR/environment/runtime-environment.json"
sha256sum "$EVIDENCE_DIR/environment/runtime-environment.json"
```

Collectorの成功後、Release Harnessにより、`runtime-go-manifest.json`の次の値を実測値に更新します。

| Manifest項目 | 値の由来 |
|---|---|
| `implementation_commit_sha` | `git rev-parse HEAD` |
| `source_tree_clean` | `git status --short`が空であること |
| `runtime_environment.environment_manifest_hash` | `runtime-environment.json`のSHA-256 |
| `runtime_environment.is_wsl2` | Collectorの実測値 `true` |
| `runtime_environment.workspace_on_linux_native_fs` | Collectorの実測値 `true` |
| `runtime_environment.python_version` | Collectorの実測値 |
| `schema_set_hash` | Registry／Schema Setの生成結果 |
| `migration_head` | SQLite Migration Headの実測値 |

### 3.3 WSL2上の隔離再検証、実bind mount試験、commit ancestry

Runtime Evidenceの生成前に、対象commitを独立したWSL2 ext4 Workspaceへ再取得し、隔離virtual environmentで開発Lockを**Hash必須**として解決します。この検証はWindows側の既存環境、展開済みZIP、または`/mnt/c`上の作業ツリーを信頼しません。`git status --short`の出力がある場合、以降の検証結果はRelease判定に使用しません。

```bash
cd ~/src/fde-harness
git fetch --tags --prune origin
git checkout --detach <release-commit-sha>
test -z "$(git status --short)"
test "$(findmnt -no FSTYPE -T "$PWD")" = ext4

rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
# §2.2 と完全に同じ3行。手順が2箇所で違うと、どちらで作った環境か
# Evidenceから判別できなくなる。
python -m pip install --require-hashes -r requirements-dev.txt
python -m pip install --no-deps -e .

ruff format --check .
ruff check .
mypy --strict src/harness
pytest tests -q
pytest tests/integration/filesystem/test_workspace_writer_mount_crossing.py -q -rs
```

最後の試験は、同一mount identityの検査だけでなく、Linux mount namespace内で実際のbind mountを作成してWorkspace Writerが境界を越える書込みを拒否することを確認します。`SKIPPED`、`XFAIL`、権限不足、またはmount namespaceを作成できない結果は**PASSではありません**。その場合は、原因と出力をEvidenceとして保存し、必要なnamespace権限を持つWSL2環境で再実行します。

`<release-commit-sha>`が主な実装修正commitを包含することは、Git object databaseを保有する独立Workspaceで明示検証します。コマンドが終了Code `0`を返すことを必要とし、SHAを推測・短縮・置換してはいけません。

```bash
export RELEASE_COMMIT="$(git rev-parse HEAD)"
export REQUIRED_ANCESTOR="3de24e8e6b9be4bdec410a0de9169cc0ca5ff3e0"
git cat-file -e "${RELEASE_COMMIT}^{commit}"
git cat-file -e "${REQUIRED_ANCESTOR}^{commit}"
git merge-base --is-ancestor "$REQUIRED_ANCESTOR" "$RELEASE_COMMIT"
printf 'required_ancestor=%s\nrelease_commit=%s\nverified_at_utc=%s\n' \
  "$REQUIRED_ANCESTOR" "$RELEASE_COMMIT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  > "$EVIDENCE_DIR/trust-anchor/commit-ancestry.txt"
```

### 3.4 外部Trust Anchorの保存

`release-binding.json`の`commit_ancestry`は、生成時に`git merge-base --is-ancestor`で検証したSHAと方法を記録します。しかしZIPはGit object databaseを含まないため、archive-only検証はこの記録の形式とBinding内Hashしか確認できず、祖先関係そのものを再証明できません。Release ZIPの外部に、少なくともGit objectを含む保存物、取得元、検証結果、ZIP SHA-256を保持します。可能なら、認証済みリモートの不変参照または検証済み署名付きtagも保持します。

```bash
mkdir -p "$EVIDENCE_DIR/trust-anchor"
git remote -v > "$EVIDENCE_DIR/trust-anchor/remotes.txt"
git show --no-patch --format=fuller "$RELEASE_COMMIT" \
  > "$EVIDENCE_DIR/trust-anchor/release-commit.txt"
git bundle create "$EVIDENCE_DIR/trust-anchor/repository.bundle" --all
git bundle verify "$EVIDENCE_DIR/trust-anchor/repository.bundle"

# 配布物を1箇所へ集めてから、**相対Pathで** SHA256SUMS を作る。
#
# `sha256sum /abs/path/...` と書くと、SUMS へ生成側の絶対Path
# （例 /home/ubuntu/...）が焼き付く。Hash自体は正しいが、受領者の
# 任意のDirectoryでは `sha256sum -c` がそのまま動かない。
# 検証されない検証手順は、無いのと同じである。
cp "$RELEASE_ZIP" "$EVIDENCE_DIR/trust-anchor/"
cd "$EVIDENCE_DIR/trust-anchor"
sha256sum "$(basename "$RELEASE_ZIP")" repository.bundle > SHA256SUMS
cd - >/dev/null
```

受領側は、SUMSと配布物を同じDirectoryへ置いて次を実行します。

```bash
cd <配布物を置いたDirectory>
sha256sum -c SHA256SUMS
```

このTrust Anchor一式はRelease ZIPと同一の可変な保管場所だけに置かず、アクセス制御されたEvidence保管庫または不変なRelease記録に複製します。署名付きtagや認証済みリモートの参照を検証できない場合、ZIPの`commit_ancestry`は**自己申告記録**にとどまります。その場合は、Runtime GO／Release GOの根拠として独立した祖先検証済みとは宣言しません。

### 3.5 Release ZIPの生成とarchive-only検証

Release ZIPはクリーンなGit treeからのみ作成し、主な実装修正commitを`--required-ancestor`で固定します。生成時の`--verify`はZIP構造、Binding、source tree hashを検証します。受領者や展開済みZIPの利用者は、Git treeを必要としない`--verify-archive`だけを実行します。

```bash
python tools/package_release_zip.py \
  --source-root . \
  --out /path/to/fde-harness-v6.zip \
  --required-ancestor 3de24e8e6b9be4bdec410a0de9169cc0ca5ff3e0 \
  --verify

python tools/package_release_zip.py \
  --verify-archive /path/to/fde-harness-v6.zip
```

`--verify-archive`は`--source-root`、`--out`、`--commit-sha`、`--required-ancestor`、`--verify`と併用できません。成功はアーカイブ構造、重複／Traversal／不安定metadata、Release Binding、commit ancestry記録、設計書／Registry／報告書Hash、source tree hashの整合を示しますが、外部Trust AnchorなしにGit ancestryを再検証したことは意味しません。

### 3.6 Case／Gate Evidenceと最終Verifier

Environment ManifestだけではGOになりません。110件のCaseと45件のGateについて、実行ごとに入力Fixture Hash、Evidence Manifest Hash、実行Status `PASS`を生成します。EvidenceはManifestが指す相対Path、たとえば`tests/cases/<TEST-ID>/<CASE-ID>/evidence.json`へ配置します。

```bash
# 実装済み・未実装を含め、Release対象のCase Harnessを全件実行してEvidenceを生成する
# ここは現状未実装であり、Case Harness／Evidence Emitterの実装が必要です。

./run_verification.sh MVP0-A \
  "$EVIDENCE_DIR/runtime-go-manifest.json" \
  "$EVIDENCE_DIR" \
  "$EVIDENCE_DIR/release"
```

| Verifier終了Code | 意味 | 対応 |
|---:|---|---|
| `0` | `RUNTIME_GO` | Release Manifest生成へ進む |
| `2` | `BLOCKED_EVIDENCE_MISSING` | Case／Gate／Environment Evidence不足を補う |
| `3` | `RUNTIME_NO_GO` | Hash不一致、偽装、件数不整合を修正し、対象Evidenceを再生成する |
| `4` | `INPUT_INVALID` | Manifest、Registry、Scope指定を修正する |

## 4. MVP0-A未実装Caseの優先順位

Coverage台帳では、110 Case中54件が試験に対応し、56件が未実装です。以下の順序は、Runtime GOの危険な抜けを先に閉じ、後続機能の再作業を避けるための推奨順です。

| 優先度 | 未実装Case数 | 対象 | 優先理由 | 完了条件 |
|---|---:|---|---|---|
| P0 | 12 | Context instruction、InvocationManifest、Event順序、Run終端、Policy stale、Config drift | 未承認／古いPolicy／混在Subjectでの実行を止める中核統制。後続Evidenceの前提 | 12 CaseのDomain・Integration試験とEvidenceをPASS |
| P1 | 14 | Crash recovery 11、I/O fault 3 | `Prepared → Execute → Observe → Receipt`の境界で不明Effectを残さない。Workspace／Ledger安全性の根幹 | Crash／ENOSPC／EIOでFail-Closedかつ復旧判断が証跡化される |
| P2 | 4 | Schema complete 2、Migration／Backup 2 | Evidenceの構文有効性・保存可能性・復元可能性を確立する | 22 SchemaのFixture是正、Cross Reference、Fresh Install、Backup RestoreをPASS |
| P3 | 12 | Approval UX 4、Drain 2、GC 2、Emergency Recovery 3、Performance 1 | 本番運用・保守の安全性と利用可能性を高める | Approval回避なし、Drain／GC／緊急経路の監査証跡、性能基準をPASS |
| P4 | 14 | Delegation Grant 14 | 高度な委任機能。MVP0-Aの基本人手承認を置き換えないため、前提統制完了後に着手 | ADR-006準拠の署名、Revocation、Scope、Audit TrailをPASS |

### 4.1 P0：最初に実装する12 Case

| 領域 | Case | 主要な実装単位 |
|---|---|---|
| Context | `AT-CONTROL-DATA-001/ARTIFACT_INSTRUCTION` | Artifact由来命令をData Roleに固定し、Controlへ昇格させないContext Assembly |
| Invocation Manifest | `AT-MANIFEST-SUBJECT-001/MIXED_SUBJECT_STATE`、`VALID_TYPED_SUBJECT` | 型付きSubject、状態混在拒否、Plan／Approval／Attempt束縛 |
| Event順序 | `AT-EVENT-ORDER-001/SEQUENCE_REGRESSION` | Action Attemptの厳密なEvent Sequence照合 |
| Run終端 | `AT-RUN-TERMINAL-001/NO_RELEASE`、`UNRECONCILED_TO_CANCELLED` | Receipt欠損・Cancel不明時のRelease不可 |
| Policy Freshness | `AT-POLICY-STALE-001/*` 5件 | 新規Read／Write／External／PaidはFresh Policy必須、in-flightはEffect状態で分岐 |
| Config | `AT-CONFIG-001/DRIFT` | Policy／Config Hashの比較、Drift時のBlockと再承認 |

### 4.2 実装順序と依存関係

```text
P0 Context + Manifest + Policy Freshness
        ↓
P0 Action Attempt/Event/Terminal統制
        ↓
P1 Crash Recovery + I/O Fault Injection
        ↓
P2 Schema Fixture + Migration/Backup
        ↓
Case Evidence Emitter（110件） + Gate Evidence Emitter（45件）
        ↓
WSL2 Environment Manifest → Runtime Verifier → RUNTIME_GO
```

## 5. Evidence生成の作業分解

| Workstream | 目的 | 成果物 |
|---|---|---|
| A. Release metadata | Commit・clean tree・schema/migrationを固定 | `runtime-go-manifest.json` |
| B. Environment | WSL2／mount／Pythonを実測 | `environment/runtime-environment.json`、`mountinfo.txt` |
| C. Case execution | 110 CaseをFixture付きで実行 | `tests/cases/<test>/<case>/evidence.json` |
| D. Gate execution | 45 Gateを実測 | Gate Evidence Manifest |
| E. Verification | Hash、件数、Path、Statusを照合 | Verifier Report、Release Manifest |

## 6. 作業上の禁止事項

* `/mnt/c`上でEvidenceを生成しない。
* `runtime-environment.json`の`is_wsl2`やfilesystem種別を手編集しない。
* Case EvidenceのHash、Status、Fixture Hashを手入力しない。
* 途中でcommit、依存関係、Schema、Migrationを変更した場合は、当該Evidenceを再利用しない。
* `BLOCKED_EVIDENCE_MISSING`を`RUNTIME_GO`に書き換えない。
* `--verify-archive`の成功を、Git object databaseによるcommit ancestry再検証やRuntime GOと取り違えない。
* Release ZIPだけをTrust Anchorとして扱わない。Git objectを含む外部保存物とその保全Hashなしに祖先関係を独立検証済みと宣言しない。
* 実bind mount試験の`SKIPPED`、`XFAIL`、権限不足をPASSとして集計しない。

## 7. 直近の実行チェックリスト

1. WSL2 Ubuntu上の`$HOME/src`へクリーンな対象commitを配置する。
2. 隔離virtual environmentでHash必須Lock、`ruff`、`mypy`、全`pytest`、実bind mount試験を実行し、SKIPPEDをPASSに含めない。
3. `git merge-base --is-ancestor`でrequired ancestorを検証し、Git bundle・remote・ZIP SHA-256を外部Trust Anchorとして保存する。
4. Release Manifest雛形をEvidence Directoryへ複製する。
5. Environment Collectorを成功させる。
6. P0の12 Caseを実装し、Case Evidence Emitterを整備する。
7. P1、P2を順に完了し、110 Case／45 Gate全件のEvidenceを生成する。
8. `./run_verification.sh MVP0-A <manifest> <evidence-root> <release-root>`を実行する。
9. 終了Code `0`以外ではRuntime GO／Release GOを宣言しない。
