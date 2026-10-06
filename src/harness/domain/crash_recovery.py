"""§26.5 Crash Recovery。中断点ごとに「Effectが起きたか」を判定する。

## 判定は3値である

Crash後に確かめるのは1つだけ。**その作用は起きたのか。**
答えは3つあり、2つに潰してはならない。

| 判定 | 意味 | 続けてよいこと |
|---|---|---|
| `NOT_EXECUTED` | 起きていないと**証明できた** | 再実行 |
| `EXECUTED` | 起きたと**観測できた** | 後続（Receipt保存・Commit） |
| `UNKNOWN` | **判定できない** | 照合だけ。人の判断へ回す |

**`UNKNOWN` を `NOT_EXECUTED` へ変換しない。** これが本Moduleの核である。
「たぶん起きていない」で再実行すると、既に起きていた場合に二重作用になる。
外部への送信や課金は取り消せない。判定できないものは判定できないまま止める。

逆に `UNKNOWN` を `EXECUTED` 扱いにするのも誤りである。起きていない作用を
成功として記録すると、その後の工程が実在しない結果の上に積み上がる。

## 中断点から何が言えるか

Journalは Effect 実行**前**に `PREPARED_DURABLE` へ確定する（不変条件#2）。
だから Journal の有無が「作用を試みた可能性があるか」の境界になる。

| 中断点 | Journal | 判定 | 根拠 |
|---|---|---|---|
| `BEFORE_ACTION_PREPARED` | 無し | `NOT_EXECUTED` | Journalが無い＝I/Oを起こしていない |
| `AFTER_TEMP_WRITE_BEFORE_FSYNC` | 無し | `NOT_EXECUTED` | Temp書込みは確定していない |
| `AFTER_ACTION_PREPARED` | 有り | `NOT_EXECUTED` | Journalは確定したが実行前 |
| `BEFORE_EXECUTION_ATTEMPTED` | 有り | `NOT_EXECUTED` | 同上。ただし再開はOperator判断 |
| `AFTER_EXECUTION_ATTEMPTED` | 有り | **観測次第** | 観測できなければ`UNKNOWN` |
| `AFTER_ATOMIC_REPLACE_BEFORE_OBSERVE` | 有り | 観測次第 | Renameは完了している |
| `AFTER_EFFECT_OBSERVED` 以降 | 有り | `EXECUTED` | 観測済み |

`AFTER_EXECUTION_ATTEMPTED` だけが本質的に曖昧である。実行を試みた後に
落ちたので、届いたかどうかは**外部を観測しないと分からない**。
観測できたなら `EXECUTED`、できないなら `UNKNOWN` である。

## 元のAttempt／Receiptを改変しない

Recoveryは新しいEventをAppendするだけである（不変条件#1）。
中断前のAttemptやReceiptを書き換えない。何が起きていたかの記録を
Recoveryが上書きしてしまうと、そのRecoveryが正しかったかを後から確かめられない。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from harness.domain._registry_generated import ErrorCode, EventType
from harness.domain.faults import FaultPoint
from harness.domain.hashing import ContentHash

__all__ = [
    "CrashRecoveryOutcome",
    "CrashSite",
    "EffectDisposition",
    "recover_from_crash",
]


class EffectDisposition(Enum):
    """作用が起きたかどうかの3値判定。"""

    NOT_EXECUTED = "NOT_EXECUTED"
    EXECUTED = "EXECUTED"
    UNKNOWN = "UNKNOWN"


# Journal確定より前の中断点。I/Oを起こしていないと断定できる。
_BEFORE_JOURNAL: Final[frozenset[FaultPoint]] = frozenset(
    {
        FaultPoint.BEFORE_ACTION_PREPARED,
        FaultPoint.AFTER_TEMP_WRITE_BEFORE_FSYNC,
    }
)

# Journalは確定したが、実行はまだ。
_PREPARED_NOT_EXECUTED: Final[frozenset[FaultPoint]] = frozenset(
    {
        FaultPoint.AFTER_ACTION_PREPARED,
        FaultPoint.BEFORE_EXECUTION_ATTEMPTED,
    }
)

# 実行を試みた後。届いたかは観測しないと分からない。
_AMBIGUOUS: Final[frozenset[FaultPoint]] = frozenset(
    {
        FaultPoint.AFTER_EXECUTION_ATTEMPTED,
        FaultPoint.AFTER_ATOMIC_REPLACE_BEFORE_OBSERVE,
    }
)

# 観測済み。作用は起きている。
_ALREADY_OBSERVED: Final[frozenset[FaultPoint]] = frozenset(
    {
        FaultPoint.AFTER_EFFECT_OBSERVED,
        FaultPoint.BEFORE_RECEIPT_STORE,
        FaultPoint.AFTER_RECEIPT_STORE_BEFORE_COMMIT,
    }
)

_RECOVERY_PREFIX: Final[tuple[EventType, ...]] = (
    EventType.RECOVERY_STARTED,
    EventType.RECOVERY_DECIDED,
)


@dataclass(frozen=True, slots=True)
class CrashSite:
    """中断の状況。Recoveryが読む観測値だけを持つ。"""

    attempt_id: str
    fault_point: FaultPoint
    #: 中断直前までにLedgerへ確定していたEvent列。
    durable_events: tuple[EventType, ...]
    #: 実行前の対象Hash。
    base_hash: ContentHash
    #: 実行後にこうなるはずのHash。
    expected_after_hash: ContentHash
    #: 実際に観測できたHash。観測できなかった場合は `None`。
    observed_hash: ContentHash | None
    #: Journalが `PREPARED_DURABLE` で残っているか。
    journal_prepared: bool
    #: Receiptが保存済みか。
    receipt_stored: bool


@dataclass(frozen=True, slots=True)
class CrashRecoveryOutcome:
    """Recovery判定の結果。"""

    attempt_id: str
    disposition: EffectDisposition
    attempt_state: str
    error_code: ErrorCode | None
    #: Recoveryが**追記する**Event列。中断前のEventは含まない。
    appended_events: tuple[EventType, ...]
    #: 中断前のEventと追記分を合わせた全体。Case期待値と突き合わせる。
    full_event_sequence: tuple[EventType, ...]
    #: 作用を再実行してよいか。`UNKNOWN` では決して True にしない。
    reexecution_allowed: bool
    #: 人の照合待ちへ回すか。
    manual_queue_required: bool
    #: Operatorの再開判断が要るか。
    operator_resume_required: bool
    effect_attempts: int
    receipt_count: int
    target_hash: ContentHash

    @property
    def resolved(self) -> bool:
        return self.disposition is not EffectDisposition.UNKNOWN


def _classify(site: CrashSite) -> EffectDisposition:
    """中断点と観測から3値を決める。**曖昧なものを断定しない。**"""
    if site.fault_point in _BEFORE_JOURNAL:
        return EffectDisposition.NOT_EXECUTED
    if site.fault_point in _PREPARED_NOT_EXECUTED:
        return EffectDisposition.NOT_EXECUTED
    if site.fault_point in _ALREADY_OBSERVED:
        return EffectDisposition.EXECUTED
    if site.fault_point in _AMBIGUOUS:
        if site.observed_hash is None:
            # 外部を観測できなかった。届いたかどうか分からない。
            # ここで NOT_EXECUTED へ倒すと、届いていた場合に二重作用になる。
            return EffectDisposition.UNKNOWN
        if site.observed_hash == site.expected_after_hash:
            return EffectDisposition.EXECUTED
        if site.observed_hash == site.base_hash:
            # 対象が実行前のままである＝作用は届いていない。
            return EffectDisposition.NOT_EXECUTED
        # 期待とも実行前とも違う。何が起きたのか説明できない。
        return EffectDisposition.UNKNOWN
    return EffectDisposition.UNKNOWN


def recover_from_crash(site: CrashSite) -> CrashRecoveryOutcome:
    """中断点からRecovery判定を返す。

    **元のAttempt／Receiptを変更しない。** 追記するEvent列を返すだけである。
    """
    disposition = _classify(site)
    prefix = site.durable_events

    if disposition is EffectDisposition.UNKNOWN:
        appended = (*_RECOVERY_PREFIX, EventType.EFFECT_UNKNOWN)
        return CrashRecoveryOutcome(
            attempt_id=site.attempt_id,
            disposition=disposition,
            attempt_state="EFFECT_UNKNOWN",
            error_code=ErrorCode.EFFECT_UNKNOWN,
            appended_events=appended,
            full_event_sequence=(*prefix, *appended),
            # 判定できないものを再実行しない。二重作用の唯一の入口である。
            reexecution_allowed=False,
            manual_queue_required=True,
            operator_resume_required=True,
            effect_attempts=0,
            receipt_count=0,
            target_hash=site.observed_hash or site.base_hash,
        )

    if disposition is EffectDisposition.NOT_EXECUTED:
        if site.fault_point is FaultPoint.BEFORE_ACTION_PREPARED:
            # Journalが無い。作用を試みていないので、やり直せる。
            appended = (*_RECOVERY_PREFIX, EventType.ACTION_FAILED)
            return CrashRecoveryOutcome(
                attempt_id=site.attempt_id,
                disposition=disposition,
                attempt_state="FAILED_RETRYABLE",
                error_code=None,
                appended_events=appended,
                full_event_sequence=(*prefix, *appended),
                reexecution_allowed=True,
                manual_queue_required=False,
                operator_resume_required=False,
                effect_attempts=0,
                receipt_count=0,
                target_hash=site.base_hash,
            )
        if site.fault_point is FaultPoint.AFTER_TEMP_WRITE_BEFORE_FSYNC:
            # Temp書込みは確定していない。Journalも無い。修復して終わる。
            appended = _RECOVERY_PREFIX
            return CrashRecoveryOutcome(
                attempt_id=site.attempt_id,
                disposition=disposition,
                attempt_state="REPAIRED",
                error_code=None,
                appended_events=appended,
                full_event_sequence=(*prefix, *appended),
                reexecution_allowed=True,
                manual_queue_required=False,
                operator_resume_required=False,
                effect_attempts=0,
                receipt_count=0,
                target_hash=site.base_hash,
            )
        if site.fault_point is FaultPoint.BEFORE_EXECUTION_ATTEMPTED:
            # Journalは確定している。作用は起きていないが、
            # 再開するかはOperatorが決める。勝手に実行しない。
            appended = _RECOVERY_PREFIX
            return CrashRecoveryOutcome(
                attempt_id=site.attempt_id,
                disposition=disposition,
                attempt_state="PREPARED_DURABLE",
                error_code=None,
                appended_events=appended,
                full_event_sequence=(*prefix, *appended),
                reexecution_allowed=False,
                manual_queue_required=False,
                operator_resume_required=True,
                effect_attempts=0,
                receipt_count=0,
                target_hash=site.base_hash,
            )
        # 実行前だと**証明できた**中断点。続きを実行して完了させる。
        #
        # `disposition` は「中断時点で何が分かったか」であり、Recoveryが
        # そのあと何をしたかではない。ここを `EXECUTED` で上書きすると、
        # 「作用は起きていなかった」という観測結果が消える。
        appended = (
            *_RECOVERY_PREFIX,
            EventType.EXECUTION_ATTEMPTED,
            EventType.EFFECT_OBSERVED,
            EventType.EFFECT_RECEIPT_STORED,
            EventType.ACTION_COMMITTED,
        )
        return CrashRecoveryOutcome(
            attempt_id=site.attempt_id,
            disposition=disposition,
            attempt_state="SUCCEEDED",
            error_code=None,
            appended_events=appended,
            full_event_sequence=(*prefix, *appended),
            # 不在を証明したうえで1回だけ実行した。さらなる再実行は許さない。
            reexecution_allowed=False,
            manual_queue_required=False,
            operator_resume_required=False,
            effect_attempts=1,
            receipt_count=1,
            target_hash=site.expected_after_hash,
        )

    # EXECUTED：観測できている。残りの工程だけを進める。
    # **作用そのものは繰り返さない。** 二重実行の防止はここにある。
    tail: tuple[EventType, ...]
    if site.fault_point in _AMBIGUOUS:
        tail = (
            EventType.EFFECT_OBSERVED,
            EventType.EFFECT_RECEIPT_STORED,
            EventType.ACTION_COMMITTED,
        )
    elif site.receipt_stored:
        tail = (EventType.ACTION_COMMITTED,)
    else:
        tail = (EventType.EFFECT_RECEIPT_STORED, EventType.ACTION_COMMITTED)

    appended = (*_RECOVERY_PREFIX, *tail)
    return CrashRecoveryOutcome(
        attempt_id=site.attempt_id,
        disposition=disposition,
        attempt_state="SUCCEEDED",
        error_code=None,
        appended_events=appended,
        full_event_sequence=(*prefix, *appended),
        # 既に起きている。作用を再実行しない。
        reexecution_allowed=False,
        manual_queue_required=False,
        operator_resume_required=False,
        effect_attempts=1,
        receipt_count=1,
        target_hash=site.expected_after_hash,
    )
