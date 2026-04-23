from types import SimpleNamespace

import pytest

from lightrag.llm import openai as openai_module
from lightrag.types import GPTKeywordExtractionFormat


class _TokenTracker:
    def __init__(self):
        self.usages = []

    def add_usage(self, usage):
        self.usages.append(usage)


class _FakeResponsesStream:
    def __init__(self, events):
        self._events = events
        self.closed = False

    def __aiter__(self):
        async def _iterator():
            for event in self._events:
                yield event

        return _iterator()

    async def aclose(self):
        self.closed = True


class _FakeResponsesAPI:
    def __init__(self, captured_calls):
        self.captured_calls = captured_calls

    async def create(self, **kwargs):
        self.captured_calls.append(("create", kwargs))
        if kwargs.get("stream"):
            return _FakeResponsesStream(
                [
                    SimpleNamespace(type="response.output_text.delta", delta="hel"),
                    SimpleNamespace(type="response.output_text.delta", delta="lo"),
                    SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            usage=SimpleNamespace(
                                input_tokens=9,
                                output_tokens=3,
                                total_tokens=12,
                            )
                        ),
                    ),
                ]
            )
        return SimpleNamespace(
            output_text="ok",
            usage=SimpleNamespace(input_tokens=11, output_tokens=2, total_tokens=13),
        )

    async def parse(self, **kwargs):
        self.captured_calls.append(("parse", kwargs))
        return SimpleNamespace(
            output_parsed=GPTKeywordExtractionFormat(
                high_level_keywords=["LightRAG"],
                low_level_keywords=["Limbal"],
            ),
            usage=SimpleNamespace(input_tokens=17, output_tokens=5, total_tokens=22),
        )


class _FakeOpenAIClient:
    def __init__(self, captured_calls):
        self.responses = _FakeResponsesAPI(captured_calls)
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.offline
@pytest.mark.asyncio
async def test_openai_complete_uses_responses_api_when_enabled(monkeypatch):
    captured_calls = []
    token_tracker = _TokenTracker()
    fake_client = _FakeOpenAIClient(captured_calls)

    monkeypatch.setenv("OPENAI_USE_RESPONSES_API", "true")
    monkeypatch.setattr(
        openai_module,
        "create_openai_async_client",
        lambda **_: fake_client,
    )

    result = await openai_module.openai_complete_if_cache(
        model="gpt-5.4-mini",
        prompt="Reply with exactly: ok",
        system_prompt="You are concise.",
        history_messages=[{"role": "assistant", "content": "previous answer"}],
        api_key="test-key",
        base_url="http://localhost:6002/v1",
        token_tracker=token_tracker,
        max_tokens=321,
    )

    assert result == "ok"
    assert token_tracker.usages == [
        {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}
    ]
    assert fake_client.closed is True
    assert captured_calls == [
        (
            "create",
            {
                "model": "gpt-5.4-mini",
                "instructions": "You are concise.",
                "input": [
                    {"role": "assistant", "content": "previous answer"},
                    {"role": "user", "content": "Reply with exactly: ok"},
                ],
                "max_output_tokens": 321,
                "stream": False,
            },
        )
    ]


@pytest.mark.offline
@pytest.mark.asyncio
async def test_openai_keyword_extraction_uses_responses_parse(monkeypatch):
    captured_calls = []
    token_tracker = _TokenTracker()
    fake_client = _FakeOpenAIClient(captured_calls)

    monkeypatch.setenv("OPENAI_USE_RESPONSES_API", "true")
    monkeypatch.setattr(
        openai_module,
        "create_openai_async_client",
        lambda **_: fake_client,
    )

    result = await openai_module.openai_complete_if_cache(
        model="gpt-5.4-mini",
        prompt="Extract keywords",
        api_key="test-key",
        base_url="http://localhost:6002/v1",
        token_tracker=token_tracker,
        keyword_extraction=True,
    )

    assert result == (
        '{"high_level_keywords":["LightRAG"],"low_level_keywords":["Limbal"]}'
    )
    assert token_tracker.usages == [
        {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22}
    ]
    assert captured_calls[0][0] == "parse"
    assert captured_calls[0][1]["text_format"] is GPTKeywordExtractionFormat


@pytest.mark.offline
@pytest.mark.asyncio
async def test_openai_streaming_uses_responses_api_deltas(monkeypatch):
    captured_calls = []
    token_tracker = _TokenTracker()
    fake_client = _FakeOpenAIClient(captured_calls)

    monkeypatch.setenv("OPENAI_USE_RESPONSES_API", "true")
    monkeypatch.setattr(
        openai_module,
        "create_openai_async_client",
        lambda **_: fake_client,
    )

    stream = await openai_module.openai_complete_if_cache(
        model="gpt-5.4-mini",
        prompt="Reply with exactly: hello",
        api_key="test-key",
        base_url="http://localhost:6002/v1",
        stream=True,
        token_tracker=token_tracker,
    )

    chunks = []
    async for chunk in stream:
        chunks.append(chunk)

    assert "".join(chunks) == "hello"
    assert token_tracker.usages == [
        {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}
    ]
    assert captured_calls[0][0] == "create"
    assert captured_calls[0][1]["stream"] is True
    assert fake_client.closed is True
