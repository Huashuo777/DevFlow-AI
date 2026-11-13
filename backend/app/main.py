
"""
整个 FastAPI 后端的应用入口与生命周期管理模块。
主要负责把各个业务路由注册到应用中、配置跨域与异常处理、初始化数据库，以及管理后台定时同步任务。
"""
# 应用运行所需的基础能力
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware # FastAPI 中用于实现 CORS 配置的中间件
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text

from app.api.routes import ( # 不同业务领域的路由实现
    action_drafts,
    chat,
    ci,
    code_graph,
    conversations,
    evaluations,
    feedback,
    issues,
    knowledge,
    project_index,
    pull_requests,
    rag,
    repos, # 此处把repos路由模块导入了
    reports,
    search,
    skills,
    webhooks,
    workspaces,
)
from app.core.config import settings # 配置
from app.db.models import Base       # ORM模型元数据
from app.db.session import engine    # 数据库连接能力
from app.services.scheduler import periodic_repo_sync_loop
from app.services.rag.vector_store import MilvusUnavailableError


# CORS 的作用是控制浏览器能否让【前端页面】【读取跨域请求的响应】，并不是身份认证或权限控制
def _cors_origins() -> list[str]:
    return [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]


def _cors_origin_regex() -> str | None:
    if settings.app_env.lower() == "production": # 生产环境，不额外配置正则匹配规则
        return None
    return r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$" # 非生产环境


