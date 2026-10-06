<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 24. ConversationMessage

`ConversationMessage`は、会話中の1発話を表すCore Schemaである。`conversation_id`で親を指す**子→親の単方向参照**を持つ。

共通必須Field：

`message_id, conversation_id, role, sequence_number, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約：

* `role`は§1.16.4のControl／Data Message Roleと同じ語彙を使う。Chat専用のrole値を追加しない。
* Provider出力は`UNTRUSTED_PROVIDER_DATA`であり、本文に命令形式が含まれていてもControl Roleへ昇格しない。
* 順序は`sequence_number`で判定する。採番IDと時刻を順序キーにしない（不変条件#4）。
* `token_count`を持たない。Token会計は`TokenBudgetPolicy`と`TokenProfileSnapshot`が持つ。
* Append-onlyとする。

拒否Fixture：

* `role`が§1.16.4の語彙に無い値。
* `sequence_number`の欠落。
* `token_count`を含む。
* `content`／`text`／`body`をinlineで含む。

`2.0.0`（v1.22）：

`content_artifact_hash`をRequiredへ追加する。§15.2によりRequired追加はMajorであり、
`1.0.0`は上書きせずread_onlyで残す。追加Fieldは1つだけで、他のFieldは`1.0.0`と同じである。

共通必須Field（`2.0.0`）：

`message_id, conversation_id, role, sequence_number, content_artifact_hash, record_id, schema_name, schema_version, created_at, producer, content_hash`

制約（`2.0.0`）：

* `content_artifact_hash`は`ContextFragment@2.0.0`と同名・同義であり、形式は`^sha256:[0-9a-f]{64}$`である。
* 参照先はArtifact CASであり、`ArtifactManifest`で解決する。参照先が存在しないMessageを受理しない。
* 本文をinlineで持たない。
* `content_hash`は本文Hash・`role`・`sequence_number`から導出する。

拒否Fixture（`2.0.0`）：

* `content_artifact_hash`の欠落。
* `content_artifact_hash`が不正Hash形式。
* 参照先ArtifactがManifestに無い。
