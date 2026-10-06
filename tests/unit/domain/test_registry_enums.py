"""Domain列挙がRegistry正本と過不足なく一致することの試験。

不変条件#18「件数を本文・コードへ手入力しない。正本は`design-source/registries/`」を
Testでも守るため、期待値はすべてRegistry YAMLとregistry-snapshot.jsonから読み出す。
本ファイルに件数リテラルを書かない。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.domain._registry_generated import CORE_SCHEMAS, REGISTRY_SOURCE_HASHES
from harness.domain.errors import (
    ERROR_CLASSIFICATION,
    ErrorClassification,
    ErrorCode,
    HarnessError,
    classification_of,
)
from harness.domain.events import EventType
from harness.domain.states import STATE_NAMESPACES, StateNamespace, is_valid_state

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRIES = REPO_ROOT / "design-source" / "registries"
SNAPSHOT = REPO_ROOT / "registry-snapshot.json"


def _registry(name: str) -> Any:
    return yaml.safe_load((REGISTRIES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snapshot() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return data


# --------------------------------------------------------------------------
# Registry YAML との一致
# --------------------------------------------------------------------------


def test_event_types_match_registry_exactly() -> None:
    expected = _registry("events.yaml")["event_types"]
    assert [member.value for member in EventType] == expected


def test_error_codes_match_registry_exactly() -> None:
    rows = _registry("errors.yaml")["error_codes"]
    assert [member.value for member in ErrorCode] == [r["error_code"] for r in rows]


def test_error_classifications_match_registry() -> None:
    rows = _registry("errors.yaml")["error_codes"]
    for row in rows:
        code = ErrorCode(row["error_code"])
        assert classification_of(code) == ErrorClassification(row["classification"])


def test_every_error_code_has_a_classification() -> None:
    assert set(ERROR_CLASSIFICATION) == set(ErrorCode)


def test_state_namespaces_match_registry_exactly() -> None:
    expected = _registry("states.yaml")["state_namespaces"]
    assert [member.value for member in StateNamespace] == list(expected)
    for namespace, members in expected.items():
        assert list(STATE_NAMESPACES[StateNamespace(namespace)]) == members


def test_core_schemas_match_registry_in_ordinal_order() -> None:
    """`CORE_SCHEMAS`は1行=1 Version。Registry形式の解釈は生成器の正本を使う。

    `schemas.yaml`の複数Version形式（§15.9）を試験側で読み直すと、解釈が2つになる。
    `tools/schema_catalog.py`（生成器が使うのと同じ関数）へ通した結果と比較する。
    """
    from schema_catalog import normalize_core_schemas

    entries = normalize_core_schemas(_registry("schemas.yaml")["core_schemas"])
    assert list(CORE_SCHEMAS) == [
        (e["schema_name"], e["schema_version"], e["path"]) for e in entries
    ]


def test_active_write_version_is_declared_for_every_schema() -> None:
    """書込み先が論理Schemaごとにちょうど1つ決まっている（§15.9）。"""
    from harness.domain._registry_generated import CORE_SCHEMA_ACTIVE_WRITE_VERSIONS
    from schema_catalog import active_write_versions, normalize_core_schemas

    entries = normalize_core_schemas(_registry("schemas.yaml")["core_schemas"])
    assert dict(CORE_SCHEMA_ACTIVE_WRITE_VERSIONS) == active_write_versions(entries)


# --------------------------------------------------------------------------
# registry-snapshot.json（Verifierの入力）との一致
# --------------------------------------------------------------------------


def test_counts_match_registry_snapshot_totals(snapshot: dict[str, Any]) -> None:
    """Verifierが読む件数とDomain層の列挙数が一致することを確認する。

    どちらもRegistry由来だが導出経路が異なるため、片側だけの乖離を検出できる。
    """
    totals = snapshot["totals"]
    assert len(EventType) == totals["event_type_count"]
    assert len(ErrorCode) == totals["error_code_count"]
    assert len(StateNamespace) == totals["state_namespace_count"]
    # 論理Schema数とVersion数は別のFieldである（§15.9）。混ぜない。
    assert len(CORE_SCHEMAS) == totals["core_schema_version_count"]
    assert len({name for name, _, _ in CORE_SCHEMAS}) == totals["core_schema_count"]


def test_generated_registry_hashes_match_current_files() -> None:
    """生成物ヘッダのHashが現在のRegistry YAMLと一致する（巻戻り検出）。"""
    import hashlib

    for name, recorded in REGISTRY_SOURCE_HASHES.items():
        actual = hashlib.sha256((REGISTRIES / name).read_bytes()).hexdigest()
        assert recorded == f"sha256:{actual}", f"{name} changed without regeneration"


def test_generated_registry_hashes_match_spec_manifest() -> None:
    """spec-manifest.json が記録するRegistry Hashとも一致する。"""
    manifest = json.loads((REPO_ROOT / "spec" / "spec-manifest.json").read_text(encoding="utf-8"))
    for name, recorded in REGISTRY_SOURCE_HASHES.items():
        assert manifest["registry_hashes"][name] == recorded


# --------------------------------------------------------------------------
# 型の振る舞い
# --------------------------------------------------------------------------


def test_error_code_and_classification_are_distinct_types() -> None:
    """§1.7.1「ClassificationとCodeを混同しない」。

    `EFFECT_UNKNOWN` は Code と Classification の双方に同名で存在する。
    値が等しくても両者は等価であってはならない。
    """
    assert ErrorCode.EFFECT_UNKNOWN.value == ErrorClassification.EFFECT_UNKNOWN.value
    assert ErrorCode.EFFECT_UNKNOWN != ErrorClassification.EFFECT_UNKNOWN


def test_registry_enums_are_not_str_subclasses() -> None:
    """`StrEnum`を使うと§1.7.1の混同が静かに成立するため素の`Enum`とする。

    `StrEnum`では`str.__eq__`により
    `ErrorCode.EFFECT_UNKNOWN == ErrorClassification.EFFECT_UNKNOWN` が真になる。
    """
    for enum_cls in (EventType, ErrorCode, ErrorClassification, StateNamespace):
        assert not issubclass(enum_cls, str), f"{enum_cls.__name__} must not be StrEnum"


def test_enum_members_are_rejected_by_canonicalizer() -> None:
    """Canonical化の境界で`.value`を書き忘れた場合にFail-Closedで停止する。"""
    from harness.domain.canonical import CanonicalizationError, canonicalize

    with pytest.raises(CanonicalizationError, match="unsupported type"):
        canonicalize({"event": EventType.RUN_CREATED})
    assert canonicalize({"event": EventType.RUN_CREATED.value}) == (b'{"event":"RUN_CREATED"}')


def test_unknown_error_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="NOT_A_REAL_CODE"):
        ErrorCode("NOT_A_REAL_CODE")


def test_harness_error_carries_code_and_classification() -> None:
    error = HarnessError(ErrorCode.STALE_FENCING_TOKEN, "fence mismatch")
    assert error.code is ErrorCode.STALE_FENCING_TOKEN
    assert error.classification is ErrorClassification.CONFLICT
    assert "STALE_FENCING_TOKEN" in str(error)


def test_is_valid_state_checks_namespace_membership() -> None:
    assert is_valid_state(StateNamespace.ACTION_ATTEMPT, "PREPARED_DURABLE")
    # PREPARED_DURABLE は RUN 名前空間には属さない。
    assert not is_valid_state(StateNamespace.RUN, "PREPARED_DURABLE")


def test_action_attempt_terminal_states_are_registered() -> None:
    """§1.4.3 の終端状態がすべてACTION_ATTEMPT名前空間に存在する。"""
    terminal = (
        "SUCCEEDED",
        "FAILED_RETRYABLE",
        "FAILED_PERMANENT",
        "BLOCKED_POLICY",
        "BLOCKED_APPROVAL",
        "BLOCKED_CONFLICT",
        "CANCELLED",
        "CANCEL_UNKNOWN",
        "EFFECT_UNKNOWN",
    )
    for state in terminal:
        assert is_valid_state(StateNamespace.ACTION_ATTEMPT, state), state


def test_run_separates_blocked_from_blocked_repair_required() -> None:
    """§1.5「`BLOCKED`は終端、`BLOCKED_REPAIR_REQUIRED`は非終端として分離する」。"""
    assert is_valid_state(StateNamespace.RUN, "BLOCKED")
    assert is_valid_state(StateNamespace.RUN, "BLOCKED_REPAIR_REQUIRED")


def test_state_namespaces_mapping_is_immutable() -> None:
    with pytest.raises(TypeError):
        STATE_NAMESPACES[StateNamespace.RUN] = ()  # type: ignore[index]
