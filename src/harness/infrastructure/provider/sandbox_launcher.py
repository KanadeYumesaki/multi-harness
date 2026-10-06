"""起動する CLI へ、Harness 側から境界を掛けてから exec する。

## なぜ CLI 任せにしないか

Codex の `unified_exec` は `--disable` でも `-c` でも `false` にできない（実測）。
モデル由来のコマンド実行能力は残る。CLI の設定だけに頼ると、依頼文や選択 File に
混ざった悪意ある指示から、承認した範囲の外を触られる余地が残る。

## 2 つの境界を掛ける

| 境界 | 何を止めるか | 効き方 |
|---|---|---|
| Landlock LSM | Filesystem の読み書き・truncate・作成・削除・rename | `execve` を越えて継承される |
| PID namespace | 子孫 Process の逃亡 | PID 1 が死ぬと Kernel が残りを SIGKILL する |

`setsid` しても PID namespace からは出られない。Process Group を見張るだけでは
逃げられることを実測したので、**Group ではなく namespace で囲う。**

## ABI を交渉する

Landlock の権利は ABI で増える。ABI 1 は truncate を **扱わない**（`WRITE_FILE` と
`TRUNCATE` は別の権利で、`TRUNCATE` は ABI 3 以降）。扱えない bit を
`handled_access_fs` へ足すと `landlock_create_ruleset` が `EINVAL` で落ちるので、
**動いている ABI が扱える bit だけ**を要求する。

そのうえで「本当に止まったか」は `--self-test` が実測する。**ABI 番号を保護の証拠に
しない。** 足りない環境では呼出側が起動そのものを拒否する。

## なぜ別の実行体にするか

`preexec_fn` は複数 Thread を持つ親からの fork になるので使わない。この Module は
独立した Process として起動され、境界を掛けてから CLI へ `execv` する。

## 効かないときは起動しない

境界を掛けられない、規則を追加できない、namespace を作れない、`execv` できない——
どれでも **exec せずに終了する。** 「掛けられなかったが動かした」を作らない。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import json
import os
import signal
import stat
import sys
from collections.abc import Callable
from typing import Any, Final

__all__ = [
    "EXIT_BOUNDARY_UNAVAILABLE",
    "EXIT_EXEC_FAILED",
    "EXIT_SELF_TEST_FAILED",
    "EXIT_USAGE",
    "NAMESPACE_REPORT_FD",
    "POLICY_CONTRACT",
    "PROTECTED_OPERATIONS",
    "SELF_TEST_FLAG",
    "UNENFORCEABLE_OPERATIONS",
    "SandboxUnavailable",
    "apply_policy",
    "handled_access_for",
    "landlock_abi_version",
    "main",
    "self_test",
]

POLICY_CONTRACT: Final[str] = "harness-landlock-policy/2"

#: 境界の実効性をその場で測るための印。
SELF_TEST_FLAG: Final[str] = "--self-test"

#: 終了 Code。呼出側はこれで「起動しなかった」を区別する。
EXIT_USAGE: Final[int] = 2
EXIT_BOUNDARY_UNAVAILABLE: Final[int] = 3
EXIT_EXEC_FAILED: Final[int] = 4
EXIT_SELF_TEST_FAILED: Final[int] = 5

#: 承認範囲の外で **必ず拒否できていなければならない** 操作。
#:
#: `--self-test` は合成 canary に対してこの全部を試す。1 つでも通れば境界は不成立
#: であり、呼出側は起動を拒否する。
PROTECTED_OPERATIONS: Final[tuple[str, ...]] = (
    "read",
    "write",
    "truncate",
    "open_trunc",
    "create",
    "rename",
    "unlink",
    "chmod",
    "utime",
)

#: metadataの変更はLandlockではなくreadonly mountで拒否する。
UNENFORCEABLE_OPERATIONS: Final[tuple[str, ...]] = ()

_NR_CREATE_RULESET: Final[int] = 444
_NR_ADD_RULE: Final[int] = 445
_NR_RESTRICT_SELF: Final[int] = 446
_CREATE_RULESET_VERSION: Final[int] = 1 << 0
_RULE_PATH_BENEATH: Final[int] = 1
_PR_SET_NO_NEW_PRIVS: Final[int] = 38

_CLONE_NEWUSER: Final[int] = 0x10000000
_CLONE_NEWPID: Final[int] = 0x20000000
_CLONE_NEWNS: Final[int] = 0x00020000
_PR_SET_PDEATHSIG: Final[int] = 1

#: namespace 識別子を呼出側へ返す fd。stdout / stderr を汚さない。
NAMESPACE_REPORT_FD: Final[int] = 3

#: ABI 1 の Filesystem 権限。
_EXECUTE: Final[int] = 1 << 0
_WRITE_FILE: Final[int] = 1 << 1
_READ_FILE: Final[int] = 1 << 2
_READ_DIR: Final[int] = 1 << 3
_REMOVE_DIR: Final[int] = 1 << 4
_REMOVE_FILE: Final[int] = 1 << 5
_MAKE_CHAR: Final[int] = 1 << 6
_MAKE_DIR: Final[int] = 1 << 7
_MAKE_REG: Final[int] = 1 << 8
_MAKE_SOCK: Final[int] = 1 << 9
_MAKE_FIFO: Final[int] = 1 << 10
_MAKE_BLOCK: Final[int] = 1 << 11
_MAKE_SYM: Final[int] = 1 << 12
#: ABI 2 以降。
_REFER: Final[int] = 1 << 13
#: ABI 3 以降。**これが無いと truncate を止められない。**
_TRUNCATE: Final[int] = 1 << 14

#: ABI ごとに扱える bit。動いている ABI に合わせて要求を絞る。
_HANDLED_BY_ABI: Final[dict[int, int]] = {
    1: (1 << 13) - 1,
    2: (1 << 14) - 1,
    3: (1 << 15) - 1,
}

#: Directory でない Path へ渡せる権限。Directory 専用 bit を混ぜると `EINVAL`。
_FILE_ONLY: Final[int] = _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE

_READ: Final[int] = _READ_FILE | _READ_DIR
_READ_EXECUTE: Final[int] = _READ | _EXECUTE
_READ_WRITE: Final[int] = (
    _READ
    | _WRITE_FILE
    | _TRUNCATE
    | _REFER
    | _REMOVE_DIR
    | _REMOVE_FILE
    | _MAKE_DIR
    | _MAKE_REG
    | _MAKE_SOCK
    | _MAKE_FIFO
    | _MAKE_SYM
    | _MAKE_CHAR
    | _MAKE_BLOCK
)

_RIGHTS_BY_KEY: Final[dict[str, int]] = {
    "read_execute": _READ_EXECUTE,
    "read_only": _READ,
    "read_write": _READ_WRITE,
}


class SandboxUnavailable(RuntimeError):
    """境界を掛けられなかった。**掛けずに起動してはならない。**"""


class _RulesetAttr(ctypes.Structure):
    _fields_ = (("handled_access_fs", ctypes.c_uint64),)


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


def _libc() -> ctypes.CDLL:
    library = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    library.syscall.restype = ctypes.c_long
    return library


def landlock_abi_version() -> int:
    """使える Landlock ABI。0 以下なら使えない。"""
    library = _libc()
    return int(
        library.syscall(
            ctypes.c_long(_NR_CREATE_RULESET),
            ctypes.c_void_p(None),
            ctypes.c_size_t(0),
            ctypes.c_uint32(_CREATE_RULESET_VERSION),
        )
    )


def handled_access_for(abi: int) -> int:
    """その ABI が扱える権限 bit。**扱えない bit を足すと ruleset 作成が失敗する。**"""
    if abi < 1:
        return 0
    return _HANDLED_BY_ABI.get(abi, _HANDLED_BY_ABI[max(_HANDLED_BY_ABI)])


def apply_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Policy を Landlock 規則として自分へ掛ける。**失敗したら例外で止める。**

    掛けられた内容（ABI と扱った権限）を返す。呼出側はこれを記録に使う。
    """
    if policy.get("contract") != POLICY_CONTRACT:
        raise SandboxUnavailable("unsupported sandbox policy contract")
    library = _libc()
    abi = landlock_abi_version()
    if abi < 1:
        raise SandboxUnavailable("landlock is not available on this kernel")
    handled = handled_access_for(abi)

    attribute = _RulesetAttr(handled)
    ruleset = library.syscall(
        ctypes.c_long(_NR_CREATE_RULESET),
        ctypes.byref(attribute),
        ctypes.c_size_t(ctypes.sizeof(attribute)),
        ctypes.c_uint32(0),
    )
    if ruleset < 0:
        raise SandboxUnavailable(f"landlock_create_ruleset failed: {ctypes.get_errno()}")
    try:
        for key, rights in _RIGHTS_BY_KEY.items():
            for entry in policy.get(key, []):
                _add_path(library, int(ruleset), str(entry), rights & handled, policy)
        if library.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise SandboxUnavailable(f"prctl(NO_NEW_PRIVS) failed: {ctypes.get_errno()}")
        if (
            library.syscall(
                ctypes.c_long(_NR_RESTRICT_SELF), ctypes.c_int(int(ruleset)), ctypes.c_uint32(0)
            )
            < 0
        ):
            raise SandboxUnavailable(f"landlock_restrict_self failed: {ctypes.get_errno()}")
    finally:
        os.close(int(ruleset))
    return {
        "landlock_abi": abi,
        "handled_access_fs": handled,
        "truncate_handled": bool(handled & _TRUNCATE),
    }


