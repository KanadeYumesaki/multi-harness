"""決定論スキャナの試験（ADR-007 §5）。

REJECT規則の検出漏れは信用情報の流出になる。ここは「検出できること」より
**「見逃さないこと」**を確かめる試験群である。
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.masking.scanner import DeterministicScanner, Disposition

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def policy() -> MaskingPolicy:
    return MaskingPolicy.load(REPO_ROOT)


@pytest.fixture(scope="module")
def scanner(policy: MaskingPolicy) -> DeterministicScanner:
    return DeterministicScanner(
        reject_categories=policy.reject_categories,
        maskable_categories=policy.maskable_categories,
    )


# ---------------------------------------------------------------------------
# REJECT規則
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIE...", "PRIVATE_KEY"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "PRIVATE_KEY"),
        ("-----BEGIN PRIVATE KEY-----", "PRIVATE_KEY"),
        ("-----BEGIN CERTIFICATE-----", "CERTIFICATE_KEY"),
        ("key id AKIAIOSFODNN7EXAMPLE here", "CLOUD_CREDENTIAL"),
        ('{"type": "service_account", "project_id": "x"}', "CLOUD_CREDENTIAL"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "BEARER_TOKEN"),
        ("use sk-abcdefghijklmnopqrstuvwxyz01 for calls", "API_KEY"),
        ("api_key = 8f2c9a1b3d4e5f60", "API_KEY"),
        ("password: hunter2xyz", "PASSWORD"),
        ("パスワード：Sup3rSecret", "PASSWORD"),
        ("Cookie: JSESSIONID=A1B2C3D4E5F6G7H8", "SESSION_COOKIE"),
        ("postgres://appuser:s3cr3t@db.internal:5432/app", "CONNECTION_STRING"),
        ("export DATABASE_PASSWORD=abc123", "DOTENV_CONTENT"),
        ("marker FDE-HARNESS-CANARY-QX7T2M9P end", "CANARY"),
        ("マイナンバーは123456789012です", "NATIONAL_ID"),
        ("SSN 123-45-6789", "NATIONAL_ID"),
        ("患者の病名を共有します", "SPECIAL_CATEGORY_DATA"),
        ("his criminal record shows", "SPECIAL_CATEGORY_DATA"),
    ],
)
def test_reject_rules_detect_their_category(
    scanner: DeterministicScanner, text: str, category: str
) -> None:
    result = scanner.scan(text)
    assert category in result.reject_categories, (
        f"{category} を検出できていない。検出: {result.reject_categories}"
    )


def test_high_entropy_token_is_detected_as_generic_secret(
    scanner: DeterministicScanner,
) -> None:
    text = "token qX7t2M9pZa4Vb8Nc1Ld6Ke3Jf5Hg0Iy2Uw ends"
    assert "SECRET_GENERIC" in scanner.scan(text).reject_categories


def test_sha256_hex_digest_is_not_flagged_as_generic_secret(
    scanner: DeterministicScanner,
) -> None:
    """16進Hashは1文字あたり最大4.0 bit。閾値4.2はこれを跨がない。

    設計書やLogにContent Hashは常に現れる。これを毎回Rejectすると
    マスキング層が実務で使えなくなる。
    """
    digest = "e20aa132de7eb03a7638ba85c7c0c54a547ed5a481e902eecd55009dca6599ea"
    assert "SECRET_GENERIC" not in scanner.scan(f"sha256:{digest}").reject_categories


def test_findings_never_carry_the_detected_value(scanner: DeterministicScanner) -> None:
    """不変条件#7。検出機構自体が漏洩経路になってはならない。"""
    secret = "AKIAIOSFODNN7EXAMPLE"
    result = scanner.scan(f"credential {secret} in config")
    assert result.findings
    for finding in result.findings:
        rendered = f"{finding.rule_id}{finding.category}{finding.count}"
        assert secret not in rendered
    assert secret not in repr(result.findings)


