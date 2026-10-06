"""決定論スキャナ。Scan#1（入力）とScan#2（出力ゲート）は同一の規則集合で動く。

## なぜ同じ規則で2回走らせるのか

Scan#1は「LLMへ渡してよいか」の判定と候補抽出、Scan#2は「出てきた結果を
外へ出してよいか」の判定である。**判定権限を持つのはScan#2だけ**とみなす
（ADR-007 §5）。LLMが過少マスクをしても、規則で検出できるものは
Scan#2が捕まえる。同じ規則で走らせるからこそ「Scan#1で候補になったのに
出力に残っている」が矛盾として現れる。

## 検出結果に値を入れない

`ScanFinding` は Rule ID・カテゴリ・件数しか持たない（不変条件#7）。
検出したSecretそのものをLog・Event・Errorへ載せると、
検出機構が漏洩経路になる。座標（`Span`）はPipeline内部でのみ使い、
Ledgerへは件数だけを出す。

## 規則は過剰検出側へ倒す

REJECT側の誤検出は「使えない」で済むが、見逃しは信用情報の流出になる。
`NATIONAL_ID` の12桁数字のように、一般の数字列を巻き込む規則を
意図的に採る（ADR-007 §6、MVP0-AではNATIONAL_IDはReject）。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from harness.domain.masking import Span

# 結果型はdomainに置く（CLAUDE.md §2）。ここは規則の実装だけを持つ。
from harness.domain.masking_result import Disposition, ScanFinding

__all__ = [
    "DeterministicScanner",
    "Disposition",
    "ScanFinding",
    "ScanResult",
]


@dataclass(frozen=True, slots=True)
class ScanResult:
    findings: tuple[ScanFinding, ...]
    candidate_spans: tuple[Span, ...]

    @property
    def reject_categories(self) -> tuple[str, ...]:
        """検出されたREJECTカテゴリ。1件でもあれば入力ごと拒否する。"""
        seen: list[str] = []
        for finding in self.findings:
            if finding.disposition is Disposition.REJECT and finding.category not in seen:
                seen.append(finding.category)
        return tuple(seen)

    @property
    def is_clean(self) -> bool:
        return not self.findings


class _Rule(Protocol):
    # 読取り専用Propertyとして宣言する。可変属性として書くと、
    # frozen dataclassである実装側と型が合わない。
    @property
    def rule_id(self) -> str: ...

    @property
    def category(self) -> str: ...

    @property
    def disposition(self) -> Disposition: ...

    def find(self, text: str) -> Iterator[tuple[int, int]]: ...


@dataclass(frozen=True, slots=True)
class _RegexRule:
    rule_id: str
    category: str
    disposition: Disposition
    pattern: re.Pattern[str]

    def find(self, text: str) -> Iterator[tuple[int, int]]:
        for match in self.pattern.finditer(text):
            yield match.start(), match.end()


@dataclass(frozen=True, slots=True)
class _EntropyRule:
    """高エントロピー文字列。Secretは形式ではなく無作為性で見つかることがある。

    16進Hashは1文字あたり最大4.0 bitであり、閾値を4.0より上に置くことで
    Content Hashの記載を巻き込まない。Base64やランダム鍵は5 bitを超える。
    """

    rule_id: str
    category: str
    disposition: Disposition
    minimum_length: int
    minimum_entropy_bits: float
    token_pattern: re.Pattern[str]

    def find(self, text: str) -> Iterator[tuple[int, int]]:
        for match in self.token_pattern.finditer(text):
            token = match.group()
            if len(token) < self.minimum_length:
                continue
            if _shannon_entropy_bits(token) >= self.minimum_entropy_bits:
                yield match.start(), match.end()


def _shannon_entropy_bits(token: str) -> float:
    counts = Counter(token)
    length = len(token)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


# ---------------------------------------------------------------------------
# Key-Value 形式のSecret
# ---------------------------------------------------------------------------

# `key: value` の**形**だけを取り、Key名の判定はPython側で行う。
#
# 正規表現1本で `api_key` `apiKey` `API-KEY` `"api_key"` を網羅しようとすると、
# 読めない上に必ず抜けが出る。実際、初版の `\b(?:api[_-]?key|...)` は
# JSON形式 `{"api_key":"short"}` を取り逃していた。Keyが引用符に挟まれると
# `\b` の位置が変わるためである。
#
# 形だけ取って正規化してから照合すれば、記法の違いは正規化で吸収できる。
# 区切りの前後は `[ \t]*` であって `\s*` ではない。改行を跨がせると
# YAMLの入れ子を誤読する。`database:\n  password: hunter2xyz` で `\s*` を使うと
# key=database / value=password となり、**本命の password 行が
# 消費済み区間に入って検出できなくなる**。
#
# 値の文字集合から `:` を除くのも同じ理由である。Scalarの値が
# 次のKeyまで伸びるのを止める。
_KEY_VALUE_SHAPE: Final[re.Pattern[str]] = re.compile(
    r"""["']?(?P<key>[A-Za-z][A-Za-z0-9_-]{1,40})["']?   # Key。引用符は任意
        [ \t]*[:=：][ \t]*                                # 区切り。改行を跨がない
        ["']?(?P<value>[^\s"',;:}\]]{1,200})["']?         # 値""",
    re.VERBOSE,
)

