"""Create disposable synthetic Git inputs for verifier contract tests.

No private repository, owner answer, credential or historical commit is read.
Outputs are test inputs, never Runtime Evidence or a replay of an owner's history.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import audit_readiness_consumers as consumer
import verify_provider_readiness_history as provider_history
import verify_readiness_report_history as consumer_history

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_PACKAGES = ("docs/decision/synthetic-a.json", "docs/decision/synthetic-b.md")


@dataclass(frozen=True)
class SyntheticHistory:
    root: Path
    manifest: dict[str, Any]


def _write(root: Path, name: str, data: bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _json(root: Path, name: str, value: Any) -> None:
    _write(root, name, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def _git(root: Path, *args: str) -> bytes:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00",
    )
    return subprocess.run(  # noqa: S603
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],  # noqa: S607
        cwd=root,
        env=env,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    _git(root, "init", "--quiet", "--initial-branch=fixture")
    _git(root, "config", "user.name", "Synthetic contract fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _write(root, "README.md", b"SYNTHETIC CONTRACT INPUT. NOT OWNER HISTORY OR RUNTIME EVIDENCE.\n")


def _commit(root: Path) -> str:
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "test: synthetic verifier input")
    return _git(root, "rev-parse", "HEAD").decode().strip()


def build_consumer_history(root: Path) -> SyntheticHistory:
    """Two independently generated reports bound to distinct synthetic commits."""
    _init(root)
    sources = [("tools/one.py", "value = 1\n"), ("src/two.py", "value = 2\n")]
    _write(root, consumer.SELF, (ROOT / consumer.SELF).read_bytes())
    report_name = str(consumer.DEFAULT_OUT.relative_to(consumer.ROOT))
    md_name = str(consumer.DEFAULT_MD.relative_to(consumer.ROOT))

    def emit() -> str:
        for name, source in sources:
            _write(root, name, source.encode())
        report = consumer.measure(sources)
        _json(root, report_name, report)
        _write(root, md_name, consumer._markdown(report).encode())
        return _commit(root)

    first = consumer_history.build_record(root, emit())
    package_name = "docs/decision/synthetic-consumer.json"
    _json(root, package_name, {"synthetic": True, "bound_to": {"consumer_audit": first["report"]}})
    sources.append(("src/three.py", "value = 3\n"))
    saved = consumer_history.build_record(root, emit())
    manifest = {
        "version": "1.0",
        "saved_report": saved,
        "package_input": {
            "package": {
                "path": package_name,
                "sha256": consumer_history.sha((root / package_name).read_bytes()),
            },
            "record": first,
        },
    }
    _json(root, "docs/audit/readiness-consumer-gap.history.json", manifest)
    return SyntheticHistory(root, manifest)


def build_provider_history(root: Path) -> SyntheticHistory:
    """Run the actual auditor on minimal synthetic source, then pin all inputs."""
    _init(root)
    for name in (*provider_history.GENERATORS, "tools/verify_provider_readiness_history.py"):
        _write(root, name, (ROOT / name).read_bytes())
    snapshot = {
        "design_version": "9.1",
        "design_sha256": "sha256:" + "a" * 64,
        "registry_snapshot_hash": "sha256:" + "b" * 64,
        "error_codes": [],
        "event_types": [],
        "state_namespaces": {},
    }
    _json(root, "registry-snapshot.json", snapshot)
    _write(root, "CLAUDE.md", b"Synthetic fixture policy, no real owner answers.\n")
    _write(root, "design-v" + snapshot["design_version"] + "-runtime-go.md", b"Synthetic design.\n")
    _write(root, "design-source/registries/route-policy.yaml", b"providers: []\n")
    _json(root, "docs/audit/chat-provider-readiness.json", {"synthetic": True})
    _write(root, "src/harness/__init__.py", b"\n")
    _write(root, "src/harness/synthetic.py", b"answer = 1\n")
    _write(root, "src/harness/infrastructure/provider/mock_provider.py", b"answer = 0\n")
    produced, md = root / "generated.json", root / "generated.md"
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(root / provider_history.GENERATOR),
            "--out",
            str(produced),
            "--md-out",
            str(md),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=60,
    )
    report_name, md_name = provider_history._report_paths()
    shutil.move(produced, root / report_name)
    shutil.move(md, root / md_name)
    for name in PROVIDER_PACKAGES:
        _write(root, name, ("SYNTHETIC INPUT: " + report_name + "\n").encode())
    _write(root, "docs/decision/OWNER-ANSWERS.yaml", b"synthetic: true\n")
    commit = _commit(root)
    manifest = {
        "version": "1.0",
        "saved_report": provider_history.build_record(root, commit),
        "bound_packages": provider_history.package_bindings(root, commit),
        "package_source_commit": commit,
    }
    _json(root, "docs/audit/provider-readiness-v3.history.json", manifest)
    return SyntheticHistory(root, manifest)


def build_canon_history(root: Path) -> SyntheticHistory:
    """Synthetic answer-time canon for resolver and successor-approval contracts."""
    import chat_canon_binding

    _init(root)
    builder_name = "docs/audit/build_codex_chat_canon_binding_review.py"
    files = (
        builder_name,
        "tools/chat_canon_binding.py",
        "tools/design_identity.py",
        "tools/check_codex_chat_canon_compatibility.py",
        "tests/spec_lint/test_codex_chat_canon_binding_lifecycle.py",
        "tests/spec_lint/test_chat_provider_values_dcr.py",
        "tests/spec_lint/test_chat_provider_readiness.py",
        "tests/spec_lint/test_chat_provider_value_input.py",
    )
    for name in files:
        _write(root, name, (ROOT / name).read_bytes())
    versions = ("1.19", "1.20", "1.21", "1.22", "1.23", "1.24")
    history = []
    snapshots = []
    previous = None
    for version in versions:
        name = chat_canon_binding.design_filename(version)
        if previous is not None:
            (root / previous).unlink()
        _write(
            root, name, ("## フェーズ別詳細設計書 v" + version + "（Synthetic Fixture）\n").encode()
        )
        snapshot = {
            "design_version": version,
            "design_sha256": consumer_history.sha((root / name).read_bytes()),
            "registry_snapshot_hash": consumer_history.sha(("registry-" + version).encode()),
            "schema_catalog_hash": consumer_history.sha(("schema-" + version).encode()),
        }
        _json(root, "registry-snapshot.json", snapshot)
        commit = _commit(root)
        history.append(
            {
                "design_version": version,
                "commit": commit,
                "package_design_sha256": snapshot["design_sha256"],
                "matching_path_count": 1,
                "match": True,
            }
        )
        snapshots.append(snapshot)
        previous = name
    module_spec = importlib.util.spec_from_file_location(
        "synthetic_canon_builder", root / builder_name
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("SYNTHETIC_BUILDER_UNAVAILABLE")
    builder = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(builder)
    with_schema = "docs/decision/DCR-CHAT-SYNTHETIC-WITH.json"
    without_schema = "docs/decision/DCR-CHAT-SYNTHETIC-WITHOUT.json"
    _json(root, with_schema, {"package_id": "SYNTHETIC-WITH", **snapshots[0]})
    _json(
        root,
        without_schema,
        {
            "package_id": "SYNTHETIC-WITHOUT",
            **{k: v for k, v in snapshots[1].items() if k != "schema_catalog_hash"},
        },
    )
    for name in (with_schema, without_schema):
        document = json.loads((root / name).read_bytes())
        document.update(
            synthetic=True,
            status="ANSWERED",
            unanswered=[],
            questions=[{"id": "SYNTHETIC", "options": [{"id": "SYNTHETIC-A"}]}],
            answers={"SYNTHETIC": {"choice_id": "SYNTHETIC-A"}},
        )
        _json(root, name, document)
    counts = {
        k: 0
        for k in (
            "all_design_bound_json",
            "all_answered_json",
            "chat_design_bound_json",
            "chat_answered_json",
            "chat_answered_with_schema_catalog_hash",
            "chat_answered_without_schema_catalog_hash",
        )
    }
    audit = {
        "synthetic": True,
        "measurement_basis": {"head": history[-1]["commit"], **snapshots[-1]},
        "historical_design_hash_matches": history[:-1],
        "package_counts": counts,
        "schema_hash_distribution": {"with_hash": [with_schema], "without_hash": [without_schema]},
    }
    _json(root, "docs/audit/codex-chat-canon-binding-review.json", audit)
    _write(
        root,
        "docs/audit/codex-chat-canon-binding-review.md",
        builder.audit_markdown(audit).encode(),
    )
    # These choices are declared only for this synthetic fixture; no user approval is created.
    answers = {}
    for question in builder.QUESTIONS:
        option = next(o for o in question["options"] if o["id"] == question["id"] + "-A")
        answers[question["id"]] = {
            "choice_id": option["id"],
            "label": option["label"],
            "detail": option["detail"],
        }
    package = {
        "synthetic": True,
        "package_id": "SYNTHETIC-CANON-CONTRACT",
        "status": "ANSWERED",
        **snapshots[-1],
        "bound_to": {
            "audit_path": "docs/audit/codex-chat-canon-binding-review.json",
            "audit_sha256": consumer_history.sha(
                (root / "docs/audit/codex-chat-canon-binding-review.json").read_bytes()
            ),
            "measurement_head": history[-1]["commit"],
        },
        "questions": builder.QUESTIONS,
        "answers": answers,
        "unanswered": [],
        "counts": {
            "questions": len(builder.QUESTIONS),
            "answers": len(answers),
            "unanswered": 0,
            "options": sum(len(q["options"]) for q in builder.QUESTIONS),
        },
        "answer_sha256": chat_canon_binding.recorded_answer_digest(
            {k: v["choice_id"] for k, v in answers.items()}
        ),
    }
    _json(root, "docs/decision/DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE.json", package)
    _write(
        root,
        "docs/decision/DCR-CODEX-CHAT-CANON-BINDING-LIFECYCLE.md",
        b"SYNTHETIC ANSWERS, NOT OWNER DECISIONS.\n",
    )
    return SyntheticHistory(root, audit)


def build_bound_audit_fixture(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Synthetic package/report pair sharing an actually committed design and snapshot."""
    fixture = build_canon_history(root)
    package = json.loads((fixture.root / "docs/decision/DCR-CHAT-SYNTHETIC-WITH.json").read_bytes())
    report = {
        key: package[key]
        for key in (
            "design_version",
            "design_sha256",
            "registry_snapshot_hash",
            "schema_catalog_hash",
        )
    }
    report["synthetic"] = True
    report_name = "docs/audit/synthetic-bound-report.json"
    _json(root, report_name, report)
    package["audit_report"] = {
        "path": report_name,
        "sha256": consumer_history.sha((root / report_name).read_bytes()),
    }
    return root, package, report
