<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 26. ApprovalConsumeResult

Owner実回答 DCR-1-A と ACRC-1〜10 を反映した1.0.0。競合の敗者を1 Attempt単位で表す。通常のReplayや未参加の拒否をこのResultへ変換しない。

共通必須Field：

`consume_result_id, grant_id, concurrency_group, attempt_id, state, error_code, successful_consumes, failed_consumes, schema_set_hash, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約：

* `state=REJECTED`、`error_code=APPROVAL_REPLAY`、`successful_consumes=0`、`failed_consumes=1`。このErrorから他Stateへ推測で写さない。
* 呼出側の群IDを、GrantがISSUEDの間に登録する参加Attemptへ束縛する。参加登録はGrantを消費せず、1 Grantにつき1群。未登録・登録済みAttemptの再送はResultを生成せず拒否する。
* 消費CASの結果確定後にResultをAppend-onlyで永続化し、ApplicationがApproval専用StreamへAPPROVAL_REPLAY_DENIEDをAppendする。CAS・Result・Eventを単一SQLite Transactionへ束縛する。
* 群の成功は必ず1件であり、成功+敗者=Nを独立した2接続競合試験で検証する。個別敗者の値で群の保証を置換しない。
* actual_subject_idはconsume_result_id。content_hashは自身以外のResult全文から導出し、Schema Setへ束縛する。

拒否Fixture：必須Field欠落、敗者successful_consumes=1、異なるError/State、未知Field、再送の敗者誤分類、Event失敗時の部分Commit。
