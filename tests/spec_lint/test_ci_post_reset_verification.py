"""v1 準備状況 Report の消費者監査を、保存記録に依らずに固定する。

守りたいのは 1 つである。**観測していないものを PASS と書かない。**

CI 起動確認の記録（非公開 Repository の CI 実行を観測した監査記録）と保存済みの
消費者 Report は、公開用の配布コピーに収録しない。それらの中身は
`tests/private_history/` の試験が確かめる。ここでは合成 Git 入力からの再現と、
いまの Code に v1 から前向きの判定をする消費者が無いことを測る。
"""

from __future__ import annotations

from pathlib import Path

import audit_readiness_consumers
import verify_readiness_report_history


def test_consumer_report_is_reproducible(tmp_path: Path) -> None:
    """合成Gitの固定入力からJSON/Markdown全Bytesを再現する。旧Reportの実証ではない。"""
    from synthetic_history_fixture import build_consumer_history

    fixture = build_consumer_history(tmp_path / "synthetic-consumer")
    record = fixture.manifest["saved_report"]
    raw, markdown = verify_readiness_report_history.reproduce(fixture.root, record)
    assert raw == (fixture.root / record["report"]["path"]).read_bytes()
    assert markdown == (fixture.root / record["markdown"]["path"]).read_bytes()
    result = verify_readiness_report_history.verify(fixture.root, fixture.manifest)
    assert result["status"] == "HISTORICAL_REPRODUCED"
    assert result["is_runtime_evidence"] is False


def test_no_consumer_makes_a_forward_decision_from_v1() -> None:
    """v1 の中身から将来の判定をしている Code が無いこと。

    v1 を読む Code がすべて悪いのではない。**凍結済み成果物を再現する Code は
    v1 を読み続けるのが正しい。** 前向きの判定に使っているものだけを落とす。
    """
    report = audit_readiness_consumers.measure()
    offenders = [
        c["path"]
        for c in report["consumers"]
        if "v1" in c["reports_read"]
        and c["output"] == "FORWARD_DECISION"
        and "v3" not in c["reports_read"]
    ]
    assert offenders == [], f"v1 の中身から将来の判定をしている: {offenders}"