def _add_path(
    library: ctypes.CDLL, ruleset: int, entry: str, rights: int, policy: dict[str, Any]
) -> None:
    """1 つの Path へ規則を足す。

    存在しない Path は `optional_paths` にある場合だけ飛ばす。そうでなければ止める。
    **書き忘れた許可を静かに無視しない。**
    """
    try:
        descriptor = os.open(entry, os.O_PATH | os.O_CLOEXEC)
    except OSError as error:
        if entry in policy.get("optional_paths", []):
            return
        raise SandboxUnavailable(f"sandbox path is unavailable: {entry}") from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            rights &= _FILE_ONLY
        rule = _PathBeneathAttr(rights, descriptor)
        code = library.syscall(
            ctypes.c_long(_NR_ADD_RULE),
            ctypes.c_int(ruleset),
            ctypes.c_int(_RULE_PATH_BENEATH),
            ctypes.byref(rule),
            ctypes.c_uint32(0),
        )
    finally:
        os.close(descriptor)
    if code < 0:
        raise SandboxUnavailable(f"landlock_add_rule failed for {entry}: {ctypes.get_errno()}")


def _write_id_maps(pid: int, uid: int, gid: int) -> None:
    """**親が子の id map を書く。**

    自分で自分の map を書くと、この環境では `EPERM` になる（実測）。map を書く側は
    元の資格情報を持っている必要がある。map が無いままだと Process は `nobody` に
    なり、CLI は自分の資格情報 File すら読めない。
    """
    for name, payload in (
        ("setgroups", "deny"),
        ("uid_map", f"0 {uid} 1"),
        ("gid_map", f"0 {gid} 1"),
    ):
        try:
            descriptor = os.open(f"/proc/{pid}/{name}", os.O_WRONLY)
        except OSError as error:
            raise SandboxUnavailable(f"cannot open {name}: errno={error.errno}") from error
        try:
            os.write(descriptor, payload.encode("ascii"))
        except OSError as error:
            raise SandboxUnavailable(f"cannot write {name}: errno={error.errno}") from error
        finally:
            os.close(descriptor)


