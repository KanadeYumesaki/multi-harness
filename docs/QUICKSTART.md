# はじめての起動

更新: 2026-10-06 / 作成支援: Codex。ローカルで試すSource Previewです。詳細モデルIDは取得していません。

multi-harnessは、同じ画面からAIを選び、文章・Markdown・既存ファイルの編集を依頼する道具です。
文章はファイル指定なしで作れます。コードの編集は、送信を承認したあと、差分を確認してもう一度承認します。
起動しただけでAIを呼んだり、生成コードを実行したりしません。

## 1. 必要なもの

- WSL2のLinux側の保存場所。検査対象のPythonは3.11と3.12です。Windows側や /mnt/ は使えません。
- Git、Pythonのvenv、ブラウザー。試験にはssh-keygenも必要です。
- CLIを使う場合はNode.js 22系、公式CLI、ご自身のログイン。
- ChatGPTログイン連携だけで使う場合は、CLIの導入は不要です。下の専用手順を使います。

UbuntuでGit、venv、ssh-keygenが無い場合は、OSのPackage Managerで導入してください。
必要なものの導入やAI利用には、それぞれの提供元の契約・利用枠が適用されます。

~~~bash
read -r -p "RepositoryのCodeボタンでコピーしたHTTPS URL: " PUBLIC_REPOSITORY_URL
git clone "$PUBLIC_REPOSITORY_URL" multi-harness
cd multi-harness
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
export PYTHONPATH=src
.venv/bin/python -m harness.presentation.cli --help
~~~

Pythonの固定依存はrequirements-dev.txtにHash付きで収録しています。認証情報、会話のDB、生成物、CLIの設定は、このRepositoryへ入れません。

## 2A. 公式CLIを使う

この版のProfile自動検出は、専用のcli-runtime/node_modules内の導入物を読みます。
通常のPATHにCLIがあるだけでは検出されない場合があります。
動作確認に用いた版はClaude Code 2.1.263、Codex 0.153.4、Gemini CLI 0.58.0です。
最新版という意味ではありません。新しい版を使うときは境界検査・CLIの引数検査から確認してください。

Node.js 22系とnpmが使えるLinux環境で、使うCLIだけを固定版で導入します。
次の例は3つをまとめたものです。任意のPackageを自動更新するコマンドではありません。

~~~bash
CLI_ROOT="$HOME/.local/share/fde-harness/cli-runtime"
npm install --prefix "$CLI_ROOT" --save-exact --ignore-scripts --omit=dev \
  @anthropic-ai/claude-code@2.1.263 \
  @openai/codex@0.153.4 \
  @google/gemini-cli@0.58.0
~~~

Gemini用Nodeの自動検出は ~/.nvm/versions/node を対象にします。別の場所のNodeを使う場合は、Profileの検出結果を確認してください。
Packageの導入後、CLI本体を起動して、ご自身のアカウントでログインします。
認証の手順は[Claude Code](https://code.claude.com/docs/en/setup)、[Codex](https://developers.openai.com/codex/auth/)、[Gemini CLI](https://geminicli.com/docs/get-started/authentication/)を参照してください。
API Keyを作成・入力することはこの手順の必須条件ではありません。すでにAPI Key方式を選んでいるCLIは別の課金経路になり得るため、送信前に確認してください。

新しい試用場所を作り、画面を起動します。すでにある場所は上書きされません。

~~~bash
.venv/bin/python tools/prepare_preview.py init \
  --directory "$HOME/multi-harness-demo" --cli-runtime-root "$CLI_ROOT"
.venv/bin/python tools/prepare_preview.py start --directory "$HOME/multi-harness-demo"
~~~

端末に表示された http://127.0.0.1:... を開きます。端末を閉じると停止します。
初期ファイルは合成のhello.pyとnotes.mdだけです。
失敗時にMockへ切り替える機能はありません。[詳しい起動・境界の説明](TRY-REAL-CLI.md)も参照してください。

## 2B. ChatGPTログイン連携だけで使う

この経路は公式OAuthとResponses APIを使います。Web版の画面を自動操作する方式ではありません。
API Keyは入力せず、ChatGPTの契約・利用枠・クレジット設定に従います。
利用可能なモデルはログイン後に確認します。無料・追加費用なしを保証する機能ではありません。

CLI Profileのない隔離Git Worktreeを用意します。次のPythonはGitを引数配列で呼び、合成ファイルだけを作ります。
既存のmulti-harness-chatgpt-demoがある場合は停止し、削除や上書きをしません。

~~~bash
.venv/bin/python - <<'PY'
import os
import subprocess
from pathlib import Path

root = Path.home() / "multi-harness-chatgpt-demo"
root.mkdir(mode=0o700)
seed = root / "seed"
seed.mkdir(mode=0o700)
(root / "state" / "cas").mkdir(parents=True, mode=0o700)
(seed / "hello.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n', encoding="utf-8")
(seed / "notes.md").write_text("# 合成の試用メモ\n", encoding="utf-8")
env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
def git(*args):
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null",
                    "-c", "commit.gpgsign=false", "-c", "user.name=Demo User",
                    "-c", "user.email=demo@example.invalid", *args],
                   cwd=seed, env=env, check=True, timeout=30)
