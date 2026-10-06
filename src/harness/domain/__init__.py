"""Domain層。

外部I/O（`sqlite3`、`os`、`subprocess`、Provider SDK）へ依存しない純粋層である。
時刻・UUID・乱数・Fault InjectionはPort経由で注入され、本層からは直接参照しない。
この制約は `tests/spec_lint/test_layer_dependencies.py` が静的に検証する。
"""

from __future__ import annotations
