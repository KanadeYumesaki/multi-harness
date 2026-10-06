"""Providerごとの実測診断が必須で、別Providerの結果を流用しない。"""

import sys
from pathlib import Path

import pytest

from harness.domain.errors import HarnessError
from harness.infrastructure.provider import cli_profiles
from harness.infrastructure.provider.boundary import UNRESOLVED_DESIGN_BLOCKERS, BoundaryMeasurement

from .conftest import make_env

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("provider", ["codex", "gemini"])
def test_generation_provider_needs_its_own_attestation(tmp_path, monkeypatch, provider):
    observed = []

    def diagnostic(argv, provider_id):
        observed.append(provider_id)
        return {"satisfied": True, "provider": provider_id}

    monkeypatch.setattr(cli_profiles, "_attest_generation_runtime", diagnostic)
    # この試験は診断結果の配線を検証する。OS境界の実測は別の境界試験で行う。
    monkeypatch.setattr(
        cli_profiles.LinuxBoundaryProbe,
        "measure",
        lambda self, request: BoundaryMeasurement(
            filesystem_denied=True,
            descendants_contained=True,
            landlock_abi=1,
            truncate_handled=False,
            blocking_reasons=UNRESOLVED_DESIGN_BLOCKERS,
        ),
    )
    env = make_env(tmp_path, provider=provider, real_boundary=True)
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        assert profiles.boundary_verdict(provider).satisfied
        assert observed == [provider]
        monkeypatch.setattr(
            cli_profiles,
            "_attest_generation_runtime",
            lambda argv, provider_id: {"satisfied": False},
        )
        fresh = cli_profiles.CliRuntimeProfiles(profiles.manifest)
        assert not fresh.boundary_verdict(provider).satisfied
    finally:
        env.services.close()


@pytest.mark.parametrize("provider", ["codex", "gemini"])
def test_generation_attestation_cannot_waive_filesystem_failure(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(
        cli_profiles, "_attest_generation_runtime", lambda argv, provider_id: {"satisfied": True}
    )
    monkeypatch.setattr(
        cli_profiles.LinuxBoundaryProbe,
        "measure",
        lambda self, request: BoundaryMeasurement(
            filesystem_denied=False,
            descendants_contained=True,
            landlock_abi=1,
            truncate_handled=False,
            blocking_reasons=(*UNRESOLVED_DESIGN_BLOCKERS, "filesystem-denial-unproven"),
        ),
    )
    env = make_env(tmp_path, provider=provider)
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        verdict = profiles.boundary_verdict(provider)
        assert not verdict.satisfied
        assert verdict.blocking_reasons == ("filesystem-denial-unproven",)
    finally:
        env.services.close()


@pytest.mark.parametrize("provider", ["codex", "gemini"])
def test_selected_model_is_independently_verified(tmp_path, monkeypatch, provider):
    env = make_env(tmp_path, provider=provider)
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        spec = profiles.resolve(provider_id=provider, model_id="unverified-model")
        monkeypatch.setattr(
            cli_profiles,
            "_attest_generation_runtime",
            lambda argv, provider_id: {"satisfied": False},
        )
        with pytest.raises(HarnessError, match="selected model"):
            profiles._probe.attest_spec(spec)
    finally:
        env.services.close()


def test_gemini_forced_settings_are_bound_to_runtime(tmp_path, monkeypatch):
    env = make_env(tmp_path, provider="gemini")
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        before = profiles.resolve(provider_id="gemini", model_id="synthetic-model")
        assert before.runtime_projection["forced_settings"]["hooksConfig"]["enabled"] is False
        monkeypatch.setitem(cli_profiles._GEMINI_SYSTEM_SETTINGS, "hooksConfig", {"enabled": True})
        after = profiles.resolve(provider_id="gemini", model_id="synthetic-model")
        assert before.runtime_hash != after.runtime_hash
    finally:
        env.services.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"mcpServers": {"synthetic": {"command": "/usr/bin/false"}}},
        {"hooksConfig": {"enabled": True}},
        {"tools": {"discoveryCommand": "/usr/bin/false"}},
        {"security": {"auth": {"selectedType": "gemini-api-key"}}},
        {"unknown": True},
        [],
    ],
)
def test_gemini_personal_config_is_rejected_before_process_start(tmp_path, settings):
    import json

    env = make_env(tmp_path, provider="gemini")
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        root = Path(profiles.manifest.home) / ".gemini"
        root.mkdir()
        (root / "settings.json").write_text(json.dumps(settings))
        with pytest.raises(HarnessError):
            profiles.resolve(provider_id="gemini", model_id="synthetic-model")
        assert not env.runner.calls
    finally:
        env.services.close()


def test_gemini_oauth_config_hash_change_invalidates_runtime(tmp_path):
    import json

    env = make_env(tmp_path, provider="gemini")
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        root = Path(profiles.manifest.home) / ".gemini"
        root.mkdir()
        p = root / "settings.json"
        p.write_text(json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}))
        before = profiles.resolve(provider_id="gemini", model_id="synthetic-model")
        p.write_text(p.read_text() + "\n")
        after = profiles.resolve(provider_id="gemini", model_id="synthetic-model")
        assert before.runtime_hash != after.runtime_hash
    finally:
        env.services.close()


def test_gemini_management_settings_are_readable_but_not_writable_in_boundary(tmp_path):
    import json
    import subprocess

    env = make_env(tmp_path, provider="gemini")
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        profiles._require_neutral_workdir()
        policy = profiles.sandbox_policy("gemini")
        settings = profiles._gemini_settings_path()
        probe = (
            "import errno,json; from pathlib import Path; "
            f"p=Path({settings!r}); d=json.loads(p.read_text()); "
            "assert d['tools']['core']==[]; "
            "print('READ_OK');\n"
            "try: p.write_text('changed')\n"
            "except OSError as e: assert e.errno in (errno.EACCES,errno.EPERM,errno.EROFS); "
            "print('WRITE_DENIED')\n"
            "else: raise AssertionError('settings writable')\n"
        )
        result = subprocess.run(  # noqa: S603 - fixed launcher and synthetic test paths
            [
                *profiles.launcher_argv,
                json.dumps(policy),
                "--",
                str(Path(sys.executable).resolve(strict=True)),
                "-c",
                probe,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["READ_OK", "WRITE_DENIED"]
    finally:
        env.services.close()


def test_codex_child_denies_escalation_with_read_only_and_keeps_approval(tmp_path):
    env = make_env(tmp_path, provider="codex")
    try:
        profiles = cli_profiles.CliRuntimeProfiles(cli_profiles.load_runtime_manifest(env.profile))
        spec = profiles.resolve(provider_id="codex", model_id="synthetic-model")
        assert spec.argv[spec.argv.index("--sandbox") + 1] == "read-only"
        assert 'approval_policy="never"' in spec.argv
        assert not env.runner.calls
    finally:
        env.services.close()
