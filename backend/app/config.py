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

    # --- LangSmith（deprecated：可观测体系已切换到本地 OpenLLMetry，见 OBSERVABILITY_SPEC）---
    LANGSMITH_TRACING: bool = False
    LANGSMITH_PROJECT: str = "agent-harness"
    LANGSMITH_ENDPOINT: str = ""
    LANGSMITH_API_KEY: str = ""

    # --- 可观测性（本地 OpenLLMetry，OBSERVABILITY_SPEC §2.7）---
    OBSERVABILITY_ENABLED: bool = True      # 总开关：tracing / 本地落库 / 聚合
    TRACE_SAMPLE_RATE: float = 1.0          # 采样率（高并发可调 0.1）
    TRACE_RETENTION_DAYS: int = 30          # span/trace 保留天数（后台清理）
    LOG_RETENTION_DAYS: int = 14            # 结构化日志保留天数
    HOURLY_RETENTION_DAYS: int = 90         # 小时聚合保留天数
    DAILY_RETENTION_DAYS: int = 365         # 日聚合保留天数
    AUDIT_RETENTION_DAYS: int = 365         # 审计日志保留天数
    METRICS_AGG_CRON: str = "0 5 0 * * *"   # 每日聚合 cron 表达式（简单轮询实现）
    # 内容采集模式：metadata_only / redacted / full（§6.3）
    CONTENT_CAPTURE_MODE: str = "metadata_only"
    REDACTION_VERSION: str = "1"            # 脱敏管线版本
    OBS_QUEUE_CAPACITY: int = 10_000        # 采集队列容量（§5.5）
    OBS_WRITE_BATCH_MAX: int = 100          # 每批最大条数
    OBS_WRITE_BATCH_MS: int = 200           # 每批最大等待毫秒
    OBS_LOG_MIN_LEVEL: str = "WARNING"      # 结构化日志转发最低级别（§8.1）
    OTLP_EXPORT_ENABLED: bool = False       # 可选 OTLP 导出（§5.6）
    OTLP_ENDPOINT: str = ""
    # 默认 SLO（§10.2）
    SLO_ERROR_RATE_BUDGET: float = 0.05     # 15min 错误率阈值
    SLO_P95_LATENCY_MS: int = 15_000        # 15min P95 延迟阈值
    SLO_P95_TTFT_MS: int = 3_000            # 15min P95 TTFT 阈值
    SLO_COST_BUDGET: float = 0.0            # 1h 成本预算（0=禁用）
    SLO_TOOL_ERROR_RATE: float = 0.10       # 工具错误率阈值
    SLO_TOOL_MIN_CALLS: int = 20            # 工具错误率最小样本
    SLO_QUALITY_REGRESSION: float = 0.15    # 质量相对 7 日基线下降幅度
    SLO_QUEUE_USAGE: float = 0.80           # 采集队列使用率阈值

    # --- 会话 ---
    SESSION_MAX_HISTORY_MESSAGES: int = 100
    SESSION_MAX_TOKEN_BUDGET: int = 500_000
    MAX_INTERRUPT_PENDING: int = 10

    # --- Skill 资源读取 ---
    SKILL_FILE_READ_MAX_BYTES: int = 256 * 1024

    # --- 多 Agent ---
    MULTI_AGENT_ENABLED: bool = False

    # --- RAG / 向量检索（BGE-M3 本地，1024 维）---
    EMBEDDING_MODEL: str = "text-embedding-bge-m3"
    EMBEDDING_BASE_URL: str = "http://192.168.31.185:1234/v1"
    EMBEDDING_API_KEY: str = "local-bge-m3"  # 无鉴权占位
    EMBEDDING_DIM: int = 1024
    RAG_TOP_K: int = 5
    RAG_SCORE_THRESHOLD: float = 0.0
    RAG_CHUNK_SIZE: int = 500
    RAG_CHUNK_OVERLAP: int = 50
    RAG_MAX_FILE_SIZE_MB: int = 20
    RAG_ALLOWED_EXTS: str = ".md,.txt,.pdf,.docx"

    # --- GLM 文件解析（异步；multipart 直传文件，图片自动走 OCR）---
    GLM_FILES_API_KEY: str = ""  # 智谱 key，空则回退本地解析
    GLM_FILES_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    GLM_FILES_MODEL: str = "glm-files-parser"
    # lite|expert|prime；expert 仅支持 pdf，图片 OCR 需 prime 或 lite
    GLM_FILES_TOOL: str = "prime"
    GLM_FILES_POLL_SECONDS: int = 3  # 异步轮询间隔
    GLM_FILES_TIMEOUT_SECONDS: int = 180  # 单文件解析超时

    # --- 上传 / 解析 ---
    KB_UPLOAD_DIR: str = "data/uploads"  # 原文件落盘目录
    KB_PUBLIC_BASE_URL: str = ""  # 预留：文件对外下载地址前缀（GLM 为 multipart 直传，无需 URL）

    # --- 混合检索 / Rerank / Query 改写（AGENTIC_RAG_UPGRADE_SPEC §1/§3.1）---
    HYBRID_RETRIEVAL_ENABLED: bool = True      # 总开关：false 时退回纯向量（兼容旧行为）
    KEYWORD_BACKEND: str = "pg_trgm"           # pg_trgm | tsvector（本期仅 pg_trgm，tsvector 预留）
    KEYWORD_TOP_K: int = 20                    # 关键词路召回数
    VECTOR_TOP_K: int = 20                     # 向量路召回数（混合时粗排候选数）
    KEYWORD_SCORE_THRESHOLD: float = 0.08      # pg_trgm similarity 阈值
    RRF_K: int = 60                            # RRF 融合常数（标准 60）

    RERANK_ENABLED: bool = True                # Rerank 总开关（服务不可达自动跳过）
    RERANK_BASE_URL: str = "http://192.168.31.185:1234/v1"   # OpenAI 兼容 /rerank
    RERANK_MODEL: str = "bge-reranker-v2-m3"
    RERANK_TOP_N: int = 5                      # 精排后返回条数
    RERANK_HTTP_TIMEOUT_SECONDS: int = 30
    RERANK_USE_FLAG_EMBEDDING: bool = False    # 进程内 FlagEmbedding fallback（可选依赖）

    QUERY_EXPAND_ENABLED: bool = False         # Query 改写开关（默认关）
    QUERY_EXPAND_TEMPLATE: str = ""            # 可选改写提示词模板，空则用内置

    # --- 长期记忆（MEMORY_SPEC §6/§7） ---
    MEMORY_ENABLED: bool = True            # 全局总开关（熔断用）
    MEMORY_AUTO_INJECT: bool = True        # 对话前自动注入记忆
    MEMORY_SEARCH_TOOL: bool = True        # 注册 memory_search 工具
    MEMORY_TOP_K: int = 5                  # 单次注入/检索上限
    MEMORY_SIM_DUP: float = 0.92           # 判重相似度阈值
    MEMORY_SIM_CONFLICT: float = 0.70      # 冲突调和阈值下限
    MEMORY_CONFIDENCE_PENDING: float = 0.6 # 低于此值进 pending
    MEMORY_DEFAULT_TTL_DAYS: int = 90      # 默认 TTL（settings 表可覆盖）
    MEMORY_EXTRACT_MAX_ITEMS: int = 10     # 单次对话最多提炼条数
    MEMORY_STRENGTHEN_THRESHOLD: int = 3   # 强化阈值：access_count 达到则不因 TTL 过期

    # --- 用户级记忆（MEMORY_SPEC §3.2 / §6.5） ---
    # 未登录阶段的统一占位用户 ID（替换 deps.py 硬编码 "api-user"）
    ANONYMOUS_USER_ID: str = "__anonymous__"

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