# 真偽値・空値だけを除く。
#
# **数字だけの値を除外してはならない。** 初版は `max_tokens: 4096` のような
# 設定値との衝突を避けるため `value.isdigit()` を除外条件に入れていたが、
# それでは `password: 1234` `api_key: 1234` `token: 123456` が素通りする。
# 数値のPINやPasswordは実在し、しかも弱い。**もっとも守るべき値を
# 除外していた**ことになる。
#
# 衝突は値の形ではなくKey名で解く。Key名はSecret語彙と完全一致で照合して
# いるため、`max_tokens` や `token_budget` はそもそも一致しない。
# 一致しない以上、値が数字かどうかを見る必要が無い。
_NON_SECRET_LITERALS: Final[frozenset[str]] = frozenset(
    {"true", "false", "null", "none", "nil", "undefined"}
)
_MIN_SECRET_VALUE_LENGTH: Final[int] = 4


def _normalize_key(key: str) -> str:
    """`API-KEY` `apiKey` `api_key` を同一視する。"""
    return key.replace("_", "").replace("-", "").lower()


@dataclass(frozen=True, slots=True)
class _KeyValueSecretRule:
    """Secretを示すKey名に値が付いている箇所を検出する。

    Key名の集合はカテゴリごとに分ける。`_Rule` は1カテゴリしか持てないため、
    カテゴリ数だけこの規則を並べる。
    """

    rule_id: str
    category: str
    disposition: Disposition
    key_words: frozenset[str]

    def find(self, text: str) -> Iterator[tuple[int, int]]:
        for match in _KEY_VALUE_SHAPE.finditer(text):
            if _normalize_key(match.group("key")) not in self.key_words:
                continue
            value = match.group("value")
            if len(value) < _MIN_SECRET_VALUE_LENGTH:
                continue
            if value.lower() in _NON_SECRET_LITERALS:
                continue
            yield match.start(), match.end()


# ---------------------------------------------------------------------------
# 規則集合
# ---------------------------------------------------------------------------
# カテゴリIDは masking-policy.yaml の登録値と一致していなければならない。
# 一致検査は DeterministicScanner.__init__ が行う。

_PEM_LABELS: Final[str] = "RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED "

