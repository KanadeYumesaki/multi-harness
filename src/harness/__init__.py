"""マルチプロバイダーAI業務実行統制基盤（Harness）。

層の依存規則（CLAUDE.md §2）:

    domain/       … sqlite3, os, subprocess, Provider SDK を import しない
    application/  … ports/ の抽象だけへ依存。Transaction境界を所有する
    ports/        … 抽象Portの定義のみ
    infrastructure/, adapters/, presentation/ … 具象実装
"""

from __future__ import annotations
