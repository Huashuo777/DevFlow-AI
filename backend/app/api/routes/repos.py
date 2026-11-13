
"""提供仓库管理相关的 API 接口，并编排仓库连接、同步、查询和删除等业务流程。"""

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import ActionDraft, AgentRun, AgentTaskRun, AgentWorkflowRun, AnalysisResult, AuditLog, ChatFeedback, ChatMessage, ChatSession, CodeRelation, CodeSymbol, Conversation, ConversationMemory, Document, EvidenceItem, Issue, KnowledgeBaseConfig, KnowledgeGraphEdge, KnowledgeSourceDocument, MemoryCandidate, PRFile, PRReviewComment, ProjectIndexState, PullRequest, RecallEvent, Repository, RetrievalTestRun, ThreadMemory, WorkflowRun, Workspace
from app.db.session import get_db
from app.schemas.repos import RepoConnectRequest, RepoConnectResponse, RepoResponse, RepoSyncRequest, RepoSyncResponse
from app.core.config import settings
from app.core.security import encrypt_token
from app.services.github.client import GitHubClient
from app.services.code_analysis import sync_repository_code_analysis_by_id
from app.services.rag.vector_store import delete_milvus_documents
from app.services.repos_sync import sync_repository

router = APIRouter() # 创建一个路由管理对象，用来集中定义一组相关的 API 接口

SUPPORTED_CONNECT_PROVIDERS = {"github", "github_compatible"} # 支持的provider
GITHUB_HOSTS = {"github.com", "www.github.com"}               # host


@dataclass(frozen=True) # 对象创建后不可轻易修改
class GitRemoteInfo:
    owner: str
    repo: str
    clone_url: str
    host: str | None = None
    default_branch: str | None = None


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _resolve_user_path(raw_path: str, field_name: str) -> Path:
    cleaned = raw_path.strip().strip('"').strip("'")
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{field_name} cannot be empty.")
    expanded = os.path.expandvars(os.path.expanduser(cleaned)) # 扩展 ~ 及 环境变量中储值
    path = Path(expanded)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _git_output(args: list[str], cwd: Path) -> str: # 通过python运行git
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True, # 非零返回则抛异常CalledProcessError
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,  # 将输出作为文本处理
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail="未找到 git 可执行文件，无法检查本地仓库。") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise HTTPException(status_code=400, detail=f"Failed to inspect local repository: {detail}") from exc
    return result.stdout.strip()


def _parse_git_remote_url(remote_url: str) -> GitRemoteInfo: # 解析git_url
    value = remote_url.strip()
    host: str | None = None
    path = ""
    scp_like = re.match(r"^(?:[^@]+@)?(?P<host>[^:]+):(?P<path>.+)$", value)

    if scp_like and "://" not in value:
        host = scp_like.group("host")
        path = scp_like.group("path")
    else:
        parsed = urlparse(value)
        host = parsed.hostname or parsed.netloc or None
        path = parsed.path

    parts = [
            item.strip() 
            for item in path.replace("\\", "/").strip("/").split("/") 
            if item.strip()]
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail=f"Cannot parse owner/repository from git remote URL: {remote_url}")

    owner = parts[-2]
    repo = re.sub(r"\.git$", "", parts[-1], flags=re.IGNORECASE)
    if not owner or not repo:
        raise HTTPException(status_code=400, detail=f"Cannot parse owner/repository from git remote URL: {remote_url}")
    return GitRemoteInfo(owner=owner, repo=repo, clone_url=value, host=host.lower() if host else None)


