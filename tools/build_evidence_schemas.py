"""Case Evidence の JSON Schema を 1.2（凍結）と 2.0（現行）で書く。

1.2 は既存 Evidence の形をそのまま記述する。**既存 Evidence を変えない。**
2.0 は evidence_kind と event_observation を Required で足し、
event_observation と event_sequence の対応を if/then で機械強制する。
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "schemas/evidence/CaseEvidence"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SHA = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
NULLABLE_STR = {"type": ["string", "null"]}

_COMMON_PROPS = {
    "case_id": {"type": "string", "minLength": 1},
    "test_id": {"type": "string", "minLength": 1},
    "gate_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
    "status": {"enum": ["PASS", "FAIL"]},
    "mismatched_fields": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
    "implementation_commit_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
    "design_sha256": SHA,
    "registry_snapshot_hash": SHA,
    "schema_catalog_hash": SHA,
    "fault_injection": {"type": ["object", "null"]},
    "source_tree_clean": {"const": True},
    "input_fixture_path": {"type": "string", "minLength": 1},
    "input_fixture_hash": SHA,
    "environment_manifest_hash": SHA,
    "expected": {
        "type": "object",
        "required": ["state", "error_code", "event_sequence", "expectation_descriptor_hash"],
        "properties": {
            "state": {"type": "string"},
            "error_code": NULLABLE_STR,
            "event_sequence": {"type": "array", "items": {"type": "string"}},
            "expectation_descriptor_hash": SHA,
        },
        "additionalProperties": False,
    },
    "side_effects": {"type": ["object", "null"]},
    "test_node_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
    "producer": {"const": "tools/emit_case_evidence.py"},
    "evidence_hash": SHA,
}

_COMMON_REQUIRED = [
    "evidence_schema_version",
    "case_id",
    "test_id",
    "gate_ids",
    "status",
    "mismatched_fields",
    "implementation_commit_sha",
    "design_sha256",
    "registry_snapshot_hash",
    "schema_catalog_hash",
    "fault_injection",
    "source_tree_clean",
    "input_fixture_path",
    "input_fixture_hash",
    "environment_manifest_hash",
    "expected",
    "observed",
    "side_effects",
    "test_node_ids",
    "producer",
    "evidence_hash",
]

# --- 1.2（凍結） -----------------------------------------------------------
v12 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://fde-harness.local/schemas/evidence/CaseEvidence/1.2.schema.json",
    "title": "Case Evidence v1.2",
    "description": (
        "Runtime GO 判定の Case 単位 Evidence（凍結）。"
        "evidence_kind と event_observation を持たないため、Ledger を観測したのか"
        "していないのかを Evidence 単体から読めない。"
        "Release 判定の入力として受理しない（設計書 v1.15 §15.11.2）。"
        "既存の Development Evidence は本形式のまま残す。2.0 へ変換しない。"
    ),
    "type": "object",
    "required": list(_COMMON_REQUIRED),
    "properties": {
        "evidence_schema_version": {"const": "1.2"},
        **_COMMON_PROPS,
        "observed": {
            "type": "object",
            "required": ["state", "error_code", "event_sequence", "actual_subject_id"],
            "properties": {
                "state": NULLABLE_STR,
                "error_code": NULLABLE_STR,
                "event_sequence": {"type": "array", "items": {"type": "string"}},
                "actual_subject_id": NULLABLE_STR,
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

# --- 2.0（現行） -----------------------------------------------------------
v20 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://fde-harness.local/schemas/evidence/CaseEvidence/2.0.schema.json",
    "title": "Case Evidence v2.0",
    "description": (
        "Runtime GO 判定の Case 単位 Evidence。"
        "evidence_kind が層を、event_observation が Ledger 観測の有無を"
        "Evidence 単体から読める形で持つ（設計書 v1.15 §26.2.1）。"
        "Required Field 追加と event_sequence の意味変更のため、"
        "§15.2 により Major 版として発行した。"
    ),
    "type": "object",
    "required": [
        *_COMMON_REQUIRED,
        "evidence_kind",
        "event_observation",
        "event_observation_policy",
    ],
    "properties": {
        "evidence_schema_version": {"const": "2.0"},
        "evidence_kind": {
            "enum": ["UNIT", "ORCHESTRATION"],
            "description": (
                "検証した層。**領域所属ではない。** どの Evidence Area へ数えるかは "
                "evidence-areas.yaml と Registry が決める。"
            ),
        },
        "event_observation_policy": {
            "enum": ["REQUIRED_EMPTY", "NOT_APPLICABLE", None],
            "description": (
                "Registry が宣言した Event 観測 Policy を写したもの。"
                "Verifier は Release 束縛の Snapshot と照合する。"
                "**欠落を値へ変換しない。** 期待Event列が非空の Case は null である。"
            ),
        },
        "event_observation": {
            "enum": ["OBSERVED", "OBSERVED_EMPTY", "NOT_APPLICABLE"],
            "description": (
                "OBSERVED: Ledger を観測し Event 列を取得した。"
                "OBSERVED_EMPTY: Ledger を観測し Event 列が空だった。"
                "NOT_APPLICABLE: Event 観測を要求しない Unit Case である。"
                "**未観測を 0 件観測へ変換しない。**"
            ),
        },
        **_COMMON_PROPS,
        "observed": {
            "type": "object",
            "required": ["state", "error_code", "event_sequence", "actual_subject_id"],
            "properties": {
                "state": NULLABLE_STR,
                "error_code": NULLABLE_STR,
                "event_sequence": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": (
                        "NOT_APPLICABLE のときだけ null。"
                        "OBSERVED_EMPTY のときは [] であり null にしない。"
                    ),
                },
                "actual_subject_id": NULLABLE_STR,
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
    "allOf": [
        {
            "title": "NOT_APPLICABLE では event_sequence を null にする",
            "if": {
                "properties": {"event_observation": {"const": "NOT_APPLICABLE"}},
                "required": ["event_observation"],
            },
            "then": {
                "properties": {"observed": {"properties": {"event_sequence": {"type": "null"}}}}
            },
        },
        {
            "title": "OBSERVED_EMPTY では event_sequence を空配列にする",
            "if": {
                "properties": {"event_observation": {"const": "OBSERVED_EMPTY"}},
                "required": ["event_observation"],
            },
            "then": {
                "properties": {
                    "observed": {"properties": {"event_sequence": {"type": "array", "maxItems": 0}}}
                }
            },
        },
        {
            "title": "OBSERVED では非空の Event 列を要求する",
            "if": {
                "properties": {"event_observation": {"const": "OBSERVED"}},
                "required": ["event_observation"],
            },
            "then": {
                "properties": {
                    "observed": {"properties": {"event_sequence": {"type": "array", "minItems": 1}}}
                }
            },
        },
        {
            "title": "REQUIRED_EMPTY は Orchestration の OBSERVED_EMPTY である",
            "if": {
                "properties": {"event_observation_policy": {"const": "REQUIRED_EMPTY"}},
                "required": ["event_observation_policy"],
            },
            "then": {
                "properties": {
                    "evidence_kind": {"const": "ORCHESTRATION"},
                    "event_observation": {"const": "OBSERVED_EMPTY"},
                }
            },
        },
        {
            "title": "NOT_APPLICABLE は Unit の NOT_APPLICABLE である",
            "if": {
                "properties": {"event_observation_policy": {"const": "NOT_APPLICABLE"}},
                "required": ["event_observation_policy"],
            },
            "then": {
                "properties": {
                    "evidence_kind": {"const": "UNIT"},
                    "event_observation": {"const": "NOT_APPLICABLE"},
                }
            },
        },
        {
            "title": "ORCHESTRATION は常に Ledger 観測を要求する",
            "if": {
                "properties": {"evidence_kind": {"const": "ORCHESTRATION"}},
                "required": ["evidence_kind"],
            },
            "then": {"properties": {"event_observation": {"enum": ["OBSERVED", "OBSERVED_EMPTY"]}}},
        },
    ],
}

# --- 3.0（現行） -----------------------------------------------------------
#
# 2.0 との違いは `observed` へ Ledger Head を Required で足したことだけである。
# 拒否したときに Head が動いていないことを、Evidence 単体から確かめられるようにする。
# 2.0 では観測はしていたが Evidence 本体へ載らず、Release 判定へ渡らなかった。
#
# Unit Case は Ledger を観測しない。測った値が無いので `null` にする。
# **0 で埋めない。** 埋めれば「見て 0 だった」と偽ることになる。
_HEAD = {
    "type": ["integer", "null"],
    "minimum": 0,
    "description": (
        "観測時点の Stream Head。NOT_APPLICABLE のときだけ null。**未観測を 0 へ変換しない。**"
    ),
}

v30 = json.loads(json.dumps(v20))
v30["$id"] = "https://fde-harness.local/schemas/evidence/CaseEvidence/3.0.schema.json"
v30["title"] = "Case Evidence v3.0"
v30["description"] = (
    "Runtime GO 判定の Case 単位 Evidence。"
    "2.0 へ ledger_head_before ／ ledger_head_after を Required で足した。"
    "拒否時に Head が動いていないことを Evidence 単体から確かめられる。"
    "Required Field 追加のため、§15.2 により Major 版として発行した。"
    "2.0 は read_only として残す（Owner Decision DEC-EVT-PRED-B / B-1）。"
)
v30["properties"]["evidence_schema_version"] = {"const": "3.0"}
v30["properties"]["observed"]["required"] = [
    *v20["properties"]["observed"]["required"],
    "ledger_head_before",
    "ledger_head_after",
]
v30["properties"]["observed"]["properties"]["ledger_head_before"] = dict(_HEAD)
v30["properties"]["observed"]["properties"]["ledger_head_after"] = dict(_HEAD)

# Event 列と同じ規則を Head へも当てる。観測していない Case は null である。
v30["allOf"] = [
    *v30["allOf"],
    {
        "title": "NOT_APPLICABLE では Ledger Head を null にする",
        "if": {
            "properties": {"event_observation": {"const": "NOT_APPLICABLE"}},
            "required": ["event_observation"],
        },
        "then": {
            "properties": {
                "observed": {
                    "properties": {
                        "ledger_head_before": {"type": "null"},
                        "ledger_head_after": {"type": "null"},
                    }
                }
            }
        },
    },
    {
        "title": "Ledger を観測した Case は Head を整数で持つ",
        "if": {
            "properties": {
                "event_observation": {"enum": ["OBSERVED", "OBSERVED_EMPTY"]},
            },
            "required": ["event_observation"],
        },
        "then": {
            "properties": {
                "observed": {
                    "properties": {
                        "ledger_head_before": {"type": "integer"},
                        "ledger_head_after": {"type": "integer"},
                    }
                }
            }
        },
    },
]


for version, doc in (("1.2", v12), ("2.0", v20), ("3.0", v30)):
    path = OUT_DIR / f"{version}.schema.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"{path.relative_to(ROOT)}  sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}")
