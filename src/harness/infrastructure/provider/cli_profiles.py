"""公式 CLI の起動 profile。**argv は Harness が組み立て、UI からは選べない。**

## ここに書いてある Option は実測で確認したものだけである

`docs/development/cli-workbench-e2e-20260908/cli-option-probe.json` に、各 CLI の
`--help` と実起動で確認した記録がある。記憶や過去の文章から Option 名を作らない。

## 実行前制限と、残る制限

各 Provider の実行前制限は `_RESTRICTIONS`、残る制限は `_RESIDUAL_RISKS` に
定数として置いてある。画面も `PROVIDER-STATUS.json` もその定数をそのまま出す。
散文とコードへ別々に書き写さない（不変条件#18 と同じ理由）。

## 環境変数は継承しない

`os.environ` を渡さない。`HOME` と固定 `PATH` だけを基本に、Provider ごとに必要な
変数を足す。`NODE_OPTIONS`・`PYTHONPATH`・proxy・endpoint 上書き・preload は
**渡す口が無い**。資格情報は各 CLI が自分で読む。Harness は 1 Byte も読まない。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from harness.domain.errors import ErrorCode, HarnessError
from harness.domain.hashing import hash_canonical
from harness.infrastructure.provider import sandbox_launcher
from harness.infrastructure.provider.boundary import (
    UNRESOLVED_DESIGN_BLOCKERS,
    BoundaryRequest,
    LinuxBoundaryProbe,
)
from harness.ports.cli_workbench import (
    BoundaryProbePort,
    BoundaryVerdict,
    CliLaunchSpec,
    CliProviderStatus,
    Containment,
)

__all__ = [
    "CLI_RUNTIME_CONTRACT",
    "FORCED_POLICY_PATHS",
    "CliRuntimeProfiles",
    "load_runtime_manifest",
]

CLI_RUNTIME_CONTRACT: Final[str] = "cli-runtime-profile/1"

#: 環境側の強制設定。**存在したら profile を有効にしない**（Fail-Closed）。
FORCED_POLICY_PATHS: Final[tuple[str, ...]] = (
    "/etc/claude-code/managed-settings.json",
    "/etc/claude-code/policies.json",
    "/etc/gemini-cli/settings.json",
    "/etc/codex/config.toml",
    "/etc/codex/rules",
)

_MODEL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,95}$")
# 候補はCLI help・公式モデル設定・導入環境の公開モデルメタデータで確認。
# 利用契約での可用性を保証する一覧ではない。ultracode/ultraは実行方式を変えるので含めない。
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_MODEL_EFFORTS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "claude": {
        "opus": _CLAUDE_EFFORTS,
        "sonnet": _CLAUDE_EFFORTS,
        "fable": _CLAUDE_EFFORTS,
        "haiku": (),
        "claude-opus-5": _CLAUDE_EFFORTS,
        "claude-sonnet-5": _CLAUDE_EFFORTS,
        "claude-fable-5": _CLAUDE_EFFORTS,
        "claude-fable-5-1": _CLAUDE_EFFORTS,
        "claude-opus-4-8": _CLAUDE_EFFORTS,
        "claude-opus-4-7": _CLAUDE_EFFORTS,
        "claude-opus-4-6": ("low", "medium", "high", "max"),
        "claude-sonnet-4-6": ("low", "medium", "high", "max"),
    },
    "codex": {
        "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max"),
        "gpt-5.6-terra": ("low", "medium", "high", "xhigh", "max"),
        "gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max"),
        "gpt-5.5": ("low", "medium", "high", "xhigh"),
    },
    "gemini": {},
}
_MODEL_PRESETS: Final[dict[str, tuple[str, ...]]] = {
    "claude": ("opus", "sonnet", "fable", "haiku"),
    "codex": tuple(_MODEL_EFFORTS["codex"]),
    "gemini": (),
}

_FIXED_PATH: Final[str] = "/usr/bin:/bin"
#: 1 度に読む大きさ。
_HASH_CHUNK_BYTES: Final[int] = 1024 * 1024

#: Gemini へ渡す system settings。**組込み tool を空にする。**
_GEMINI_SYSTEM_SETTINGS: Final[dict[str, Any]] = {
    "tools": {"core": [], "discoveryCommand": "", "callCommand": ""},
    "mcp": {"allowed": [], "serverCommand": ""},
    "mcpServers": {},
    "security": {"folderTrust": {"enabled": False}},
    "experimental": {"extensionManagement": False, "enableAgents": False},
    "hooksConfig": {"enabled": False},
    "admin": {
        "mcp": {"enabled": False},
        "extensions": {"enabled": False},
        "skills": {"enabled": False},
    },
    "telemetry": {"enabled": False},
}

#: Codex で `--disable` すると false になることを実測した機能。
#:
#: 名前は `codex features list` の出力から採っている。創作していない。
_CODEX_DISABLED_FEATURES: Final[tuple[str, ...]] = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode_host",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "in_app_local_automation",
    "mentions_v2",
    "multi_agent",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "sleep_tool",
    "sqlite",
    "steer",
    "tool_suggest",
    "unified_exec",
    "unified_exec_zsh_fork",
    "view_image",
    "workspace_dependencies",
)

#: `--disable` を指定しても `true` のままだと実測した機能。
#:
#: `unified_exec` は `--disable`、`-c features.unified_exec=false`、
#: `-c experimental_use_unified_exec_tool=false` のどれでも落ちない。**モデル由来の
#: コマンド実行能力は CLI の設定では消せない。** だから Harness 側が Landlock で
#: Filesystem の境界を掛ける。ここへ想定外の機能が増えたら `attest()` が止める。
_CODEX_UNREMOVABLE_FEATURES: Final[frozenset[str]] = frozenset({"unified_exec"})

#: Sandbox が読める System Path。
_SYSTEM_READ_EXECUTE: Final[tuple[str, ...]] = ("/usr", "/lib", "/lib64", "/bin", "/sbin")
_SYSTEM_READ_ONLY: Final[tuple[str, ...]] = ("/etc", "/dev/urandom", "/dev/random")
_SYSTEM_READ_WRITE: Final[tuple[str, ...]] = ("/dev/null", "/dev/zero")
#: 無くても止めない Path。**許可の書き忘れは黙って飛ばさない。**
_OPTIONAL_PATHS: Final[tuple[str, ...]] = ("/lib64", "/sbin", "/dev/random", "/dev/zero")

#: Provider が自分の資格情報と State を置く場所。**Harness は中身を読まない。**
_PROVIDER_STATE_DIRS: Final[dict[str, tuple[str, ...]]] = {
    "codex": (".codex",),
    "claude": (".claude", ".claude.json"),
    "gemini": (".gemini",),
}

#: attest が使う合成 canary の File 名。
_GEMINI_PROMPT: Final[str] = (
    "Read the JSON object provided on standard input and follow its instruction. "
    "Reply with exactly one JSON object and nothing else."
)


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    provider_id: str
    display_name: str
    package: str
    package_version: str | None
    argv_prefix: tuple[str, ...]
    suggested_models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """Owner が用意する profile File の内容。**Secret を持たない。**"""

    contract: str
    home: str
    neutral_workdir: str
    state_dir: str
    #: 公式 CLI を入れた専用 Directory。**Sandbox が読み書きを許す唯一の CLI 領域。**
    cli_runtime_root: str
    providers: tuple[ProviderRuntime, ...]

    def provider(self, provider_id: str) -> ProviderRuntime | None:
        for candidate in self.providers:
            if candidate.provider_id == provider_id:
                return candidate
        return None


def load_runtime_manifest(path: Path) -> RuntimeManifest:
    """profile File を厳格に読む。**未知 Key と相対 Path を拒否する。**"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI runtime profile could not be read"
        ) from error
    if not isinstance(raw, dict) or raw.get("contract") != CLI_RUNTIME_CONTRACT:
        raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "unsupported CLI runtime profile")
    allowed = {
        "contract",
        "home",
        "neutral_workdir",
        "state_dir",
        "cli_runtime_root",
        "providers",
    }
    if set(raw) - allowed:
        raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI runtime profile has unknown keys")
    providers: list[ProviderRuntime] = []
    entries = raw.get("providers")
    if not isinstance(entries, dict) or not entries:
        raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI runtime profile has no providers")
    for provider_id in sorted(entries):
        entry = entries[provider_id]
        if provider_id not in {"codex", "claude", "gemini"} or not isinstance(entry, dict):
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "unknown provider in profile")
        argv_prefix = entry.get("argv_prefix")
        if not isinstance(argv_prefix, list) or not argv_prefix:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "argv_prefix must be a list")
        for item in argv_prefix:
            if not isinstance(item, str) or not item.startswith("/"):
                raise HarnessError(
                    ErrorCode.RUNTIME_SPEC_MISMATCH, "argv_prefix must hold absolute paths"
                )
        models = entry.get("suggested_models", [])
        if not isinstance(models, list) or any(
            not isinstance(item, str) or not _MODEL_RE.match(item) for item in models
        ):
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "suggested_models are malformed")
        version = entry.get("package_version")
        providers.append(
            ProviderRuntime(
                provider_id=provider_id,
                display_name=str(entry.get("display_name", provider_id)),
                package=str(entry.get("package", "")),
                package_version=str(version) if isinstance(version, str) else None,
                argv_prefix=tuple(argv_prefix),
                suggested_models=tuple(models),
            )
        )
    for name in ("home", "neutral_workdir", "state_dir", "cli_runtime_root"):
        value = raw.get(name)
        if not isinstance(value, str) or not value.startswith("/"):
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, f"{name} must be an absolute path")
    return RuntimeManifest(
        contract=CLI_RUNTIME_CONTRACT,
        home=str(raw["home"]),
        neutral_workdir=str(raw["neutral_workdir"]),
        state_dir=str(raw["state_dir"]),
        cli_runtime_root=str(raw["cli_runtime_root"]),
        providers=tuple(providers),
    )


