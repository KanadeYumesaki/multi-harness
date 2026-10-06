# 新規AIスレッド開始契約

このファイルは、スレッドに依存しない同一品質の開発開始手順である。

## 開始前Gate

1. Implementation RepositoryとWorkspaceがWSL2 Linux native filesystem上にあること。`/mnt/*`上でCode生成しない。
2. `AGENTS.md`を最初に全文読む。
3. `spec/spec-manifest.json` の `source_hash`と設計書SHA-256を照合する。
4. `python tools/lint_spec.py --design <design> --registries design-source/registries --snapshot registry-snapshot.json --spec-dir spec`を実行する。
5. 1スレッドで1つのTaskだけを扱い、`TASK-BRIEF.template.md`を埋める。
6. 同一Taskの`blocked/records/BLK-*.json`に`OPEN`がある場合は重複実行せず、`BLOCKED-RECOVERY.md`の復帰条件を先に処理する。
7. Release候補では`RUNTIME_GO_VERIFIER_SHA256`（CI保護変数）を確認する。未設定環境は`UNTRUSTED_REVIEW_ONLY`であり、Runtime GOを宣言しない。

## 読み込み順序

```text
AGENTS.md
→ spec/spec-manifest.json
→ spec/00-common.md
→ 対象Phase shard（MVP0-Aならspec/10-mvp0a.md）
→ 関連Registryの対象行
→ 触るSchemaのspec/20-schemas/<Schema>.md
→ TASK-BRIEF
→ OPEN BLOCKED Record（該当Taskがある場合は復帰判定まで実装しない）
```

`spec/99-reference/`は実装Taskの文脈に含めない。本文のみで判断できない場合は、推測せずTaskを`BLOCKED_SPEC_CLARIFICATION`とする。

## 完了Gate

Spec Lint、Manifest Validator、型検査、変更PackageのUnit、関連Integration、Security Checkの全合格とEvidence保存を完了条件とする。
