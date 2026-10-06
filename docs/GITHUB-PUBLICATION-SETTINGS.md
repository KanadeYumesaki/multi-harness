# GitHub公開先の設定を確認する

これは公開Source Previewの準備手順です。Ownerによる公開承認、Runtime GO、Human Releaseの証拠ではありません。元の非公開Repositoryと回答・Report・履歴を保持し、別の配布コピーを使います。Repository名やモデル名をToolが創作することはありません。

## 検証する項目

| 項目 | 必要な状態 |
|---|---|
| 公開先 | 指定したowner/nameとGitHub数値IDが一致、Public、非Archived、main |
| 配布Commit | 新しい1 CommitコピーのHEADとTreeが公開先mainのCommit/Treeと一致 |
| Actions | enabled、selected、完全SHAへのPinを必須。現Workflowから導く参照だけを許可 |
| Workflow Token | read、PR Reviewの承認不可 |
| Fork PR | すべての外部Contributorで実行前承認 |
| Branch | 管理者にも保護、force-push/削除不可、PR経路、Stale Review破棄、Bypass Allowanceなし |
| Required checks | 常に出るJob名を導出し、GitHub Actionsの実App IDへ束縛、Strictで最新Branchへ追随 |
| Secret対策 | RepositoryのSecret scanningとPush protectionをそれぞれenabledと観測 |
| 非公開の脆弱性窓口 | Private vulnerability reportingをenabledと観測 |
| 完全CI | 配布Commitのmain push・CI実物の最新Run/最新Attemptで、全Job成功、同Commit・同AppのCheck Runに一致 |

Python行列は `tools/ci_scope.py` の定数から読み、名称はWorkflowから導きます。文書PRでは軽量行列だけが出るため、完全行列の追加版をすべてのPRのRequiredへ入れると永久Pendingになります。常時必須名と配布Commitの完全検査名を分け、Tag専用JobはBranchのRequiredへ入れません。名前を固定値で転記しません。

単独開発者のため、Required Approving Review Countは0でPR経路を要求します。管理者を無条件Bypassへ戻しません。組織的な職務分離や第三者承認を保証する設定ではありません。Harness内の送信承認・適用承認は別の仕組みです。

## GETだけで新しく観測する

GitHub CLIでログインしているWSL/Linux環境で実行します。GitHub.com以外へ送信しません。必要なのは対象Repositoryの設定とActionsの読取り権限です。Secret Alert API、Secrets一覧、認証Token取得APIは呼びません。Repository APIに付随する未知の一時値は比較・保存に使いません。

```bash
# 実在する公開先を指定し、Numeric IDもAPIから取得する。placeholderのまま実行しない。
PUBLIC_REPOSITORY='OWNER/REPOSITORY'
PUBLIC_REPOSITORY_ID="$(gh api "repos/$PUBLIC_REPOSITORY" --jq .id)"
python tools/check_github_publication_settings.py \
  --repo . --repository "$PUBLIC_REPOSITORY" \
  --repository-id "$PUBLIC_REPOSITORY_ID" --mode public-copy \
  --out /tmp/new-github-settings-inspection
```

新しいDirectoryを指定します。Cleanなcheckout、Commit/Tree/入力HashとTool Hashへ束縛し、設定・Runを二度読んで、判定に使うFieldの変更を検出します。APIに付随する一時値や順序だけの違いを設定変更と混同しません。古いReceiptの入力、offlineの合格モード、設定適用・公開・承認省略Flagはありません。通信失敗時はGETだけを最大2回試し、連続輸送障害でCircuitを開いて残りを欠測にします。

`receipt.json` は判定・原因Code・HTTP状態・GETの時刻と対象・Source Hashを保存します。認証Scope/Client ID、Token、外部stderr、Secret Alertの値、未知のAPI本文は保存しません。API403/404/422、欠測、不正型、部分Pagination、別App/古いCommit/古いRun Attempt/再実行中はPASSにしません。CIのSkipも成功へ読み替えません。