_REJECT_RULES: Final[tuple[_Rule, ...]] = (
    _RegexRule(
        "R-PRIVATE-KEY-PEM",
        "PRIVATE_KEY",
        Disposition.REJECT,
        re.compile(rf"-----BEGIN (?:{_PEM_LABELS})?PRIVATE KEY-----"),
    ),
    _RegexRule(
        "R-CERTIFICATE-PEM",
        "CERTIFICATE_KEY",
        Disposition.REJECT,
        re.compile(r"-----BEGIN (?:CERTIFICATE|CERTIFICATE REQUEST)-----"),
    ),
    _RegexRule(
        "R-AWS-ACCESS-KEY-ID",
        "CLOUD_CREDENTIAL",
        Disposition.REJECT,
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b"),
    ),
    _RegexRule(
        "R-GCP-SERVICE-ACCOUNT",
        "CLOUD_CREDENTIAL",
        Disposition.REJECT,
        re.compile(r'"type"\s*:\s*"service_account"'),
    ),
    _RegexRule(
        "R-BEARER-TOKEN",
        "BEARER_TOKEN",
        Disposition.REJECT,
        re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{16,}={0,2}"),
    ),
    _RegexRule(
        "R-VENDOR-API-KEY",
        "API_KEY",
        Disposition.REJECT,
        re.compile(
            r"\b(?:sk|pk|rk)-[A-Za-z0-9]{20,}\b"
            r"|\bgh[pousr]_[A-Za-z0-9]{30,}\b"
            r"|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
        ),
    ),
    # --- Key-Value 形式。記法差はKey正規化で吸収する ---
    _KeyValueSecretRule(
        "R-SECRET-KV-PASSWORD",
        "PASSWORD",
        Disposition.REJECT,
        frozenset({"password", "passwd", "pwd", "userpassword", "dbpassword"}),
    ),
    _KeyValueSecretRule(
        "R-SECRET-KV-API-KEY",
        "API_KEY",
        Disposition.REJECT,
        frozenset({"apikey", "apisecret", "clientsecret", "consumersecret", "subscriptionkey"}),
    ),
    _KeyValueSecretRule(
        "R-SECRET-KV-TOKEN",
        "BEARER_TOKEN",
        Disposition.REJECT,
        # `token` 単体も含める。ADR-007 §6 は認証情報を即Rejectと定めており、
        # 過剰Rejectより見逃しの方が高くつく。設定値との衝突は
        # 「純粋な10進数を除く」規則で避けている（`max_tokens: 4096` は通る）。
        frozenset(
            {
                "token",
                "accesstoken",
                "refreshtoken",
                "idtoken",
                "authtoken",
                "bearertoken",
                "authorization",
                "auth",
            }
        ),
    ),
    _KeyValueSecretRule(
        "R-SECRET-KV-GENERIC",
        "SECRET_GENERIC",
        Disposition.REJECT,
        frozenset({"secret", "credential", "credentials", "signingkey", "encryptionkey"}),
    ),
    _KeyValueSecretRule(
        "R-SECRET-KV-PRIVATE-KEY",
        "PRIVATE_KEY",
        Disposition.REJECT,
        frozenset({"privatekey", "secretkey", "signingprivatekey"}),
    ),
    _KeyValueSecretRule(
        "R-SECRET-KV-SESSION",
        "SESSION_COOKIE",
        Disposition.REJECT,
        frozenset({"sessionid", "jsessionid", "phpsessid", "sessionkey", "sid"}),
    ),
    _RegexRule(
        "R-SESSION-COOKIE-HEADER",
        "SESSION_COOKIE",
        Disposition.REJECT,
        # `Cookie: JSESSIONID=...` はKey-Value規則では捕まらない。
        # 外側の `Cookie:` が先に一致し、内側のSession IDが
        # 消費済み区間へ入ってしまうためである。Header形は別建てにする。
        re.compile(
            r"(?i)\b(?:jsessionid|phpsessid|sessionid|session_id|asp\.net_sessionid)"
            r"\s*=\s*\S{8,}"
        ),
    ),
    _RegexRule(
        "R-PASSWORD-JP",
        "PASSWORD",
        Disposition.REJECT,
        # 日本語Key。`_KEY_VALUE_SHAPE` のKey文字集合はASCIIのため別建てにする。
        re.compile(r"(?:パスワード|暗証番号|合言葉)\s*[:=：]\s*\S{4,}"),
    ),
    _RegexRule(
        "R-URL-WITH-CREDENTIAL",
        "BEARER_TOKEN",
        Disposition.REJECT,
        # 資格情報をQuery Parameterへ載せたURL。初版はこれを
        # `URL_WITH_IDENTIFIER`（MASKABLE）として**Maskerへ渡していた**。
        # ADR-007 §6 は認証情報をLLMへ送らず即Rejectと定めている。
        re.compile(
            r"https?://[^\s]*[?&]"
            r"(?:token|access_token|api_key|apikey|key|secret|password|passwd"
            r"|auth|sig|signature|credential)="
            r"[^\s&]{4,}",
            re.IGNORECASE,
        ),
    ),
    _RegexRule(
        "R-CONNECTION-STRING",
        "CONNECTION_STRING",
        Disposition.REJECT,
        # userinfo に password を含むURI。`user:pass@host` の形。
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/?#@]+:[^\s:/?#@]+@"),
    ),
    _RegexRule(
        "R-DOTENV-SECRET-ASSIGNMENT",
        "DOTENV_CONTENT",
        Disposition.REJECT,
        re.compile(
            r"(?m)^(?:export\s+)?[A-Z][A-Z0-9_]*"
            r"(?:SECRET|TOKEN|KEY|PASSWORD|CREDENTIAL)\s*=\s*\S+"
        ),
    ),
    _RegexRule(
        "R-CANARY",
        "CANARY",
        Disposition.REJECT,
        # 試験用Canary。検出できなければマスキング層が働いていない証拠になる。
        re.compile(r"FDE-HARNESS-CANARY-[A-Z0-9]{8,}"),
    ),
    _RegexRule(
        "R-NATIONAL-ID-JP-MYNUMBER",
        "NATIONAL_ID",
        Disposition.REJECT,
        # マイナンバーは12桁。区切り文字なしの12桁数字を広く拒否する。
        re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])"),
    ),
    _RegexRule(
        "R-NATIONAL-ID-JP-MYNUMBER-GROUPED",
        "NATIONAL_ID",
        Disposition.REJECT,
        # 通知カード・帳票では4桁ずつ区切って表記される。区切りなしだけを
        # 見ていると、実務でもっとも多い書き方を取り逃がす。
        re.compile(r"(?<![0-9])[0-9]{4}[ \-‐－]?[0-9]{4}[ \-‐－]?[0-9]{4}(?![0-9])"),
    ),
    _RegexRule(
        "R-NATIONAL-ID-US-SSN",
        "NATIONAL_ID",
        Disposition.REJECT,
        re.compile(r"\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b"),
    ),
    _RegexRule(
        "R-SPECIAL-CATEGORY-KEYWORD",
        "SPECIAL_CATEGORY_DATA",
        Disposition.REJECT,
        # 要配慮個人情報。マスクせず拒否する（masking-policy.yaml）。
        re.compile(
            r"(?:病名|診断名|既往症|通院歴|障害者手帳|前科|犯罪歴|逮捕歴|人種|信条|"
            r"宗教|支持政党|労働組合|遺伝情報|性的指向"
            r"|(?i:\bmedical\s+diagnosis\b|\bcriminal\s+record\b|\bsexual\s+orientation\b))"
        ),
    ),
    _EntropyRule(
        "R-HIGH-ENTROPY-TOKEN",
        "SECRET_GENERIC",
        Disposition.REJECT,
        minimum_length=32,
        minimum_entropy_bits=4.2,
        token_pattern=re.compile(r"[A-Za-z0-9+/=_\-]{32,}"),
    ),
)

