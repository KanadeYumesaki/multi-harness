# Verifier信頼境界

`verify_runtime_go.py` と `ci/verifier-source.sha256` は同じCommitで無許可に変更しない。Verifier変更時は必ず次を行う。

1. `AT-VERIFIER-*` 全件を実行。
2. 設計書の変更がある場合は、Spec LintとRegistry Snapshotを再生成。
3. `python tools/refresh_verifier_integrity.py --target verify_runtime_go.py --expected ci/verifier-source.sha256` を実行。
4. ブランチ保護環境で、Verifier変更とHash更新のReviewを分離する。

Release環境では、Organization／Environment保護変数`RUNTIME_GO_VERIFIER_SHA256`へ同じHashを登録し、CIから`--trusted-hash`で渡す。変数未設定のローカル検証は整合性チェックに留まり、Runtime GOの信頼根とはしない。

Hashファイル自体を改ざんできる単一UIDに対する改ざん耐性は提供しない。GitHubのRequired Checkと保護Branchを使えない環境では、Release GOではなく`UNTRUSTED_REVIEW_ONLY`とする。
