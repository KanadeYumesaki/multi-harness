"""`/proc/self/mountinfo` の解析と最長Prefix一致（§0.1／§1.16.2）。

I/Oを持たない。Fileを読むのは`infrastructure/filesystem/workspace_boundary.py`。
本Moduleは**文字列を受け取って構造化するだけ**である。分離しておくと、
実環境に存在しないFilesystem（drvfs、9p、cifs）の判定を固定Fixtureで試験できる。
WSL2上でNFSをmountしないと9p拒否を確認できない、という状態を避ける。

## 文字列Prefixで `/mnt` を判定しない

§1.16.2 は「文字列Prefixだけで`/mnt`判定せず、Mount情報とRoot Directory Handleの
Identityを併用する」と定める。`/mnt` で始まるかどうかの判定は、
`/mnt` という名前のext4 Directoryを作れば回避できてしまう。逆に、
`/home/x/ws` が実は drvfs でmountされている場合を見逃す。

判定の根拠はMount Entryの**Filesystem Type**であり、Path文字列ではない。
Path文字列は「どのMount Entryを見るか」を決めるためだけに使う。

## mountinfo の行形式（proc(5)）

    36 35 98:0 /mnt1 /mnt rw,noatime master:1 - ext3 /dev/root rw,errors=continue
    (1)(2)(3)  (4)   (5)  (6)        (7)      (8)(9)  (10)     (11)

(7) の Optional Field は**0個以上**あり、`-` で終端される。個数が可変なので
固定index では読めない。`-` の位置を探してから後半を読む。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "MountEntry",
    "find_mount_for_path",
    "parse_mountinfo",
    "unescape_mountinfo_field",
]

# proc(5): space, tab, newline, backslash は8進数Escapeで書かれる。
_ESCAPES: Final[dict[str, str]] = {
    "040": " ",
    "011": "\t",
    "012": "\n",
    "134": "\\",
}


def unescape_mountinfo_field(field: str) -> str:
    """`\\040` 形式のEscapeを戻す。

    Mount Pointに空白を含むPathは実在する。Escapeを戻さずに比較すると、
    そのMountだけ最長Prefix一致から漏れる。
    """
    if "\\" not in field:
        return field
    out: list[str] = []
    index = 0
    while index < len(field):
        if field[index] == "\\" and index + 3 < len(field) + 1:
            code = field[index + 1 : index + 4]
            if code in _ESCAPES:
                out.append(_ESCAPES[code])
                index += 4
                continue
        out.append(field[index])
        index += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class MountEntry:
    """mountinfo 1行。Runtime Attestationへ記録する項目を全て持つ（§1.16.2）。"""

    mount_id: int
    parent_id: int
    major: int
    minor: int
    root: str
    mount_point: str
    mount_options: str
    filesystem_type: str
    mount_source: str
    super_options: str
    # File内の出現順。同一Mount Pointが複数ある場合、後のものが手前を覆う。
    order: int

    @property
    def device_id(self) -> tuple[int, int]:
        """`st_dev` と突き合わせるためのMajor:Minor。"""
        return (self.major, self.minor)


def parse_mountinfo(text: str) -> tuple[MountEntry, ...]:
    """mountinfo全文を解析する。壊れた行は黙って飛ばさず例外にする。

    読み飛ばしを許すと、拒否すべきMountの行が壊れていた場合に
    「該当Mountなし」となり、判定が緩む方向へ倒れる。
    """
    entries: list[MountEntry] = []
    for order, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        entries.append(_parse_line(line, order))
    return tuple(entries)


def _parse_line(line: str, order: int) -> MountEntry:
    fields = line.split(" ")
    if len(fields) < 10:
        raise ValueError(f"mountinfo line has {len(fields)} fields, expected at least 10")

    try:
        separator = fields.index("-", 6)
    except ValueError:
        raise ValueError("mountinfo line has no '-' separator") from None
    if len(fields) < separator + 4:
        raise ValueError("mountinfo line is truncated after the '-' separator")

    major_minor = fields[2].split(":")
    if len(major_minor) != 2:
        raise ValueError(f"mountinfo major:minor field is malformed: {len(major_minor)} parts")

    return MountEntry(
        mount_id=int(fields[0]),
        parent_id=int(fields[1]),
        major=int(major_minor[0]),
        minor=int(major_minor[1]),
        root=unescape_mountinfo_field(fields[3]),
        mount_point=unescape_mountinfo_field(fields[4]),
        mount_options=fields[5],
        filesystem_type=fields[separator + 1],
        mount_source=unescape_mountinfo_field(fields[separator + 2]),
        super_options=fields[separator + 3],
        order=order,
    )


def _is_path_prefix(prefix: str, path: str) -> bool:
    """Segment境界で判定する。

    文字列prefix比較だと `/mnt` が `/mnthome` を覆ってしまう。
    `PathScope` と同じ理由で境界を見る。
    """
    if prefix == "/":
        return path.startswith("/")
    return path == prefix or path.startswith(prefix + "/")


def find_mount_for_path(entries: tuple[MountEntry, ...], path: str) -> MountEntry | None:
    """Pathを含む最長Prefix一致のMountを返す。

    同じMount Pointに複数のMountが積まれている場合、**後から積んだ方**を返す。
    実際に見えるのはそちらであり、下に隠れたMountのFilesystem Typeで
    判定すると実体と食い違う。
    """
    best: MountEntry | None = None
    for entry in entries:
        if not _is_path_prefix(entry.mount_point, path):
            continue
        if best is None:
            best = entry
            continue
        if len(entry.mount_point) > len(best.mount_point):
            best = entry
        elif len(entry.mount_point) == len(best.mount_point) and entry.order > best.order:
            # 同一Mount Pointへの積み重ね。後勝ち。
            best = entry
    return best
