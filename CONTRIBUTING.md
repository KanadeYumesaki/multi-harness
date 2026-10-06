# 変更を提出する手順

最初に `AGENTS.md`、`THREAD-START.md`、対象の `spec/` shardを読んでください。設計全文や秘密データをAIへ丸ごと渡さないでください。

## 開発

WSL2のLinux Filesystemで隔離した作業ブランチを使います。Pythonの依存は `requirements-dev.txt` から `--require-hashes` で導入してください。OS側にGit、OpenSSHのssh-keygen、Node.js（ブラウザー検査には対応する実Chromium）などの前提が必要です。未導入を試験Skipで隠さないでください。

```bash
python tools/lint_spec.py --design design-v1.25-runtime-go.md \
  --registries design-source/registries --snapshot registry-snapshot.json --spec-dir spec
./run-checks.sh
python tools/build_ucd_assigned_bitmap.py --version 14.0.0 \
  --out src/harness/masking/ucd/14.0.0-assigned-codepoints --check
git diff --check
```

`run-checks.sh` は全pytestを含みます。同じ全pytestを重複実行する必要はありません。UCD検査は固定HTTPS配布元を参照します。ログには検査対象Commit、コマンド、終了値、Hashを残し、実行していない検査をPASSと記載しないでください。

## PR

目的、変更内容、正常/失敗/境界/権限の検査、影響、既知の制限、復旧手順を記載してください。既存の回答、過去Report、ユーザー変更を保持し、Hash束縛された保存物を機械的に再生成しないでください。テスト削除・Skip・期待値緩和による合格は認めません。

実Provider送信、依存や権限の追加、公開・Releaseなどは、そのTaskの承認範囲を確認してください。CIへ認証情報や実DBを渡さないでください。セキュリティ上の問題は `SECURITY.md` に従って報告してください。
