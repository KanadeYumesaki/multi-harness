"""一度凍結したPlan入力をプロセス間で共有する純粋なBytes形式。"""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from harness.domain.canonical import canonicalize, parse_strict_json
from harness.domain.hashing import ContentHash
from harness.domain.plan import PlanAction, PlanAuthority, PlanBuildInput


def _wire(value: Any) -> Any:
    if isinstance(value, ContentHash):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_wire(item) for item in value]
    return value


def freeze_plan_request(build_input: PlanBuildInput, authority: PlanAuthority) -> bytes:
    # immutable Bytesだけを後続へ渡し、呼出側のnested dict変更から隔離する。
    return canonicalize({"input": _wire(build_input), "authority": _wire(authority)})


def read_plan_request(payload: bytes) -> tuple[PlanBuildInput, PlanAuthority]:
    document = parse_strict_json(payload.decode("utf-8"))
    if not isinstance(document, dict) or set(document) != {"input", "authority"}:
        raise ValueError("invalid plan request")
    data, authority = document["input"], document["authority"]
    if not isinstance(data, dict) or set(data) != {field.name for field in fields(PlanBuildInput)}:
        raise ValueError("invalid plan input")
    if not isinstance(authority, dict) or set(authority) != {
        field.name for field in fields(PlanAuthority)
    }:
        raise ValueError("invalid plan authority")
    actions = data["normalized_actions"]
    if not isinstance(actions, list):
        raise ValueError("invalid actions")
    for action in actions:
        if not isinstance(action, dict) or set(action) != {
            field.name for field in fields(PlanAction)
        }:
            raise ValueError("invalid action")
        action["dependency_keys"] = tuple(action["dependency_keys"])
    data["normalized_actions"] = tuple(PlanAction(**action) for action in actions)
    for name, value in list(data.items()):
        if name.endswith("_hash") and value is not None:
            data[name] = ContentHash.parse(value)
    return PlanBuildInput(**data), PlanAuthority(**authority)
