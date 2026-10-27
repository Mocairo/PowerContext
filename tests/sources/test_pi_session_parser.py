"""Pi archive contract boundaries independent of persistence."""

import json
import random

import pytest

from powercontext.sources.pi_session import PiSessionInput, parse_pi_session


def archive(records, *, leaf=None, session_id="session"):
    header = {"type": "session", "version": 3, "id": session_id}
    content = "\n".join(json.dumps(row, ensure_ascii=False) for row in [header, *records]).encode()
    return PiSessionInput(content=content, leaf_id=leaf)


def message(event_id, parent=None, content="text", role="user", **extra):
    return {
        "type": "message",
        "id": event_id,
        "parentId": parent,
        "message": {"role": role, "content": content, **extra},
    }


def test_all_visible_formats_preserve_raw_records_and_original_whitespace():
    records = [
        message("root", content="  原始\r\n文本\u2028分隔  "),
        message(
            "tools",
            "root",
            [
                {"type": "text", "text": "before"},
                {"type": "thinking", "thinking": "hidden"},
                {"type": "image", "data": "hidden"},
                {"type": "toolCall", "name": "lookup", "id": "call", "arguments": {"z": "中文", "a": {"b": 2}}},
                {"type": "text", "text": "after\n"},
            ],
            "assistant",
            usage={"tokens": 17},
        ),
        message(
            "result",
            "tools",
            [{"type": "text", "text": "原始结果"}],
            "toolResult",
            toolName="lookup",
            toolCallId="call",
        ),
        message("bash", "result", role="bashExecution", command="do not execute", output="output\n"),
        {"type": "compaction", "id": "compact", "parentId": "bash", "summary": " summary ", "firstKeptEntryId": "bash"},
        {"type": "branch_summary", "id": "branch", "parentId": "compact", "summary": "branch summary"},
        {
            "type": "custom_message",
            "id": "custom",
            "parentId": "branch",
            "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
        },
    ]
    events = parse_pi_session(archive(records))
    assert [event.record for event in events] == records
    assert [event.content for event in events] == [
        "user:   原始\r\n文本\u2028分隔  ",
        'assistant: before\n[tool_call lookup call] {"a":{"b":2},"z":"中文"}\nafter\n',
        "toolResult: [tool_result lookup call] 原始结果",
        "bashExecution: $ do not execute\noutput\n",
        "compaction:  summary ",
        "branch_summary: branch summary",
        "custom: one\ntwo",
    ]
    assert all(event.session_id == "session" for event in events)


def test_controls_and_empty_events_connect_branches_and_default_last_node():
    records = [
        message("root"),
        message("left", "root", "left"),
        {"type": "model_change", "id": "control", "parentId": "root"},
        message("invisible", "control", [{"type": "thinking", "thinking": "private"}]),
        message("empty", "invisible", ""),
        message("right", "empty", "right"),
        {"type": "label", "id": "last-control", "parentId": "right"},
    ]
    default = parse_pi_session(archive(records))
    assert [event.event_id for event in default] == ["root", "right"]
    assert default[-1].parent_id == "empty"
    assert parse_pi_session(archive(records, leaf="last-control")) == default
    assert [event.event_id for event in parse_pi_session(archive(records, leaf="left"))] == ["root", "left"]
    assert [event.event_id for event in parse_pi_session(archive(records, leaf="control"))] == ["root"]