非公開元Repositoryの状態調査には `--mode source-survey` を使います。公開先の代わりに合格させず、必ず非0/BLOCKEDになります。非公開Repositoryで非対応の公開専用機能と、権限/通信による欠測を区別します。追加Rulesetがある場合は未Reviewのまま合格にしません。本版はClassic Branch Protectionと空の追加Rulesetを検証する範囲です。

## 設定案を確認してから適用する

`proposed-payloads.json` は適用していない設定案です。参照Actions/Required名はSourceから生成し、App IDは実APIから確認できた場合だけ入れます。IDがnullなら適用せず、確認をやり直します。Tool自体にPUT/PATCH/POSTの実行入口はありません。

| 案のKey | API操作（Owner承認後だけ） |
|---|---|
| actions | PUT `/repos/{owner}/{repo}/actions/permissions` |
| allowed_actions | PUT `/repos/{owner}/{repo}/actions/permissions/selected-actions` |
| token | PUT `/repos/{owner}/{repo}/actions/permissions/workflow` |
| fork | PUT `/repos/{owner}/{repo}/actions/permissions/fork-pr-contributor-approval` |
| protection | PUT `/repos/{owner}/{repo}/branches/main/protection` |
| security | PATCH `/repos/{owner}/{repo}` |
| private_reporting | PUT `/repos/{owner}/{repo}/private-vulnerability-reporting`（Bodyなし） |

この表を自動実行するScriptは付けていません。別の公開先名/ID、配布対象Commit、設定案、未達を確認することが承認対象です。既存非公開Repositoryのvisibility変更やmainへの上書きに使わないでください。設定APIの成功応答だけで完了にせず、GETでReadbackして新しいReceiptを採ります。

## 契約と最初の公開に残る条件

GitHubの機能はRepository visibilityや契約で異なります。既存Repositoryで契約制約の403が出ても、公開先で保護済みとは判断できません。Freeの公開Repositoryと非公開Repositoryは利用条件が異なり、正確な契約名をAPI欠測から推測しません。

最初のmain Commitが無いとBranch保護を設定できません。また、現在の契約で非公開Repositoryの保護が利用不可なら、非公開のままで全設定を先に成立させることはできません。公開Bootstrapの手順・この期間の制限は最終Owner判断の対象です。未承認のPublic化・初回push・有料契約変更・Actions実行で解消しないでください。

この検証のPASSは、観測時点の選択Branchの設定/配布Commit/完全CIについてだけ成立します。他Ref/全履歴の開示とSecret Scan、依存/SBOM監査、README、公開Owner承認は別に必要です。Codeを更新したら配布コピーを再生成し、対象Commitに対する検査を新しく採取します。

一次資料: [Actions permissions](https://docs.github.com/en/rest/actions/permissions)、[Branch protection](https://docs.github.com/en/rest/branches/branch-protection)、[Secret scanningの利用条件](https://docs.github.com/en/code-security/how-tos/secure-your-secrets/detect-secret-leaks/enable-secret-scanning)、[非公開の脆弱性報告](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)、[Workflow Run](https://docs.github.com/en/rest/actions/workflow-runs)、[Workflow jobs](https://docs.github.com/en/rest/actions/workflow-jobs)。


## 公開後にPRで修正した場合

public-copyは初回の1 Commitコピーだけを検査します。その条件は変更しません。
公開後の修正にはpublic-updateを使い、初回公開Rootの40桁SHAを明示してください。
初回Rootと異なる履歴、複数のRoot、現在のmain/Commit/Treeと異なるコピーは拒否します。
mainの完全CIと全設定の条件は初回と同じです。全Refの開示/Secret Scanは別に必要です。

~~~bash
python tools/check_github_publication_settings.py \
  --repo . --repository "$PUBLIC_REPOSITORY" \
  --repository-id "$PUBLIC_REPOSITORY_ID" --mode public-update \
  --public-root-commit <初回公開Rootの40桁SHA> \
  --out /tmp/new-public-update-inspection
~~~

Rootは初回の公開Receiptから確認してください。非公開の元RepositoryのRootを指定して公開版として扱ってはいけません。
この入口もGETだけで、Branch保護の解除、force-push、履歴の書換え、検査の省略は行いません。
