"""C運用の禁止事項を機械で確かめる（公開用の現行運用規則）。

## なぜ試験にするのか

C運用の決定は「何をしないか」で構成されている。

* CIの `EXTERNALLY_ANCHORED` だけでRelease GOにしない
* Repository内のFingerprintを外部根拠にしない
* `trust_anchor` をRuntime Evidence Areaへ無断追加しない

禁止事項は、破ったときに何も起きないから破られる。**破れない形にする**か、
少なくとも破ったら落ちる形にしておく。散文の禁止事項は、書いた本人にも
効かない。実際、手順書へ「配布物をRepositoryの中へ置くな」と書いた直後に、
Workflow側で同じことをしていた。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.spec_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
C_OPERATION = REPO_ROOT / "docs" / "PUBLIC-RELEASE-OPERATION.md"
VERIFIER = REPO_ROOT / "tools" / "verify_trust_anchor.py"
SNAPSHOT = REPO_ROOT / "registry-snapshot.json"


def test_c_operation_document_exists() -> None:
    """手順書が参照している文書が実在すること。

    リンク切れの手順書は、読んだ人がそこで止まる。
    """
    assert C_OPERATION.is_file(), f"C運用指示書が無い: {C_OPERATION}"
    setup = (REPO_ROOT / "docs" / "TRUST-ANCHOR-SETUP.md").read_text(encoding="utf-8")
    assert C_OPERATION.name in setup, "TRUST-ANCHOR-SETUP.md から参照されていない"


def test_trust_anchor_is_not_a_runtime_evidence_area() -> None:
    """**`trust_anchor` をEvidence Areaへ無断で足さない。**

    足すと Registry・Snapshot・Spec・設計書のHashが連鎖して変わる。
    これは実装ではなく設計変更であり、レビューの承認が要る。

    Snapshotに現れた時点で落とす。「気付いたら14個になっていた」を
    防ぐのが目的で、追加そのものを永久に禁じるわけではない。
    """
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "trust_anchor" not in serialized, (
        "registry-snapshot.json に trust_anchor が現れている。"
        "Evidence Areaへの追加は設計変更として別途レビューを経ること"
    )


def test_emitted_evidence_declares_it_is_not_a_runtime_evidence_area() -> None:
    """出力自体へ「Runtime GO の Evidence ではない」と書いてあること。

    文書に書くだけだと、JSONだけを見た人には伝わらない。
    受け取った側が単体で判断できるようにする。
    """
    source = VERIFIER.read_text(encoding="utf-8")
    assert '"runtime_go_evidence_area": False' in source, (
        "Evidence出力に runtime_go_evidence_area の宣言が無い"
    )


def test_trust_level_distinguishes_the_two_outcomes() -> None:
    """`EXTERNALLY_ANCHORED` と `UNTRUSTED_REVIEW_ONLY` を区別すること。

    同じ「PASS」でも意味が違う。区別できないと、開発中の確認結果を
    Release GO の根拠として持ち出せてしまう。
    """
    source = VERIFIER.read_text(encoding="utf-8")
    assert "EXTERNALLY_ANCHORED" in source
    assert "UNTRUSTED_REVIEW_ONLY" in source


def test_c_operation_forbids_treating_ci_as_the_trust_anchor() -> None:
    """C運用の中心。CIをTrust Anchorの最終根拠にしない旨が書かれていること。"""
    text = C_OPERATION.read_text(encoding="utf-8")
    assert "UNTRUSTED_REVIEW_ONLY" in text
    assert "オフライン" in text, "受領側オフライン検証への言及が無い"
    assert "source_tree_hash" in text, "ZIPと署名Commitの照合手順が無い"


def test_release_workflow_does_not_claim_release_go() -> None:
    """Workflowが `REVIEWED_RELEASE` や Release GO を名乗っていないこと。

    CIの出力語彙にRelease判定を混ぜると、Job名やLogを見た人が
    「CIが緑＝Release可」と読む。判定はCIの外にある。
    """
    for relative in (".github/workflows/ci.yml", "ci/github-actions-ci.yml"):
        document = yaml.safe_load((REPO_ROOT / relative).read_text(encoding="utf-8"))
        job = document["jobs"]["release-tag"]
        serialized = json.dumps(job, ensure_ascii=False)
        assert "REVIEWED_RELEASE" not in serialized, f"{relative}: CIがRelease判定を名乗っている"
        assert "RELEASE_GO" not in serialized, f"{relative}: CIがRelease判定を名乗っている"
