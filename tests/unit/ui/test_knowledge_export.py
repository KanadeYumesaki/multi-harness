"""Exercise the browser's real export parser with synthetic branched ChatGPT JSON."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(
  'src/harness/presentation/local_ui/static/knowledge-import.js', 'utf8');
const context = vm.createContext({});
vm.runInContext(source.split('\nlet knowledgePreview =')[0], context);
const value = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const chats = context.chatGptExportList(value);
  process.stdout.write(JSON.stringify({ok:true, result:context.chatGptCurrentBranch(chats[0])}));
} catch (error) {
  process.stdout.write(JSON.stringify({ok:false}));
}
"""


def exported() -> dict[str, Any]:
    return {
        "title": "合成チャット",
        "current_node": "answer",
        "mapping": {
            "root": {"parent": None, "message": None},
            "request": {
                "parent": "root",
                "message": {
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["合成の質問"]},
                },
            },
            "answer": {
                "parent": "request",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["採用した回答"]},
                },
            },
            "other": {
                "parent": "request",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["別の分岐の回答"]},
                },
            },
        },
    }


def parse(value: Any) -> dict[str, Any]:
    executable = shutil.which("node")
    if executable is None:
        raise RuntimeError("Node is required to verify the actual browser parser")
    # Fixed script; fixture JSON is stdin data, never executable code.
    result = subprocess.run(  # noqa: S603
        [str(Path(executable).resolve()), "-e", SCRIPT],
        cwd=ROOT,
        input=json.dumps(value),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_only_current_branch_is_selected_and_other_branch_is_disclosed() -> None:
    result = parse([exported()])
    assert result["ok"]
    body = result["result"]
    assert "合成の質問" in body["content"] and "採用した回答" in body["content"]
    assert "別の分岐の回答" not in body["content"]
    assert body["other_nodes"] == 1
    assert "分岐外ノード 1" in body["omission_note"]


@pytest.mark.parametrize("fault", ["cycle", "missing", "parent", "current", "format", "empty"])
def test_undetermined_branch_is_rejected(fault: str) -> None:
    value: Any = exported()
    if fault == "cycle":
        value["mapping"]["request"]["parent"] = "answer"
    elif fault == "missing":
        value["mapping"].pop("request")
    elif fault == "parent":
        value["mapping"]["root"]["parent"] = 7
    elif fault == "current":
        value.pop("current_node")
    elif fault == "format":
        value = {"messages": []}
    elif fault == "empty":
        value = []
    assert parse(value)["ok"] is False


def test_images_and_unsupported_messages_are_disclosed_without_fabricating_content() -> None:
    value = exported()
    answer = value["mapping"]["answer"]["message"]
    answer["content"] = {
        "content_type": "multimodal_text",
        "parts": ["画像に添えた文章", {"asset_pointer": "synthetic-image"}],
    }
    value["mapping"]["request"]["message"]["content"] = {"content_type": "unknown"}
    result = parse(value)["result"]
    assert "画像に添えた文章" in result["content"]
    assert "synthetic-image" not in result["content"]
    assert result["omitted_parts"] == 1 and result["omitted_messages"] == 1


def test_original_system_role_is_only_a_reference_label() -> None:
    value = exported()
    value["mapping"]["answer"]["message"]["author"]["role"] = "system"
    content = parse(value)["result"]["content"]
    assert "[system / 元の会話内のラベル・権限なし]" in content
