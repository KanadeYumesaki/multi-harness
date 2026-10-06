# fde-harness 公開用ソース候補

**公開前の配布コピーです。正式ReleaseでもRuntime GOでもなく、実Providerでの動作も測定していません。**

自然言語の依頼を受け、ローカルCLIまたはChatGPTログイン連携からの生成案を確認し、人が承認してファイルへ反映するための開発中の基盤です。送信と適用は別々に承認します。Providerの導入・認証・実行制限が揃わない場合は停止します。

このコピーは非公開の開発Repositoryから、収録台帳に従って書き出した配布コピーです。開発Repositoryの既存Git履歴は含めず、書き出し時に作った1 Commitだけの新しい履歴を持ちます。開発記録・画面画像・個別引き継ぎ・レビュー記録・Ownerの回答・監査記録・Block Recordは含めません。コード・試験・設計・Schemaと、表示義務のあるライセンスを保持します。公開可否の判断が済んでいない資料は既定で除外しました。コピー元の検査合格やRelease証跡をこのRepositoryの実績に読み替えません。保存監査の固定Git入力は含まないため、その再現検査はこのコピーでは行いません。

## 現在地

| 判定 | このコピーでの状態 |
|---|---|
| ローカル検査（`run-checks.sh` の `LOCAL_OK`） | このコピー自身で実行できます。合成入力による契約試験です |
| GitHub CI | このコピーでは未実行です |
| 実Providerへの送信・接続 | **未測定**です。試験は合成CLIと境界Probeの代役を使います |
| Runtime GO | **未成立**です。WSL2実機のRelease Gateだけが生成できます |
| Human Release | **未成立**です。人のRelease操作と信頼根の設定が必要です |
| Release署名の信頼根 | 設定していません。既定では `UNTRUSTED_REVIEW_ONLY` で止まります（[設定手順](docs/TRUST-ANCHOR-SETUP.md)） |
| 一般公開 | 未実施です。Ownerの承認が必要です（[公開前確認](docs/PUBLICATION.md)） |

## 会話を引き継いでAIを切り替える

コード編集で選んだ会話の全履歴または明示した最近の履歴を、次のAIへの依頼に含めます。
過去の依頼、生成コード、当時の差分、適用確認を保持し、未適用案や失敗は区別します。
保存した会話の全役割の本文も取り込めます。再起動後は会話を選び直して続けられます。
送信前の確認・毎回の送信承認・別の適用承認が必要です。
履歴の欠損・改変・上限超過や結果不明は停止します。使い方は[USAGE.md](USAGE.md)を参照してください。

対象はこのHarness内に保存した会話です。CLI本体の別セッションや非公開の思考過程は取り込みません。
生成コードの試験結果は、実際の適用観測と区別して表示します。

## 試す前に

- WSL2/LinuxのネイティブFilesystem、Python 3.11以上を使用してください。
- 状態DB/CASはコードのDirectoryと分離し、合成データの隔離Worktreeから試してください。
- UIを外部に公開しないでください。実Providerへの送信は契約・利用枠と内容を確認して承認してください。
- 生成コードの安全や完成を保証しません。自動実行しないでください。

