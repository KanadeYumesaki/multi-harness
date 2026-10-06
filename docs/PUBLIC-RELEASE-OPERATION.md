# 公開ソースとReleaseを区別する運用規則

作成日: 2026-09-29 / 作成: Codex。公開用コピーの現行規則です。
旧所有者がいつ何を実施したかを再現したReportではありません。

1. ソースコードの公開、CIの成功、署名検証、Runtime GO、Human Releaseを別々に扱います。
   CIの `EXTERNALLY_ANCHORED` だけではRelease GOを宣言しません。
2. Repository内の鍵一覧やFingerprintだけを外部信頼根と扱いません。受領者が信頼する鍵と
   検証器の根拠は、Repositoryとは独立した経路で確認する必要があります。
3. 信頼根が未確立なら `UNTRUSTED_REVIEW_ONLY` のままです。公開用コピーは元Repositoryの
   承認、署名、実測結果を引き継ぎません。`trust_anchor` をRuntime Evidence Areaへ追加しません。
4. 配布物を検証する際は、受領者が別Directoryでオフライン検証を行います。信頼する鍵の
   Fingerprint、署名付きTagが指すCommit、配布物のHash、`source_tree_hash` を照合します。
   ZIPの記録だけではGitの祖先関係を再証明できません。署名対象のGit入力が必要です。
5. 検証できない、入力が欠ける、Hashが異なる場合は停止して記録します。ReportのHashを
   追随変更したり、元の回答を新しいCommitへの承認と扱ったりしません。
6. 配布準備は公開の許可ではありません。push、Public化、Release発行はそれぞれ明示的な
   利用者操作にします。この文書を追加しても、外部送信やRelease発行は行いません。

機械的な検証の実装は `tools/verify_trust_anchor.py` と既存の検証試験を参照してください。
Runtime GOはRegistryと実測Evidenceで判定し、合成の契約試験を流用しません。