def _inspect_local_repository(raw_path: str) -> tuple[Path, GitRemoteInfo]: 
    """本地路径是否位于 Git 仓库中，并提取仓库根目录、关联远程仓库信息和当前分支"""
    path = _resolve_user_path(raw_path, "local_path") # 得到绝对路径
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Local repository path does not exist or is not a directory: {path}")

    # git rev-parse：用于查询 Git 仓库相关信息。
    # --is-inside-work-tree：判断当前目录是否位于 Git 工作树中
    inside_work_tree = _git_output(["rev-parse", "--is-inside-work-tree"], path).lower()
    if inside_work_tree != "true":
        raise HTTPException(status_code=400, detail=f"Local path is not inside a git repository: {path}")

    # 返回当前 Git 仓库工作树的根目录【整个仓库的根目录】
    repo_root = Path(_git_output(["rev-parse", "--show-toplevel"], path)).resolve()
    remote_url = _git_output(["remote", "get-url", "origin"], repo_root)
    remote_info = _parse_git_remote_url(remote_url) # 根据url解析相关信息
    current_branch = _git_output(["branch", "--show-current"], repo_root) or None
    return repo_root, GitRemoteInfo(
        owner=remote_info.owner,
        repo=remote_info.repo,
        clone_url=remote_info.clone_url,
        host=remote_info.host,
        default_branch=current_branch,
    )


def _infer_provider(requested_provider: str | None, remote_host: str | None) -> str:
    provider = requested_provider or "github"
    if remote_host and remote_host not in GITHUB_HOSTS and provider == "github":
        return "github_compatible"
    return provider
""" 应使用哪种仓库服务，通过哪个API地址访问 """

def _effective_api_base_url(provider: str, raw_base_url: str | None, remote_host: str | None) -> str | None:
    if provider == "github":
        return "https://api.github.com"
    base_url = _clean_text(raw_base_url)
    if base_url:
        return base_url.rstrip("/")
    if remote_host and remote_host not in GITHUB_HOSTS:
        return f"https://{remote_host}/api/v3"
    return None


def _clone_target_from_parent(raw_parent: str, repo_name: str) -> Path:
    parent = _resolve_user_path(raw_parent, "clone_parent_dir")
    if parent.exists() and not parent.is_dir(): # 父目录不能是个文件
        raise HTTPException(status_code=400, detail=f"Download parent path is not a directory: {parent}")

    target = (parent / repo_name).resolve() # 计算目标仓库路径
    if target.exists() and not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Download target exists and is not a directory: {target}")
    if target.exists() and not (target / ".git").exists() and any(target.iterdir()):# 路径存在且不存在.git且至少有一个子项
        raise HTTPException(
            status_code=400,
            detail=f"Download target already exists and is not an empty git repository: {target}",
        )
    return target

