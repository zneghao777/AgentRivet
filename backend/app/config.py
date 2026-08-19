"""配置中心：集中管理所有配置项（pydantic-settings）。

所有配置项必须在此定义，禁止在业务代码中散落读取 os.environ。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置（从环境变量 / .env 读取）。"""

    # --- 服务 ---
    APP_NAME: str = "agent-harness"
    APP_ENV: str = "dev"  # dev / test / prod
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"

    # --- 安全 ---
    API_KEYS: str = ""  # 逗号分隔的合法 API Key
    DEFAULT_TENANT: str = "default"

    # --- 模型 ---
    LLM_MODEL: str = "deepseek-chat"
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = ""
    LLM_TEMPERATURE: float = 0.7
    LLM_MAX_TOKENS: int = 4096

    # --- 数据库 ---
    DATABASE_URL: str = (
        "postgresql+psycopg://harness:harness@localhost:5432/harness"
    )

    # --- 缓存 ---
    REDIS_URL: str | None = None

    # --- LangGraph ---
    CHECKPOINT_SAVE_MODE: str = "async"  # exit / async / sync
    GRAPH_RECURSION_LIMIT: int = 25
    AGENT_TIMEOUT_SECONDS: float = 120.0
    GRAPH_INTERRUPT_TOOLS: bool = False  # 工具调用前 HITL 审批（预留）

    # --- LangSmith ---
    LANGSMITH_TRACING: bool = True
    LANGSMITH_PROJECT: str = "agent-harness"
    LANGSMITH_ENDPOINT: str = ""
    LANGSMITH_API_KEY: str = ""

    # --- 会话 ---
    SESSION_MAX_HISTORY_MESSAGES: int = 100
    SESSION_MAX_TOKEN_BUDGET: int = 500_000
    MAX_INTERRUPT_PENDING: int = 10

    # --- 多 Agent ---
    MULTI_AGENT_ENABLED: bool = False

    # --- RAG / 向量检索 ---
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_BASE_URL: str = ""  # 缺省复用 LLM_BASE_URL
    EMBEDDING_API_KEY: str = ""  # 缺省复用 LLM_API_KEY
    EMBEDDING_DIM: int = 1536
    RAG_TOP_K: int = 5
    RAG_SCORE_THRESHOLD: float = 0.0
    RAG_CHUNK_SIZE: int = 500
    RAG_CHUNK_OVERLAP: int = 50
    RAG_MAX_FILE_SIZE_MB: int = 20
    RAG_ALLOWED_EXTS: str = ".md,.txt,.pdf,.docx"

    @property
    def embedding_base_url(self) -> str:
        """Embedding 服务地址：缺省复用 LLM_BASE_URL。"""
        return self.EMBEDDING_BASE_URL or self.LLM_BASE_URL

    @property
    def embedding_api_key(self) -> str:
        """Embedding API Key：缺省复用 LLM_API_KEY。"""
        return self.EMBEDDING_API_KEY or self.LLM_API_KEY

    @property
    def rag_allowed_exts_list(self) -> list[str]:
        """解析后的允许上传扩展名列表。"""
        return [e.strip().lower() for e in self.RAG_ALLOWED_EXTS.split(",") if e.strip()]

    @property
    def api_keys_list(self) -> list[str]:
        """解析后的合法 API Key 列表。"""
        return [k.strip() for k in self.API_KEYS.split(",") if k.strip()]

    @property
    def langsmith_project(self) -> str:
        """Project 命名规则：{APP_NAME}-{APP_ENV}。"""
        if self.LANGSMITH_PROJECT and "-" not in self.LANGSMITH_PROJECT:
            return f"{self.APP_NAME}-{self.APP_ENV}"
        return self.LANGSMITH_PROJECT

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
