# BLOCKEDからの復帰契約

`BLOCKED_SPEC_CLARIFICATION`、`BLOCKED_EVIDENCE_MISSING`、`HARNESS_FAILURE`をAIの停止理由だけにしない。必ず`blocked/records/BLK-*.json`を作り、次の人間判断または環境変更へ引き継ぐ。

## 記録

```bash
python tools/create_blocked_record.py \
  --task-id MVP0A-SPIKE-03 \
  --release-scope MVP0-A \
  --category SPEC_CLARIFICATION \
  --summary "openat2 fallbackの境界が本文から一意に決まらない" \
  --command python tools/lint_spec.py ... \
  --exit-code 1 \
  --next-action "設計OwnerがFallback許可条件を決定する" \
  --acceptance "設計書へ条件を追記" "spec shardとsnapshotを再生成" \
  --owner maker \
  --design <design> --registry registry-snapshot.json
```

## 再開条件

1. `status=OPEN`の記録を同じTaskで再発させない。
2. OwnerがDecisionまたは環境変更を記録する。
3. `design_sha256`またはRegistry Snapshotが変わった場合、旧記録を解決済みにせず新しいRecordを作る。
4. Acceptanceを実行し、Evidence Pathを追記する。
5. `status=RESOLVED`へ更新してから、`THREAD-START.md`の開始Gateを最初から再実行する。

人間の判断が不要な単純なHarness障害は、原因・修正Commit・再実行結果を同じRecordへ追記する。仕様変更、脅威モデル変更、Release Scope変更は必ず`DECISION_REQUIRED`とする。

## 正式Evidenceで閉じる

`tools/collect_unit_cases.py`が出す**正式Envelope**で閉じる場合は、旧`--evidence`ではなく正式方式を明示して選ぶ。

```bash
python tools/resolve_blocked_record.py \
  --blocker-id BLK-YYYYMMDD-... \
  --formal-manifest "$EVIDENCE_ROOT/units/manifest-with-units.json" \
  --evidence-root "$EVIDENCE_ROOT" \
  --verification-report "$EVIDENCE_ROOT/runtime-report.json" \
  --release-scope MVP0-A \
  --implementation-commit <測定した実装候補Commit> \
  --summary "..." --verified-by "..." "..." \
  --decided-by "..." --decided-at 2026-01-01T00:00:00Z
```

* 旧Evidenceは`evidence_hash`（Body全体の自己Hash）を持つ。正式Envelopeは持たず、**Manifestの`evidence_manifest_hash`がFile BytesのSHA-256**である。**片方の検証をもう片方へ流用しない。**
* 方式の入力が欠けたときに暗黙で片方へ倒さない。どちらも指定しなければ停止する。
* 対象Caseは**Recordの自由文から拾わない。** 保存済みOwner決定（`docs/decision/`）と現行Registry Snapshotの突合から導く。決定当時Unitでも、いまの分類器がそう言っていなければ閉じない。
* `supersedes`を辿って継承した受入条件をすべて集める。設計改訂による`design_sha256`／`registry_snapshot_hash`の更新は正常な履歴であり、**旧RecordのHashを現在値へ書き換えない。**
* Evidence RootはRepositoryの外に置き、実装候補Commitを`--implementation-commit`で明示する。Commit AのEvidenceをCommit Bの実測と呼ばない。
* 全体が`BLOCKED_EVIDENCE_MISSING`でも、対象Caseと対象領域が充足していれば当該Recordは閉じられる。ただし**それはRuntime GOではない。** 除外した未達は件数ごとResolutionへ残す。

更新は検証がすべて通った最後に、`status`／`resolved_at`／`resolution`だけへ行う。不変Fieldが1つでも動けばToolが停止する。