@dataclass
class CliRuntimeProfiles:
    """`CliProfilePort` の具象。**起動直前の再照合をここが持つ。**

    ## 実行体の同一性は毎回 Bytes から取り直す

    以前は 8 MiB 以上の File を `(path,size,mtime_ns,ino,dev)` で Cache していた。
    mtime は同一 UID で復元できるので、**Bytes を書き換えても再検証が通った**
    （`docs/development/cli-workbench-review-fix-20260908/reproduction-before.json`）。
    Cache は廃止した。246 MiB の native binary でも 0.3 秒で再 Hash できるため、
    承認失効判定と最終起動前検査で節約する理由が無い。
    """

    manifest: RuntimeManifest
    #: 境界の実効性を測るとき「触れられないこと」を確かめる Path。
    #:
    #: 組立側が Workspace・DB・CAS を渡す。空のままにしない。空だと `attest()` は
    #: Fail-Closed で止まる。
    probe_paths: tuple[str, ...] = ()
    #: 能力検証の実装。**組立の既定は実測 Probe である。**
    #:
    #: 差し替え口は試験専用に置いてある。`cli.py` も `composition.py` も既定の
    #: ままにする。Flag も環境変数も用意しない（試験:
    #: `tests/integration/workbench/test_boundary_gate.py`）。
    boundary_probe: BoundaryProbePort | None = None

    def __post_init__(self) -> None:
        self._probe: BoundaryProbePort = self.boundary_probe or _DefaultBoundaryProbe(self)

    def boundary_verdict(self, provider_id: str) -> BoundaryVerdict:
        """この Provider を起動してよいかを、境界の実測から決める。"""
        return self._probe.verdict(provider_id)

    def boundary_request(self, provider_id: str) -> BoundaryRequest:
        """実測 Probe へ渡す材料。**canary は state_dir の下の合成 File だけ。**"""
        return BoundaryRequest(
            launcher_argv=self.launcher_argv,
            policy=self.sandbox_policy(provider_id),
            env=self._environment(provider_id),
            cwd=self.manifest.neutral_workdir,
            canary_root=str(Path(self.manifest.state_dir) / "attest"),
            read_only_probes=tuple(self.probe_paths),
        )

    @property
    def launcher_argv(self) -> tuple[str, str]:
        """Sandbox Launcher を起動する 2 語。"""
        return (
            os.path.realpath(sys.executable),
            str(Path(sandbox_launcher.__file__).resolve()),
        )

    def sandbox_policy(self, provider_id: str) -> dict[str, Any]:
        """Provider ごとの Filesystem 境界。**許すものだけを並べる。**

        `$HOME` そのものは許さない。許すのは CLI の実行体、System Path、中立の
        作業 Directory、TMPDIR、そして **その Provider 自身の設定・資格情報
        Directory** だけである。利用者の Source Tree も、Harness の DB も CAS も、
        SSH 鍵も、他 Provider の資格情報も、この境界の外になる。

        資格情報 Directory を許すのは、CLI が自分で認証するために要るからである。
        **Harness はその中身を読まない。** ただし境界の内側にある以上、モデル由来の
        コマンドからは読まれ得る。これは `_RESIDUAL_RISKS` へ明記する。
        """
        home = Path(self.manifest.home)
        provider_paths = [str(home / name) for name in _PROVIDER_STATE_DIRS.get(provider_id, ())]
        interpreters = sorted(
            {
                str(Path(item).resolve().parent)
                for runtime in self.manifest.providers
                for item in runtime.argv_prefix
                if not Path(item).resolve().is_relative_to(Path(self.manifest.cli_runtime_root))
            }
        )
        return {
            "contract": sandbox_launcher.POLICY_CONTRACT,
            "read_execute": [
                *_SYSTEM_READ_EXECUTE,
                self.manifest.cli_runtime_root,
                *interpreters,
            ],
            # Bunは起動時に自分自身のメモリ配置を読む。/proc全体や
            # environ、他PIDは許さず、exec後も同じPIDのmaps 1件だけを許す。
            "read_only": [
                *_SYSTEM_READ_ONLY,
                *_resolver_paths(),
                *(["/proc/self/maps"] if provider_id == "claude" else []),
                *([self._gemini_settings_path()] if provider_id == "gemini" else []),
            ],
            "read_write": [
                self.manifest.neutral_workdir,
                str(Path(self.manifest.state_dir) / "tmp"),
                *provider_paths,
                *_SYSTEM_READ_WRITE,
            ],
            "optional_paths": [*_OPTIONAL_PATHS, *provider_paths, *_resolver_paths()],
        }

    # -- 実行前制限の実測 ---------------------------------------------------

    def attest(self, spec: CliLaunchSpec) -> dict[str, Any]:
        """制限が **実際に効いていること** を、その場で測って返す。

        1. Landlock 境界の実効性。承認した payload の外（Workspace・DB・CAS・合成
           canary）へ、Launcher 配下の Process と **その子** から触れられないことを
           確かめる。
        2. Codex は自分の `features list` で、無効化したはずの機能が本当に `false`
           かを確かめる。想定外の機能が `true` のままなら止める。

        「設定を書いた」ではなく「触れなかった」「false だった」を証拠にする。
        測れなかったら例外で止める。**測れないまま起動しない。**
        """
        # 測るときも CLI と同じ場所で動かす。**前提を先に満たす。**
        self._require_neutral_workdir()
        boundary = self._attest_boundary(spec)
        features = self._attest_codex_features(spec) if spec.provider_id == "codex" else None
        if isinstance(self._probe, _DefaultBoundaryProbe) and spec.provider_id != "claude":
            boundary["generation_attestation"] = self._probe.attest_spec(spec)
        return {
            "boundary": boundary,
            "provider_features": features,
            "method": "LANDLOCK_SELF_TEST"
            + ("_AND_CODEX_FEATURE_LIST" if features is not None else ""),
        }

    def _attest_boundary(self, spec: CliLaunchSpec) -> dict[str, Any]:
        """必要な境界が **実際に効いていること** を測る。効かなければ止める。

        測るのは Filesystem の操作範囲（合成 canary への read / write / truncate /
        O_TRUNC / create / rename / unlink）と、子孫の封じ込め（`setsid` で離脱した
        子が PID namespace ごと片付くか）である。metadata変更はreadonly mountが拒否する。
        """
        if not self.probe_paths:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "sandbox attestation needs at least one path that must stay unreachable",
            )
        verdict = self.boundary_verdict(spec.provider_id)
        if not verdict.satisfied:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, _blocking_text(verdict))
        return {
            "probe_paths": list(self.probe_paths),
            "measured": _boundary_contract_measurement(dict(verdict.details)),
            "unenforceable_operations": list(sandbox_launcher.UNENFORCEABLE_OPERATIONS),
            "required_operations": list(sandbox_launcher.PROTECTED_OPERATIONS),
            "mount_policy": "recursive-readonly-with-explicit-write-paths",
            "all_denied": True,
        }

    def _attest_codex_features(self, spec: CliLaunchSpec) -> dict[str, Any]:
        """Codex 自身の機能一覧で、無効化が効いていることを確かめる。"""
        policy = self.sandbox_policy(spec.provider_id)
        runtime = self.manifest.provider(spec.provider_id)
        if runtime is None:  # pragma: no cover - resolve が先に落ちる
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "provider is not in the profile")
        argv = [
            *self.launcher_argv,
            json.dumps(policy, separators=(",", ":"), sort_keys=True),
            "--",
            *runtime.argv_prefix,
            "features",
            "list",
            *_codex_restriction_argv(),
        ]
        completed = _run_probe(argv, spec)
        if completed.returncode != 0:
            # 未知の機能名を渡すと Codex は非 0 で落ちる。版が変わって名前が消えた
            # ときに **黙って無効化をやめない** ための Fail-Closed である。
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "codex rejected the declared feature restrictions",
            )
        observed = _parse_feature_list(completed.stdout.decode("utf-8", "replace"))
        missing = [name for name in _CODEX_DISABLED_FEATURES if name not in observed]
        if missing:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "codex no longer reports every restricted feature: " + ",".join(sorted(missing)),
            )
        still_on = sorted(name for name in _CODEX_DISABLED_FEATURES if observed.get(name))
        unexpected = [name for name in still_on if name not in _CODEX_UNREMOVABLE_FEATURES]
        if unexpected:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "codex features stayed enabled unexpectedly: " + ",".join(unexpected),
            )
        return {
            "requested_disabled": list(_CODEX_DISABLED_FEATURES),
            "still_enabled": still_on,
            "known_unremovable": sorted(_CODEX_UNREMOVABLE_FEATURES),
            "contained_by": "HARNESS_LANDLOCK_BOUNDARY",
        }

    # -- 状態表示 -----------------------------------------------------------

    def statuses(self) -> tuple[CliProviderStatus, ...]:
        forced = self._forced_policy_files()
        rows: list[CliProviderStatus] = []
        for runtime in self.manifest.providers:
            installed = all(Path(item).is_file() for item in runtime.argv_prefix)
            blocking: str | None = None
            if not installed:
                blocking = "実行体が見つからない。導入をやり直す"
            elif forced:
                blocking = "環境側の強制設定が存在する: " + ", ".join(forced)
            verified = False
            if blocking is None:
                try:
                    self._require_neutral_workdir()
                    verified = True
                except HarnessError as error:
                    blocking = str(error)
            if blocking is None:
                # 必要な境界が **実際に効くか** を測る。効かなければ選ばせない。
                verdict = self.boundary_verdict(runtime.provider_id)
                if not verdict.satisfied:
                    verified = False
                    blocking = _blocking_text(verdict)
            rows.append(
                CliProviderStatus(
                    provider_id=runtime.provider_id,
                    display_name=runtime.display_name,
                    installed=installed,
                    package_version=runtime.package_version,
                    profile_verified=verified,
                    restriction_summary=_RESTRICTIONS[runtime.provider_id],
                    residual_risks=_RESIDUAL_RISKS[runtime.provider_id],
                    login_state="UNVERIFIED",
                    login_hint=_LOGIN_HINTS[runtime.provider_id],
                    models=tuple(
                        dict.fromkeys(
                            (*runtime.suggested_models, *_MODEL_PRESETS[runtime.provider_id])
                        )
                    ),
                    model_efforts=_MODEL_EFFORTS[runtime.provider_id],
                    blocking_reason=blocking,
                )
            )
        return tuple(rows)

    # -- 起動仕様 -----------------------------------------------------------

    def resolve(
        self, *, provider_id: str, model_id: str, reasoning_effort: str | None = None
    ) -> CliLaunchSpec:
        if provider_id == "gemini":
            self._validate_gemini_personal_config()
        runtime = self.manifest.provider(provider_id)
        if runtime is None:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "provider is not in the profile")
        if not _MODEL_RE.match(model_id):
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION, "model id has an unaccepted shape"
            )
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str)
            or reasoning_effort not in _MODEL_EFFORTS.get(provider_id, {}).get(model_id, ())
        ):
            raise HarnessError(
                ErrorCode.SCHEMA_CONDITIONAL_VIOLATION,
                "reasoning effort is not supported for this provider/model",
            )
        policy = self.sandbox_policy(provider_id)
        # **Harness 側の境界を掛けてから CLI を exec する。** Process は増えない
        # （Launcher が自分を CLI へ置き換える）。argv は Plan へ束縛されるので、
        # 境界を緩めれば承認は失効する。
        argv = (
            *self.launcher_argv,
            json.dumps(policy, separators=(",", ":"), sort_keys=True),
            "--",
            *runtime.argv_prefix,
            *_provider_argv(provider_id, model_id, self.manifest.neutral_workdir, reasoning_effort),
        )
        env = self._environment(provider_id)
        executables = self._executable_identity(runtime)
        projection: dict[str, Any] = {
            "provider_id": provider_id,
            "model_id": model_id,
            "package": runtime.package,
            "package_version": runtime.package_version,
            "argv": list(argv),
            "env_keys": sorted(env),
            "env_values_non_secret": {
                key: value for key, value in sorted(env.items()) if key != "HOME"
            },
            "cwd": self.manifest.neutral_workdir,
            "cwd_kind": "NEUTRAL_EMPTY_DIRECTORY",
            "executables": executables,
            "sandbox_policy": policy,
            "sandbox_kind": "HARNESS_LANDLOCK_AND_PID_NAMESPACE",
            "containment": Containment.PID_NAMESPACE,
            "required_operations": list(sandbox_launcher.PROTECTED_OPERATIONS),
            "unenforceable_operations": list(sandbox_launcher.UNENFORCEABLE_OPERATIONS),
            "restrictions": list(_RESTRICTIONS[provider_id]),
            "residual_risks": list(_RESIDUAL_RISKS[provider_id]),
            "forced_policy_files_present": self._forced_policy_files(),
            "credential_route": "CLI_OWNED_NOT_READ_BY_HARNESS",
            "stdin_only_payload": True,
        }
        if provider_id == "gemini":
            projection["forced_settings"] = deepcopy(_GEMINI_SYSTEM_SETTINGS)
        if reasoning_effort is not None:
            projection["reasoning_effort"] = reasoning_effort
        return CliLaunchSpec(
            provider_id=provider_id,
            model_id=model_id,
            argv=argv,
            env=env,
            cwd=self.manifest.neutral_workdir,
            runtime_hash=hash_canonical(
                projection, artifact_type="cli-workbench-runtime", schema_major=1
            ),
            runtime_projection=projection,
            containment=Containment.PID_NAMESPACE,
            reasoning_effort=reasoning_effort,
        )

    def verify(self, spec: CliLaunchSpec) -> None:
        """起動直前の再照合。**更新後の実行体で古い承認を使わせない。**"""
        forced = self._forced_policy_files()
        if forced:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "environment forces CLI policy files; the provider is not enabled",
            )
        self._require_neutral_workdir()
        verdict = self.boundary_verdict(spec.provider_id)
        if not verdict.satisfied:
            # 起動直前にも確かめる。**Plan を作った時点の判断を使い回さない。**
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, _blocking_text(verdict))
        current = self.resolve(
            provider_id=spec.provider_id,
            model_id=spec.model_id,
            reasoning_effort=spec.reasoning_effort,
        )
        if current.runtime_hash != spec.runtime_hash or current.argv != spec.argv:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI runtime identity changed since the plan"
            )

    # -- 内部 ---------------------------------------------------------------

    def _environment(self, provider_id: str) -> dict[str, str]:
        env = {
            "HOME": self.manifest.home,
            "PATH": _FIXED_PATH,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(Path(self.manifest.state_dir) / "tmp"),
            "NO_COLOR": "1",
            "CI": "1",
        }
        if provider_id == "claude":
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        if provider_id == "gemini":
            env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = self._gemini_settings_path()
        return env

    def _validate_gemini_personal_config(self) -> None:
        root = Path(self.manifest.home) / ".gemini"
        # 管理設定より前にMCP起動が起きるため、読ませる個人設定を制限する。
        for name in (".env", "GEMINI.md", "policies", "extensions", "skills"):
            path = root / name
            if path.exists() or path.is_symlink():
                raise HarnessError(
                    ErrorCode.RUNTIME_SPEC_MISMATCH,
                    "Gemini personal extensions or configuration require isolation",
                )
        path = root / "settings.json"
        if not path.exists() and not path.is_symlink():
            return
        self._file_identity(path)
        if path.stat().st_size > 16384:
            raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "Gemini settings exceed limit")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            security = document.get("security", {})
            auth = security.get("auth", {})
            valid = (
                isinstance(document, dict)
                and set(document) <= {"security"}
                and isinstance(security, dict)
                and set(security) <= {"auth"}
                and isinstance(auth, dict)
                and set(auth) <= {"selectedType"}
                and auth.get("selectedType") in (None, "oauth-personal")
            )
        except (OSError, ValueError, AttributeError, TypeError):
            valid = False
        if not valid:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "Gemini personal settings must contain only the CLI OAuth method",
            )

    def _gemini_settings_path(self) -> str:
        path = Path(self.manifest.state_dir) / "gemini-system-settings.json"
        payload = json.dumps(_GEMINI_SYSTEM_SETTINGS, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if existing != payload:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
        return str(path)

    def _require_neutral_workdir(self) -> None:
        """CLI の cwd は **空の中立 Directory** でなければならない。"""
        path = Path(self.manifest.neutral_workdir)
        path.mkdir(parents=True, exist_ok=True)
        (Path(self.manifest.state_dir) / "tmp").mkdir(parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise HarnessError(ErrorCode.PATH_OUTSIDE_CAPABILITY, "neutral workdir is not a dir")
        entries = sorted(item.name for item in path.iterdir())
        if entries:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "neutral CLI working directory must stay empty",
            )

    def _forced_policy_files(self) -> list[str]:
        return [candidate for candidate in FORCED_POLICY_PATHS if Path(candidate).exists()]

    def _executable_identity(self, runtime: ProviderRuntime) -> list[dict[str, Any]]:
        """entrypoint だけでなく、Launcher と bundle まで含める。

        境界を掛けるのは Launcher なので、**Launcher が差し替わったら承認は失効
        する**。Python 実行体も同じ理由で含める。
        """
        rows: list[dict[str, Any]] = []
        for item in self.launcher_argv:
            rows.append(self._file_identity(Path(item)))
        for item in runtime.argv_prefix:
            rows.append(self._file_identity(Path(item)))
        for extra in sorted(_bundle_files(runtime)):
            rows.append(self._file_identity(extra))
        if runtime.provider_id == "gemini":
            personal = Path(self.manifest.home) / ".gemini" / "settings.json"
            if personal.exists() or personal.is_symlink():
                rows.append(self._file_identity(personal))
        if runtime.provider_id in {"claude", "codex", "gemini"}:
            rows.append(self._file_identity(_tool_probe_path()))
            rows.append(self._file_identity(Path("/usr/bin/unshare")))
        return rows

    def _file_identity(self, path: Path) -> dict[str, Any]:
        """1 つの実行体の同一性。**Bytes を毎回読んで Hash する。**

        * 最終要素の Symlink は `O_NOFOLLOW` で拒否する。
        * Hash は **開いた fd から**読む。名前を開き直さないので、読んでいる実体と
          Hash した実体が食い違わない。
        * 読む前と読んだ後で `fstat` を照合する。読込み中に書き換えられたら
          停止する。
        * `dev`/`ino` も同一性へ入れる。同じ Bytes でも別の実体へ入れ替われば
          Plan は失効する。
        """
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as error:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "declared CLI executable is missing or is a symlink",
            ) from error
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise HarnessError(
                    ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI executable must be a regular file"
                )
            digest = _sha256_descriptor(descriptor)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH, "CLI executable changed while it was being read"
            )
        return {
            "path": str(path),
            "size_bytes": before.st_size,
            "sha256": digest,
            "device": before.st_dev,
            "inode": before.st_ino,
        }


