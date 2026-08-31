"""Graph RAG extraction calls must use the shared background LLM guard."""

from __future__ import annotations

import pytest

from app.knowledge.graph import llm_client


class _FakeLLM:
    def __init__(self, *, fail_json_mode: bool = False) -> None:
        self.fail_json_mode = fail_json_mode
        self.calls: list[dict] = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.fail_json_mode and kwargs.get("response_format"):
            raise ValueError("response_format is unsupported")
        return type("Response", (), {"content": '{"entities": []}'})()


@pytest.mark.asyncio
async def test_graph_extraction_uses_background_pool(monkeypatch):
    fake = _FakeLLM()
    factory_calls: list[dict] = []

    def _create_llm(*args, **kwargs):
        factory_calls.append({"args": args, "kwargs": kwargs})
        return fake

    monkeypatch.setattr(llm_client, "create_llm", _create_llm)
    content = await llm_client._chat_completion_inner(
        "http://model.test/v1",
        "test-key",
        "graph-model",
        [{"role": "user", "content": "extract"}],
        temperature=0.1,
        max_tokens=512,
        json_mode=True,
    )

    assert content == '{"entities": []}'
    assert factory_calls[0]["kwargs"]["call_channel"] == "background"
    assert factory_calls[0]["kwargs"]["max_tokens"] == 512
    assert fake.calls[0]["kwargs"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_graph_extraction_retries_without_json_mode(monkeypatch):
    fake = _FakeLLM(fail_json_mode=True)
    monkeypatch.setattr(llm_client, "create_llm", lambda *args, **kwargs: fake)

    content = await llm_client._post_chat(
        "http://model.test/v1",
        "test-key",
        {
            "model": "graph-model",
            "messages": [{"role": "user", "content": "extract"}],
            "temperature": 0.1,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
        },
        {},
        json_mode=True,
    )

    assert content == '{"entities": []}'
    assert len(fake.calls) == 2
    assert "response_format" in fake.calls[0]["kwargs"]
    assert fake.calls[1]["kwargs"] == {}
