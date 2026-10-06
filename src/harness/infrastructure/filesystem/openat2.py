"""`openat2(2)` の薄いラッパ（§1.16.2、ADR-003）。

ADR-003「`openat2`は`ctypes`によるsyscall直呼びをスパイクで検証する。
利用不能時は各Path Componentを`openat`、`O_NOFOLLOW`、`fstat`で検証する
安全なDirectory WalkへFail-Closedで切り替える」。

`openat2` を使う理由は、Path解決の**全体**をKernel側で制約できる点にある。
ユーザ空間でComponentごとに検査する方式は、検査と`open`の間に対象が
差し替わる余地（TOCTOU）が残る。`RESOLVE_BENEATH`等はKernelが解決中に
判定するため、この隙間が無い。

利用可否は実行時に判定する。`ENOSYS`（Kernelが未対応）と`EPERM`（seccomp等で
遮断）を利用不能として扱い、それ以外のerrnoは通常の失敗として返す。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
from typing import Final

__all__ = [
    "RESOLVE_BENEATH",
    "RESOLVE_NO_MAGICLINKS",
    "RESOLVE_NO_SYMLINKS",
    "RESOLVE_NO_XDEV",
    "Openat2Unavailable",
    "is_available",
    "openat2",
]

# x86_64 / aarch64 いずれも 437。他Architectureは未確認のため利用不能扱いにする。
_SYSCALL_OPENAT2: Final[dict[str, int]] = {"x86_64": 437, "aarch64": 437}

RESOLVE_NO_XDEV: Final[int] = 0x01
RESOLVE_NO_MAGICLINKS: Final[int] = 0x02
RESOLVE_NO_SYMLINKS: Final[int] = 0x04
RESOLVE_BENEATH: Final[int] = 0x08


class Openat2Unavailable(RuntimeError):
    """Kernelまたは実行環境が`openat2`を提供しない。"""


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


_libc: ctypes.CDLL | None = None
_available: bool | None = None


def _load_libc() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        name = ctypes.util.find_library("c") or "libc.so.6"
        _libc = ctypes.CDLL(name, use_errno=True)
    return _libc


def _syscall_number() -> int:
    number = _SYSCALL_OPENAT2.get(platform.machine())
    if number is None:
        raise Openat2Unavailable(f"openat2 syscall number is unknown for {platform.machine()!r}")
    return number


def openat2(dir_fd: int, path: str, *, flags: int, resolve: int) -> int:
    """`openat2` を呼び、File Descriptorを返す。

    `path` は `dir_fd` からの相対Pathでなければならない。
    """
    libc = _load_libc()
    how = _OpenHow(flags=flags, mode=0, resolve=resolve)
    ctypes.set_errno(0)
    result = libc.syscall(
        ctypes.c_long(_syscall_number()),
        ctypes.c_int(dir_fd),
        ctypes.c_char_p(path.encode("utf-8")),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if result < 0:
        code = ctypes.get_errno()
        if code in (errno.ENOSYS, errno.EPERM):
            raise Openat2Unavailable(f"openat2 unavailable: {os.strerror(code)}")
        raise OSError(code, os.strerror(code), path)
    return int(result)


def is_available() -> bool:
    """実際に1回呼んで利用可否を判定する。結果はProcess内で記憶する。"""
    global _available
    if _available is not None:
        return _available
    try:
        dir_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    except OSError:  # pragma: no cover - "/" が開けない環境は想定外
        _available = False
        return _available
    try:
        fd = openat2(dir_fd, ".", flags=os.O_RDONLY | os.O_DIRECTORY, resolve=RESOLVE_BENEATH)
        os.close(fd)
        _available = True
    except Openat2Unavailable:
        _available = False
    except OSError:
        # errnoがENOSYS/EPERM以外なら syscall 自体は通っている
        _available = True
    finally:
        os.close(dir_fd)
    return _available
