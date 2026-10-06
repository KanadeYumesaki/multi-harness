"""§1.16.2.1 Input Read Orchestrator の統合試験。

実 SQLite の正本 Ledger と実 Filesystem を使う。`LedgerProbe` の代替ではなく
**正本 Ledger を読み戻して**観測する。Orchestrator が Append するように
なった以上、代替を観測しても「Ledger に残ったか」は確かめられない。

対象 Case（F-1-A）:

* `AT-PATH-001/LINUX_ESCAPE`
* `AT-INPUT-PATH-001/SYMLINK`
* `AT-INPUT-PATH-001/SPECIAL_FILE`

`AT-INPUT-PATH-001/MOUNT_CROSSING` は Mount Namespace が要るため
`test_mount_crossing.py` 側で扱う。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harness.application.input_read_orchestrator import (
    InputReadOrchestrator,
    InputReadRequest,
)
from harness.application.masking_policy_gate import MaskingPolicyGate
from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.events import EventType
from harness.domain.input_read import CapabilityScope, ReadDecision
from harness.infrastructure.filesystem.safe_reader import CapabilityBroker, SafeInputReader
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy
from harness.infrastructure.masking.mock_masker import StaticPhraseMasker
from harness.infrastructure.masking.pipeline import MaskingPipeline
from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.sqlite.connection_factory import ConnectionFactory, ConnectionRole
from harness.infrastructure.sqlite.event_ledger_repository import SqliteEventLedgerRepository
from harness.infrastructure.sqlite.migrations import migrate
from harness.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork
from harness.ports.event_ledger import NewEvent
from harness.ports.safe_input_reader import ReadEvidence

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "support"))

from case_probe import observe_case
from input_read_observation import InputReadEffectProbe, capture_input_read
from real_ledger_view import RealLedgerView

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_ID = "cap-1"
STREAM_ID = "INPUT_READ_STREAM"
PAYLOAD = b"readable content\n"


@dataclass
class FrozenClock:
    stamp: str = "2026-08-20T00:00:00Z"

    def now(self) -> str:
        return self.stamp


class StaticClassifier:
    """読取れたBytesを一定の分類へ落とす。成功経路の Event を確かめるためだけに使う。"""

    def __init__(self, label: str = "UNTRUSTED_INPUT") -> None:
        self.label = label
        self.calls = 0

    def classify(self, payload: bytes, evidence: ReadEvidence) -> str:
        self.calls += 1
        return self.label


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "readme.md").write_bytes(PAYLOAD)
    (root / "secret").mkdir()
    (root / "secret" / "keys.txt").write_bytes(b"do not read\n")
    return root


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[tuple[ConnectionFactory, sqlite3.Connection]]:
    factory = ConnectionFactory(tmp_path / "harness.db")
    migrate(factory, recorded_at="2026-08-20T00:00:00Z")
    made = factory.connect(ConnectionRole.RUNTIME)
    try:
        yield factory, made
    finally:
        made.close()


@pytest.fixture
def orchestrator(
    workspace: Path,
    connection: tuple[ConnectionFactory, sqlite3.Connection],
) -> Iterator[tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path]]:
    factory, made = connection
    ledger = SqliteEventLedgerRepository(made)
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as broker:
        broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
        yield (
            InputReadOrchestrator(
                reader=SafeInputReader(broker),
                ledger=ledger,
                unit_of_work=SqliteUnitOfWork(factory, made),
                clock=FrozenClock(),
            ),
            ledger,
            workspace,
        )


class _StaticClassifier:
    """読取れた Bytes を一律で `TEXT` と分類する。

    分類の中身は本 Case の主題ではない。`INPUT_ARTIFACT_CLASSIFIED` が出る
    ところまで進むことだけが要る。
    """

    def classify(self, payload: bytes, evidence: Any) -> str:
        return "TEXT"


@pytest.fixture
def masking_orchestrator(
    workspace: Path,
    connection: tuple[ConnectionFactory, sqlite3.Connection],
) -> Iterator[tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path]]:
    """Masking Policy Gate を挿した Orchestrator。

    Gate は Masking Pipeline の判定を Input Read の判定へ写す。写す口は
    `MaskingPolicyGate` だけで、試験側で `REJECTED` を `DENIED` へ読み替えない。
    """
    factory, made = connection
    ledger = SqliteEventLedgerRepository(made)
    policy = MaskingPolicy.load(REPO_ROOT)
    pipeline = MaskingPipeline(policy, StaticPhraseMasker(phrases={}), repo_root=REPO_ROOT)
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as broker:
        broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
        yield (
            InputReadOrchestrator(
                reader=SafeInputReader(broker),
                ledger=ledger,
                unit_of_work=SqliteUnitOfWork(factory, made),
                clock=FrozenClock(),
                classifier=_StaticClassifier(),
                masking_gate=MaskingPolicyGate(pipeline),
            ),
            ledger,
            workspace,
        )


@pytest.mark.case("AT-MASKING-001/SECRET_REJECTED_NOT_MASKED")
def test_secret_input_is_denied_by_the_orchestrator(
    masking_orchestrator: tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path],
    case_observation: Any,
) -> None:
    """Secret を含む入力は読取れても Input Read として `DENIED` になる。

    Masking Pipeline 自身は `MASKING_RESULT / REJECTED` を返す。それを
    Input Read の判定へ引き取るのは Orchestration である（§1.16.2.3）。
    Event 列は**正本 Ledger から読み戻す**。試験側で作らない。
    """
    orchestrator, ledger, workspace = masking_orchestrator
    # Canary は Secret の形をした値であり、Evidence へは載せない。
    (workspace / "docs" / "config.ini").write_text(
        "key AKIAIOSFODNN7EXAMPLE in config\n", encoding="utf-8"
    )

    view = RealLedgerView(ledger, STREAM_ID)
    head_before = view.head
    request = _request("docs/config.ini")
    effects = InputReadEffectProbe()
    with pytest.MonkeyPatch.context() as patch:
        effects.install(patch)
        outcome = orchestrator.read(request)
    assert outcome.payload is None
    assert outcome.read_evidence is None
    case_observation.typed_result = capture_input_read(
        outcome, request, ledger, effects, head_before=head_before
    )

    assert outcome.state == ReadDecision.DENIED.value
    # Policy 境界の拒否は Error Code を持たない（§1.16.3.2 / MASK-5-C）。
    assert outcome.error_code is None
    assert outcome.masking_denial is not None
    assert outcome.masking_denial.masker_invocation_count == 0
    assert outcome.ledger_head_before == head_before
    assert outcome.ledger_head_after == view.head

    observe_case(
        case_observation,
        "AT-MASKING-001/SECRET_REJECTED_NOT_MASKED",
        state=outcome.state,
        subject_id=outcome.read_decision_id,
        error_code=None,
        ledger=view,
        effects=effects,
        head_before=head_before,
        payload={
            "capability_id": CAPABILITY_ID,
            "classification": outcome.classification,
            "denied_categories": sorted(outcome.masking_denial.rejected_categories),
            "masker_invocation_count": outcome.masking_denial.masker_invocation_count,
            "artifact_persisted_count": effects.artifact_put_attempts,
            "producer_module": "harness.application.input_read_orchestrator",
            "producer_symbol": "InputReadOrchestrator.read",
        },
    )


def _request(path: str, decision_id: str = "read-1") -> InputReadRequest:
    return InputReadRequest(
        stream_id=STREAM_ID,
        read_decision_id=decision_id,
        capability_id=CAPABILITY_ID,
        relative_path=path,
    )


def _observe(
    observation: Any,
    case_id: str,
    orchestrator_bundle: tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path],
    path: str,
) -> None:
    """Orchestrator を実行し、**正本 Ledger を読み戻して**観測する。"""
    orchestrator, ledger, _ = orchestrator_bundle
    view = RealLedgerView(ledger, STREAM_ID)
    head_before = view.head

    request = _request(path)
    effects = InputReadEffectProbe()
    with pytest.MonkeyPatch.context() as patch:
        effects.install(patch)
        outcome = orchestrator.read(request)
    observation.typed_result = capture_input_read(
        outcome, request, ledger, effects, head_before=head_before
    )

    # Orchestrator の申告と Ledger の実測が一致することを先に固定する。
    assert outcome.ledger_head_before == head_before, case_id
    assert outcome.ledger_head_after == view.head, case_id

    observe_case(
        observation,
        case_id,
        state=outcome.state,
        subject_id=outcome.read_decision_id,
        error_code=outcome.error_code.value if outcome.error_code else None,
        ledger=view,
        effects=effects,
        head_before=head_before,
        payload={
            "capability_id": CAPABILITY_ID,
            "relative_path": path,
            "capability_path_hash": str(outcome.capability_path_hash),
            "producer_module": "harness.application.input_read_orchestrator",
            "producer_symbol": "InputReadOrchestrator.read",
        },
    )


# --------------------------------------------------------------------------
# 拒否系（F-1-A 対象）
# --------------------------------------------------------------------------


@pytest.mark.case("AT-PATH-001/LINUX_ESCAPE")
def test_path_escape_is_denied_and_recorded(
    orchestrator: tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path],
    case_observation: Any,
) -> None:
    _observe(case_observation, "AT-PATH-001/LINUX_ESCAPE", orchestrator, "../../etc/passwd")


@pytest.mark.case("AT-INPUT-PATH-001/SYMLINK")
def test_symlink_is_denied_and_recorded(
    orchestrator: tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path],
    case_observation: Any,
) -> None:
    _, _, workspace = orchestrator
    (workspace / "docs" / "escape").symlink_to("/etc/passwd")
    _observe(case_observation, "AT-INPUT-PATH-001/SYMLINK", orchestrator, "docs/escape")


@pytest.mark.case("AT-INPUT-PATH-001/SPECIAL_FILE")
def test_fifo_is_denied_and_recorded(
    orchestrator: tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path],
    case_observation: Any,
) -> None:
    _, _, workspace = orchestrator
    os.mkfifo(workspace / "docs" / "pipe")
    _observe(case_observation, "AT-INPUT-PATH-001/SPECIAL_FILE", orchestrator, "docs/pipe")


# --------------------------------------------------------------------------
# 正常系
# --------------------------------------------------------------------------


def test_allowed_read_appends_started_only_without_classifier(
    orchestrator: tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path],
) -> None:
    """分類器を渡さなければ`INPUT_ARTIFACT_CLASSIFIED`を出さない。

    出す根拠が無いのに出すと、分類していないのに分類したことになる。
    """
    instance, ledger, _ = orchestrator
    outcome = instance.read(_request("docs/readme.md"))

    assert outcome.state == ReadDecision.ALLOWED.value
    assert outcome.error_code is None
    assert outcome.payload == PAYLOAD
    assert outcome.classification is None
    assert outcome.events == (EventType.INPUT_READ_STARTED,)
    view = RealLedgerView(ledger, STREAM_ID)
    assert view.appended == ["INPUT_READ_STARTED"]
    assert outcome.ledger_head_after == 1


def test_allowed_read_with_classifier_appends_classified(
    workspace: Path,
    connection: tuple[ConnectionFactory, sqlite3.Connection],
) -> None:
    factory, made = connection
    ledger = SqliteEventLedgerRepository(made)
    classifier = StaticClassifier()
    with CapabilityBroker(FilesystemPolicy.load(REPO_ROOT), allow_test_filesystems=True) as broker:
        broker.issue(CAPABILITY_ID, workspace, CapabilityScope(("docs",)))
        instance = InputReadOrchestrator(
            reader=SafeInputReader(broker),
            ledger=ledger,
            unit_of_work=SqliteUnitOfWork(factory, made),
            clock=FrozenClock(),
            classifier=classifier,
        )
        outcome = instance.read(_request("docs/readme.md"))

    assert classifier.calls == 1
    assert outcome.classification == "UNTRUSTED_INPUT"
    assert outcome.events == (
        EventType.INPUT_READ_STARTED,
        EventType.INPUT_ARTIFACT_CLASSIFIED,
    )
    assert RealLedgerView(ledger, STREAM_ID).appended == [
        "INPUT_READ_STARTED",
        "INPUT_ARTIFACT_CLASSIFIED",
    ]


# --------------------------------------------------------------------------
# Transaction 系
# --------------------------------------------------------------------------


class ExplodingReader:
    """判定を返さず落ちる Reader。Reader Failure の規則を確かめる。"""

    def open_read(self, capability_id: str, relative_path: str) -> object:
        raise RuntimeError("reader exploded")

    def enumerate(self, capability_id: str, relative_directory: str) -> object:
        raise NotImplementedError


def test_reader_failure_stops_with_effect_unknown_and_leaves_no_event(
    connection: tuple[ConnectionFactory, sqlite3.Connection],
) -> None:
    """Readerが落ちたら`INPUT_READ_DENIED`を推測でAppendしない（§1.16.2.1）。

    Transactionが巻き戻るので`INPUT_READ_STARTED`も残らない。Partial Eventを
    作らないことと、拒否を推測しないことを同時に満たす。
    """
    factory, made = connection
    ledger = SqliteEventLedgerRepository(made)
    instance = InputReadOrchestrator(
        reader=ExplodingReader(),  # type: ignore[arg-type]
        ledger=ledger,
        unit_of_work=SqliteUnitOfWork(factory, made),
        clock=FrozenClock(),
    )

    with pytest.raises(HarnessError) as error:
        instance.read(_request("docs/readme.md"))

    assert error.value.code is ErrorCode.EFFECT_UNKNOWN
    view = RealLedgerView(ledger, STREAM_ID)
    assert view.appended == [], "Rollbackしたのに Event が残っている"
    assert view.head == 0


def test_stale_head_is_rejected_by_the_cas(
    orchestrator: tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path],
    connection: tuple[ConnectionFactory, sqlite3.Connection],
) -> None:
    """古いHeadでAppendするとCASが外れる。これが二重Append防止の実体である。

    Orchestrator は毎回 `stream_head` を読み直すので、公開APIからは古いHeadを
    渡せない。ここでは Ledger へ直接、進んだ Stream に対して古い
    `expected_stream_sequence` を渡し、拒否されることを確かめる。

    Transaction の中で行う。Transaction 無しの Append は別の理由
    （不変条件#15）で拒否され、CAS の検証にならない。
    """
    instance, ledger, _ = orchestrator
    factory, made = connection
    first = instance.read(_request("../../etc/passwd"))
    assert first.ledger_head_after == 2

    view = RealLedgerView(ledger, STREAM_ID)
    before = view.appended

    with pytest.raises(HarnessError) as error, factory.begin_immediate(made):
        ledger.append(
            [
                NewEvent(
                    stream_id=STREAM_ID,
                    event_type=EventType.INPUT_READ_STARTED.value,
                    payload_hash=first.capability_path_hash,
                    recorded_at="2026-08-20T00:00:00Z",
                )
            ],
            expected_stream_sequence=0,  # 古いHead
        )

    assert error.value.code is ErrorCode.EVENT_ORDER_VIOLATION
    assert RealLedgerView(ledger, STREAM_ID).appended == before


def test_second_read_continues_the_stream_without_duplication(
    orchestrator: tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path],
) -> None:
    """別Requestは Stream を伸ばす。Head を読み直しているので CAS が通る。"""
    instance, ledger, workspace = orchestrator
    (workspace / "docs" / "escape").symlink_to("/etc/passwd")

    first = instance.read(_request("../../etc/passwd", "read-1"))
    second = instance.read(_request("docs/escape", "read-2"))

    assert first.ledger_head_after == 2
    assert second.ledger_head_before == 2
    assert second.ledger_head_after == 4
    assert RealLedgerView(ledger, STREAM_ID).appended == [
        "INPUT_READ_STARTED",
        "INPUT_READ_DENIED",
        "INPUT_READ_STARTED",
        "INPUT_READ_DENIED",
    ]


def test_hardlink_is_denied_without_payload_and_recorded(
    orchestrator: tuple[InputReadOrchestrator, SqliteEventLedgerRepository, Path],
) -> None:
    """Readerの拒否を実SQLiteへ記録し、範囲外Bytesを後段へ渡さない。"""
    instance, ledger, workspace = orchestrator
    outside = workspace.parent / "outside-hardlink.txt"
    outside.write_bytes(b"synthetic outside content\n")
    os.link(outside, workspace / "docs" / "linked.txt")
    outcome = instance.read(_request("docs/linked.txt"))
    assert outcome.state == ReadDecision.DENIED.value
    assert outcome.error_code is ErrorCode.PATH_OUTSIDE_CAPABILITY
    assert outcome.payload is None
    assert outcome.classification is None
    assert RealLedgerView(ledger, STREAM_ID).appended == ["INPUT_READ_STARTED", "INPUT_READ_DENIED"]
    assert ledger.verify_chain(STREAM_ID).valid
