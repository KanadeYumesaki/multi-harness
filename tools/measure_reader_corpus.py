"""Run a synthetic filesystem corpus against the real Linux reader.

The fallback and delayed-return cases explicitly inject conditions; no Provider
is invoked and no user workspace is opened. Raw observations are retained.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

from harness.domain.errors import HarnessError
from harness.domain.hashing import hash_bytes
from harness.domain.input_read import CapabilityScope, ReadDenial
from harness.infrastructure.filesystem import safe_reader as module
from harness.infrastructure.filesystem.workspace_boundary import FilesystemPolicy


def measure(repo: Path, base: Path, mode: str) -> dict:
    repo, base = repo.resolve(strict=True), base.resolve()
    if mode not in ("auto", "fallback"):
        raise ValueError("UNKNOWN_READER_MODE")
    if base == repo or repo in base.parents or str(base).startswith("/mnt/"):
        raise ValueError("UNSAFE_CORPUS_DESTINATION")
    if base.parent.stat().st_dev != repo.stat().st_dev:
        raise ValueError("CORPUS_FILESYSTEM_MISMATCH")
    base.mkdir(exist_ok=False)
    workspace = base / "w"
    docs = workspace / "d"
    docs.mkdir(parents=True)
    (docs / "good").mkdir()
    payload = b"synthetic reader corpus\n"
    (docs / "good/a").write_bytes(payload)
    (docs / "good/z").write_bytes(b"")
    outside = base / "outside"
    outside.write_bytes(b"synthetic outside input\n")
    (docs / "symlink").symlink_to(outside)
    (docs / "inside-link").symlink_to(docs / "good/a")
    (docs / "linkdir").symlink_to(docs / "good", target_is_directory=True)
    (docs / "magic").symlink_to("/proc/self/fd/0")
    os.link(outside, docs / "hardlink")
    os.mkfifo(docs / "fifo")
    for name, size in (("exact", module._MAX_BYTES), ("over", module._MAX_BYTES + 1)):
        with (docs / name).open("xb") as file:
            file.truncate(size)
    (docs / "timed").write_bytes(payload)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    availability = module.is_available
    native_available = availability()
    if mode == "fallback":
        module.is_available = lambda: False
    observations = []
    try:
        sock.bind(str(docs / "sock"))
        with module.CapabilityBroker(FilesystemPolicy.load(repo)) as broker:
            broker.issue("corpus", workspace, CapabilityScope(("d",)))
            reader = module.SafeInputReader(broker)

            def read(
                name: str,
                path: str,
                expected_error: str | None,
                capability="corpus",
                expected_payload: bytes | None = None,
                expected_size: int | None = None,
            ):
                started = time.monotonic()
                result = reader.open_read(capability, path)
                denied = isinstance(result, ReadDenial)
                error = result.error_code.value if denied else None
                row = {
                    "scenario": name,
                    "requested_path": path,
                    "denied": denied,
                    "error_code": error,
                    "elapsed_seconds": time.monotonic() - started,
                    "expected_error": expected_error,
                    "returned_size": None if denied else len(result[0]),
                    "returned_hash": None if denied else str(hash_bytes(result[0])),
                }
                ok = denied == (expected_error is not None) and error == expected_error
                if expected_payload is not None:
                    ok = ok and not denied and result[0] == expected_payload
                if expected_size is not None:
                    ok = ok and not denied and len(result[0]) == expected_size
                row["passed"] = bool(ok)
                observations.append(row)

            read("regular", "d/good/a", None, expected_payload=payload)
            read("empty-file", "d/good/z", None, expected_payload=b"")
            read("size-boundary", "d/exact", None, expected_size=module._MAX_BYTES)
            for name, path in (
                ("size-over", "d/over"),
                ("hardlink", "d/hardlink"),
                ("traversal", "../outside"),
                ("absolute", str(outside)),
                ("empty-path", ""),
                ("nul", "d/\0"),
                ("windows", "C:/synthetic.txt"),
                ("scope", "outside"),
                ("depth", "d/" + "/".join(["x"] * module._MAX_DEPTH)),
            ):
                read(name, path, "PATH_OUTSIDE_CAPABILITY")
            for name, path in (
                ("symlink-outside", "d/symlink"),
                ("symlink-inside", "d/inside-link"),
                ("symlink-directory", "d/linkdir/a"),
                ("magic-link", "d/magic"),
            ):
                read(name, path, "SYMLINK_DENIED")
            for name in ("fifo", "sock", "good"):
                read("special-" + name, "d/" + name, "SPECIAL_FILE_DENIED")
            read("unknown-capability", "d/good/a", "PATH_OUTSIDE_CAPABILITY", "unknown")
            good = reader.enumerate("corpus", "d/good")
            bad = reader.enumerate("corpus", "d")
            observations.append(
                {
                    "scenario": "enumeration",
                    "observed_paths": good if isinstance(good, list) else None,
                    "unsafe_directory_denied": isinstance(bad, ReadDenial),
                    "passed": good == ["d/good/a", "d/good/z"] and isinstance(bad, ReadDenial),
                }
            )
            original_read = os.read
            timed_inode = (docs / "timed").stat().st_ino
            delayed = False

            def delayed_read(fd, size):
                nonlocal delayed
                data = original_read(fd, size)
                if not delayed and os.fstat(fd).st_ino == timed_inode:
                    delayed = True
                    time.sleep(module._MAX_READ_SECONDS + 0.05)
                return data

            try:
                os.read = delayed_read
                read("deadline", "d/timed", "PATH_OUTSIDE_CAPABILITY")
                observations[-1]["fault_injection"] = (
                    "real read with delayed return; kernel stall not simulated"
                )
                observations[-1]["delay_injected"] = delayed
                observations[-1]["passed"] = observations[-1]["passed"] and delayed
            finally:
                os.read = original_read
            broker.revoke("corpus")
            read("revoked-capability", "d/good/a", "PATH_OUTSIDE_CAPABILITY")
            for name in ("/proc", "/sys", "/dev", "/run"):
                error = None
                try:
                    broker.issue("virtual-" + name, Path(name), CapabilityScope(("synthetic",)))
                except HarnessError as exc:
                    error = exc.code.value
                observations.append(
                    {
                        "scenario": "virtual-root-" + name,
                        "error_code": error,
                        "passed": error == "WORKSPACE_ON_FOREIGN_FS_DENIED",
                    }
                )
    finally:
        sock.close()
        module.is_available = availability
    return {
        "mode": mode,
        "native_openat2_available": native_available,
        "read_time_limit_seconds": module._MAX_READ_SECONDS,
        "observations": observations,
        "passed": all(row["passed"] for row in observations),
        "latency_guarantee": "late input is rejected; blocked kernel syscall is not preempted",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=("auto", "fallback"), required=True)
    args = parser.parse_args()
    result = measure(args.repo_root, args.out, args.mode)
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "mode": result["mode"],
                "observations": len(result["observations"]),
            }
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
