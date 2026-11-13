"""负责协调远程数据获取、数据转换、本地数据库写入的业务流程，而具体的 API 请求交给其他模块执行"""
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt_token
from app.db.models import Issue, PRFile, PRReviewComment, PullRequest, Repository, WorkflowRun
from app.schemas.repos import RepoSyncRequest, RepoSyncResponse
from app.services.github.actions import get_workflow_run_logs, list_workflow_run_jobs, list_workflow_runs
from app.services.github.issues import list_issues
from app.services.github.pull_requests import list_pull_request_files, list_pull_request_review_comments, list_pull_requests


def parse_github_datetime(value: str | None) -> datetime | None:
    """将 GitHub 返回的时间字符串转换成 Python 的 datetime 对象"""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def login_of(user: dict | None) -> str | None:
    """ 从字典提取login 【可能就是用户名吧】"""
    return user.get("login") if user else None


# 接收数据库会话、仓库对象和同步配置
async def sync_repository(db: Session, repo: Repository, payload: RepoSyncRequest) -> RepoSyncResponse:
    """执行完整的数据同步业务流程"""
    # 1.准备同步环境 
    if repo.provider not in {"github", "github_compatible"}: # 只允许同步指定仓库类型
        raise HTTPException(status_code=400, detail=f"{repo.provider} sync is not implemented yet")

    synced = {"issues": 0, "pull_requests": 0, "workflow_runs": 0} # 统计处理记录条数
    token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None # 解密token
    base_url = repo.api_base_url 

    # 此处属于配置上的内容，可选同步issue、pr、workflow run
    # 2.同步issue
    if payload.sync_issues:
        try:
            # 根据远程issue ID查找本地记录，存在则更新，不存在则新增
            remote_issues = await list_issues(repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=400, detail=f"GitHub issues sync failed: {exc.response.status_code} {exc.response.text}") from exc
        for item in remote_issues:
            issue = db.query(Issue).filter(Issue.repo_id == repo.id, Issue.github_issue_id == item.get("id")).one_or_none()
            if issue is None:
                issue = Issue(repo_id=repo.id, github_issue_id=item.get("id"), number=item.get("number", 0), title=item.get("title") or "")
                db.add(issue)
            issue.number = item.get("number", issue.number)
            issue.title = item.get("title") or issue.title
            issue.body = item.get("body")
            issue.state = item.get("state") or "open"
            issue.labels = [label.get("name") for label in item.get("labels", []) if label.get("name")]
            issue.author = login_of(item.get("user"))
            issue.assignees = [login_of(user) for user in item.get("assignees", []) if login_of(user)]
            issue.created_at = parse_github_datetime(item.get("created_at"))
            issue.updated_at = parse_github_datetime(item.get("updated_at"))
            issue.closed_at = parse_github_datetime(item.get("closed_at"))
            synced["issues"] += 1
    
    # 3.同步pr 相对复杂 需要维护pr关联数据【关联数据全量重建策略】
    if payload.sync_pull_requests:
        try:
            remote_prs = await list_pull_requests(repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=400, detail=f"GitHub pull requests sync failed: {exc.response.status_code} {exc.response.text}") from exc
        for item in remote_prs:
            pr = db.query(PullRequest).filter(PullRequest.repo_id == repo.id, PullRequest.github_pr_id == item.get("id")).one_or_none()
            if pr is None:
                pr = PullRequest(repo_id=repo.id, github_pr_id=item.get("id"), number=item.get("number", 0), title=item.get("title") or "")
                db.add(pr)
                db.flush() # 注意此处，不同于issue
                # 将当前待处理的数据库变更发送给数据库，但不会提交事务。这里通常是为了让新建的 PR 获得数据库生成的主键 pr.id，以便后续创建关联记录
            pr.number = item.get("number", pr.number) # 创建是创建 更新是更新
            pr.title = item.get("title") or pr.title
            pr.body = item.get("body")
            pr.state = item.get("state") or "open"
            pr.author = login_of(item.get("user"))
            pr.base_branch = (item.get("base") or {}).get("ref")
            pr.head_branch = (item.get("head") or {}).get("ref")
            pr.merged_at = parse_github_datetime(item.get("merged_at"))
            pr.created_at = parse_github_datetime(item.get("created_at"))
            pr.updated_at = parse_github_datetime(item.get("updated_at"))

            db.query(PRFile).filter(PRFile.pr_id == pr.id).delete() # 删除【本地】该 PR 之前的文件记录，再从远程重新获取文件列表并逐条写入
            try:
                # 查【远程】PRfile【List】
                files = await list_pull_request_files(repo.owner, repo.name, pr.number, token=token, base_url=base_url)
            except httpx.HTTPStatusError:
                files = []
            for file_item in files:
                db.add(
                    PRFile(
                        pr_id=pr.id,
                        filename=file_item.get("filename") or "",
                        status=file_item.get("status") or "modified",
                        additions=file_item.get("additions") or 0,
                        deletions=file_item.get("deletions") or 0,
                        patch=file_item.get("patch"),
                    )
                )
            db.query(PRReviewComment).filter(PRReviewComment.pr_id == pr.id).delete() # Review Comments 采用同样的模式
            try:
                review_comments = await list_pull_request_review_comments(repo.owner, repo.name, pr.number, token=token, base_url=base_url)
            except httpx.HTTPStatusError:
                review_comments = [] # maybe 潜在问题：请求失败不应该自动等同于远程数据为空。 更稳妥的做法通常是在请求成功后，再替换本地数据
            for comment_item in review_comments:
                db.add(
                    PRReviewComment(
                        pr_id=pr.id,
                        github_comment_id=comment_item.get("id"),
                        body=comment_item.get("body"),
                        path=comment_item.get("path"),
                        line=comment_item.get("line") or comment_item.get("original_line"),
                        author=login_of(comment_item.get("user")),
                        created_at=parse_github_datetime(comment_item.get("created_at")),
                        updated_at=parse_github_datetime(comment_item.get("updated_at")),
                    )
                )
            synced["pull_requests"] += 1

    # 4.同步workflow run【对应 GitHub Actions 的一次工作流运行记录】
    if payload.sync_workflow_runs:
        try:
            remote_runs = await list_workflow_runs(repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        except httpx.HTTPStatusError:
            remote_runs = []
        for item in remote_runs:
            run = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id, WorkflowRun.github_run_id == item.get("id")).one_or_none()
            if run is None:
                run = WorkflowRun(repo_id=repo.id, github_run_id=item.get("id"), name=item.get("name") or "workflow")
                db.add(run)
            run.name = item.get("name") or run.name
            run.status = item.get("status") or "unknown"
            run.conclusion = item.get("conclusion")
            run.html_url = item.get("html_url")
            run.created_at = parse_github_datetime(item.get("created_at"))
            run.updated_at = parse_github_datetime(item.get("updated_at"))
            # 两层关联信息：
            # 第一层：同步Jobs和Steps
            if run.github_run_id:
                try:
                    jobs = await list_workflow_run_jobs(repo.owner, repo.name, run.github_run_id, token=token, base_url=base_url)
                    run.jobs = [
                        {
                            "id": job.get("id"),
                            "name": job.get("name"),
                            "status": job.get("status"),
                            "conclusion": job.get("conclusion"),
                            "html_url": job.get("html_url"),
                            "started_at": job.get("started_at"),
                            "completed_at": job.get("completed_at"),
                            "steps": [
                                {
                                    "name": step.get("name"),
                                    "status": step.get("status"),
                                    "conclusion": step.get("conclusion"),
                                    "number": step.get("number"),
                                }
                                for step in job.get("steps", []) # notice here~
                            ],
                        }
                        for job in jobs # 是每个job哦~~
                    ]
                except httpx.HTTPStatusError:
                    run.jobs = run.jobs or []
            if run.conclusion == "failure" and run.github_run_id: # 只读取执行结果为失败的工作流日志
                try:
                    run.logs_text = await get_workflow_run_logs(repo.owner, repo.name, run.github_run_id, token=token, base_url=base_url)
                except httpx.HTTPStatusError:
                    run.logs_text = run.logs_text # 工作流附加信息获取失败时，尽量保留已有数据，而不是清空
            synced["workflow_runs"] += 1

    repo.last_sync_at = datetime.now(timezone.utc) # 更新同步时间
    db.flush()  # 刷新数据库
    db.commit() # 提交事务
    return RepoSyncResponse(repo_id=repo.id, status="completed", synced=synced)


async def sync_repository_by_id(repo_id: UUID, payload: RepoSyncRequest) -> RepoSyncResponse | None:
    """根据 ID 查询仓库，并负责创建和关闭数据库会话"""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        repo = db.get(Repository, repo_id)
        if repo is None:
            return None
        return await sync_repository(db, repo, payload)
    finally:
        db.close()

"""
事务边界问题：
虽然最终数据库提交是统一进行的，但前面发生的某些异常会直接抛出，而另一些异常会被捕获并继续执行。
对于直接抛出的异常，当前函数没有显式调用 db.rollback()，是否回滚以及何时回滚还取决于外层调用方的数据库会话管理
"""