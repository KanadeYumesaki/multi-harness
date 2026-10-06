"""部分観測失敗とPID再利用境界の回帰。実プロセスへsignalしない。"""

import errno
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.infrastructure.provider import cli_runner, process_containment

pytestmark = pytest.mark.integration


def test_partial_namespace_denial_is_unverifiable():
    with (
        patch.object(process_containment.os, "listdir", return_value=["101", "202"]),
        patch.object(
            process_containment, "read_process_namespace", side_effect=["other", None, "host"]
        ),
        patch.object(
            process_containment.os, "stat", return_value=SimpleNamespace(st_uid=os.getuid())
        ),
        patch.object(process_containment, "_is_zombie", return_value=False),
    ):
        result = process_containment.namespace_members("sandbox")
    assert not result.reliable
    assert not result.empty


def test_target_stat_denial_is_unverifiable():
    with (
        patch.object(process_containment.os, "listdir", return_value=["202"]),
        patch.object(process_containment, "read_process_namespace", return_value="sandbox"),
        patch("builtins.open", side_effect=PermissionError(errno.EACCES, "synthetic")),
    ):
        result = process_containment.namespace_members("sandbox")
    assert not result.reliable
    assert not result.empty


def test_signal_uses_pinned_process_handle():
    with (
        patch.object(cli_runner.os, "pidfd_open", return_value=90) as opened,
        patch.object(process_containment, "read_process_namespace", return_value="sandbox"),
        patch.object(cli_runner.signal, "pidfd_send_signal") as sent,
        patch.object(cli_runner.os, "close") as closed,
        patch.object(cli_runner.os, "kill", side_effect=AssertionError("numeric pid signal")),
    ):
        assert cli_runner._signal_namespace_member(202, "sandbox", 15)
    opened.assert_called_once_with(202)
    sent.assert_called_once_with(90, 15)
    closed.assert_called_once_with(90)


def test_recycled_pid_in_other_namespace_is_not_signalled():
    with (
        patch.object(cli_runner.os, "pidfd_open", return_value=90),
        patch.object(process_containment, "read_process_namespace", return_value="other"),
        patch.object(cli_runner.signal, "pidfd_send_signal") as sent,
        patch.object(cli_runner.os, "close") as closed,
    ):
        assert not cli_runner._signal_namespace_member(202, "sandbox", 15)
    sent.assert_not_called()
    closed.assert_called_once_with(90)


def test_pidfd_unavailable_fails_closed():
    with patch.object(cli_runner.os, "pidfd_open", side_effect=OSError(errno.ENOSYS, "synthetic")):
        assert not cli_runner._signal_namespace_member(202, "sandbox", 15)
