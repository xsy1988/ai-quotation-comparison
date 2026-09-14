import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """所有测试用独立临时库，不污染 data/quotes.db。"""
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "test.db"))
    yield


@pytest.fixture(autouse=True)
def isolated_snapshots(tmp_path, monkeypatch):
    """所有测试把落库快照写到临时目录，绝不覆盖 data/snapshots/quote_{id}.json。

    个别用例只隔离了库、忘了隔离快照目录，persist 会按 quote_id 覆盖真实快照（实测
    test_compare_engine 有 4 个用例覆盖了真实 data/snapshots/quote_1.json、quote_2.json，
    导致真实报价单快照被测试数据替换）。此处统一隔离，用例自身再覆盖也无冲突。
    """
    from app import persist as persist_module

    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    yield


@pytest.fixture(autouse=True)
def isolated_object_store(tmp_path, monkeypatch):
    """所有测试把对象存储写到临时目录，不污染 data/object_store/。"""
    from app import storage

    monkeypatch.setenv("OBJECT_STORE_DIR", str(tmp_path / "object_store"))
    storage.set_store(None)
    yield
    storage.set_store(None)


@pytest.fixture(autouse=True)
def isolated_llm_traces(tmp_path, monkeypatch):
    """所有测试把 LLM 轮次留痕写到临时目录，不污染 data/llm_trace/。"""
    from app.pipeline import layout_understand

    monkeypatch.setattr(layout_understand, "LLM_TRACE_DIR", tmp_path / "llm_trace")
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


@pytest.fixture(autouse=True)
def verify_shadow_off_by_default(monkeypatch):
    """测试默认关闭 LLM-B 影子复核（LLM_B_VERIFY=0），避免 verify 额外调用 chat_fn
    干扰既有 pipeline 用例的 mock 调用计数；验证开关行为的用例自行覆盖本 fixture。
    """
    monkeypatch.setenv("LLM_B_VERIFY", "0")
    yield