def _die_with_parent() -> None:
    """親が死んだら自分も死ぬ。**監督者が消えても取り残されない。**"""
    _libc().prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


def main(argv: list[str]) -> int:
    """`<policy json> -- <argv...>` を受けて、境界を掛けてから exec する。"""
    if "--" not in argv:
        sys.stderr.write("sandbox_launcher: missing '--' separator\n")
        return EXIT_USAGE
    separator = argv.index("--")
    if separator != 1:
        sys.stderr.write("sandbox_launcher: expected exactly one policy argument\n")
        return EXIT_USAGE
    target = argv[separator + 1 :]
    if not target:
        sys.stderr.write("sandbox_launcher: missing target argv\n")
        return EXIT_USAGE
    try:
        policy = json.loads(argv[0])
    except ValueError:
        sys.stderr.write("sandbox_launcher: policy is not valid json\n")
        return EXIT_USAGE
    if not isinstance(policy, dict):
        sys.stderr.write("sandbox_launcher: policy must be a json object\n")
        return EXIT_USAGE

    return _launch(policy, target)


def _launch(policy: dict[str, Any], target: list[str]) -> int:
    """namespace を作り、境界を掛けてから CLI を起動する。

    3 段になる。**外側が map を書けないと、内側は `nobody` のままになる。**

        outer（この Process）… 子の id map を書き、監督する
          └ inner … user/pid namespace を作る。ここはまだ旧 PID namespace
              └ contained … 新 PID namespace の PID 1。Landlock を掛けて exec

    `contained` が死ぬと Kernel が namespace の残りを SIGKILL する。`setsid` した
    子孫も道連れになる。
    """
    ready_read, ready_write = os.pipe()
    go_read, go_write = os.pipe()
    uid, gid = os.getuid(), os.getgid()
    inner = os.fork()
    if inner == 0:
        os.close(ready_read)
        os.close(go_write)
        os._exit(_namespace_child(policy, target, ready_write, go_read))
    os.close(ready_write)
    os.close(go_read)
    try:
        signal_byte = os.read(ready_read, 1)
    except OSError:
        signal_byte = b""
    finally:
        os.close(ready_read)
    if signal_byte != b"1":
        os.close(go_write)
        _reap(inner)
        sys.stderr.write("sandbox_launcher: could not create the pid namespace\n")
        return EXIT_BOUNDARY_UNAVAILABLE
    try:
        _write_id_maps(inner, uid, gid)
    except SandboxUnavailable as error:
        os.close(go_write)
        _reap(inner)
        sys.stderr.write(f"sandbox_launcher: {error}\n")
        return EXIT_BOUNDARY_UNAVAILABLE
    os.write(go_write, b"1")
    os.close(go_write)
    return _supervise(inner)