_MASKABLE_RULES: Final[tuple[_Rule, ...]] = (
    _RegexRule(
        "M-EMAIL",
        "EMAIL",
        Disposition.MASKABLE,
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    ),
    _RegexRule(
        "M-URL-WITH-IDENTIFIER",
        "URL_WITH_IDENTIFIER",
        Disposition.MASKABLE,
        # `token` をここから外した。資格情報を含むURLは
        # `R-URL-WITH-CREDENTIAL` がREJECT側で扱う。マスクして
        # Maskerへ渡してよいのは**識別子**であって認証情報ではない。
        re.compile(r"https?://[^\s]*[?&](?:id|uid|user|user_id|email|account)=[^\s&]*[^\s&.,)]"),
    ),
    _RegexRule(
        "M-PHONE-JP",
        "PHONE_NUMBER",
        Disposition.MASKABLE,
        re.compile(r"(?<![0-9-])0[0-9]{1,4}-[0-9]{1,4}-[0-9]{4}(?![0-9-])"),
    ),
    _RegexRule(
        "M-PHONE-E164",
        "PHONE_NUMBER",
        Disposition.MASKABLE,
        re.compile(r"(?<![0-9])\+[0-9]{1,3}-?[0-9]{1,4}-?[0-9]{1,4}-?[0-9]{3,4}(?![0-9])"),
    ),
    _RegexRule(
        "M-POSTAL-JP",
        "POSTAL_ADDRESS",
        Disposition.MASKABLE,
        re.compile(r"〒\s?[0-9]{3}-[0-9]{4}"),
    ),
    _RegexRule(
        "M-EMPLOYEE-ID",
        "EMPLOYEE_ID",
        Disposition.MASKABLE,
        re.compile(
            r"(?:社員番号|従業員番号|社員ID|(?i:employee\s+id))\s*[:：]?\s*[A-Za-z0-9\-]{4,}"
        ),
    ),
    _RegexRule(
        "M-BANK-ACCOUNT",
        "BANK_ACCOUNT",
        Disposition.MASKABLE,
        re.compile(r"(?:口座番号|口座No|(?i:account\s+(?:number|no\.?)))\s*[:：]?\s*[0-9\-]{7,}"),
    ),
    _RegexRule(
        "M-DATE-OF-BIRTH",
        "DATE_OF_BIRTH",
        Disposition.MASKABLE,
        # 日付単体は拾わない。生年月日を示す語に続くものだけを候補にする。
        re.compile(
            r"(?:生年月日|誕生日|(?i:date\s+of\s+birth|\bdob\b))\s*[:：]?\s*"
            r"(?:[0-9]{4}[-/年][0-9]{1,2}[-/月][0-9]{1,2}日?"
            r"|[0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4})"
        ),
    ),
)

