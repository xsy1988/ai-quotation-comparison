"""应用配置唯一入口：集中定义全部配置项（含默认值），散点统一 `from app.config import settings`。

.env 加载：项目未引入 python-dotenv（pyproject 依赖里没有），这里做极简解析——
导入本模块时读 backend/.env（KEY=VALUE、# 注释、可选引号），只补 os.environ 中
缺失的键（已有环境变量优先，与 python-dotenv 默认行为一致）。

settings 为惰性属性对象：每次访问实时读 os.environ，因此测试里
monkeypatch.setenv 依旧生效，进程运行期改环境变量也即时可见。
"""

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BACKEND_DIR / ".env"


def _load_dotenv(path: Path = ENV_PATH) -> None:
    """极简 .env 加载：解析 KEY=VALUE 行写入 os.environ（不覆盖已有环境变量）。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


class Settings:
    """惰性配置访问：属性实时读 os.environ（monkeypatch.setenv 兼容）。"""

    # --- 服务监听 ---
    @property
    def host(self) -> str:
        return os.environ.get("HOST", "127.0.0.1")

    @property
    def port(self) -> int:
        return int(os.environ.get("PORT", "8000"))

    # --- 存储 ---
    @property
    def quotes_db_path(self) -> str | None:
        """SQLite 库文件路径覆盖；None 时用默认 backend/data/quotes.db。"""
        return os.environ.get("QUOTES_DB_PATH") or None

    # --- 流水线 ---
    @property
    def parse_concurrency(self) -> int:
        """run_task 文件级并发解析的最大 worker 线程数。"""
        return max(1, int(os.environ.get("PARSE_CONCURRENCY", "3")))

    @property
    def llm_b_verify(self) -> bool:
        """LLM-B 影子复核开关："0" 关闭，其余（含缺省）开启。"""
        return os.environ.get("LLM_B_VERIFY", "1") != "0"

    # --- LLM 网关 ---
    @property
    def llm_base_url(self) -> str:
        return os.environ.get("LLM_BASE_URL", "http://localhost:18080/v1")

    @property
    def llm_api_key(self) -> str:
        return os.environ.get("LLM_API_KEY", "local-demo-key")

    @property
    def llm_timeout(self) -> float:
        return float(os.environ.get("LLM_TIMEOUT", "60"))

    @property
    def llm_model(self) -> str:
        return os.environ.get("LLM_MODEL", "qwen3.8-max")

    @property
    def llm_enable_thinking(self) -> bool:
        return os.environ.get("LLM_ENABLE_THINKING", "0") == "1"


settings = Settings()
