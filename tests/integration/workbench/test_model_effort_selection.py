"""モデル・推論選択を承認と実CLI引数へ束縛する。外部モデル送信はしない。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from harness.domain.errors import HarnessError
from harness.infrastructure.provider.cli_profiles import CliRuntimeProfiles, load_runtime_manifest

from .conftest import make_env

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_claude_selected_effort_reaches_plan_payload_and_real_process(tmp_path: Path, level: str):
    env = make_env(tmp_path, provider="claude")
    try:
        g = env.gateway
        s = g.create_session(
            provider_id="claude",
            model_id="opus",
            relative_path="hello.py",
            instruction="説明を追加",
            reasoning_effort=level,
        )
        sid = s["session_id"]
        c = g.confirmation(sid)
        assert s["reasoning_effort"] == c["reasoning_effort"] == level
        assert json.loads(c["request_payload_text"])["reasoning_effort"] == level
        g.approve_send(sid, execution_plan_hash=s["execution_plan_hash"])
        g.start_send(sid, execution_plan_hash=s["execution_plan_hash"])
        assert g.wait_for_idle(60)
        assert g.session(sid)["state"] == "PROPOSAL_READY"
        spec = env.runner.calls[0]
        assert spec.argv[spec.argv.index("--effort") + 1] == level
        assert spec.reasoning_effort == level
    finally:
        env.services.close()


def test_effort_changes_plan_and_cannot_reuse_another_approval(tmp_path: Path):
    env = make_env(tmp_path, provider="claude")
    try:
        g = env.gateway
        args = {
            "provider_id": "claude",
            "model_id": "opus",
            "relative_path": "hello.py",
            "instruction": "説明",
        }
        low = g.create_session(**args, reasoning_effort="low")
        high = g.create_session(**args, reasoning_effort="high")
        assert low["plan_content_hash"] != high["plan_content_hash"]
        assert low["runtime_hash"] != high["runtime_hash"]
        with pytest.raises(HarnessError):
            g.approve_send(high["session_id"], execution_plan_hash=low["execution_plan_hash"])
        assert env.runner.calls == []
    finally:
        env.services.close()


@pytest.mark.parametrize(
    "provider,model,effort",
    [
        ("claude", "haiku", "high"),
        ("claude", "opus", "ultracode"),
        ("claude", "opus", "high --tools Bash"),
        ("claude", "unknown-model", "high"),
        ("claude", "claude-opus-4-6", "xhigh"),
        ("gemini", "fake-model", "high"),
        ("codex", "gpt-5.5", "max"),
    ],
)
def test_unsupported_effort_is_rejected_before_spawn(tmp_path, provider, model, effort):
    env = make_env(tmp_path, provider=provider)
    try:
        with pytest.raises(HarnessError):
            env.gateway.create_session(
                provider_id=provider,
                model_id=model,
                relative_path="hello.py",
                instruction="説明",
                reasoning_effort=effort,
            )
        assert not env.runner.calls
    finally:
        env.services.close()


def test_codex_effort_is_an_explicit_config_argument(tmp_path: Path):
    env = make_env(tmp_path)
    try:
        profiles = CliRuntimeProfiles(
            load_runtime_manifest(env.profile), boundary_probe=env.gateway._service.profiles._probe
        )
        spec = profiles.resolve(
            provider_id="codex", model_id="gpt-5.6-sol", reasoning_effort="xhigh"
        )
        assert 'model_reasoning_effort="xhigh"' in spec.argv
        profiles.verify(spec)
        with pytest.raises(HarnessError):
            profiles.verify(replace(spec, reasoning_effort="low"))
    finally:
        env.services.close()