## CLIの確認

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
PYTHONPATH=src .venv/bin/python -m harness.presentation.cli --help
```

CLIの導入・ログインはそれぞれの提供元に従ってください。このコピーに資格情報・状態DBは含めません。

## 実CLI用の画面を試す

初回は [隔離Worktreeの準備と起動](docs/TRY-REAL-CLI.md) を使います。新しいDirectoryへ合成ファイル、DB/CAS、実CLI Profileを準備し、本番UIにつなぎます。準備や起動だけでProviderへ依頼を送信することはありません。CLIが利用できない場合にMockへ切り替えません。

```bash
.venv/bin/python tools/prepare_preview.py init --directory "$HOME/harness-preview"
.venv/bin/python tools/prepare_preview.py start --directory "$HOME/harness-preview"
```

公式CLIの導入・認証と境界検証が別途必要です。モデル/推論値、完全payloadを確認し、送信と適用を別々に承認します。

## 検査

```bash
PATH="$PWD/.venv/bin:$PATH" PYTHONPATH=src:tools bash ./run-checks.sh
```

公開版では実際の監査器・検証器を合成Git履歴で検査します。元所有者の実履歴監査は非公開側に保持します。これは検証器の契約試験であり、過去Reportの再現やRuntime GOの証明ではありません。詳しくは [検証範囲](docs/PUBLIC-VERIFICATION.md) と [操作手順](USAGE.md) を参照してください。

[安全な利用](SECURITY.md) / [第三者データと許諾](THIRD_PARTY_NOTICES.md) / [公開前確認](docs/PUBLICATION.md)

生成日: 2026-09-29 / 更新: 2026-09-30 / 作成支援: Codex, Claude Code。元の著作権表示はLICENSEに保持しています。

過去の監査で使ったローカル入力・保存Reportはこのコピーに含めません。元のHomeや元Repositoryへ自動で接続しません。
## 正本の同一性

以下は同封した正本のBytesから取得した識別値です。検査合格やReleaseの証明ではありません。

```text
設計書 : sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31
Registry Snapshot: sha256:03ce54570cb2ecac655877c6c6bd5484ece060f13a465e02e30426c0b5b96343
```

生成日時: 2026-10-04（日本時間）
生成者: Codex（GPT-6系。このセッションの詳細なモデルIDは取得していません）
安全利用: 送信内容と差分を確認して、別々に承認してください。機密情報を含めず、生成コードを実行する前に確認と試験を行ってください。

## ChatGPTログイン連携と履歴の送信範囲

ChatGPTの契約を使う場合は、コード編集欄の「ChatGPTでログイン」から公式ページで連携を許可します。利用可能なモデルをアカウントから取得し、その候補から選びます。CLIの導入やAPI Keyの入力は不要です。公式Responses APIへOAuthで送信する方式で、Web版の既存会話を取得する機能ではありません。

送信内容を確認してから生成を承認し、生成後は別に差分の適用を承認します。ログイン・モデル選択・設定保存だけでは生成しません。認証トークンはサーバーのメモリーだけに保持し、再起動後は再ログインします。切断の遠隔確認に失敗した場合は、画面の案内に従ってChatGPT設定でも確認してください。

「全履歴」または「最近の履歴」を選べます。最初の依頼と保存したsystem・developerメッセージを残し、古い本文を選ばない場合は元のArtifactを保持して、除外理由と参照Hashを記録します。「必ず伝えたい要点・制約」は毎回の送信内容へ含めます。自動のAI要約は呼び出しません。結果不明や改変は、選択範囲の外にあっても停止します。

容量は実際のUTF-8 Bytesで表示します。モデル固有のトークン数と文脈上限は未計測です。このChatGPT連携では出力トークン上限を指定できず、ローカルの受信容量・時間上限は課金上限になりません。[ChatGPTの利用枠・クレジット設定](https://chatgpt.com/settings/usage)で確認してください。実アカウントでのログイン・生成は、合成応答による検査と別の確認です。

CLIを使わない場合も、Linux上の隔離Git Worktreeと、その外のDB/CASを用意します。起動は既存のuiコマンドで、--workspaceを指定し、--cli-runtime-profileを省略できます。CLIも併用する場合は従来どおりProfileを指定します。Workspace自体を省略するとコード編集は未設定のままです。

この拡張は状態DBをversion 14へ移行し、登録情報だけを同じDBに保存します。Access/refresh/ID tokenは保存しません。更新前のDB/CASを保全してください。旧コードへ戻す際はサーバーを停止し、対応する更新前のDB/CASコピーを別Directoryで使います。表やversion番号だけを戻さないでください。

仕様根拠: [公式ログイン](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)、[モデルと生成](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)、[認証情報・利用枠](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)。

更新: 2026-10-05 / Codex（このセッションの詳細モデルIDは取得していません）。LOCAL_OK、実Provider、Runtime GO、Human Releaseは別判定です。


## AIワークスペースの文章用途（2026-10-05）

文章作成・要約・企画・比較・レビュー・相談は、対象ファイルを選ばず使えます。
参考資料・メモを任意で入力し、送信前に内容を確認して承認します。
結果は文章またはMarkdownで履歴に保存し、コピー・ダウンロードできます。
「この会話で続きを依頼」からAIを切り替えて続けられます。
ファイル編集は別の用途です。送信承認に加え、差分の適用を別途承認します。

生成した内容の正確性や専門的な判断を保証しません。機密・個人情報は入力しないでください。
この経路はWeb調査・Word/Excel/PDF・画像生成・自動送信・自動公開を行いません。
作成支援: Codex。内部モデルIDは取得していないため記載しません。
