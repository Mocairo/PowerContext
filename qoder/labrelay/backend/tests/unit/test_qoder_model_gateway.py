from types import SimpleNamespace

import pytest

from labrelay.model.qoder_agent import ModelUnavailableError, QoderAgentModelGateway


class FakeQuery:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompt = None
        self.options = None

    async def __call__(self, *, prompt, options):
        self.prompt = prompt
        self.options = options
        yield SimpleNamespace(content=[SimpleNamespace(text=self.text)])


async def test_generate_json_parses_fenced_json_and_uses_safe_options() -> None:
    fake = FakeQuery('```json\n{"headline": "baseline", "next_step": "check cache"}\n```')
    gateway = QoderAgentModelGateway(
        model="qwen3.8-flash",
        query_func=fake,
    )

    result = await gateway.generate_json("Summarize the handoff.", {"objective": "baseline"})

    assert result == {"headline": "baseline", "next_step": "check cache"}
    assert fake.options.model == "qwen3.8-flash"
    assert fake.options.max_turns == 4
    assert fake.options.tools == []
    assert fake.options.setting_sources == []
    assert fake.options.mcp_servers == {}
    assert fake.options.strict_mcp_config is True
    assert fake.options.permission_mode == "dontAsk"
    assert "Bash" in fake.options.disallowed_tools
    assert "objective" in fake.prompt
    assert "baseline" in fake.prompt


async def test_generate_json_rejects_unusable_text() -> None:
    gateway = QoderAgentModelGateway(query_func=FakeQuery("not json"))

    with pytest.raises(ModelUnavailableError):
        await gateway.generate_json("Return JSON.", {"task": "test"})


async def test_empty_model_response_is_unavailable() -> None:
    gateway = QoderAgentModelGateway(query_func=FakeQuery(""))

    with pytest.raises(ModelUnavailableError):
        await gateway.generate_json("Return JSON.", {"task": "test"})


def test_deterministic_embeddings_are_normalized_and_stable() -> None:
    gateway = QoderAgentModelGateway(query_func=FakeQuery("{}"), dimension=16)

    first = gateway.embed(["baseline"])[0]
    second = gateway.embed(["baseline"])[0]
    other = gateway.embed(["other"])[0]

    assert len(first) == 16
    assert first == second
    assert first != other
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6


def test_default_model_is_the_qwen_catalog_key() -> None:
    """Qoder model IDs are catalog keys; ``qfmodel`` is Qwen3.8-Flash."""
    gateway = QoderAgentModelGateway(query_func=FakeQuery("{}"))
    assert gateway.model == "qfmodel"


async def test_duplicated_model_text_still_parses_the_first_object() -> None:
    payload = '{"headline": "baseline", "next_step": "check cache"}'
    gateway = QoderAgentModelGateway(query_func=FakeQuery(payload + payload))

    result = await gateway.generate_json("Return JSON.", {"task": "test"})

    assert result == {"headline": "baseline", "next_step": "check cache"}


async def test_top_level_task_array_is_folded_into_the_tasks_key() -> None:
    gateway = QoderAgentModelGateway(query_func=FakeQuery('[{"stage": 0, "title": "x"}]'))

    result = await gateway.generate_json("Return JSON.", {"stages": []})

    assert result == {"tasks": [{"stage": 0, "title": "x"}]}


async def test_concatenated_task_objects_are_folded_and_deduplicated() -> None:
    text = '{"stage": 0, "title": "a"}{"stage": 0, "title": "a"}{"stage": 1, "title": "b"}'
    gateway = QoderAgentModelGateway(query_func=FakeQuery(text))

    result = await gateway.generate_json("Return JSON.", {"stages": []})

    assert result == {"tasks": [{"stage": 0, "title": "a"}, {"stage": 1, "title": "b"}]}
