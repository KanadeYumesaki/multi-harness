#!/usr/bin/env python3
"""設計書の同一性（版番号とHash）を1箇所で決める。

## なぜ共有Moduleにするか

版番号は `build_spec_shards.py` と `build_registry_snapshot.py` に
`"1.8"` という**文字列リテラルで2箇所**書かれていた。v1.9へ改名した際、
File名・表題・`§23.4`は更新されたのに、生成物の`source_version`と
`design_version`は`1.8`のまま出た。

これは不変条件#18「件数を本文・コードへ手入力しない。正本はRegistry」と
同じ形の事故である。版番号の正本は設計書の表題であり、生成Toolの中ではない。
手入力した値は、正本が動いたことに気づかない。

Step 3-b が正規化関数の4重複を共有Moduleへ畳んだのと同じ扱いにする。
食い違いを事後に見つけるのではなく、起こり得なくする。

## 版番号の正本

設計書1行目付近の表題行である。

    ## フェーズ別詳細設計書 v1.9（Runtime GO判定実行正本・…）

ここから `1.9` を読む。**表題が読めない場合はFail-Closedで停止する**（不変条件#9）。
既定値へ落とすと、表題を壊したまま生成物だけが古い版番号を主張する。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

__all__ = ["DESIGN_TITLE_PATTERN", "design_sha256", "design_version"]

DESIGN_TITLE_PATTERN = re.compile(r"^##\s*フェーズ別詳細設計書\s*v(\d+\.\d+)", re.MULTILINE)


def design_version(design: Path) -> str:
    """設計書の表題から版番号を読む。読めなければ停止する。"""
    text = design.read_text(encoding="utf-8")
    match = DESIGN_TITLE_PATTERN.search(text)
    if match is None:
        raise ValueError(
            f"{design}: 表題から版番号を読めない。"
            "'## フェーズ別詳細設計書 vX.Y（…' の行が必要である"
        )
    return match.group(1)


def design_sha256(design: Path) -> str:
    """設計書Bytesの `sha256:` 付きHash。"""
    return "sha256:" + hashlib.sha256(design.read_bytes()).hexdigest()
