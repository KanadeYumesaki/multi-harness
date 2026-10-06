"""診断分類から外部の本文が漏れない。"""

import pytest

from harness.infrastructure.provider.cli_runner import _classify_stderr, _stderr_source_locations

pytestmark = pytest.mark.integration


def test_only_fixed_categories_are_returned_for_sensitive_diagnostics():
    result = _classify_stderr(
        b"EACCES /private/account token=synthetic-never-return; authentication failed"
    )
    assert result == ("AUTHENTICATION", "FS_PERMISSION")
    assert "private" not in repr(result)
    assert "synthetic" not in repr(result)


def test_unknown_stderr_is_not_copied_or_used_as_a_category():
    assert _classify_stderr(b"arbitrary-untrusted-diagnostic") == ()


def test_diagnostic_sample_has_a_fixed_bound():
    assert _classify_stderr(b"x" * 4096 + b"EACCES") == ()


def test_stack_locations_only_reference_verified_runtime_sources():
    payload = b"secret account /private/user.js:123:4 /runtime/gemini.js:456:7 token=untrusted"
    assert _stderr_source_locations(payload, ("/runtime/gemini.js",)) == (("gemini.js", 456),)
