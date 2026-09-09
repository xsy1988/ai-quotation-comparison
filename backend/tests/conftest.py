import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """所有测试用独立临时库，不污染 data/quotes.db。"""
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "test.db"))
    yield


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """测试默认禁止真实 LLM 网关调用；需要模拟 LLM 的测试自行 monkeypatch 覆盖本fixture。

    pipeline / mapping_runner 经 app.llm.client.chat_json 调网关，此处替换为抛
    LLMUnavailable，使 LLM 路径自动降级/跳过，不触网、不依赖网关状态。
    """
    from app.llm import client as llm_client
    from app.llm.client import LLMUnavailable

    def _blocked(messages, **kwargs):
        raise LLMUnavailable("测试环境已禁用真实 LLM 调用（conftest no_real_llm）")

    monkeypatch.setattr(llm_client, "chat_json", _blocked)
    yield