def test_clean_text_produces_no_findings(scanner: DeterministicScanner) -> None:
    result = scanner.scan("四半期の売上集計を作成し、部門別に比較してください。")
    assert result.is_clean
    assert result.candidate_spans == ()


# ---------------------------------------------------------------------------
# MASKABLE候補
# ---------------------------------------------------------------------------


def test_email_becomes_a_candidate_span(scanner: DeterministicScanner) -> None:
    text = "連絡先は alice@example.com です"
    result = scanner.scan(text)
    spans = [span for span in result.candidate_spans if span.category == "EMAIL"]
    assert len(spans) == 1
    assert text[spans[0].start : spans[0].end] == "alice@example.com"


def test_candidate_spans_do_not_overlap(scanner: DeterministicScanner) -> None:
    """`allow_overlap: false`。規則同士の重なりはScannerが解いてから返す。"""
    text = "see https://app.example.com/x?email=bob@example.com now"
    spans = scanner.scan(text).candidate_spans
    for earlier, later in itertools.pairwise(spans):
        assert earlier.end <= later.start


def test_longer_match_wins_over_a_shorter_contained_one(
    scanner: DeterministicScanner,
) -> None:
    """短い方を採ると、外側に残った識別子がマスクされずに出ていく。"""
    text = "https://app.example.com/x?email=bob@example.com"
    spans = scanner.scan(text).candidate_spans
    assert len(spans) == 1
    assert spans[0].category == "URL_WITH_IDENTIFIER"
    assert text[spans[0].start : spans[0].end] == text


def test_candidate_spans_are_sorted_by_start(scanner: DeterministicScanner) -> None:
    text = "a@x.co と b@y.co と 03-1234-5678"
    spans = scanner.scan(text).candidate_spans
    assert list(spans) == sorted(spans, key=lambda span: span.start)


def test_bare_date_is_not_a_birth_date_candidate(scanner: DeterministicScanner) -> None:
    """日付単体を拾うと、業務文書の大半が候補で埋まる。"""
    plain = scanner.scan("納期は2025年3月31日です")
    assert not any(span.category == "DATE_OF_BIRTH" for span in plain.candidate_spans)
    labelled = scanner.scan("生年月日: 1980年1月2日")
    assert any(span.category == "DATE_OF_BIRTH" for span in labelled.candidate_spans)


def test_person_name_has_no_deterministic_rule(scanner: DeterministicScanner) -> None:
    """人名を正規表現で判定できないことがLLMを噛ませる理由である（ADR-007 §1）。

    この試験が落ちるときは、決定論規則で人名を判定しようとしている。
    その規則は必ず誤検出と見逃しの両方を生む。
    """
    result = scanner.scan("担当は山田太郎さんです")
    assert not any(
        span.category in {"PERSON_NAME", "CUSTOMER_NAME", "ORGANIZATION_NAME"}
        for span in result.candidate_spans
    )


# ---------------------------------------------------------------------------
# 規則とRegistryの整合
# ---------------------------------------------------------------------------


def test_every_rule_targets_a_registered_category(policy: MaskingPolicy) -> None:
    """構築時にカテゴリの登録を検査する。未登録なら検出結果に行き場が無い。"""
    with pytest.raises(ValueError, match="not registered"):
        DeterministicScanner(
            reject_categories=[],
            maskable_categories=policy.maskable_categories,
        )


def test_findings_are_ordered_deterministically(scanner: DeterministicScanner) -> None:
    text = "password: abcd1234 and AKIAIOSFODNN7EXAMPLE and 123456789012"
    first = scanner.scan(text).findings
    second = scanner.scan(text).findings
    assert first == second
    assert list(first) == sorted(first, key=lambda finding: finding.rule_id)


def test_counts_reflect_multiple_occurrences(scanner: DeterministicScanner) -> None:
    result = scanner.scan("a@x.co b@y.co c@z.co")
    email = [finding for finding in result.findings if finding.category == "EMAIL"]
    assert len(email) == 1
    assert email[0].count == 3
    assert email[0].disposition is Disposition.MASKABLE