## 路由负责接请求和安排流程，repos_sync 服务负责实际同步外部数据
""" 核心函数： 仓库连接 的 完整流程 
    @router.post("/connect")：注册一个 POST 接口。
    payload: RepoConnectRequest：FastAPI 将请求数据解析、校验为请求 Schema 对象。
    background_tasks: BackgroundTasks：用于登记后台任务。
    db: Session = Depends(get_db)：注入数据库会话。
    -> RepoConnectResponse：声明函数返回的 Python 类型。【提示】
    response_model=RepoConnectResponse：要求 FastAPI 按该响应模型处理响应数据【配置】
"""
@router.post("/connect", response_model=RepoConnectResponse)  
async def connect_repo(payload: RepoConnectRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> RepoConnectResponse:
    # 1.读取并清理需求参数
    local_path = _clean_text(payload.local_path)
    clone_parent_dir = _clean_text(payload.clone_parent_dir)
    owner = _clean_text(payload.owner)
    repo_name = _clean_text(payload.repo)
    requested_provider = payload.provider or "github"
    token = payload.token or payload.github_token

    # 2.确定连接模式
    checkout_path: Path | None = None
    checkout_mode = "managed"
    remote_info: GitRemoteInfo | None = None

    if local_path: # 模式 A：连接本地仓库
        checkout_path, remote_info = _inspect_local_repository(local_path)
        owner = remote_info.owner
        repo_name = remote_info.repo
        checkout_mode = "local"
    elif not owner or not repo_name: # 模式 B：根据仓库名称连接
        raise HTTPException(status_code=400, detail="Provide either a local repository path or owner/repository.")

    # 3.确定服务类型和API地址
    provider = _infer_provider(requested_provider, remote_info.host if remote_info else None)
    base_url = _effective_api_base_url(provider, payload.api_base_url, remote_info.host if remote_info else None)
    if provider not in SUPPORTED_CONNECT_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"{provider} real connection is not implemented yet. Use demo mode or github_compatible with a GitHub-compatible REST API.",
        )

    # 4.确定克隆目标目录
    if not local_path and clone_parent_dir:
        checkout_path = _clone_target_from_parent(clone_parent_dir, repo_name or "repository")

    # 5.初始化仓库信息，并尝试访问远程API
    full_name = f"{owner}/{repo_name}"
    effective_demo_mode = payload.demo_mode
    fallback_message: str | None = None
    repo_data = {
        "id": None,
        "description": "Demo repository for DevFlow AI",
        "default_branch": remote_info.default_branch if remote_info and remote_info.default_branch else "main",
        "clone_url": remote_info.clone_url if remote_info else f"https://github.com/{owner}/{repo_name}.git",
    }
    if not payload.demo_mode:
        try: # 创建客户端对象并执行仓库信息查询
            repo_data = await GitHubClient(token, base_url).get_repo(owner or "", repo_name or "")
        except Exception as exc:
            if token or not settings.demo_mode: # 只有没给token且全局demo启动，才允许回退本地缓存
                raise HTTPException(status_code=400, detail=f"Repository connection failed: {exc}") from exc
            effective_demo_mode = True # 本次请求最终采用的模式
            fallback_message = (
                f"GitHub verification failed without a token: {exc}. "
                "Added the repository in local cache mode; add a token or retry later for full GitHub sync."
            )

    # 6.创建或更新数据库记录
    repo = db.query(Repository).filter(Repository.full_name == full_name).one_or_none()
    if repo is None:
        repo = Repository(owner=owner or "", name=repo_name or "", full_name=full_name)
        db.add(repo)
    repo.owner = owner or repo.owner
    repo.name = repo_name or repo.name
    repo.provider = provider
    repo.api_base_url = base_url
    if token:
        repo.github_token_encrypted = encrypt_token(token, settings.token_encryption_key)
    repo.description = repo_data.get("description")
    repo.default_branch = repo_data.get("default_branch")
    repo.clone_url = repo_data.get("clone_url") or (remote_info.clone_url if remote_info else f"https://github.com/{owner}/{repo_name}.git")
    repo.local_path = str(checkout_path) if checkout_path else None
    repo.checkout_mode = checkout_mode
    repo.github_id = repo_data.get("id")
    db.commit()      # 提交当前会话中的数据库事务
    db.refresh(repo) # 从数据库重新加载这个对象的状态

    # 7.同步数据，登记后台任务
    sync_result: RepoSyncResponse | None = None
    if not effective_demo_mode:
        sync_result = await sync_repository( # 直接等待执行仓库数据同步
            db,
            repo,
            RepoSyncRequest(
                sync_issues=payload.sync_issues,
                sync_pull_requests=payload.sync_pull_requests,
                sync_workflow_runs=payload.sync_workflow_runs,
                limit=payload.initial_sync_limit,
            ),
        )
        # 登记一个后台任务，但不会在此处执行时立即调用任务函数【把一个函数安排在当前 HTTP 响应发送之后执行，让用户不必一直等待这个任务完成】
        # sync_repository_code_analysis_by_id：登记给后台执行的代码分析函数，参数是仓库 ID
        background_tasks.add_task(sync_repository_code_analysis_by_id, repo.id) 
    elif fallback_message:
        background_tasks.add_task(sync_repository_code_analysis_by_id, repo.id)
    return RepoConnectResponse(
        repo_id=repo.id,
        full_name=repo.full_name,
        provider=repo.provider,
        api_base_url=repo.api_base_url,
        clone_url=repo.clone_url,
        local_path=repo.local_path,
        checkout_mode=repo.checkout_mode,
        default_branch=repo.default_branch,
        connected=True,
        demo_mode=effective_demo_mode,
        message=(
            fallback_message
            or (
                "Demo mode only created a local repository record."
                if effective_demo_mode
                else "Repository connection verified. Initial Issue/PR/CI sync completed; code checkout and graph analysis started."
            )
        ),
        sync_status=sync_result.status if sync_result else ("fallback" if fallback_message else "skipped"),
        synced=sync_result.synced if sync_result else {"issues": 0, "pull_requests": 0, "workflow_runs": 0},
    )