def _namespace_child(
    policy: dict[str, Any], target: list[str], ready_write: int, go_read: int
) -> int:
    """user/pid namespace を作り、PID 1 になる子を起こす。"""
    _die_with_parent()
    library = _libc()
    created = library.unshare(_CLONE_NEWUSER | _CLONE_NEWPID | _CLONE_NEWNS) == 0
    try:
        os.write(ready_write, b"1" if created else b"0")
    except OSError:
        return EXIT_BOUNDARY_UNAVAILABLE
    finally:
        os.close(ready_write)
    if not created:
        return EXIT_BOUNDARY_UNAVAILABLE
    try:
        os.read(go_read, 1)
    except OSError:
        return EXIT_BOUNDARY_UNAVAILABLE
    finally:
        os.close(go_read)
    _report_namespace()
    contained = os.fork()
    if contained == 0:
        _die_with_parent()
        os._exit(_run_contained(policy, target))
    return _supervise(contained)


def _report_namespace() -> None:
    """子孫が入る PID namespace の識別子を、専用 fd が渡されていれば書く。

    呼出側は fd が無い場合、`/proc/<pid>/task/<tid>/children` を辿って同じ識別子を
    得る。**どちらの経路でも得られなければ、呼出側は残存を UNVERIFIABLE と扱う。**
    """
    try:
        identifier = os.readlink("/proc/self/ns/pid_for_children")
    except OSError:  # pragma: no cover - procfs が無い環境は対象外
        return
    try:
        os.write(NAMESPACE_REPORT_FD, (identifier + "\n").encode("utf-8"))
    except OSError:
        # 呼出側が fd を用意していない。procfs 経由で辿ってもらう。
        return


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass


class _MountAttr(ctypes.Structure):
    _fields_ = (
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    )


def _readonly_filesystem(policy: dict[str, Any]) -> None:
    """子のmount namespaceだけを読取り専用化し、宣言済み書込先だけを戻す。"""
    library = _libc()
    if library.mount(None, b"/", None, 16384 | (1 << 18), None) != 0:
        raise SandboxUnavailable("could not make mounts private")
    writable: list[bytes] = []
    for entry in policy.get("read_write", []):
        path = os.fsencode(entry)
        if not os.path.exists(entry):
            if entry in policy.get("optional_paths", []):
                continue
            raise SandboxUnavailable("writable mount path missing")
        # subtreeごとにmountを分け、rootを戻さずに例外だけを設定する。
        if library.mount(path, path, None, 4096 | 16384, None) != 0:
            raise SandboxUnavailable("could not bind writable mount")
        writable.append(path)
    readonly = _MountAttr(1, 0, 0, 0)
    if library.syscall(442, -100, b"/", 0x8000, ctypes.byref(readonly), ctypes.sizeof(readonly)):
        raise SandboxUnavailable("recursive read-only mount unavailable")
    readwrite = _MountAttr(0, 1, 0, 0)
    for path in writable:
        if library.syscall(
            442, -100, path, 0x8000, ctypes.byref(readwrite), ctypes.sizeof(readwrite)
        ):
            raise SandboxUnavailable("writable mount exception unavailable")
    # 同じuser namespaceの権限でreadonlyを解除できないよう全capabilityを落とす。
    for capability in range(64):
        library.prctl(24, capability, 0, 0, 0)
    header = (ctypes.c_uint32 * 2)(0x20080522, 0)
    data = (ctypes.c_uint32 * 6)()
    if library.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise SandboxUnavailable("could not drop mount capabilities")


def _run_contained(policy: dict[str, Any], target: list[str]) -> int:
    """PID namespace の PID 1 として、Landlock を掛けてから exec する。"""
    try:
        _readonly_filesystem(policy)
        applied = apply_policy(policy)
        applied["readonly_mounts"] = True
    except SandboxUnavailable as error:
        sys.stderr.write(f"sandbox_launcher: {error}\n")
        return EXIT_BOUNDARY_UNAVAILABLE
    if target[0] == SELF_TEST_FLAG:
        try:
            request = json.loads(target[1]) if len(target) > 1 else {}
        except ValueError:
            request = {}
        report = self_test(request if isinstance(request, dict) else {}, applied)
        sys.stdout.write(json.dumps(report, sort_keys=True))
        sys.stdout.flush()
        return 0 if report["all_denied"] else EXIT_SELF_TEST_FAILED
    try:
        # 自分を CLI へ置き換える。境界は exec を越えて残る。
        os.execv(target[0], target)  # noqa: S606 - fixed argv list, no shell
    except OSError as error:
        sys.stderr.write(f"sandbox_launcher: exec failed: {error.errno}\n")
    return EXIT_EXEC_FAILED


def _supervise(child: int) -> int:
    """namespace の PID 1 を見張る。**自分が終わるときは道連れにする。**"""

    def relay(number: int, _frame: object) -> None:
        try:
            os.kill(child, number)
        except OSError:
            pass

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, relay)
    status = 0
    try:
        _, status = os.waitpid(child, 0)
    except OSError:
        return EXIT_EXEC_FAILED
    finally:
        # PID 1 を確実に落とす。落ちれば Kernel が namespace の残りを片付ける。
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass
    return os.waitstatus_to_exitcode(status)


