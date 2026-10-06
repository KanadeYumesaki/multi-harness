<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 25. ConversationSnapshot

`ConversationSnapshot`は、ある時点の会話Contextを再現するためのCore Schemaである。Message集合、Schema Set、設計正本の3つへ束縛する。

共通必須Field：

`snapshot_id, conversation_id, snapshot_hash, message_set_hash, schema_set_hash, design_sha256, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約：

* `message_set_hash`は各`ConversationMessage`の`content_hash`を`sequence_number`昇順で並べたもののHashである。
* `snapshot_hash`は自分自身を入力に取らない。
* ID、時刻、PID、Filesystem列挙順をHash入力へ入れない（不変条件#4）。
* `schema_set_hash`は**実際に使用したSchema Version**だけを含める。未使用Schemaの追加でSnapshotのHashを動かさない。
* `conversation_id`を必須とし、どの会話のSnapshotかをEvidence単体で検証できるようにする。
* Append-onlyとする。

拒否Fixture：

* `conversation_id`の欠落。
* `snapshot_hash`を自身の入力へ含めた値。
* `message_set_hash`が不正Hash。
