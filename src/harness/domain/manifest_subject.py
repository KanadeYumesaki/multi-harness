"""§19.1 Manifest Subject束縛。`expected_state` は Subject型の名前空間でだけ解釈する。

## 何を防ぐ規則か

Manifestは「どのSubjectが、どのStateになるはずか」を宣言する。
Subject型とState名前空間が対応していないと、**別の型のStateで合否を判定**できてしまう。

例：`OUTBOX_RECORD` のSubjectに `ACTION_ATTEMPT` の `SUCCEEDED` を書く。
どちらも実在するEnum値なので、名前だけ見れば正しく見える。しかし
`OUTBOX_RECORD` は `SUCCEEDED` という状態を持たない。この Manifest は
**決して成立しない条件**を宣言しており、そのまま通すと
「判定できないものを判定できたことにする」経路になる。

`STATE_NAMESPACES` は Registry 生成物であり、Subject型名がそのまま
名前空間名である。したがって照合は「その名前空間に属するか」だけで足りる。

## Plan／Approval／Attempt の束縛

同じManifestが指すPlan・Approval・Attemptは、**同じActionの同じ試行**を
指していなければならない。片方だけ差し替えられると、
承認された内容と実行された内容が食い違ったまま検証を通る。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness.domain._registry_generated import STATE_NAMESPACES, ErrorCode, StateNamespace
from harness.domain.hashing import ContentHash

__all__ = [
    "ManifestSubjectBinding",
    "ManifestValidationResult",
    "validate_manifest_subject",
]

_ACCEPTED: Final[str] = "ACCEPTED"
_REJECTED: Final[str] = "REJECTED"


@dataclass(frozen=True, slots=True)
class ManifestSubjectBinding:
    """Manifest 1件が宣言するSubject束縛。"""

    manifest_validation_id: str
    expected_subject_type: str
    expected_subject_id: str
    expected_state: str
    #: 同一Actionを指すはずの3者。差し替えられていないことを照合する。
    plan_content_hash: ContentHash
    approval_plan_content_hash: ContentHash
    attempt_plan_content_hash: ContentHash


@dataclass(frozen=True, slots=True)
class ManifestValidationResult:
    """検証結果。`MANIFEST_VALIDATION_RESULT` 名前空間のStateを持つ。"""

    manifest_validation_id: str
    state: str
    error_code: ErrorCode | None
    #: 不一致の説明。実値を載せない（不変条件#7）。
    detail: str | None = None

    @property
    def accepted(self) -> bool:
        return self.state == _ACCEPTED

    @property
    def rejected(self) -> bool:
        return self.state == _REJECTED


def _namespace_of(subject_type: str) -> tuple[str, ...] | None:
    """Subject型に対応するState名前空間。未知の型は `None`。"""
    try:
        namespace = StateNamespace(subject_type)
    except ValueError:
        return None
    return STATE_NAMESPACES[namespace]


def validate_manifest_subject(binding: ManifestSubjectBinding) -> ManifestValidationResult:
    """Subject型とStateの対応、Plan／Approval／Attemptの束縛を検証する。

    **判定できない宣言を`ACCEPTED`にしない。** 未知のSubject型、
    名前空間外のState、Plan Hashの食い違いはすべて `REJECTED` である。
    """
    states = _namespace_of(binding.expected_subject_type)
    if states is None:
        return ManifestValidationResult(
            manifest_validation_id=binding.manifest_validation_id,
            state=_REJECTED,
            error_code=ErrorCode.EXPECTED_STATE_SUBJECT_MISMATCH,
            detail="unknown subject type",
        )

    if binding.expected_state not in states:
        # 他の名前空間には存在するStateかもしれない。だからこそ危ない。
        # 名前だけ見れば正しく見えるが、このSubjectは決してその状態にならない。
        return ManifestValidationResult(
            manifest_validation_id=binding.manifest_validation_id,
            state=_REJECTED,
            error_code=ErrorCode.EXPECTED_STATE_SUBJECT_MISMATCH,
            detail="expected_state is not in the subject type's namespace",
        )

    if not binding.expected_subject_id:
        return ManifestValidationResult(
            manifest_validation_id=binding.manifest_validation_id,
            state=_REJECTED,
            error_code=ErrorCode.EXPECTED_STATE_SUBJECT_MISMATCH,
            detail="expected_subject_id is empty",
        )

    # Plan／Approval／Attempt が同じActionを指していること。
    # 片方だけ差し替えられると、承認内容と実行内容が食い違ったまま通る。
    bound = {
        binding.plan_content_hash,
        binding.approval_plan_content_hash,
        binding.attempt_plan_content_hash,
    }
    if len(bound) != 1:
        return ManifestValidationResult(
            manifest_validation_id=binding.manifest_validation_id,
            state=_REJECTED,
            error_code=ErrorCode.EXPECTED_STATE_SUBJECT_MISMATCH,
            detail="plan/approval/attempt are not bound to the same plan_content_hash",
        )

    return ManifestValidationResult(
        manifest_validation_id=binding.manifest_validation_id,
        state=_ACCEPTED,
        error_code=None,
    )
