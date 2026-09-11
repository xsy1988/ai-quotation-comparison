"""LLM 网关客户端：markdown 围栏剥离、非法 JSON 抛 LLMError。"""

import pytest

from app.llm.client import LLMError, chat_json


class _Usage:
    prompt_tokens = 3
    completion_tokens = 2
    total_tokens = 5


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _Completions:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        return _Response(self._content)


class _Chat:
    def __init__(self, content):
        self.completions = _Completions(content)


class _FakeClient:
    def __init__(self, content):
        self.chat = _Chat(content)


def test_chat_json_strips_markdown_fence():
    client = _FakeClient('```json\n{"a": 1}\n```')
    parsed, usage = chat_json([{"role": "user", "content": "hi"}], client=client)
    assert parsed == {"a": 1}
    assert usage["total_tokens"] == 5


def test_chat_json_plain_still_works():
    client = _FakeClient('{"b": 2}')
    parsed, _ = chat_json([], client=client)
    assert parsed == {"b": 2}


def test_chat_json_invalid_raises_llm_error():
    client = _FakeClient("不是 JSON")
    with pytest.raises(LLMError):
        chat_json([], client=client)


def test_chat_json_accepts_top_level_list():
    """OCR 专用模型可能输出裸 JSON 数组；数组是合法结构化输出，放行给调用方处理。"""
    payload = [{"rotate_rect": [1, 2, 3, 4, 90], "text": "a"}]
    import json as _json

    client = _FakeClient(_json.dumps(payload, ensure_ascii=False))
    parsed, _ = chat_json([], client=client)
    assert parsed == payload