# PERSON_NAME／CUSTOMER_NAME／ORGANIZATION_NAME に決定論規則は無い。
# 正規表現で人名を判定できないためであり、これがLLMを噛ませる理由そのものである
# （ADR-007 §1）。この3カテゴリはLLM提案Spanでのみマスクされ、
# Scan#2でも検出されない。残存リスクとしてADR-007 §9に記載がある。


class DeterministicScanner:
    """Scan#1／Scan#2を実行する。状態を持たない。"""

    def __init__(
        self,
        *,
        reject_categories: Sequence[str],
        maskable_categories: Sequence[str],
        rules: Sequence[_Rule] | None = None,
    ) -> None:
        self._rules: tuple[_Rule, ...] = (
            tuple(rules) if rules is not None else _REJECT_RULES + _MASKABLE_RULES
        )
        registered_reject = frozenset(reject_categories)
        registered_maskable = frozenset(maskable_categories)

        # 規則のカテゴリがRegistryに無ければ、その規則の検出結果は
        # 行き場が無い。黙って無視せず、構築時に落とす。
        for rule in self._rules:
            expected = (
                registered_reject if rule.disposition is Disposition.REJECT else registered_maskable
            )
            if rule.category not in expected:
                raise ValueError(
                    f"rule {rule.rule_id} targets category {rule.category!r} "
                    f"which is not registered as {rule.disposition.value}"
                )

    # ------------------------------------------------------------------

    def scan(self, text: str) -> ScanResult:
        """全規則を適用する。

        REJECT検出があってもMASKABLE規則を止めない。呼出側が
        「Rejectなので候補は使わない」と判断できるよう、
        結果は完全な形で返す。
        """
        counts: dict[tuple[str, str, Disposition], int] = {}
        raw: list[tuple[int, int, str, Disposition, int]] = []

        for order, rule in enumerate(self._rules):
            for start, end in rule.find(text):
                key = (rule.rule_id, rule.category, rule.disposition)
                counts[key] = counts.get(key, 0) + 1
                raw.append((start, end, rule.category, rule.disposition, order))

        findings = tuple(
            ScanFinding(rule_id, category, disposition, count)
            for (rule_id, category, disposition), count in sorted(
                counts.items(), key=lambda item: item[0][0]
            )
        )
        return ScanResult(
            findings=findings,
            candidate_spans=self._resolve_candidates(raw),
        )

    @staticmethod
    def _resolve_candidates(
        raw: list[tuple[int, int, str, Disposition, int]],
    ) -> tuple[Span, ...]:
        """MASKABLE検出から非重複の候補Spanを作る。

        `allow_overlap: false` であるため、規則同士の重なりをここで解く。
        EMAILがURL_WITH_IDENTIFIERの内側に現れる、といった重なりは普通に起きる。

        **長い方を優先する。** 開始位置順に選ぶと、先に現れた短いSpanが
        後続の長いSpanを弾き、長い方の残りがマスクされずに出ていく。
        `https://h/?email=a@b.com` でEMAILを先に採ると、URL側の
        識別子部分が露出する。長さ降順で選べばこの取りこぼしが起きない。
        """
        maskable = [row for row in raw if row[3] is Disposition.MASKABLE]
        # 長い順 → 開始位置昇順 → 規則定義順。後ろ2項は同着時の決定性のため。
        maskable.sort(key=lambda row: (-(row[1] - row[0]), row[0], row[4]))

        selected: list[Span] = []
        for start, end, category, _disposition, _order in maskable:
            candidate = Span(start=start, end=end, category=category)
            if any(chosen.overlaps(candidate) for chosen in selected):
                continue
            selected.append(candidate)
        return tuple(sorted(selected, key=lambda span: (span.start, span.end)))
