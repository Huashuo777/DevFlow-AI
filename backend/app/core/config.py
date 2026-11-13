from pydantic_settings import BaseSettings, SettingsConfigDict
# Pydantic 是 Python 中一个非常常用的数据校验和数据建模库 【数据定义、解析、校验、转换】


class Settings(BaseSettings):
    app_env: str = "development"
    app_name: str = "DevFlow AI"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001"

    database_url: str = "postgresql+psycopg://devflow:devflow@localhost:5432/devflow"

    github_token: str | None = None
    github_api_base_url: str = "https://api.github.com"

    llm_api_key: str | None = None
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-pro"
    
    rag_llm_enabled: bool = True
    embedding_provider: str = "qwen_local" # 决定走哪条路径
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_dimensions: int = 1024
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    local_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    local_embedding_device: str | None = None
    qwen_embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    dashscope_api_key: str | None = None
    dashscope_embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1" # 本地部署和这个云端服务是并列分支
   
    rerank_api_key: str | None = None
    rerank_base_url: str | None = None
    qwen_rerank_model: str = "qwen3-rerank"
    qwen_retrieval_instruction: str = "Given a user question, retrieve passages that contain the facts needed to answer it."

    milvus_enabled: bool = True
    milvus_uri: str = "http://localhost:19530"
    milvus_token: str | None = None
    milvus_collection: str = "devflow_rag_qwen1024"
    rag_rerank_top_n: int = 24
    
    ragas_enabled: bool = True              # RAGAS：RAG评估配置
    ragas_judge_api_key: str | None = None  # 服务密钥
    ragas_judge_base_url: str | None = None # 服务地址
    ragas_judge_model: str | None = None
    ragas_timeout_seconds: float = 120.0                # 单次评估超时时间
    ragas_judge_max_tokens: int = 8192
    ragas_answer_relevancy_strictness: int = 1          # 答案相关程度的评估严格程度
    ragas_max_cases: int = 5
    ragas_context_precision_min: float = 0.75           # 检索的与上下文的相关精确度的阈值
    ragas_context_recall_min: float = 0.80
    ragas_faithfulness_min: float = 0.90
    ragas_answer_relevancy_min: float = 0.80 
    ragas_agent_goal_accuracy_min: float = 0.80
    ragas_tool_call_accuracy_min: float = 0.90
    ragas_tool_call_f1_min: float = 0.90
    
    context_max_input_tokens: int = 24000               # 允许使用的输入预算
    context_reserved_response_tokens: int = 3000        # 模型回答预留的预算
    context_dynamic_model_window_enabled: bool = True
    context_model_window_tokens: int | None = None
    context_compact_reserved_output_tokens: int = 12000
    context_auto_compact_enabled: bool = True
    context_warning_buffer_tokens: int = 20000          # 触发上下文容量警告的缓冲阈值
    context_auto_compact_buffer_tokens: int = 13000     # 自动压缩使用的缓冲阈值
    context_aggressive_buffer_tokens: int = 8000        # 激进压缩使用的缓冲阈值
    context_manual_buffer_tokens: int = 3000            # 手动压缩使用的缓冲阈值
    context_max_compaction_failures: int = 3
    context_microcompact_keep_recent_messages: int = 8  # 轻量压缩【主要对压缩部分的处理不一样，可能轻量的处理少一点，但正式的处理则直接成摘要了】
    context_compact_keep_recent_messages: int = 8       # 正式压缩
    context_compact_summary_tokens: int = 1800
    context_system_ratio: float = 0.12                  # 不同类型上下文预算分配
    context_memory_ratio: float = 0.22
    context_evidence_ratio: float = 0.34
    context_recent_ratio: float = 0.22
    context_tool_ratio: float = 0.10
    
    workflow_task_timeout_seconds: float = 90.0         # 工作流中单task执行超时时间
   
    prompt_cache_enabled: bool = True                   # 提示词缓存配置
    prompt_cache_key: str | None = "devflow-agent-v1"   # 缓存标识，用于区分缓存分组
    prompt_cache_retention: str | None = None           # 缓存保留策略
    
    repo_checkout_dir: str = ".data/repos"              # 文件存储路径配置【文件系统中】
    upload_dir: str = ".data/uploads"

    token_encryption_key: str = "change-me-in-local-dev" # 加密 Token 等敏感信息使用的密钥
    demo_mode: bool = True
    github_webhook_secret: str | None = None            # GitHub Webhook 签名验证密钥
    feishu_feedback_webhook_url: str | None = None      # 飞书反馈通知 Webhook 地址
    feedback_trace_base_url: str | None = None          # 反馈追踪链接的基础 URL
    
    auto_sync_enabled: bool = True
    auto_sync_interval_seconds: int = 300               # 自动同步间隔
    auto_sync_limit: int = 50                           # 单次同步处理上限

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore") # Pydantic Settings核心机制， 另extra="ignore"：忽略 .env 中存在但 Settings 类没有定义的额外字段
    # model_config：Settings实例化时告诉 Pydantic 应该如何读取配置。【环境变量的优先级高于 .env 文件，而 .env 文件的优先级高于代码默认值】

settings = Settings()
