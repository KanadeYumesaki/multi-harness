"""`/proc/self/mountinfo` 解析の試験（§1.16.2）。

実環境に無いFilesystemを扱うため、全てFixture文字列で行う。
"""

from __future__ import annotations

import pytest

from harness.domain.mountinfo import (
    find_mount_for_path,
    parse_mountinfo,
    unescape_mountinfo_field,
)

pytestmark = pytest.mark.unit

# 実測したWSL2の抜粋。Optional Field の個数が行ごとに違う点が要点。
SAMPLE = """\
23 28 0:22 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
24 28 0:23 / /sys rw,nosuid,nodev,noexec,relatime - sysfs sysfs rw
28 1 8:32 / / rw,relatime - ext4 /dev/sdc rw,discard,errors=remount-ro
55 28 0:52 / /mnt/wsl rw,relatime - tmpfs none rw
141 28 0:60 / /mnt/c rw,noatime shared:2 master:1 - 9p C:\\134 rw,dirsync,aname=drvfs
160 28 0:70 / /srv/share rw,relatime - nfs4 10.0.0.5:/export rw
"""


def test_parses_every_line() -> None:
    entries = parse_mountinfo(SAMPLE)
    assert len(entries) == 6
    assert [entry.filesystem_type for entry in entries] == [
        "proc",
        "sysfs",
        "ext4",
        "tmpfs",
        "9p",
        "nfs4",
    ]


def test_optional_fields_do_not_shift_the_filesystem_type() -> None:
    """Optional Field は0個以上。固定indexで読むと種別を取り違える。

    `/mnt/c` の行は `shared:2 master:1` の2個を持つ。この行だけ
    Filesystem Type の位置が2つ後ろへずれる。
    """
    entries = parse_mountinfo(SAMPLE)
    mnt_c = next(entry for entry in entries if entry.mount_point == "/mnt/c")
    assert mnt_c.filesystem_type == "9p"
    assert mnt_c.mount_options == "rw,noatime"


def test_major_minor_is_split() -> None:
    entries = parse_mountinfo(SAMPLE)
    root = next(entry for entry in entries if entry.mount_point == "/")
    assert root.device_id == (8, 32)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/plain", "/plain"),
        ("/with\\040space", "/with space"),
        ("/tab\\011here", "/tab\there"),
        ("/back\\134slash", "/back\\slash"),
        ("/multi\\040a\\040b", "/multi a b"),
    ],
)
def test_octal_escapes_are_restored(raw: str, expected: str) -> None:
    """Mount Pointに空白を含むPathは実在する。

    Escapeを戻さないと、そのMountだけ最長Prefix一致から漏れる。
    """
    assert unescape_mountinfo_field(raw) == expected


@pytest.mark.parametrize(
    "line",
    [
        "23 28 0:22 / /proc rw - proc",
        "23 28 0:22 / /proc rw,relatime proc proc rw",
        "23 28 022 / /proc rw - proc proc rw",
    ],
    ids=["truncated", "no_separator", "malformed_major_minor"],
)
def test_malformed_line_raises_instead_of_being_skipped(line: str) -> None:
    """読み飛ばしを許すと判定が緩む方向へ倒れる。

    拒否すべきMountの行が壊れていた場合、黙って飛ばすと「該当Mountなし」
    となり、そのWorkspaceが素性不明のまま通る。
    """
    with pytest.raises(ValueError):
        parse_mountinfo(line)


# ---------------------------------------------------------------------------
# 最長Prefix一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_type", "expected_mount_point"),
    [
        ("/home/user/ws", "ext4", "/"),
        ("/mnt/c", "9p", "/mnt/c"),
        ("/mnt/c/Users/x/ws", "9p", "/mnt/c"),
        ("/mnt/wsl", "tmpfs", "/mnt/wsl"),
        ("/mnt/other", "ext4", "/"),
        ("/srv/share/data", "nfs4", "/srv/share"),
        ("/srv/other", "ext4", "/"),
        ("/", "ext4", "/"),
    ],
)
def test_longest_prefix_wins(path: str, expected_type: str, expected_mount_point: str) -> None:
    entries = parse_mountinfo(SAMPLE)
    entry = find_mount_for_path(entries, path)
    assert entry is not None
    assert entry.filesystem_type == expected_type
    assert entry.mount_point == expected_mount_point


def test_prefix_match_respects_segment_boundaries() -> None:
    """文字列prefix比較だと `/mnt` が `/mnthome` を覆ってしまう。"""
    entries = parse_mountinfo(SAMPLE + "200 28 0:80 / /mnt rw - ext4 /dev/sdd rw\n")
    assert find_mount_for_path(entries, "/mnthome/ws").mount_point == "/"  # type: ignore[union-attr]
    assert find_mount_for_path(entries, "/mnt/x").mount_point == "/mnt"  # type: ignore[union-attr]


def test_later_mount_on_the_same_point_shadows_the_earlier_one() -> None:
    """同一Mount Pointへの積み重ねは後勝ち。

    実際に見えるのは後から積んだ方であり、下に隠れたMountの種別で
    判定すると実体と食い違う。
    """
    stacked = SAMPLE + "300 28 0:90 / /srv/share rw - cifs //server/share rw\n"
    entry = find_mount_for_path(parse_mountinfo(stacked), "/srv/share/data")
    assert entry is not None
    assert entry.filesystem_type == "cifs"


def test_no_matching_mount_returns_none() -> None:
    entries = parse_mountinfo("23 28 0:22 / /proc rw - proc proc rw\n")
    assert find_mount_for_path(entries, "/home/user") is None
