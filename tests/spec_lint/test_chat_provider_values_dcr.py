"""Provider 契約の具体値 DCR の公開側試験。

`DCR-CHAT-PROVIDER-VALUES` の保存 Package・監査 Report は非公開の回答記録であり、
公開用の配布コピーに収録しない。その内容（未回答のまま止まっている、値を捏造して
いない、Hash が記録値と一致する等）は `tests/private_history/` の試験が確かめる。

ここでは保存記録に依らない契約だけを測る。

* 回答済み Package を合成 Git 履歴の回答時点 Canon へ束縛できる
* `errors.yaml` の Error Code を勝手に増やしていない
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import chat_canon_binding

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
ERRORS = REPO_ROOT / "design-source/registries/errors.yaml"
SNAPSHOT = REPO_ROOT / "registry-snapshot.json"


# ---------------------------------------------------------------------------
# 回答時点 Canon への束縛（合成 Git 履歴）
# ---------------------------------------------------------------------------


def test_bound_to_the_answer_time_canon(tmp_path: Path) -> None:
    """合成Package/Reportを実Git履歴へ束縛する。私的な旧回答の正当性は主張しない。"""
    from synthetic_history_fixture import build_bound_audit_fixture

    root, package, report = build_bound_audit_fixture(tmp_path / "bound-audit")
    resolved = chat_canon_binding.verify_answer_time_canon(
        root, package, package_id=package["package_id"]
    )
    assert resolved["design"]["resolved_hash"] == package["design_sha256"]
    assert resolved["design"]["design_version"] == package["design_version"]
    assert resolved["snapshot"]["compared_fields"] == [
        "design_version",
        "design_sha256",
        "registry_snapshot_hash",
        "schema_catalog_hash",
    ]
    # 監査 Report は Package と同じ Canon を名乗る。**現行 Snapshot ではない。**
    assert report["design_sha256"] == package["design_sha256"]
    assert report["registry_snapshot_hash"] == package["registry_snapshot_hash"]


# ---------------------------------------------------------------------------
# 値を捏造していない
# ---------------------------------------------------------------------------


def test_error_code_registry_did_not_grow() -> None:
    """`errors.yaml` の件数が Snapshot と一致すること。"""
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    document = yaml.safe_load(ERRORS.read_text(encoding="utf-8"))
    assert len(document["error_codes"]) == len(snapshot["error_codes"])
