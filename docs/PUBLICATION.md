# GitHub公開前の確認

現在は公開準備中です。現HEADを公開可能と認定した記録ではありません。正式Release / Runtime GOも未成立です。ソースをPreviewとして公開する判断と、業務利用のRelease判定を別々に記録してください。

## 公開対象を固定する

既存RepositoryをそのままPublicにすると、現ファイルだけでなくGit履歴、Commit作者、過去の開発記録や画像も閲覧可能になります。`.gitignore` は既に追跡されたファイルや履歴を隠しません。公開用コピーを選ぶ場合は、元Repository・回答・Reportを非公開で保持し、公開コピーの収録Manifestと新しい検査を作ってください。旧Runtime証跡のCommit束縛を公開コピーへ読み替えないでください。

## 公開前の必要条件

1. 対象Commit/ブランチ/収録ファイル/履歴範囲を確定し、作業木がCleanである。
2. 現ファイルと対象履歴をSecret Scannerで走査する。検出0件だけで安全保証にせず、誤検出・試験用文字列・未判定を所在と根拠で区分する。広いPath除外や全件Baselineで見えなくしない。
3. ローカルPath、メール、スクリーンショット、依頼内容、Commit作者など、公開する個人/環境情報を確認する。値を公開用Reportへ再掲しない。
4. LICENSE・依存ライセンス・脆弱性照合・SBOMを確認する。欠測・通信失敗・適用外環境はPASSにしない。
5. READMEの現在地と起動手順が実装に合い、未完成・制限・危険な使い方が分かる。
6. 公開対象Commitに対する必要検査が通る。ローカル結果をGitHub CI結果へ読み替えない。
7. GitHub上でActionsの権限、Fork PRの承認設定、Branch rules/Required checks、Secret scanning/Push protection、Private vulnerability reportingを確認する。利用可能な項目や契約に差があるため、有効化済みと推測しない。
8. Ownerが公開対象と未達/制限を確認し、公開操作を承認する。

## CIの境界

イベント値はenv経由でShellへ渡し、run本文へGitHub式を展開しない。Actionsは公式参照を確認した完全Commit SHAへ固定し、Checkoutは資格情報を保持しない。Token権限はcontents: readを既定とする。Fork PRにSecretsや実Providerを渡さず、pull_request_targetから未信頼コードを実行する入口を追加しない。

CIは `tools/run_public_dependency_audit.py` からRuntime/開発LockのLicense・脆弱性照合・SBOMを採取します。SBOMの実コマンドは `cyclonedx-py requirements <lock> --spec-version 1.6 --output-reproducible --validate --output-file <new-file>`。`-i` はPackage Index指定なので入力Fileに使いません。

## 同じ対象へ束縛する依存監査

```bash
# Cleanな検査対象Commitで実行。出力先はRepository外の新規Directory。
python tools/run_public_dependency_audit.py --repo . \
  --out /tmp/new-fde-harness-dependency-audit
```

固定LockからPackage名・版を取り出し、導入済みDistributionの名前/版と照合してからLicenseを調べます。PEP 639の `License-Expression` があれば式を検証し、無効な式を古いLicense欄で通しません。License以外のClassifierは根拠になりません。GPL/AGPLの拒否と既存のBUILD_TOOLING例外だけを適用し、名前の大小文字・`.` / `_` / `-` の違いでRuntime例外を有効化できないようにしています。許諾の法的認定ではありません。

脆弱性サービスへ渡す情報は公開Package名と固定版だけです。Provider認証・利用者のpip設定・Proxy環境変数を子Processへ渡さず、再解決・自動更新・脆弱性除外もしません。rc=0だけで成功にせず、適用される全依存が結果に一度ずつ存在し、固定版と一致し、`vulns: []` を観測できたことを検証します。通信失敗・欠測・部分結果は失敗です。

SBOMはRuntime/開発Lockの宣言全部を表し、環境MarkerでLinuxへ導入しないPackageも含みます。各SBOMを2回生成し、Componentの名前/版の完全一致・Schema検証・Bytesの一致を確認します。ライセンス情報は別の `*-licenses.json` に保持します。適用外PackageのLicense/脆弱性はLinuxでは未検証と記録します。OS・Browser・外部CLI全体のSBOM/監査ではありません。

`receipt.json` は対象Commit/Tree・入力Hash・Tool版・段階別argv/終了値/Log Hash/Report Hashを保持します。保存済みDirectoryやReportは上書きせず、監査中にSourceが変わればPASSになりません。新しいCommitには新しい監査が必要です。

## 個別検査の例

