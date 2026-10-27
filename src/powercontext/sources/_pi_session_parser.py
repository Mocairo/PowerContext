# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Parse Pi v3 archives without discarding evidence preceding summaries."""

from __future__ import annotations

import json
import math
from typing import cast

from pydantic import BaseModel, JsonValue


class PiSessionInput(BaseModel):
    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def _invalid_constant(value: str) -> float:
    raise ValueError(f"Pi records must contain finite JSON numbers: {value}")  # noqa: TRY003


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        return _invalid_constant(value)
    return number


def _string(value: JsonValue, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Pi {field} must be a string")  # noqa: TRY003, TRY004
    return value


def _visible_blocks(value: JsonValue, *, tool_calls: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError("Pi content must be a string or block array")  # noqa: TRY003, TRY004
    parts = []
    for block in value:
        if not isinstance(block, dict):
            raise ValueError("Pi content blocks must be objects")  # noqa: TRY003, TRY004
        if block.get("type") == "text":
            parts.append(_string(block.get("text"), "text"))
        elif tool_calls and block.get("type") == "toolCall":
            name = _string(block.get("name"), "tool name")
            call_id = _string(block.get("id"), "tool call ID")
            arguments = json.dumps(
                block.get("arguments", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            parts.append(f"[tool_call {name} {call_id}] {arguments}")
    return "\n".join(parts)


def _message_body(message: dict[str, JsonValue]) -> tuple[str, str]:
    role = _string(message.get("role"), "message role")
    if not role:
        raise ValueError("Pi message role must not be empty")  # noqa: TRY003
    if role == "bashExecution":
        command = _string(message.get("command", ""), "bash command")
        output = _string(message.get("output", ""), "bash output")
        return role, f"$ {command}\n{output}" if command or output else ""
    body = _visible_blocks(message.get("content"), tool_calls=True)
    if role == "toolResult" and body:
        name = _string(message.get("toolName"), "tool result name")
        call_id = _string(message.get("toolCallId"), "tool result call ID")
        body = f"[tool_result {name} {call_id}] {body}"
    return role, body


def _event_from_record(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    event_type = cast(str, record["type"])
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError("Pi message records must contain a message object")  # noqa: TRY003
        role, body = _message_body(message)
    elif event_type in {"compaction", "branch_summary"}:
        role = event_type
        body = _string(record.get("summary", ""), "summary")
    elif event_type == "custom_message":
        role, body = "custom", _visible_blocks(record.get("content"))
    else:
        return None
    if not body:
        return None
    # JSON decoding and the graph/body checks above establish every field's type.
    # Retain that raw record instead of recursively copying and validating it again.
    return PiEventInput.model_construct(
        session_id=session_id,
        event_id=cast(str, record["id"]),
        parent_id=cast(str | None, record["parentId"]),
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _read_archive(content: bytes) -> tuple[str, dict[str, dict[str, JsonValue]]]:
    lines = (line for line in content.decode("utf-8-sig").split("\n") if line.strip())
    header = json.loads(next(lines, "null"), parse_constant=_invalid_constant, parse_float=_finite_float)
    if (
        not isinstance(header, dict)
        or header.get("type") != "session"
        or type(header.get("version")) is not int
        or header["version"] != 3
        or not isinstance(header.get("id"), str)
        or not header["id"]
    ):
        raise ValueError("Pi archive must begin with a v3 session header and nonempty ID")  # noqa: TRY003
    session_id = header["id"]
    records: dict[str, dict[str, JsonValue]] = {}
    for line in lines:
        record = json.loads(line, parse_constant=_invalid_constant, parse_float=_finite_float)
        if not isinstance(record, dict):
            raise ValueError("Pi archive records must be objects")  # noqa: TRY003, TRY004
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id or event_id in records:
            raise ValueError("Pi event IDs must be unique nonempty strings")  # noqa: TRY003
        if not isinstance(record.get("type"), str) or not record["type"]:
            raise ValueError("Pi records must have a nonempty type")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError("Pi records must include parentId")  # noqa: TRY003
        parent = record["parentId"]
        if parent is not None and (not isinstance(parent, str) or parent not in records):
            raise ValueError("Pi parentId must be null or refer to an earlier event")  # noqa: TRY003
        records[event_id] = record
    return session_id, records


def _parse_archive(
    value: PiSessionInput,
) -> tuple[str, dict[str, dict[str, JsonValue]], tuple[PiEventInput, ...]]:
    """Validate every record before constructing the selected root-to-leaf path."""
    session_id, records = _read_archive(value.content)
    leaf = next(reversed(records), None)
    if value.leaf_id is not None:
        leaf = value.leaf_id
        if leaf not in records:
            raise ValueError("Pi selected leaf does not exist")  # noqa: TRY003

    chain = []
    while leaf is not None:
        record = records[leaf]
        chain.append(record)
        leaf = cast(str | None, record["parentId"])
    events = []
    for record in reversed(chain):
        event = _event_from_record(session_id, record)
        if event is not None:
            events.append(event)
    return session_id, records, tuple(events)


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return all original visible events on the selected Pi v3 branch."""
    return _parse_archive(value)[2]
