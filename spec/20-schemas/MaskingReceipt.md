<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 22. MaskingReceipt

`MaskingReceipt`は、入力の正規化、検出、置換、二次検査の結果を監査可能に保存するCore Schemaである。Masking処理の結果は、実際に適用したPolicy SnapshotをHashで束縛し、正規化Profile ArtifactのHashをPolicy Snapshot Hashとして代用してはならない。

共通必須Field：

`masking_receipt_id, source_artifact_hash, masked_artifact_hash, policy_snapshot_hash, normalization_profile_artifact_hash, scanner_profile_hash, masking_result, created_at, content_hash`

制約：

* 永続化前に`CoreSchemaRegistry.validate()`でSchema検証を実行する。
* `policy_snapshot_hash`は適用Policy SnapshotのHashであり、`normalization_profile_artifact_hash`とは別の意味を持つ。
* `normalization_profile_artifact_hash`は再現可能なUnicode正規化Profileを束縛する。
* 二次検査でSecretまたは禁止PIIが残る場合は、後続Effectを生成せずFail-Closedで停止する。
* Receiptは原文やSecret値を含めず、必要最小限のHash・分類・統計だけを記録する。

拒否Fixture：

* `policy_snapshot_hash`が不正Hashまたは欠落。
* 正規化Profile HashをPolicy Snapshot Hash欄へ流用。
* 必須の二次検査結果が欠落。
* Schema検証前の永続化。

Upcaster：Policy束縛、正規化Profile、Secret非保持規約の意味変更はMajor。

### Schema Delivery Gate

* `registries/schemas.yaml`のCore Schema全てにValid／Invalid Exampleが存在。
* `Run`、`ActionIntent`、`ActionAttempt`、`ExecutionPlan`、`InvocationManifest`、`ApprovalGrant`、`RuntimeAttestation`、`EffectReceipt`、`OperationJournal`、`InputReadCapability`はProperty Testを必須化。
* `AT-SCHEMA-CONDITIONAL-001`でRun／ActionAttempt／ApprovalGrantのState／Status別`oneOf`または`if/then`を機械検証する。
* `AT-SCHEMA-COMPLETE-001`でCore SchemaのValid／Invalid Fixture、Hash Pattern、`additionalProperties=false`、Cross-referenceを機械検証する。
* 全Example Hashを`schema-example-manifest.json`へ固定。
* UpcasterはGolden RecordでLossless性を検証。
* Schema Set Hashを生成し、MVP0-A Execution Planへ含める。
* 実ファイル未作成またはTest未実行の状態は`UNVERIFIED`。