git("init", "-b", "codex/demo")
git("add", "--", "hello.py", "notes.md")
git("commit", "-m", "chore: create synthetic demo")
git("worktree", "add", "--detach", str(root / "worktree"))
PY

.venv/bin/python -m harness.presentation.cli ui \
  --database "$HOME/multi-harness-chatgpt-demo/state/state.sqlite3" \
  --artifact-root "$HOME/multi-harness-chatgpt-demo/state/cas" \
  --repo-root "$PWD" \
  --workspace "$HOME/multi-harness-chatgpt-demo/worktree"
~~~

画面の「ChatGPTでログイン」で公式ページへ進みます。接続後にモデル・推論値を選びます。
ログインだけでは生成しません。サーバー再起動後は再ログインします。
認証トークンはサーバーのメモリーだけに保持します。
公式仕様は[登録とログイン](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)を参照してください。

## 3. 最初の依頼

文章を試す場合は「AIワークスペース」で文章・要約などを選びます。
「短い自己紹介の雛形をMarkdownで作成してください。個人名は含めないでください。」と依頼し、
AI・モデル・推論値を確認します。ファイル選択は不要です。

「送信内容を確認」では、AIへ渡す依頼・資料・会話を含む完全なJSONが表示されます。
内容と利用枠を確認して送信を承認します。結果はコピー・.md/.txtの保存で使えます。

コードを試す場合は「コード・ファイル編集」でhello.pyを選び、
「日本語の説明コメントを追加してください。」と依頼します。
生成後に差分を確認し、送信とは別に適用を承認します。自動実行はしません。
複数選択は1ファイルずつ生成・確認・適用して進みます。

## 4. AIを切り替えて続ける

同じ会話を選び、次の依頼でClaude・Codex・Gemini・ChatGPTを切り替えられます。
全履歴または明示した最近の履歴と「必ず伝えたい要点・制約」を送信前に確認します。
上限を超えると停止します。自動で内容を捨てたり、無料のAI要約を保証したりしません。

ChatGPT Web版の会話・メモリーはログインだけで取得できません。
ご自身が書き出したJSONから選んだ会話、または貼り付けた本文を、内容確認後にローカル保存できます。
画像や別の分岐など、取り込めない内容は画面に示します。[取り込みの手順](CHATGPT-KNOWLEDGE.md)を参照してください。

## 困ったとき

| 表示・状況 | 確認すること |
|---|---|
| CLIが見つからない | 専用cli-runtime/node_modulesへの導入、Gemini用Nodeの保存先、Profileの検出結果 |
| 境界検査で拒否 | 表示された理由を確認。Linux Kernel・Filesystem・CLI版が対応しない場合、Flagで回避しない |
| 認証で止まる | 対象CLIを端末で起動し、本人がログイン。認証FileをRepositoryへコピーしない |
| モデル・推論値が無い | 導入CLIのProfileまたはログイン後の候補を確認。未対応の値を創作しない |
| 送信量が大きい | 会話・資料を自分で選び直す。省略された内容を送信前に確認 |
| 受付と復旧が参照専用 | 通常利用に操作不要。必要な場合はUSAGE.mdの現在UIDによる起動手順を使う |
| 送信結果が不明 | 同じ依頼を再送せずJournal/Receiptを確認。自動再送しない |
| 起動場所が拒否 | Linux側の新しい専用Directoryを使う。/mnt/、Windows側、Source内の状態保存は不可 |
| 更新後に古い版へ戻す | Serverを停止し、更新前のDB/CASの対応するコピーを別Directoryで復元 |

通常の停止は端末でCtrl+Cです。同じ保存先で再起動すればローカル会話を保持します。
接続・課金、履歴の送信範囲、安全境界は[USAGE.md](../USAGE.md)と[SECURITY.md](../SECURITY.md)に記載しています。

## 検査と現在の制限

~~~bash
PATH="$PWD/.venv/bin:$PATH" PYTHONPATH=src:tools bash ./run-checks.sh
python tools/scan_public_disclosure.py --repo . --history --report /tmp/multi-harness-disclosure.json
~~~

実ブラウザーの検査はNode.jsとLinux版Chromiumが別途必要です。詳しくは[検証範囲](PUBLIC-VERIFICATION.md)を参照してください。
全テストとCIが通っても、実AIの生成成功・利用料金・Runtime GO・Human Releaseは別の確認です。
この版は新規File作成・削除、まとめて一括生成・一括適用、適用取消し、Office/PDF/画像生成、課金上限の保証を提供しません。
UIをインターネットへ公開するための認証・多人数運用の製品ではありません。
