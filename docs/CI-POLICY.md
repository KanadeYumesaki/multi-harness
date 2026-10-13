# CIを必要なときだけ実行する

更新: 2026-10-13 / Codex（GPT-6系）

毎日の実行、通常BranchのPush、PRの作成・更新ではGitHub Actionsを起動しません。検査・必須Check・実行権限は維持します。

## 通常の検査

GitHubの「Actions」→「ci」→「Run workflow」で検査するBranchを選びます。clone内のGitHub CLIなら次を使えます。

~~~bash
gh workflow run ci.yml --ref main
~~~

これは指定したCommitの完全検査です。ローカル検査やRuntime GOとは別の判定です。

## 保護されたPRを検査する

GitHubは workflow_dispatch のCheckをPR必須Checkへ評価しません。このため、PRをMergeする検査には、利用者が明示作成する codex/ci-run/ 以下のBranchだけを使います。Jobは全検査のままで、権限を増やしたりBranch保護を外したりしません。

対象Repositoryのcloneで、PRのBranchを取得してから、その同じCommitをCI専用Branchへ送ります。

~~~bash
git fetch origin YOUR_WORK_BRANCH
git push origin FETCH_HEAD:refs/heads/codex/ci-run/YOUR_UNIQUE_REQUEST
~~~

YOUR_WORK_BRANCHとYOUR_UNIQUE_REQUESTを対象に合わせて変更してください。必ず対象Repositoryのcloneで実行し、CIが調べたCommitがPRの最新Headと同じか確認します。CI Branchは既存のCommitを指すだけで、別Commitへ成功結果をコピーしません。

PRを更新した後は、新しい検査要求用のBranchを作ります。同じCommitを再検査する場合は、元のPush Runの「Re-run all jobs」を使えます。PRをMergeするまでCI Branchを削除・更新しないでください。削除もPushイベントになるため、不要な実行を避けるにはそのまま保持してください。

専用Branchを作る操作が、CIを実行したいという明示指示になります。通常の開発Branchやmainへ送るだけでは起動しません。

## 保持する安全条件

- Workflow正本 ci/github-actions-ci.yml と実定義 .github/workflows/ci.yml を同時変更します。
- contents:read、ActionのCommit固定、checkoutの認証情報非保持、timeout、concurrency、必須Check名を維持します。
- CI専用BranchとRun workflowはいずれもPython全版・全pytest・Spec・License/SBOM・deepを実行します。
- 検査Skip、偽のCommit status、管理者によるBranch保護の迂回を使いません。
- 公開設定監査は、default branchの同じCommitの workflow_dispatch Runを引き続き要求します。PR用Push Runをdefault branchの公開実績へ読み替えません。

通常開発は ./run-checks.sh を使います。LOCAL_OK、GitHub CI、実Provider、Runtime GO、Human Releaseは別の判定です。

起動条件を戻す際は、方針・Workflow・監査Tool・試験を同時にレビューしたPRで変更します。

一次資料: [GitHub公式の手動実行](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)、[必須Checkの制約](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)。
