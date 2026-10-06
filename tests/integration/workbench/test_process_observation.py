"""部分観測失敗とPID再利用境界の回帰。実プロセスへsignalしない。"""

import errno
import os
from io import StringIO
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
        # Synthetic PIDs must never read the host process table by coincidence.
        patch("builtins.open", side_effect=PermissionError(errno.EACCES, "synthetic")),
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


@pytest.mark.parametrize("number", [errno.ENOENT, errno.ESRCH, errno.EACCES, errno.EIO])
def test_process_namespace_read_failure_returns_unknown(number: int) -> None:
    with patch.object(process_containment.os, "readlink", side_effect=OSError(number, "synthetic")):
        assert process_containment.read_process_namespace(202) is None


@pytest.mark.parametrize(
    "status,reliable",
    [("NSpid:\t202\n", True), ("NSpid:\t202 1\n", False), ("Name:\tsynthetic\n", False)],
)
def test_only_a_confirmed_root_namespace_process_can_be_excluded(
    status: str, reliable: bool
) -> None:
    def namespace(pid: int, which: str = "pid") -> str | None:
        return {101: "other", 202: None}.get(pid, "host")

    with (
        patch.object(process_containment.os, "listdir", return_value=["101", "202"]),
        patch.object(process_containment, "read_process_namespace", side_effect=namespace),
        patch.object(
            process_containment.os, "stat", return_value=SimpleNamespace(st_uid=os.getuid())
        ),
        patch.object(process_containment, "_is_zombie", return_value=False),
        patch("builtins.open", return_value=StringIO(status)),
    ):
        result = process_containment.namespace_members("sandbox")
    assert result.reliable is reliable
    assert result.empty is reliable
    assert result.members == ()


@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO])
def test_a_process_table_that_cannot_be_listed_is_not_empty(number: int) -> None:
    with patch.object(process_containment.os, "listdir", side_effect=OSError(number, "synthetic")):
        result = process_containment.namespace_members("sandbox")
    assert not result.reliable
    assert not result.empty


@pytest.mark.parametrize("number", [errno.ENOENT, errno.ESRCH, errno.EACCES, errno.EIO])
def test_unknown_owner_is_excluded_only_when_disappearance_is_confirmed(number: int) -> None:
    def namespace(pid: int, which: str = "pid") -> str | None:
        return {101: "other", 202: None}.get(pid, "host")

    with (
        patch.object(process_containment.os, "listdir", return_value=["101", "202"]),
        patch.object(process_containment, "read_process_namespace", side_effect=namespace),
        patch.object(process_containment.os, "stat", side_effect=OSError(number, "synthetic")),
        patch.object(process_containment, "_is_zombie", return_value=False),
        patch("builtins.open", side_effect=PermissionError(errno.EACCES, "synthetic")),
    ):
        result = process_containment.namespace_members("sandbox")
    assert result.reliable is (number in {errno.ENOENT, errno.ESRCH})
    assert result.empty is result.reliable


def test_excluded_processes_are_not_read_and_members_are_sorted() -> None:
    observed: list[int] = []

    def namespace(pid: int, which: str = "pid") -> str:
        observed.append(pid)
        return "sandbox"

    with (
        patch.object(process_containment.os, "listdir", return_value=["self", "202", "101", "303"]),
        patch.object(process_containment, "read_process_namespace", side_effect=namespace),
        patch.object(process_containment, "_is_zombie", return_value=False),
    ):
        result = process_containment.namespace_members("sandbox", exclude=frozenset({303}))
    assert result.reliable and not result.empty
    assert result.members == (101, 202)
    assert observed == [202, 101]


@pytest.mark.parametrize(
    "body,zombie",
    [
        ("202 (synthetic) S 1 1", False),
        ("202 (synthetic) Z 1 1", True),
        ("invalid stat", None),
        ("202 (synthetic)", None),
    ],
)
def test_only_an_observed_zombie_counts_as_stopped(body: str, zombie: bool | None) -> None:
    with patch("builtins.open", return_value=StringIO(body)):
        assert process_containment._is_zombie(202) is zombie


@pytest.mark.parametrize("number", [errno.ENOENT, errno.ESRCH, errno.EACCES, errno.EIO])
def test_zombie_read_failure_distinguishes_disappearance_from_unknown(number: int) -> None:
    with patch("builtins.open", side_effect=OSError(number, "synthetic")):
        result = process_containment._is_zombie(202)
    assert result is (True if number in {errno.ENOENT, errno.ESRCH} else None)