def test_bom_crlf_blank_lines_and_header_only():
    value = archive([message("e", content="中文")])
    raw = b"\xef\xbb\xbf\n" + value.content.replace(b"\n", b"\r\n\r\n") + b"\r\n"
    assert parse_pi_session(PiSessionInput(content=raw)) == parse_pi_session(value)
    assert parse_pi_session(archive([])) == ()
    with pytest.raises(ValueError):
        parse_pi_session(archive([], leaf="missing"))


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        1,
        {"type": "control", "id": "missing-parent"},
        {"type": "control", "id": "bad-parent", "parentId": []},
        {"type": "control", "id": "bad-parent", "parentId": 1},
        {"type": "control", "id": "missing-parent", "parentId": "absent"},
        {"type": "control", "id": "self", "parentId": "self"},
        {"type": "control", "id": "root", "parentId": None},
        {"type": "control", "id": "", "parentId": None},
        {"type": "control", "id": 1, "parentId": None},
        {"id": "missing-type", "parentId": None},
    ],
)
def test_invalid_unselected_graph_nodes_fail_before_return(record):
    with pytest.raises(ValueError):
        parse_pi_session(archive([message("root"), record], leaf="root"))


@pytest.mark.parametrize(
    "suffix",
    [
        b"\n{",
        b"\nnull",
        b"\n\xff",
        b'\n{"id":"x","parentId":null,"type":"control","n":NaN}',
        b'\n{"id":"x","parentId":null,"type":"control","n":Infinity}',
        b'\n{"id":"x","parentId":null,"type":"control","n":1e999}',
    ],
)
def test_malformed_trailing_data_never_returns_selected_prefix(suffix):
    valid = archive([message("root")], leaf="root")
    with pytest.raises(ValueError):
        parse_pi_session(valid.model_copy(update={"content": valid.content + suffix}))


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"type": "session", "version": "3", "id": "s"},
        {"type": "session", "version": 3.0, "id": "s"},
        {"type": "session", "version": 3, "id": ""},
        {"type": "session", "version": 3, "id": 123},
    ],
)
def test_header_requires_v3_and_string_identity(header):
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=json.dumps(header).encode()))


def test_long_ancestry_uses_iteration_and_preserves_every_original_event():
    records = [message(str(index), str(index - 1) if index else None) for index in range(5000)]
    assert [event.event_id for event in parse_pi_session(archive(records))] == [str(index) for index in range(5000)]


def test_distinct_roots_select_only_ancestor_path():
    records = [message("a"), message("b"), message("c", "a")]
    assert [event.event_id for event in parse_pi_session(archive(records))] == ["a", "c"]


@pytest.mark.parametrize("back_edge", [None, "early"])
def test_forward_parent_and_two_node_cycle_fail(back_edge):
    records = [message("early", "late"), message("late", back_edge)]
    with pytest.raises(ValueError):
        parse_pi_session(archive(records))


@pytest.mark.parametrize("content", ["", [], [{"type": "image", "data": "invisible"}]])
def test_empty_tool_results_remain_graph_nodes_without_manufactured_text(content):
    records = [
        message("empty", content=content, role="toolResult", toolName="read", toolCallId="call"),
        message("tail", "empty"),
    ]
    events = parse_pi_session(archive(records))
    assert [event.event_id for event in events] == ["tail"]
    assert events[0].parent_id == "empty"


def test_seeded_forests_preserve_each_selected_original_path():
    generator = random.Random(20261027)  # noqa: S311 - reproducible test inputs
    for case in range(300):
        records, expected_paths = [], {None: []}
        for index in range(generator.randrange(1, 45)):
            event_id = f"{case}-{index}"
            parent = generator.choice(list(expected_paths))
            kind = generator.choice(["user", "assistant", "compaction", "branch_summary", "control"])
            text = f"evidence {event_id}"
            if kind in {"user", "assistant"}:
                record = message(event_id, parent, text, kind)
            else:
                record = {"id": event_id, "parentId": parent, "type": kind, "summary": text}
            records.append(record)
            expected_paths[event_id] = [*expected_paths[parent]]
            if kind != "control":
                expected_paths[event_id].append((event_id, f"{kind}: {text}", record))
        leaf = generator.choice(list(expected_paths))
        events = parse_pi_session(archive(records, leaf=leaf))
        expected = expected_paths[records[-1]["id"] if leaf is None else leaf]
        assert [(event.event_id, event.content, event.record) for event in events] == expected