class _DefaultBoundaryProbe:
    """実測 Probe を `CliRuntimeProfiles` へつなぐ既定実装。

    `LinuxBoundaryProbe` は Kernel 能力を 1 度だけ測って覚える。Provider ごとに
    Policy が違うので、Provider ごとに測る。
    """

    def __init__(self, profiles: CliRuntimeProfiles) -> None:
        self._profiles = profiles
        self._probe = LinuxBoundaryProbe()
        self._tool_reports: dict[str, dict[str, Any]] = {}

    def attest_spec(self, spec: CliLaunchSpec) -> dict[str, Any]:
        # 選択したモデル・推論値・実行体ごとに確認し、結果も承認対象へ含める。
        key = str(spec.runtime_hash)
        report = self._tool_reports.get(key)
        if report is None:
            argv = spec.argv[spec.argv.index("--") + 1 :]
            report = _attest_generation_runtime(argv, spec.provider_id)
            self._tool_reports[key] = report
        if report.get("satisfied") is not True:
            raise HarnessError(
                ErrorCode.RUNTIME_SPEC_MISMATCH,
                "selected model generation tools could not be verified",
            )
        return report

    def verdict(self, provider_id: str) -> BoundaryVerdict:
        if provider_id == "gemini":
            try:
                self._profiles._validate_gemini_personal_config()
            except HarnessError:
                reason = "Geminiの個人設定は認証方式のみ許可。hooks/MCP/追加設定は隔離が必要"
                return BoundaryVerdict(False, (reason,), {"blocking_reasons": [reason]})
        measurement = self._probe.measure(self._profiles.boundary_request(provider_id))
        reasons = measurement.blocking_reasons
        details = measurement.as_dict()
        if provider_id in {"claude", "codex", "gemini"}:
            runtime = self._profiles.manifest.provider(provider_id)
            if runtime is not None:
                identity = json.dumps(self._profiles._executable_identity(runtime), sort_keys=True)
                report = self._tool_reports.get(identity)
                if report is None:
                    probe_argv = (
                        *runtime.argv_prefix,
                        *_provider_argv(
                            provider_id, "synthetic-model", self._profiles.manifest.neutral_workdir
                        ),
                    )
                    report = (
                        _attest_tool_free_runtime(probe_argv)
                        if provider_id == "claude"
                        else _attest_generation_runtime(probe_argv, provider_id)
                    )
                    self._tool_reports[identity] = report
                details["tool_free_attestation"] = report
                if report.get("satisfied") is True:
                    # CLI本体を認証/通信の信頼主体とし、操作toolは実測で拒否する。
                    # 各Providerの診断に合格した場合だけ。FS/PID境界の失敗は残す。
                    reasons = tuple(
                        reason for reason in reasons if reason not in UNRESOLVED_DESIGN_BLOCKERS
                    )
                else:
                    reasons = (*reasons, provider_id + "の生成専用tool拒否を実測できない")
        details["satisfied"] = not reasons
        details["blocking_reasons"] = list(reasons)
        return BoundaryVerdict(satisfied=not reasons, blocking_reasons=reasons, details=details)