def test_children_lookup_keeps_valid_threads_and_deduplicates() -> None:
    contents = {"101": "202 303", "102": "303 404", "103": "malformed"}

    def read(path: str, **kwargs: object) -> StringIO:
        return StringIO(contents[path.split("/")[-2]])

    with (
        patch.object(process_containment.os, "listdir", return_value=list(contents)),
        patch("builtins.open", side_effect=read),
    ):
        assert process_containment._children_of(100) == (202, 303, 404)


def test_children_lookup_failure_cannot_invent_a_namespace() -> None:
    with (
        patch.object(process_containment.os, "listdir", side_effect=PermissionError("synthetic")),
        patch.object(process_containment.time, "monotonic", side_effect=[0.0, 1.0]),
    ):
        with pytest.raises(process_containment.NamespaceLookupFailed):
            process_containment.find_sandbox_namespace(100, own_namespace="host", budget_seconds=0)


def test_namespace_created_as_the_launcher_exits_gets_one_final_observation() -> None:
    with (
        patch.object(process_containment, "_children_of", side_effect=[(), (202,)]),
        patch.object(process_containment, "read_process_namespace", return_value="sandbox"),
        patch.object(process_containment.time, "monotonic", side_effect=[0.0, 0.1]),
    ):
        assert (
            process_containment.find_sandbox_namespace(
                100, own_namespace="host", still_running=lambda: False
            )
            == "sandbox"
        )


def test_namespace_lookup_waits_but_never_accepts_the_host_namespace() -> None:
    with (
        patch.object(process_containment, "_children_of", return_value=(202,)),
        patch.object(process_containment, "read_process_namespace", return_value="host"),
        patch.object(process_containment.time, "monotonic", side_effect=[0.0, 0.1, 2.0]),
        patch.object(process_containment.time, "sleep") as slept,
    ):
        with pytest.raises(process_containment.NamespaceLookupFailed):
            process_containment.find_sandbox_namespace(
                100, own_namespace="host", budget_seconds=1.0
            )
    slept.assert_called_once()


@pytest.mark.parametrize("failure", [ProcessLookupError, PermissionError])
def test_pinned_signal_error_preserves_identity_and_closes_the_handle(
    failure: type[OSError],
) -> None:
    with (
        patch.object(cli_runner.os, "pidfd_open", return_value=90),
        patch.object(process_containment, "read_process_namespace", return_value="sandbox"),
        patch.object(cli_runner.signal, "pidfd_send_signal", side_effect=failure("synthetic")),
        patch.object(cli_runner.os, "close") as closed,
    ):
        assert cli_runner._signal_namespace_member(202, "sandbox", 15) is (
            failure is ProcessLookupError
        )
    closed.assert_called_once_with(90)


def test_disappeared_pid_does_not_trigger_a_numeric_signal() -> None:
    with (
        patch.object(cli_runner.os, "pidfd_open", side_effect=ProcessLookupError("synthetic")),
        patch.object(cli_runner.os, "kill") as killed,
    ):
        assert cli_runner._signal_namespace_member(202, "sandbox", 15)
    killed.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["before_signal", "during_observation", "signal_denied"])
def test_cleanup_stops_on_uncertain_observation_without_claiming_empty(
    failure_stage: str,
) -> None:
    census = process_containment.ProcessCensus
    unknown = census("sandbox", (), reliable=False)
    present = census("sandbox", (202,), reliable=True)
    observations = [unknown] if failure_stage == "before_signal" else [present, unknown]
    with (
        patch.object(cli_runner, "namespace_members", side_effect=observations),
        patch.object(
            cli_runner, "_signal_namespace_member", return_value=failure_stage != "signal_denied"
        ) as sent,
    ):
        result = cli_runner._settle_namespace("sandbox")
    assert result.verdict is cli_runner.DescendantCleanup.UNVERIFIABLE
    if failure_stage == "before_signal":
        sent.assert_not_called()
    else:
        sent.assert_called_once_with(202, "sandbox", cli_runner.signal.SIGTERM)


def test_cleanup_escalates_then_reports_observed_residuals() -> None:
    census = process_containment.ProcessCensus("sandbox", (202,), reliable=True)
    with (
        patch.object(cli_runner, "namespace_members", return_value=census),
        patch.object(cli_runner, "_signal_namespace_member", return_value=True) as sent,
        patch.object(cli_runner.time, "monotonic", side_effect=[0.0, 4.0, 5.0, 9.0]),
    ):
        result = cli_runner._settle_namespace("sandbox")
    assert result.verdict is cli_runner.DescendantCleanup.RESIDUAL_PROCESSES
    assert result.residual == (202,)
    assert sent.call_args_list == [
        ((202, "sandbox", cli_runner.signal.SIGTERM),),
        ((202, "sandbox", cli_runner.signal.SIGKILL),),
    ]