@router.get("", response_model=list[RepoResponse])
async def list_repos(db: Session = Depends(get_db)) -> list[Repository]: # 查询仓库列表
    return (
        db.query(Repository)
        .filter(Repository.provider != "knowledge_base")
        .order_by(Repository.updated_at.desc())
        .all()
    )


@router.post("/{repo_id}/sync", response_model=RepoSyncResponse)
async def sync_repo( # 手动同步仓库
    repo_id: UUID,
    payload: RepoSyncRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RepoSyncResponse:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    result = await sync_repository(db, repo, payload)
    background_tasks.add_task(sync_repository_code_analysis_by_id, repo.id)
    return result
# 路由层负责流程编排，服务层负责具体业务操作


@router.post("/{repo_id}/select")
async def select_repo(repo_id: UUID, db: Session = Depends(get_db)) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    repo.updated_at = datetime.now(timezone.utc) #maybe 只是最后一次使用的 时间？？不一定是修改的时间？
    db.commit()
    return {"repo_id": str(repo.id), "full_name": repo.full_name, "selected": True}


@router.delete("/{repo_id}") # 重点分析：delete不只是repo一条记录，而是所有关联的业务数据
async def delete_repo(repo_id: UUID, db: Session = Depends(get_db)) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")

    # .all() 之前主要是在构建查询，调用 .all() 时才真正执行数据库查询
    issue_ids = [item.id for item in db.query(Issue.id).filter(Issue.repo_id == repo.id).all()]
    pr_ids = [item.id for item in db.query(PullRequest.id).filter(PullRequest.repo_id == repo.id).all()]
    run_ids = [item.id for item in db.query(WorkflowRun.id).filter(WorkflowRun.repo_id == repo.id).all()]
    conversation_ids = [item.id for item in db.query(Conversation.id).filter(Conversation.repo_id == repo.id).all()]
    session_ids = [
        item.id
        for item in db.query(ChatSession.id)
        .filter(
            or_(
                ChatSession.repo_id == repo.id,
                ChatSession.conversation_id.in_(conversation_ids) if conversation_ids else False,
            )
        )
        .all()
    ]
    workflow_ids = [
        item.id
        for item in db.query(AgentWorkflowRun.id)
        .filter(
            or_(
                AgentWorkflowRun.repo_id == repo.id,
                AgentWorkflowRun.conversation_id.in_(conversation_ids) if conversation_ids else False,
            )
        )
        .all()
    ]

    # 删除分析结果和关联数据
    for target_type, ids in [
        ("issue", issue_ids),
        ("pull_request", pr_ids),
        ("workflow_run", run_ids),
    ]:
        if ids:
            db.query(AnalysisResult).filter(
                AnalysisResult.target_type == target_type,
                AnalysisResult.target_id.in_(ids),
            ).delete(synchronize_session=False)
    
    # 处理文件和知识库记录 
    knowledge_sources = (
        db.query(KnowledgeSourceDocument)
        .filter(KnowledgeSourceDocument.repo_id == repo.id)
        .all()
    )
    stored_paths = [Path(source.stored_path) for source in knowledge_sources]

    if pr_ids:
        db.query(PRFile).filter(PRFile.pr_id.in_(pr_ids)).delete(synchronize_session=False)
        db.query(PRReviewComment).filter(PRReviewComment.pr_id.in_(pr_ids)).delete(synchronize_session=False)
    db.query(Document).filter(Document.repo_id == repo.id).delete(synchronize_session=False)
    db.query(RetrievalTestRun).filter(RetrievalTestRun.repo_id == repo.id).delete(synchronize_session=False)
    db.query(KnowledgeSourceDocument).filter(KnowledgeSourceDocument.repo_id == repo.id).delete(synchronize_session=False)
    db.query(KnowledgeBaseConfig).filter(KnowledgeBaseConfig.repo_id == repo.id).delete(synchronize_session=False)
    db.query(KnowledgeGraphEdge).filter(KnowledgeGraphEdge.repo_id == repo.id).delete(synchronize_session=False)
    db.query(ProjectIndexState).filter(ProjectIndexState.repo_id == repo.id).delete(synchronize_session=False)
    db.query(CodeRelation).filter(CodeRelation.repo_id == repo.id).delete(synchronize_session=False)
    db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo.id).delete(synchronize_session=False)
    db.query(ActionDraft).filter(ActionDraft.repo_id == repo.id).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.repo_id == repo.id).delete(synchronize_session=False)
    db.query(ChatFeedback).filter(ChatFeedback.repo_id == repo.id).delete(synchronize_session=False)
    db.query(MemoryCandidate).filter(
        or_(
            MemoryCandidate.repo_id == repo.id,
            MemoryCandidate.conversation_id.in_(conversation_ids) if conversation_ids else False,
            MemoryCandidate.session_id.in_(session_ids) if session_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(RecallEvent).filter(
        or_(
            RecallEvent.repo_id == repo.id,
            RecallEvent.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    if workflow_ids:
        db.query(AgentTaskRun).filter(AgentTaskRun.workflow_id.in_(workflow_ids)).delete(synchronize_session=False)
    db.query(AgentWorkflowRun).filter(
        or_(
            AgentWorkflowRun.repo_id == repo.id,
            AgentWorkflowRun.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ChatMessage).filter(
        or_(
            ChatMessage.repo_id == repo.id,
            ChatMessage.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ChatSession).filter(
        or_(
            ChatSession.repo_id == repo.id,
            ChatSession.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ConversationMemory).filter(
        or_(
            ConversationMemory.repo_id == repo.id,
            ConversationMemory.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(ThreadMemory).filter(ThreadMemory.repo_id == repo.id).delete(synchronize_session=False)
    db.query(EvidenceItem).filter(
        or_(
            EvidenceItem.repo_id == repo.id,
            EvidenceItem.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(AgentRun).filter(
        or_(
            AgentRun.repo_id == repo.id,
            AgentRun.conversation_id.in_(conversation_ids) if conversation_ids else False,
        )
    ).delete(synchronize_session=False)
    db.query(Conversation).filter(Conversation.repo_id == repo.id).delete(synchronize_session=False)
    db.query(Issue).filter(Issue.repo_id == repo.id).delete(synchronize_session=False)
    db.query(PullRequest).filter(PullRequest.repo_id == repo.id).delete(synchronize_session=False)
    db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).delete(synchronize_session=False)
    # 更新工作区中的仓库列表
    for workspace in db.query(Workspace).all():
        next_repo_ids = [item for item in (workspace.repo_ids or []) if str(item) != str(repo.id)]
        if next_repo_ids != (workspace.repo_ids or []):
            workspace.repo_ids = next_repo_ids
    db.delete(repo)
    db.commit()
    # 清理向量数据库文档
    milvus_deleted = delete_milvus_documents(repo_id=repo.id, strict=False)
    removed_files = 0
    file_cleanup_errors: list[str] = []
    for path in stored_paths:
        try:
            if path.exists() and path.is_file():
                path.unlink() # 删除磁盘上实际文件
                removed_files += 1
        except OSError as exc:
            file_cleanup_errors.append(f"{path}: {exc}")

    return {
        "repo_id": str(repo_id),
        "deleted": True,
        "cleanup": {
            "milvus_deleted": milvus_deleted,
            "removed_files": removed_files,
            "file_errors": file_cleanup_errors,
        },
    }


@router.get("/{repo_id}/sync-status") # 查询同步状态
async def sync_status(repo_id: UUID, db: Session = Depends(get_db)) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return {
        "repo_id": str(repo.id),
        "status": "completed" if repo.last_sync_at else "not_started", # 恐怕不能区分中间状态 进行/失败/部分成功这些
        "last_sync_at": repo.last_sync_at,
        "message": "最近一次同步已成功完成。" if repo.last_sync_at else "仓库还没有同步过。",
    }