def self_test(request: dict[str, Any], applied: dict[str, Any] | None = None) -> dict[str, Any]:
    """Policy 適用後に、承認範囲の外を触れないことを操作ごとに確かめる。

    ## canary と実 Path を分ける

    truncate や unlink は **壊す** 操作なので、合成 canary にだけ試す。実際の
    Workspace・DB・CAS へは読取りしか試さない。**実データを canary にしない。**

    ## exec を越えた継承まで見る

    自分自身と、`execve` を経た子 Process の両方で試す。fork だけでは
    「CLI が内部で何を起動しても出られない」の証拠にならない。
    """
    canaries = [str(item) for item in request.get("canaries", [])]
    read_only = [str(item) for item in request.get("read_only_probes", [])]
    destructive = {entry: _probe_operations(entry) for entry in canaries}
    observed = {
        entry: {"read": _attempt(functools.partial(_read_one, entry))} for entry in read_only
    }
    child = _probe_via_exec(canaries[0]) if canaries else {}

    def denied(report: dict[str, str], names: tuple[str, ...]) -> bool:
        return all(
            report.get(name, "MISSING").startswith(("DENIED", "NOT_APPLICABLE")) for name in names
        )

    canary_ok = bool(canaries) and all(
        denied(report, PROTECTED_OPERATIONS) for report in destructive.values()
    )
    read_ok = all(
        value.startswith(("DENIED", "NOT_APPLICABLE"))
        for report in observed.values()
        for value in report.values()
    )
    child_ok = bool(child) and denied(child, PROTECTED_OPERATIONS)
    return {
        "landlock_abi": (applied or {}).get("landlock_abi", landlock_abi_version()),
        "handled_access_fs": (applied or {}).get("handled_access_fs"),
        "truncate_handled": (applied or {}).get("truncate_handled"),
        "readonly_mounts": (applied or {}).get("readonly_mounts", False),
        "required_operations": list(PROTECTED_OPERATIONS),
        "unenforceable_operations": list(UNENFORCEABLE_OPERATIONS),
        "canaries": destructive,
        "read_only_probes": observed,
        "descendant_after_exec": child,
        "pid_namespace": _pid_namespace_id(),
        "all_denied": canary_ok and read_ok and child_ok,
    }


def _probe_operations(entry: str) -> dict[str, str]:
    """1 つの合成 canary へ、保護すべき操作をひととおり試す。

    `ENOENT` や `EISDIR` を拒否の証拠にしない。**存在する物へ試して `EACCES` を見る。**
    Directory には当てはまらない操作を `NOT_APPLICABLE` として分ける。
    `chmod` は元の mode をそのまま書き戻すので、通っても壊さない。
    """
    is_directory = os.path.isdir(entry)
    sibling = entry + ".harness-probe-create"
    inside = os.path.join(entry, ".harness-probe-create")
    try:
        mode = stat.S_IMODE(os.stat(entry).st_mode)
    except OSError:
        mode = 0o600
    results = {
        "read": _attempt(lambda: _read_one(entry)),
        "create": _attempt(lambda: _create_one(inside if is_directory else sibling)),
        "rename": _attempt(lambda: os.rename(entry, entry + ".harness-probe-rename")),
        "unlink": _attempt(lambda: os.rmdir(entry) if is_directory else os.unlink(entry)),
        "chmod": _attempt(lambda: os.chmod(entry, mode)),
        "utime": _attempt(lambda: os.utime(entry, None)),
    }
    if is_directory:
        for name in ("write", "truncate", "open_trunc"):
            results[name] = "NOT_APPLICABLE(directory)"
    else:
        results["write"] = _attempt(lambda: _write_one(entry))
        results["truncate"] = _attempt(lambda: os.truncate(entry, 0))
        results["open_trunc"] = _attempt(lambda: _open_trunc(entry))
    return results


def _attempt(action: Callable[[], object]) -> str:
    """1 操作を試して、拒否されたかどうかを分類する。

    `EACCES` / `EPERM` / readonly mountの`EROFS`を拒否として数える。
    `ENOENT`や`EISDIR`は境界の拒否ではないため、別の名前で返す。
    """
    try:
        action()
    except PermissionError as error:
        return f"DENIED(errno={error.errno})"
    except OSError as error:
        if error.errno == 30:  # EROFS: 独立したreadonly mount境界による拒否。
            return f"DENIED(errno={error.errno})"
        return f"INCONCLUSIVE(errno={error.errno})"
    return "ALLOWED"


