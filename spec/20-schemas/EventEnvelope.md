<!-- generated; source_hash=sha256:cef978e653bbf16b0f3c1cf9b3125829c4d3fea742db2f93753016c64f889f31; do not edit -->

### 1. EventEnvelope

必須Field：

`event_id, stream_id, stream_type, stream_sequence, global_sequence, event_type, event_version, causation_id, actor_id, payload_schema_id, payload_hash, previous_event_hash, event_hash`

制約：

* `(stream_id, stream_sequence)`、`global_sequence`、`event_id`は一意。
* `event_type`は§1.4.2と§1.8の正規Eventだけ。
* `event_hash`はHeader＋Payload Hash＋Previous HashをDomain-separated Hash。
* Payload本文は別Artifact参照可。

規範Fixture断片：`{"event_type":"PLAN_RESOLVED","stream_sequence":4}`
拒否例：`{"event_type":"ACTION_SUCCEEDED"}`
Upcaster：未知Event名を推測変換しない。明示Mappingがある旧EventのみProjection時に変換。
