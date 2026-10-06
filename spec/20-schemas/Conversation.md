<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 23. Conversation

`Conversation`は、1本の会話を識別するCore Schemaである。会話の本文は保持せず、識別子と内容Hashだけを持つ。**子Messageを列挙しない。**

共通必須Field：

`conversation_id, conversation_hash, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約：

* `message_ids`を持たない。Message追加のたびに親Recordを書き換えないためである。
* `message_count`を持たない。件数はMessage集合から導出する（不変条件#18）。
* Append-onlyとし、既存RecordをUPDATE／DELETEしない。訂正はCompensating EventのAppendで行う。

拒否Fixture：

* `conversation_hash`が不正Hashまたは欠落。
* `message_ids`または`message_count`を含む。