def _read_one(entry: str) -> None:
    if os.path.isdir(entry):
        os.listdir(entry)
        return
    with open(entry, "rb") as handle:
        handle.read(1)


def _write_one(entry: str) -> None:
    with open(entry, "r+b") as handle:
        handle.write(b"x")


def _open_trunc(entry: str) -> None:
    os.close(os.open(entry, os.O_WRONLY | os.O_TRUNC))


def _create_one(entry: str) -> None:
    os.close(os.open(entry, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    os.unlink(entry)


#: `execve` 後の子が実行する probe。**この Module の File を読ませない。**
#:
#: Launcher 自身は Repository の中にあり、境界の内側からは読めない（読めてしまう方が
#: おかしい）。だから子には Path ではなく `-c` で本文を渡す。
_CHILD_PROBE_SOURCE = """
import json, os, stat, sys
entry = sys.argv[1]
is_dir = os.path.isdir(entry)
sibling = entry + '.harness-probe-create'
inside = os.path.join(entry, '.harness-probe-create')
try:
    mode = stat.S_IMODE(os.stat(entry).st_mode)
except OSError:
    mode = 0o600

def attempt(action):
    try:
        action()
    except PermissionError as error:
        return 'DENIED(errno=%d)' % error.errno
    except OSError as error:
        return ('DENIED(errno=%d)' if error.errno == 30 else 'INCONCLUSIVE(errno=%d)') % error.errno
    return 'ALLOWED'

def read_one():
    if is_dir:
        os.listdir(entry)
    else:
        open(entry, 'rb').read(1)

def create_one():
    target = inside if is_dir else sibling
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    os.unlink(target)

out = {
    'read': attempt(read_one),
    'create': attempt(create_one),
    'rename': attempt(lambda: os.rename(entry, entry + '.harness-probe-rename')),
    'unlink': attempt(lambda: os.rmdir(entry) if is_dir else os.unlink(entry)),
    'chmod': attempt(lambda: os.chmod(entry, mode)),
    'utime': attempt(lambda: os.utime(entry, None)),
}
if is_dir:
    out['write'] = 'NOT_APPLICABLE(directory)'
    out['truncate'] = 'NOT_APPLICABLE(directory)'
    out['open_trunc'] = 'NOT_APPLICABLE(directory)'
else:
    out['write'] = attempt(lambda: open(entry, 'r+b').write(b'x'))
    out['truncate'] = attempt(lambda: os.truncate(entry, 0))
    out['open_trunc'] = attempt(lambda: os.close(os.open(entry, os.O_WRONLY | os.O_TRUNC)))
sys.stdout.write(json.dumps(out, sort_keys=True))
"""


def _probe_via_exec(entry: str) -> dict[str, str]:
    """`execve` を経た子から同じ操作を試す。**fork だけでは継承の証拠にならない。**"""
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - 子側は別 Process
        # 子には報告先が無い。**握り潰さず、終了 Code で区別して返す。**
        code = 70
        try:
            os.close(read_end)
            os.dup2(write_end, 1)
            os.close(write_end)
            os.execv(  # noqa: S606 - fixed argv list, no shell
                sys.executable,
                [sys.executable, "-c", _CHILD_PROBE_SOURCE, entry],
            )
        except BaseException:
            code = 71
        os._exit(code)
    os.close(write_end)
    try:
        with os.fdopen(read_end, "rb") as handle:
            payload = handle.read().decode("utf-8", "replace")
    finally:
        os.waitpid(pid, 0)
    try:
        parsed = json.loads(payload)
    except ValueError:
        return {"probe": f"INCONCLUSIVE(unreadable:{payload[:40]})"}
    return parsed if isinstance(parsed, dict) else {"probe": "INCONCLUSIVE(shape)"}


def _pid_namespace_id() -> str | None:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:  # pragma: no cover - procfs が無い環境は対象外
        return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
