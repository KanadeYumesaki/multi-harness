"""Domain層用の正規UTC Timestamp処理。

Domain層はOS Clockや``datetime``へ依存しない。時刻は必ず
``YYYY-MM-DDTHH:MM:SSZ``の文字列として受け、純粋な暦計算で比較する。
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["canonical_timestamp", "timestamp_from_seconds", "timestamp_seconds"]

_TIMESTAMP: Final[re.Pattern[str]] = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})Z$"
)
_MONTH_LENGTHS: Final[tuple[int, ...]] = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def canonical_timestamp(value: str) -> str:
    """正規UTC Timestampを検証し、そのまま返す。"""
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError("timestamp must be canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    year = int(match["year"])
    month = int(match["month"])
    day = int(match["day"])
    hour = int(match["hour"])
    minute = int(match["minute"])
    second = int(match["second"])
    if year < 1 or not 1 <= month <= 12:
        raise ValueError("timestamp has an invalid year or month")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise ValueError("timestamp has an invalid time")
    days = _MONTH_LENGTHS[month - 1] + (1 if month == 2 and _leap(year) else 0)
    if not 1 <= day <= days:
        raise ValueError("timestamp has an invalid day")
    return value


def timestamp_seconds(value: str) -> int:
    """TimestampをUTC epochからの秒数へ変換し、差分比較に利用する。"""
    checked = canonical_timestamp(value)
    year = int(checked[0:4])
    month = int(checked[5:7])
    day = int(checked[8:10])
    hour = int(checked[11:13])
    minute = int(checked[14:16])
    second = int(checked[17:19])
    prior_year = year - 1
    days = prior_year * 365 + prior_year // 4 - prior_year // 100 + prior_year // 400
    for current_month in range(1, month):
        days += _MONTH_LENGTHS[current_month - 1]
        if current_month == 2 and _leap(year):
            days += 1
    days += day - 1
    return (((days * 24) + hour) * 60 + minute) * 60 + second


def timestamp_from_seconds(total_seconds: int) -> str:
    """`timestamp_seconds` の逆写像。**同じ基準で書き戻す。**

    期限を「いま + TTL」で作る側が、独自の暦計算を持たないようにするために置く。
    基準がずれると期限が遠い未来になり、**期限切れ検査が黙って効かなくなる**。
    実際にその形の誤りを一度作ったので、往復一致を試験で固定してある。

    `timestamp_seconds` は 0001-01-01T00:00:00Z を 0 とする秒数を返す。ここでは
    Howard Hinnant の `civil_from_days` を使って暦へ戻す。閏年規則を近似しない。
    """
    if total_seconds < 0:
        raise ValueError("timestamp seconds must not be negative")
    days, remainder = divmod(total_seconds, 86400)
    hour, remainder = divmod(remainder, 3600)
    minute, second = divmod(remainder, 60)
    year, month, day = _civil_from_days(days)
    if not 1 <= year <= 9999:
        raise ValueError("timestamp is outside the representable range")
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


#: 0001-01-01 から 1970-01-01 までの日数。`timestamp_seconds` の基準を Unix 基準へ移す。
_DAYS_BEFORE_UNIX_EPOCH: Final[int] = 719162


def _civil_from_days(days_since_year_one: int) -> tuple[int, int, int]:
    """Howard Hinnant の `civil_from_days`（Unix 基準）へ寄せてから暦へ戻す。"""
    days = days_since_year_one - _DAYS_BEFORE_UNIX_EPOCH + 719468
    era = (days if days >= 0 else days - 146096) // 146097
    day_of_era = days - era * 146097
    year_of_era = (
        day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096
    ) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    shifted_month = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * shifted_month + 2) // 5 + 1
    month = shifted_month + 3 if shifted_month < 10 else shifted_month - 9
    return (year + 1 if month <= 2 else year, month, day)


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
