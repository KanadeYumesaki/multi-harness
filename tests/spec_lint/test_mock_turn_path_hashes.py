"""Mock の 1 往復の経路へダミー Hash を入れていないことを固定する。

`test_mock_turn_metadata_audit.py` は Mock Metadata の決定（Owner の回答記録）と
その監査を対象にしており、公開用の配布コピーには収録しない。そのうち Production
の Code だけを見る規則をここへ写し、配布コピーでも走らせる（PUBLIC-03）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Mock の 1 往復で Metadata として返す Field。
FIELDS = ("instruction_hash", "output_schema_hash", "model_id")


def test_no_dummy_hash_is_introduced_on_the_mock_turn_path() -> None:
    """Mock の 1 往復の経路へ、ダミー Hash を入れていないこと。

    Masking の側には `sha256:0…0` を使う箇所が既に在るが、**それは別の
    `instruction_hash` である。** Chat の経路へ持ち込まない。
    """
    api = (REPO_ROOT / "src/harness/presentation/local_ui/api.py").read_text(encoding="utf-8")
    assert '"0" * 64' not in api
    assert "sha256:0000" not in api
    for name in FIELDS:
        assert f'{name}=""' not in api, f"{name} を空文字で埋めている"
        assert f"{name}=0" not in api, f"{name} を 0 で埋めている"
