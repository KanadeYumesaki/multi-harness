#!/usr/bin/env python3
"""送信を claim した直後に **本当に落ちる** 子 Process。

`services.close()` を通ると Worker が行儀よく畳まれてしまい、「crash のあとで
再起動したら何が起きるか」を測れない。だからここでは `os._exit()` で即座に
落ちる。atexit も finalizer も走らない。DB には Durable な状態だけが残る。

引数: <tmp_path> <mode> <session-id-output-path>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration" / "workbench"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))

from conftest import make_env  # noqa: E402


def main() -> int:
    tmp_path = Path(sys.argv[1])
    mode = sys.argv[2]
    output = Path(sys.argv[3])
    env = make_env(tmp_path, mode=mode)
    session = env.gateway.create_session(
        provider_id="codex",
        model_id="fake-model",
        relative_path="hello.py",
        instruction="コメントを足してください。",
    )
    session = env.gateway.approve_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    env.gateway.start_send(
        session["session_id"], execution_plan_hash=session["execution_plan_hash"]
    )
    output.write_text(session["session_id"], encoding="utf-8")
    # **ここで落ちる。** 片付けも Worker の合流もしない。
    sys.stdout.flush()
    os._exit(9)


if __name__ == "__main__":
    raise SystemExit(main())
