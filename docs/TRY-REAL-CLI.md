# 実CLIを隔離した作業場所で試す

更新: 2026-10-02 / 作成支援: Codex。公開ソースPreviewの手順です。正式Releaseではありません。

この手順は本物のClaude・Codex・Gemini CLI用です。CLIが見つからない、境界を確認できない、認証できない場合にMockへ切り替えることはありません。生成の成功と契約上の利用可能モデルは、実際の送信で別途確認します。

## 最初の準備

READMEの固定依存を入れたWSL2/LinuxのRepositoryで実行します。公式CLIは別に導入し、ご自身の契約でログインしてください。このToolはインストール・ログイン・API Keyの保存を行いません。現在のdiscoveryは `--cli-runtime-root` の `node_modules` にある公式CLIを読み、Gemini用Nodeは `~/.nvm/versions/node` から検出します。通常のPATHにCLIがあるだけでは、このProfileに収録されない場合があります。

```bash
# まだ存在しないDirectoryを選ぶ。親Directoryは事前に存在する必要があります。
.venv/bin/python tools/prepare_preview.py init \
  --directory "$HOME/harness-preview" \
  --cli-runtime-root "$HOME/.local/share/fde-harness/cli-runtime"

.venv/bin/python tools/prepare_preview.py start \
  --directory "$HOME/harness-preview"
```

起動時の `http://127.0.0.1:...` をブラウザーで開きます。URLは起動のたびに変わります。Portを固定する場合だけ `start --port 44487` のように指定できます。Bind先を外部へ変えるOptionはありません。起動中のTerminalは開いたままにします。

`init` は次を準備します。

| 保存先 | 内容 |
|---|---|
| `seed/` | 合成ファイルだけを持つ新しいGit Repository。既存開発履歴をコピーしない |
| `worktree/` | seedから切り離したWorktree。初期対象は `hello.py` と `notes.md` |
| `state/` | 本番UIが使う単一DBとCAS。Worktreeの外に分離 |
| `runtime/` | 導入済みCLIのProfileと、空の中立作業Directory |
| `preview.json` | 準備Path・Profile Hashの照合用metadata。Runtime Evidenceではない |

既存Directory/File/Symlinkは上書きしません。Source Repositoryの中、Windows Filesystem、`/mnt/*`、許可されないFilesystem、Symlinkの親は拒否します。Gitのglobal/system config、hooks、署名、askpass、Provider Key環境変数を準備Processへ引き継ぎません。資格情報はコピーしません。

準備失敗時は部分Directoryを残し、可能な場合は `preparation-failed.json` へ原因Codeを保存します。資格情報や外部stderrの値は出しません。内容を確認して別の新しいDirectoryでやり直してください。既存の準備を自動削除して再作成する操作はありません。

## 画面から編集する

1. コード編集欄でProviderを選びます。**境界検証済み**と**認証/実接続**は別の表示です。拒否理由があれば解消するまで送信できません。
2. CLIに対応するモデルと推論レベルを選びます。表示候補は利用契約の保証ではありません。候補がない場合は、実際に利用できるモデルIDを手入力します。存在しないIDや推論値をToolが作ることはありません。
3. `hello.py` を選び、「日本語の説明コメントを追加してください。」など、小さな依頼を入力します。複数選択は1ファイルずつ進みます。
4. 完全な送信payload、対象、モデル、推論値、Hash、期限、利用枠/費用の未確認項目を見て、送信を承認します。**準備・起動・設定保存だけでは生成しません。**
5. 実CLIの結果から差分を確認し、別の承認で適用します。送信承認を適用承認に流用しません。生成コードは自動実行しません。
6. 適用Receiptと対象以外が変わっていないことを確認します。次のファイルは、新しい生成・差分承認で進めます。

設定を保存しても、確認中のPlanは変わりません。別設定で送る場合は新しい依頼を作成します。CLI自身の内部通信再試行はHarnessから完全には制御できないため、CLI起動1回をHTTP要求1回と同一視しません。契約や利用枠、追加費用が不明な場合は送信前に確認してください。

## 止める・再開する

TerminalでCtrl+Cを押して本番UIを終了します。再度 `start` すると同じDB/CASと選択設定を使います。Profileや準備Pathの改変・Symlinkへの交換は起動前に拒否します。Profileを作り直す場合は、新しいPreview Directoryを使います。

送信中に終了/通信障害が起きた場合、結果不明を成功や未送信と決めつけないでください。Journal/Receiptを確認し、**自動再送しません**。更新前は既存Backup手順でDB/CASを保全し、古いコードへ戻す場合は対応するBackupを別Directoryへ復元します。Schema version番号だけを戻さないでください。

## 初版の範囲

既存のUTF-8ファイルを1件ずつ完全置換するPreviewです。操作toolはCLIから外し、Workspace/DB/CASの境界と子孫Processの確認を起動前に検査します。CLI本体は認証と通信の信頼主体であり、独立した宛先制限やCLI自身からのCredential遮断は提供しません。単一UIDに対する改ざん耐性や組織的な職務分離を主張しません。

新規ファイル作成/削除・まとめて複数ファイルを生成して一括適用・取消しButton・金銭上限の保証・Runtime GO/Human Releaseは、この入口が完成した証明ではありません。実ブラウザーの合成生成試験と、実Providerの生成成功も区別します。

公開先への送信/Public化は [公開前確認](PUBLICATION.md) と [GitHub設定確認](GITHUB-PUBLICATION-SETTINGS.md) の対象です。ローカルのPreview起動は公開操作の承認ではありません。