# 组装整个应用
def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="0.1.0")

    # 注册全局异常处理器：当请求处理过程中出现 MilvusUnavailableError，FastAPI 会调用这个处理器，把异常转换成 HTTP 503 响应
    @app.exception_handler(MilvusUnavailableError)
    # _request是当前的请求对象，通常是 Starlette 的 Request 对象
    # Starlette 是一个轻量级的 Python Web 框架，而 FastAPI 是建立在 Starlette 之上的框架。Request 则是 Starlette 提供的一个对象，用来表示客户端发来的 HTTP 请求
    async def milvus_unavailable_handler(_request, exc: MilvusUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "service": "milvus", "status": "degraded"},
        )
    
    # 注册跨域中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),  # 允许哪些前端来源[明确列出允许的来源]
        allow_origin_regex=_cors_origin_regex(), # 通过正则表达式匹配允许的来源
        allow_credentials=True,         # 是否允许跨源请求携带凭证，例如 Cookie
        allow_methods=["*"],            # 允许哪些 HTTP 方法，例如 GET、POST、DELETE
        allow_headers=["*"],            # 允许请求携带哪些 HTTP 请求头
    )

    # 注册业务接口【前端发起 HTTP 请求，后端收到后由 FastAPI 匹配路由并调用对应的接口函数，然后执行函数里的操作】
    app.include_router(
        repos.router, # 提供具体接口的路由对象
        prefix="/api/repos", # 该路由下的接口统一添加 URL 前缀
        tags=["repos"] # 组织自动生成的 API 文档
        )
    app.include_router(conversations.router, prefix="/api/repos", tags=["conversations"])
    app.include_router(workspaces.router, prefix="/api/workspaces", tags=["workspaces"])
    app.include_router(issues.router, prefix="/api", tags=["issues"])
    app.include_router(pull_requests.router, prefix="/api", tags=["pull_requests"])
    app.include_router(ci.router, prefix="/api", tags=["ci"])
    app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
    app.include_router(feedback.router, prefix="/api/chat/feedback", tags=["chat_feedback"])
    app.include_router(action_drafts.router, prefix="/api/action-drafts", tags=["action_drafts"])
    app.include_router(reports.router, prefix="/api/reports", tags=["reports"])
    app.include_router(search.router, prefix="/api/search", tags=["search"])
    app.include_router(skills.router, prefix="/api/skills", tags=["skills"])
    app.include_router(knowledge.router, prefix="/api/repos", tags=["knowledge"])
    app.include_router(project_index.router, prefix="/api/repos", tags=["project_index"])
    app.include_router(rag.router, prefix="/api/rag", tags=["rag"])
    app.include_router(code_graph.router, prefix="/api", tags=["code_graph"])
    app.include_router(evaluations.router, prefix="/api/evals", tags=["evaluations"])
    app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name}

    # 最重要的部分：启动事件！
    @app.on_event("startup") # 表示把下面的函数注册为 FastAPI 的启动事件处理函数
    async def create_demo_tables() -> None:
        # 课程项目默认自动建表；生产风格流程会在后续加入 Alembic 迁移。
        Base.metadata.create_all(bind=engine)  
        # 根据 Base.metadata 中登记的表结构信息，通过 engine 连接数据库，创建尚不存在的表，但已存在的表不会根据最新定义升级
        inspector = inspect(engine) # 创建数据库检查器，后续为先检查再修改。
        if "users" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("users")}
            if "role" not in columns:
                with engine.begin() as connection: # 获取数据库连接，并开启一个事务上下文
                    # text(...)：将 SQL 字符串包装成 SQLAlchemy 可执行的 SQL 表达式对象
                    # 通过连接把 SQL 发送给数据库执行
                    connection.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(80) DEFAULT 'owner'"))
        if "repositories" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("repositories")} 
            # 获取 repositories 表中所有字段（列）的名称
            with engine.begin() as connection:
                if "provider" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN provider VARCHAR(80) DEFAULT 'github'"))
                if "api_base_url" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN api_base_url TEXT"))
                if "clone_url" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN clone_url TEXT"))
                if "local_path" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN local_path TEXT"))
                if "checkout_mode" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN checkout_mode VARCHAR(80) DEFAULT 'managed'"))
                if "github_token_encrypted" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN github_token_encrypted TEXT"))
        if "workflow_runs" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("workflow_runs")}
            if "jobs" not in columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE workflow_runs ADD COLUMN jobs JSON"))
        for table_name in ["code_symbols", "code_relations"]:
            if table_name in inspector.get_table_names():
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                with engine.begin() as connection:
                    if "pr_id" not in columns:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN pr_id UUID"))
                    if "pr_number" not in columns:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN pr_number INTEGER"))
        for table_name in ["chat_messages", "chat_sessions", "agent_runs", "evidence_items"]:
            if table_name in inspector.get_table_names():
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                if "conversation_id" not in columns:
                    with engine.begin() as connection:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN conversation_id UUID"))
        for table_name in ["thread_memories", "conversation_memories"]:
            if table_name in inspector.get_table_names():
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                for column_name in ["facts", "tasks", "user_preferences", "repo_context", "citations"]:
                    if column_name not in columns:
                        with engine.begin() as connection:
                            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} JSON"))
        if "documents" in inspector.get_table_names(): # 删除旧版向量字段，调整数据存储结构
            document_columns = {column["name"] for column in inspector.get_columns("documents")}
            if "embedding" in document_columns:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE documents DROP COLUMN embedding"))
        from app.db.session import SessionLocal
        from app.services.conversations import backfill_all_default_conversations
        from app.services.code_analysis import purge_legacy_code_documents
        from app.services.rag.vector_store import delete_milvus_documents, milvus_runtime_status

        db = SessionLocal()
        try:
            backfill_all_default_conversations(db) # 为符合条件的数据补齐默认会话【根据新业务规则，为旧记录补齐过去没有保存的信息】
            if purge_legacy_code_documents(db):    # 清理旧版代码文档
                db.commit()
        finally:
            db.close()
        # 记录 Milvus 状态并清理旧向量数据
        app.state.rag_vector_status = milvus_runtime_status() # 获取 Milvus 运行状态，并将结果存储在应用的 state 中
        delete_milvus_documents(source_types=["code_file"], strict=False) # 调用向量存储服务删除 source_types 属于 code_file 的文档
        # 按配置启动仓库定时同步任务【创建并保存一个独立运行的后台任务】
        if settings.auto_sync_enabled:
            app.state.repo_sync_task = asyncio.create_task(periodic_repo_sync_loop())

    # 关闭事件：停止后台任务
    @app.on_event("shutdown")
    async def stop_background_tasks() -> None:
        task = getattr(app.state, "repo_sync_task", None) # 尝试获取之前保存的task
        if task:
            task.cancel() # 请求取消任务

    return app


app = create_app() # 导入时即已被执行 得到FastAPI应用实例 等 uvicorn 来 call 这个对象


"""
创建并配置 FastAPI 应用，集中注册业务路由、中间件和异常处理器；
在应用启动时初始化数据库结构、执行历史数据兼容与清理，
检查 RAG 向量存储状态，并按配置启动仓库定时同步任务；
在应用关闭时请求取消后台同步任务。
"""