def _boundary_contract_measurement(measured: dict[str, Any]) -> dict[str, Any]:
    """承認には検証結果を束縛し、測定用namespaceの識別番号だけを除く。

    RequestとWorkerは独立したProbeを持ち、正常でも番号は異なる。
    生の番号はBoundaryVerdict.detailsに保持する。拒否結果、対象Path、
    Kernel能力、未知のFieldは比較対象に残す。入力の証跡を変更しない。
    """
    contract = deepcopy(measured)
    details = contract.get("details")
    if isinstance(details, dict):
        containment = details.get("containment")
        if isinstance(containment, dict):
            containment.pop("namespace", None)
            containment.pop("own_namespace", None)
        filesystem = details.get("filesystem")
        if isinstance(filesystem, dict):
            filesystem.pop("pid_namespace", None)
    return contract


def _tool_probe_path() -> Path:
    return Path(__file__).resolve().parents[4] / "tools" / "attest_tool_free_cli.py"


def _attest_tool_free_runtime(argv: tuple[str, ...]) -> dict[str, Any]:
    return _run_tool_probe(argv, "claude")


def _attest_generation_runtime(argv: tuple[str, ...], provider_id: str) -> dict[str, Any]:
    return _run_tool_probe(argv, provider_id)


def _run_tool_probe(argv: tuple[str, ...], provider_id: str) -> dict[str, Any]:
    """偽応答だけの診断。実認証情報と外部ネットワークは渡さない。"""
    try:
        result = subprocess.run(  # noqa: S603 - fixed diagnostic in isolated namespaces
            [
                "/usr/bin/unshare",
                "--user",
                "--map-root-user",
                "--net",
                "--pid",
                "--fork",
                "--kill-child",
                os.path.realpath(sys.executable),
                str(_tool_probe_path()),
                json.dumps(list(argv)),
                *(
                    [provider_id, json.dumps(_GEMINI_SYSTEM_SETTINGS)]
                    if provider_id != "claude"
                    else []
                ),
            ],
            capture_output=True,
            timeout=50,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        report = json.loads(result.stdout)
        if result.returncode == 0 and isinstance(report, dict):
            return report
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return {"satisfied": False, "reason": "isolated tool-free attestation failed"}


def _blocking_text(verdict: BoundaryVerdict) -> str:
    """起動を止めた理由を 1 本の文にする。**理由を落とさない。**"""
    return "必要な実行前境界が成立しない: " + " / ".join(verdict.blocking_reasons)


def _resolver_paths() -> tuple[str, ...]:
    """名前解決に要る File のうち、`/etc` の外にあるものを足す。

    WSL2 では `/etc/resolv.conf` が `/mnt/wsl/resolv.conf` への Symlink になっている。
    `/etc` を許すだけでは実体へ届かず、**DNS が引けないまま起動してしまう。**
    ここで許すのは解決先の **File 1 つだけ** である。`/mnt` も `/mnt/wsl` も
    Directory としては許さない（Windows 側 Filesystem を境界へ入れない）。
    """
    found: list[str] = []
    for candidate in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
        try:
            resolved = os.path.realpath(candidate)
        except OSError:  # pragma: no cover - realpath はほぼ失敗しない
            continue
        if resolved != candidate and Path(resolved).is_file():
            found.append(resolved)
    # nss が systemd-resolved を使う環境向け。無ければ飛ばす。
    for optional in ("/run/systemd/resolve", "/run/resolvconf"):
        if Path(optional).is_dir():
            found.append(optional)
    return tuple(sorted(set(found)))


def _codex_restriction_argv() -> tuple[str, ...]:
    """Codex へ渡す制限用 argv。**argv も attest も同じ 1 か所から作る。**"""
    disables: list[str] = []
    for name in _CODEX_DISABLED_FEATURES:
        disables.extend(("--disable", name))
    return (
        "-c",
        "tools.web_search=false",
        "-c",
        'web_search="disabled"',
        "-c",
        "mcp_servers={}",
        "-c",
        "hooks={}",
        *disables,
    )


def _parse_feature_list(text: str) -> dict[str, bool]:
    """`codex features list` の 1 行から名前と実効状態を取る。"""
    observed: dict[str, bool] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[-1] not in {"true", "false"}:
            continue
        observed[fields[0]] = fields[-1] == "true"
    return observed


def _run_probe(argv: list[str], spec: CliLaunchSpec) -> subprocess.CompletedProcess[bytes]:
    """attest 用の短い起動。**モデルへは何も送らない。**"""
    try:
        return subprocess.run(  # noqa: S603 - fixed argv list, no shell, explicit env
            argv,
            cwd=spec.cwd,
            env=dict(spec.env),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HarnessError(
            ErrorCode.RUNTIME_SPEC_MISMATCH, "restriction attestation could not be measured"
        ) from error


def _bundle_files(runtime: ProviderRuntime) -> list[Path]:
    """動的に読み込まれる bundle を同一性へ含める。**entrypoint だけで足りない。**"""
    if runtime.provider_id != "gemini":
        return []
    entry = Path(runtime.argv_prefix[-1])
    directory = entry.parent
    return [item for item in directory.glob("*.js") if item.is_file() and item != entry]


def _sha256_descriptor(descriptor: int) -> str:
    """開いた fd から読む。**Path を開き直さない。**"""
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, _HASH_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _provider_argv(
    provider_id: str, model_id: str, workdir: str, reasoning_effort: str | None = None
) -> tuple[str, ...]:
    """Provider ごとの固定 argv。**依頼文も本文もここへ入れない（stdin だけ）。**"""
    if provider_id == "codex":
        return (
            "exec",
            "--json",
            "--model",
            model_id,
            "--sandbox",
            "read-only",
            # Owner承認済み。子CLIの昇格要求を拒否し、Harnessの承認は維持する。
            "-c",
            'approval_policy="never"',
            "--cd",
            workdir,
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--color",
            "never",
            *_codex_restriction_argv(),
            *(
                ("-c", "model_reasoning_effort=" + json.dumps(reasoning_effort))
                if reasoning_effort is not None
                else ()
            ),
            # `-` を渡すと PROMPT を stdin から読む（`codex exec --help` 実測）。
            "-",
        )
    if provider_id == "claude":
        return (
            "--print",
            "--output-format",
            "json",
            "--model",
            model_id,
            *(("--effort", reasoning_effort) if reasoning_effort is not None else ()),
            "--tools",
            "",
            "--restricted",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--permission-mode",
            "manual",
            "--permission-prompts",
            "none",
        )
    if provider_id == "gemini":
        return (
            "--output-format",
            "json",
            "--model",
            model_id,
            "--approval-mode",
            "plan",
            "--skip-trust",
            "--extensions",
            "none",
            # stdin の内容の後ろへ連結される固定文（`gemini --help` 実測）。
            "--prompt",
            _GEMINI_PROMPT,
        )
    raise HarnessError(ErrorCode.RUNTIME_SPEC_MISMATCH, "provider has no verified argv profile")


_SANDBOX_LINE: Final[str] = (
    "Harness が Landlock（Filesystem）と PID namespace（子孫 Process）の境界を掛けてから "
    "exec する。Workspace・DB・CAS・利用者の Home は読めない。境界は execve を越えて "
    "継承され、setsid した子孫も namespace から出られない"
)

#: 起動前に必ず測る、という宣言。**測れなければ起動しない。**
_GATE_LINE: Final[str] = (
    "起動前に合成 canary で read / write / truncate / O_TRUNC / 作成 / rename / unlink を "
    "実際に試し、拒否できたことを確かめる。1 つでも通れば Provider を起動しない"
)

_RESTRICTIONS: Final[dict[str, tuple[str, ...]]] = {
    "codex": (
        _SANDBOX_LINE,
        _GATE_LINE,
        "--disable で shell_tool / apps / browser_use / plugins / hooks 等を無効化し、"
        "起動前に features list で false を実測する",
        "--sandbox read-only + approval_policy=never（書込み・昇格要求を拒否）",
        "--ignore-user-config（~/.codex/config.toml を読まない。認証だけ CODEX_HOME を使う）",
        "--ignore-rules（user/project の execpolicy .rules を読まない）",
        "--ephemeral（session file を残さない）",
        "-c tools.web_search=false / mcp_servers={} / hooks={}",
        "cwd は空の中立 Directory。Workspace を渡さない",
        "入力は stdin だけ。argv と env に本文を入れない",
    ),
    "claude": (
        _SANDBOX_LINE,
        _GATE_LINE,
        "--restricted（コマンド実行系 tool を外し、user/project/local settings を無視）",
        '--tools ""（組込み tool を全て無効化し、オフライン診断で拒否を確認）',
        "--strict-mcp-config（--mcp-config 未指定なので MCP server 0 件）",
        "--disable-slash-commands（skill を無効化）",
        "--permission-mode manual + --permission-prompts none（prompt は自動拒否）",
        "--no-session-persistence（session を保存しない）",
        "cwd は空の中立 Directory。Workspace を渡さない",
    ),
    "gemini": (
        _SANDBOX_LINE,
        _GATE_LINE,
        "--approval-mode plan（read-only mode）",
        "--skip-trust（folder trust による approval-mode 上書きを避ける）",
        "--extensions none（extension を読み込まない）",
        "GEMINI_CLI_SYSTEM_SETTINGS_PATH で tools.core=[] / mcpServers={} を強制",
        "cwd は空の中立 Directory。Workspace を渡さない",
    ),
}

#: どの Provider にも共通で残る限界。
#:
#: ここに書いてあることは **緩和の承認ではない。** 1 つ目と 2 つ目（R1-B）は
#: `boundary.UNRESOLVED_DESIGN_BLOCKERS` に対応し、解消するまで Provider は
#: 起動できない（調査: `docs/development/cli-workbench-boundary-fix-20260908/`
#: `R1B-CREDENTIAL-AND-NETWORK.md`）。
_COMMON_RESIDUAL: Final[tuple[str, ...]] = (
    "境界の内側にある Provider 自身の資格情報は、モデル由来のコマンドから読まれ得る。"
    "認証主体とモデル由来処理を分離できていない（R1-B・未解決）",
    "通信先を制限していない。Landlock の Network 規則は ABI 4 以降で、"
    "宛先 Host の制限はどの ABI にも無い（R1-B・未解決）",
    "保護対象の変更はreadonly mountで拒否する。書込可能なのは設定済みの作業用領域だけ",
    "子孫の全数確認は PID namespace を特定できたときだけ成り立つ。"
    "特定できなければ UNVERIFIABLE を返し、EFFECT_UNKNOWN として閉じる",
    "実行体の同一性は毎回 Bytes を再 Hash して判定する。最終検査から execve までの"
    "わずかな時間差は残る。改ざん耐性ではなく改ざん検知である",
)

_RESIDUAL_RISKS: Final[dict[str, tuple[str, ...]]] = {
    "codex": (
        "unified_execのfeature表示はtrueだが、生成用設定でshell等が未登録であることを"
        "実要求と不正toolの拒否から検証する。feature表示だけを実行能力の証明にしない",
        "CLI 内部の reconnect と WebSocket→HTTPS fallback は Harness から制御できない。"
        "spawn 1 回は Provider への HTTP 1 回を意味しない（実測で確認）",
        "read-only sandbox は読み取りや通信の禁止ではない",
        "CLI本体を認証・通信の信頼主体とする。資格情報はCLI自身が利用しHarnessは読まない",
        "CLI本体の通信先を独立した仕組みでは制限していない",
        *_COMMON_RESIDUAL[2:],
    ),
    "claude": (
        "managed settings（/etc/claude-code/managed-settings.json）は --restricted でも残る。"
        "存在すれば profile 自体を無効にする",
        "内部の system prompt と再試行は Harness から可視化・制御できない",
        "Owner承認済み: Claude CLI本体を認証・通信の信頼主体とする。"
        "資格情報はCLI自身からアクセス可能で、Harnessは読まない",
        "CLI本体の通信先を独立した仕組みでは制限していない",
        "モデル由来toolは無効化し、外部通信を切った診断で空のtool一覧と不正要求の拒否を確認する",
        *_COMMON_RESIDUAL[2:],
    ),
    "gemini": (
        "未ログインだと対話プロンプトで停止する。timeout で打ち切り EFFECT_UNKNOWN になる",
        "/etc/gemini-cli/settings.json が置かれると system 層が競合する。"
        "存在すれば profile 自体を無効にする",
        "bundle chunk まで同一性へ含めるが、Node 実行時の解決先すべては網羅していない",
        "空のtool宣言と不正toolの拒否を、外部通信不能環境の実CLI要求で検証する",
        "CLI本体を認証・通信の信頼主体とする。資格情報はCLI自身が利用しHarnessは読まない",
        "CLI本体の通信先を独立した仕組みでは制限していない",
        *_COMMON_RESIDUAL[2:],
    ),
}

_LOGIN_HINTS: Final[dict[str, str]] = {
    "codex": "端末で `codex login` を実行する（Harness は資格情報を読まない）",
    "claude": "端末で `claude auth login`（または対話起動して `/login`）を実行する",
    "gemini": "端末で `gemini` を対話起動し、表示される認証手順に従う",
}


def default_manifest_path(state_root: Path) -> Path:
    return state_root / "cli-runtime-profile.json"


def discover_manifest(state_root: Path, cli_runtime_root: Path, home: Path) -> dict[str, Any]:
    """導入済み CLI から profile の雛形を作る。**存在するものだけを書く。**"""
    node = home / ".nvm/versions/node"
    node_bin: str | None = None
    if node.is_dir():
        for version in sorted(node.iterdir(), reverse=True):
            candidate = version / "bin/node"
            if candidate.is_file():
                node_bin = str(candidate)
                break
    modules = cli_runtime_root / "node_modules"
    providers: dict[str, Any] = {}
    codex = modules / "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
    if codex.is_file():
        providers["codex"] = {
            "display_name": "Codex CLI (OpenAI)",
            "package": "@openai/codex",
            "package_version": _package_version(modules / "@openai/codex/package.json"),
            "argv_prefix": [str(codex)],
            "suggested_models": [],
        }
    claude = modules / "@anthropic-ai/claude-code-linux-x64/claude"
    if claude.is_file():
        providers["claude"] = {
            "display_name": "Claude Code CLI (Anthropic)",
            "package": "@anthropic-ai/claude-code",
            "package_version": _package_version(modules / "@anthropic-ai/claude-code/package.json"),
            "argv_prefix": [str(claude)],
            "suggested_models": [],
        }
    gemini = modules / "@google/gemini-cli/bundle/gemini.js"
    if gemini.is_file() and node_bin is not None:
        providers["gemini"] = {
            "display_name": "Gemini CLI (Google)",
            "package": "@google/gemini-cli",
            "package_version": _package_version(modules / "@google/gemini-cli/package.json"),
            "argv_prefix": [node_bin, str(gemini)],
            "suggested_models": [],
        }
    return {
        "contract": CLI_RUNTIME_CONTRACT,
        "home": str(home),
        "neutral_workdir": str(state_root / "workbench-cwd"),
        "state_dir": str(state_root),
        "cli_runtime_root": str(cli_runtime_root),
        "providers": providers,
    }


def _package_version(package_json: Path) -> str | None:
    try:
        raw = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = raw.get("version") if isinstance(raw, dict) else None
    return version if isinstance(version, str) else None