```bash
pip-audit --strict --require-hashes --no-deps --disable-pip -r requirements-dev.txt
python tools/check_licenses.py --requirements requirements.txt
python tools/check_licenses.py --requirements requirements-dev.txt
cyclonedx-py requirements requirements-dev.txt --output-file /tmp/fde-harness-sbom.json
```

完全Pin/Hash付きロックの直接照合では依存再解決を省けますが、照合サービスの応答不能は未検証です。SBOMの対象は指定したPython依存であり、OS・ブラウザー・外部CLI全体を包含しません。Gitleaksは公式配布のChecksumを確認した版を隔離領域で使い、`--redact=100` と既定ルールで履歴/作業木を走査します。結果の生値は表示しないでください。

## 開示検査とSecret Scanの分類

個人・環境情報は `tools/scan_public_disclosure.py` で作業木と全Refの履歴を走査します。出力はPath・行・種別・Commitだけで、値は出しません。

```bash
python tools/scan_public_disclosure.py --repo . --history --report /tmp/disclosure.json
```

分類済みの位置は `ci/disclosure-allowlist.json` に、path・line・kind・行のSHA-256の完全一致で置きます。行が変われば未分類へ戻り、使われない項目は失敗にします。Glob・絶対Path・親参照は受け付けません。

Gitleaksの既知検出は `.gitleaksignore` の `path:rule:line` と `ci/secret-scan-classification.json` を1対1に束縛します。現在の行だけでなく、履歴Reportの各Commitの行・File全体・消費者もGit objectから再検証します。過去のPythonを実行することはありません。

除外なし走査は元の木から隔離して行います。空Directoryを `--gitleaks-ignore-path` に渡すだけでは、走査Rootのignoreが使われることがあります。次のToolは原Repositoryのignoreを保持し、独立bare mirrorで全Refの履歴、ignore/configを収録しないGit archiveで現在の木を走査します。暗黙の設定、Baseline、`gitleaks:allow` コメントはraw走査の根拠にしません。

```bash
# 検査対象は元の非公開履歴を含まない、Cleanな公開コピー。
python tools/run_public_secret_scan.py \
  --repo /absolute/path/to/public-copy \
  --gitleaks /absolute/path/to/checksum-verified/gitleaks \
  --out /absolute/path/to/new-secret-scan-evidence
```

出力先は新規Directoryが必要です。既存Reportを再利用しません。raw走査の終了Code 1は検出ありを示し、その新鮮なRedacted Reportが全て分類と一致した場合だけ受理します。除外適用後は終了Code 0かつ検出0が必要です。Scanner失敗・Report欠落/0 Byte/不正JSON・Commit欠落・分類不一致があればToolは非0で止まり、`receipt.json` もPASSにしません。

`git-raw.json` / `dir-raw.json` は値を伏せた検出、`git-classified.json` / `dir-classified.json` は除外適用後の結果です。Receiptが対象Commit/Tree/RefのHash、段階別終了値、ReportのHashを記録します。件数だけ一致させたり、終了Code 1を一律に成功へ変換したりしないでください。

分類器を直接使う場合も種別を明示します。履歴は `--report-kind git`、作業木は `--report-kind dir` です。履歴ReportのCommit欠落を作業木Reportとして扱ってはいけません。分類記録の追加・変更は、値を出力せずに根拠を示せる場合に限ります。

## 正式Releaseの追加条件

Registryの全Case/Gate/領域を未変更Verifierで検証し、環境/Commit/Schema/耐久性の束縛、信頼根、人のRelease操作を別途満たす必要があります。READMEやCIが緑という理由で不足を解消扱いにしてはいけません。

参考: [GitHub公式の安全な利用](https://docs.github.com/en/actions/reference/security/secure-use)。


Metadataの根拠: [Python Core Metadata](https://packaging.python.org/en/latest/specifications/core-metadata/#license-expression)、[PEP 639](https://peps.python.org/pep-0639/)。照合の範囲: [pip-audit公式](https://github.com/pypa/pip-audit)、[CycloneDX Python公式](https://cyclonedx-bom-tool.readthedocs.io/en/latest/usage.html)。

## GitHub設定の実測入口

`tools/check_github_publication_settings.py` はGETだけで、指定した公開先の名前・数値ID・配布Commit/Tree・最新完全CIと実設定を確認します。Actions/Required checksはWorkflowとScopeから導出し、欠測や古いRunを通しません。設定案は生成しますが適用は行いません。非公開の元Repository調査は公開先の合格へ読み替えません。[設定手順とBootstrapの制限](GITHUB-PUBLICATION-SETTINGS.md)を参照してください。Owner承認、全Refの開示/Secret Scan、Runtime GO/Human Releaseは別の条件です。
