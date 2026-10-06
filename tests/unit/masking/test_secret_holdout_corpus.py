"""Secret検出のHold-out Corpus（ADR-007 §6、レビュー BLOCKER 1）。

## なぜ規則の試験と別に置くのか

`test_scanner.py` は「実装した規則が動くこと」を確かめる。規則を書いた本人が
試験も書くため、**規則が想定していない書き方は試験にも現れない**。実際、
初版は次を全て取り逃していた。

    {"password":"hunter2"}      JSON形式（Keyが引用符に挟まれる）
    {"api_key":"short"}         同上 + 短い値
    1234 5678 9012              区切り付きマイナンバー
    ?token=...                  URLのQuery Parameter（MASKABLE扱いだった）

本Fileは**規則の実装を見ずに「業務入力に現れ得るSecretの書き方」を列挙する**
という方針で作る。規則を足すたびにここへ寄せるのではなく、
ここが落ちたら規則を直す。向きを逆にしないことが要点である。

## 過剰Reject側も同じ重みで固定する

見逃しは流出、過剰Rejectは業務停止であり、どちらも実害がある。
`SAFE_CORPUS` は「絶対にRejectしてはならない普通の業務入力」を並べる。
片側だけを厳しくすると、もう片側が静かに壊れる。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from harness.infrastructure.masking.policy import MaskingPolicy
from harness.infrastructure.masking.scanner import DeterministicScanner, ScanFinding

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def scanner() -> DeterministicScanner:
    policy = MaskingPolicy.load(REPO_ROOT)
    return DeterministicScanner(
        reject_categories=policy.reject_categories,
        maskable_categories=policy.maskable_categories,
    )


# ---------------------------------------------------------------------------
# 必ずRejectされなければならない
# ---------------------------------------------------------------------------

SECRET_CORPUS: list[tuple[str, str]] = [
    # --- JSON ---
    ("json_password", '{"password":"hunter2"}'),
    ("json_api_key_short", '{"api_key":"short"}'),
    ("json_camel_case", '{"apiKey": "abcdefghijkl"}'),
    ("json_nested", '{"db": {"password": "p@ssw0rd!"}}'),
    ("json_client_secret", '{"client_secret":"9f8e7d6c5b4a3210"}'),
    ("json_bearer", '{"authorization":"Bearer abcdefghijklmnopqrst"}'),
    # --- 数字だけの値 ---
    # 初版は `value.isdigit()` を除外条件に入れており、これらを全て見逃していた。
    # 数値のPIN・Passwordは実在し、しかも弱い。もっとも守るべき値だった。
    ("numeric_password_4", "password: 1234"),
    ("numeric_password_6", "password: 123456"),
    ("numeric_password_json", '{"password":"1234"}'),
    ("numeric_token", "token: 123456"),
    ("numeric_api_key", "api_key: 1234"),
    ("numeric_pin_jp", "暗証番号: 4823"),
    # --- YAML ---
    ("yaml_password", "database:\n  password: hunter2xyz"),
    ("yaml_token", "auth:\n  token: abcdef123456"),
    ("yaml_quoted", 'secret: "s3cr3t-value"'),
    # --- .env / shell ---
    ("dotenv_password", "DATABASE_PASSWORD=abc123"),
    ("dotenv_export", "export API_SECRET=zyxw9876"),
    ("shell_assignment", "TOKEN=abcdef123456"),
    # --- HTTP ---
    ("http_bearer_header", "Authorization: Bearer abcdefghijklmnopqrstuvwx"),
    ("http_cookie", "Cookie: JSESSIONID=A1B2C3D4E5F6G7H8"),
    ("url_token_param", "https://x.example/cb?token=abcdefghijklmnop"),
    ("url_api_key_param", "https://x.example/v1?api_key=abcdef123456"),
    ("url_signature_param", "https://x.example/o?sig=9f8e7d6c5b4a3210"),
    ("url_userinfo", "postgres://appuser:s3cr3t@db.internal:5432/app"),
    # --- Vendor形式 ---
    ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
    ("aws_temp_key_id", "ASIAIOSFODNN7EXAMPLE"),
    ("github_pat", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"),
    ("slack_token", "xoxb-123456789012-abcdefghijkl"),
    ("openai_style", "sk-abcdefghijklmnopqrstuvwxyz01"),
    ("gcp_service_account", '{"type": "service_account", "project_id": "x"}'),
    # --- PEM ---
    ("pem_rsa", "-----BEGIN RSA PRIVATE KEY-----"),
    ("pem_openssh", "-----BEGIN OPENSSH PRIVATE KEY-----"),
    ("pem_plain", "-----BEGIN PRIVATE KEY-----"),
    ("pem_encrypted", "-----BEGIN ENCRYPTED PRIVATE KEY-----"),
    ("pem_certificate", "-----BEGIN CERTIFICATE-----"),
    # --- 日本語Key ---
    ("jp_password", "パスワード：Sup3rSecret"),
    ("jp_pin", "暗証番号: 8823xyz"),
    # --- 国民識別番号 ---
    ("mynumber_bare", "マイナンバーは123456789012です"),
    ("mynumber_spaced", "1234 5678 9012"),
    ("mynumber_hyphen", "1234-5678-9012"),
    ("us_ssn", "SSN 123-45-6789"),
    # --- 要配慮個人情報 ---
    ("jp_diagnosis", "患者の病名を共有します"),
    ("jp_criminal", "前科の有無を確認"),
    ("en_criminal", "his criminal record shows"),
    # --- 高エントロピー ---
    ("high_entropy", "token qX7t2M9pZa4Vb8Nc1Ld6Ke3Jf5Hg0Iy2Uw ends"),
    # --- Canary ---
    ("canary", "marker FDE-HARNESS-CANARY-QX7T2M9P end"),
]


@pytest.mark.parametrize(
    "text", [text for _, text in SECRET_CORPUS], ids=[name for name, _ in SECRET_CORPUS]
)
def test_secret_corpus_is_rejected(scanner: DeterministicScanner, text: str) -> None:
    """見逃しは信用情報の流出になる。1件も通してはならない。"""
    result = scanner.scan(text)
    assert result.reject_categories, (
        "Secretを検出できていない。決定論スキャナが見逃すとLLMへ渡る。\n"
        f"  検出候補（MASKABLE）: {sorted({s.category for s in result.candidate_spans})}"
    )


def test_no_secret_is_merely_maskable(scanner: DeterministicScanner) -> None:
    """SecretをMASKABLEとして扱っていないこと。

    MASKABLEはMaskerへ渡す判断である。認証情報がここに落ちると、
    「マスクするためにLLMへ送る」というADR-007 §6が禁じた経路が開く。
    """
    leaked: list[str] = []
    for name, text in SECRET_CORPUS:
        result = scanner.scan(text)
        if not result.reject_categories and result.candidate_spans:
            leaked.append(name)
    assert not leaked, f"SecretがMASKABLE扱いになっている: {leaked}"


# ---------------------------------------------------------------------------
# 絶対にRejectしてはならない
# ---------------------------------------------------------------------------

SAFE_CORPUS: list[tuple[str, str]] = [
    ("config_max_tokens", "max_tokens: 4096"),
    ("config_token_budget", "token_budget: 1000"),
    ("config_retry", "retry: 3"),
    ("config_bool", "enabled: true"),
    ("config_null", "cache: null"),
    ("plain_japanese", "四半期の売上を部門別に集計してください。"),
    ("delivery_date", "納期は2025年3月31日です"),
    ("content_hash", "sha256:e20aa132de7eb03a7638ba85c7c0c54a547ed5a481e902eecd55009dca6599ea"),
    ("order_number", "注文番号 1234-5678 の件"),
    ("phone_number", "電話番号は 03-1234-5678 です"),
    ("plain_url", "https://example.com/docs/getting-started"),
    ("url_with_email", "https://app.example.com/x?email=bob@example.com"),
    ("email_only", "連絡先は alice@example.com です"),
    ("markdown_heading", "## 15.9 Core Schema Catalog v1"),
    ("version_string", "version: 1.8.0"),
]


@pytest.mark.parametrize(
    "text", [text for _, text in SAFE_CORPUS], ids=[name for name, _ in SAFE_CORPUS]
)
def test_safe_corpus_is_not_rejected(scanner: DeterministicScanner, text: str) -> None:
    """過剰Rejectは業務停止になる。普通の入力を止めてはならない。"""
    result = scanner.scan(text)
    assert not result.reject_categories, (
        f"普通の業務入力をRejectしている: {result.reject_categories}"
    )


# ---------------------------------------------------------------------------
# 検出結果に値を含めない
# ---------------------------------------------------------------------------


def test_finding_has_no_field_that_could_carry_a_value() -> None:
    """不変条件#7を**構造**で保証する。

    部分文字列の照合では保証にならない。Rule IDに `R-CERTIFICATE-PEM` の
    ような語が入るため、入力 `-----BEGIN CERTIFICATE-----` と偶然一致して
    「値が漏れている」と誤判定する。実際に初版の粗い照合はここで誤検出した。

    `ScanFinding` が持てるFieldを固定すれば、値を載せる余地そのものが無い。
    Fieldを増やす変更はこの試験で必ず止まる。
    """
    names = {field.name for field in dataclasses.fields(ScanFinding)}
    assert names == {"rule_id", "category", "disposition", "count"}, (
        f"ScanFindingのFieldが変わっている: {sorted(names)}\n"
        "値を運べるFieldを足すと、検出機構そのものが漏洩経路になる。"
    )


def test_distinctive_secret_value_never_reaches_the_finding(
    scanner: DeterministicScanner,
) -> None:
    """Rule IDと衝突し得ない目印を使い、値が載らないことを実測する。"""
    marker = "Zq7Xw2Nv9Kb4Ld6P"
    for template in (
        "password: {}",
        '{{"api_key":"{}"}}',
        "https://x.example/?token={}",
        "Authorization: Bearer {}",
    ):
        text = template.format(marker)
        result = scanner.scan(text)
        assert result.reject_categories, f"検出できていない: {text!r}"
        assert marker not in repr(result.findings)
        assert all(marker not in f"{f.rule_id}{f.category}" for f in result.findings)
