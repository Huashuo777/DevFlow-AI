""" 读取已保存的原始文档，完成文本提取、切片、向量化，并将切片及向量写入知识库，最后更新处理状态 """
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db.models import KnowledgeSourceDocument
from app.db.session import SessionLocal
from app.services.rag.chunking import chunk_document
from app.services.rag.document_extraction import MAX_DOCUMENT_BYTES, extract_document_text
from app.services.rag.embeddings import embed_knowledge_texts
from app.services.rag.knowledge_base import get_or_create_config
from app.services.rag.vector_store import DocumentPayload, add_documents, delete_document_group

SUPPORTED_KNOWLEDGE_SUFFIXES = {".txt", ".md", ".markdown", ".mdx", ".pdf", ".docx", ".json", ".csv", ".log"}
EMBEDDING_BATCH_SIZE = 64 # 分批调用Embedding服务时 每批次里最多处理的文本数


def extract_text(filename: str, data: bytes) -> str:
    return extract_document_text(filename, data)


def _embed_in_batches(
    texts: list[str],
    *,
    provider: str,
    model: str,
    dimensions: int,
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE): 
        vectors.extend( # 合并每批向量【使用 extend() 而不是 append()，是为了把每批返回的多个向量逐个添加到 vectors 中，而不是将整批结果作为一个额外的嵌套列表添加进去】
            embed_knowledge_texts( 
                texts[start : start + EMBEDDING_BATCH_SIZE], # 每批次最多处理EMBEDDING_BATCH_SIZE条文本，再合并
                provider=provider,
                model=model,
                dimensions=dimensions,
            )
        )
    return vectors #list[list[float]] 【其中每个文本向量可能是高纬度的 故list[float]】


def _set_status(document_id: uuid.UUID, status: str, *, error: str | None = None) -> None:
    db = SessionLocal()
    try:# 从数据库中查询 KnowledgeSourceDocument 类型的记录，主键为 document_id 的对象，并赋值给 document
        document = db.get(KnowledgeSourceDocument, document_id) # 注意区分 这里不是python字典的get 而是db.get
        if document is None:
            return
        document.status = status
        document.error_message = error
        document.updated_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def process_knowledge_document(document_id: uuid.UUID) -> None:
    _set_status(document_id, "parsing") # 先变更状态 开始解析
    db = SessionLocal()
    try:
        # 1.获取文档记录与文件【通过 SessionLocal() 查询[元]数据，读取 stored_path 对应的文件】
        source = db.get(KnowledgeSourceDocument, document_id)
        if source is None:
            return
        with Path(source.stored_path).open("rb") as handle: # 从数据库记录中获取此前保存的文件路径，再读取原始文件
            data = handle.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"knowledge file exceeds the {MAX_DOCUMENT_BYTES} byte limit")
        # 2.提取文本
        text = extract_text(source.name, data)
        if not text.strip():
            raise ValueError("文档中没有可索引的文本")

        # 3.文本切片【chunk_document()，根据知识库配置决定切片长度和重叠长度】
        source.status = "splitting"
        source.character_count = len(text)
        db.commit() # 每次都在开始时更新状态并提交事务 方便追踪
        config = get_or_create_config(db, source.repo_id) # 获取或创建知识库配置
        chunks = chunk_document( # 执行切片
            text,
            source.name, 
            max_chars=config.chunk_size,    # 切片长度配置
            overlap=config.chunk_overlap,   # 相邻切片间重叠长度
        )
        if not chunks:
            raise ValueError("文档切分后没有有效片段")

        # 4.生成向量
        source.status = "embedding"
        source.chunk_count = len(chunks)
        db.commit()
        titles = []
        for index, chunk in enumerate(chunks): # 为每个文本切片（chunk）生成一个便于识别的标题，并将标题保存到 titles 列表中
            label = chunk.get("section_title")
            if not label and chunk.get("page"):
                label = f"Page {chunk['page']}"
            label = str(label or f"Chunk {index + 1}") # 实在没有有效label 就用默认的“Chunk 1”这样的label
            child_suffix = f" ({int(chunk.get('child_index') or 0) + 1})" if chunk.get("child_index") else ""
            titles.append(f"{source.name} · {label}{child_suffix}")
        contents = [str(chunk["content"]) for chunk in chunks] # 生成切片的正文
        
        vectors = _embed_in_batches(
            [f"{title}\n{content}" for title, content in zip(titles, contents)], # 标题正文拼一起
            provider=config.embedding_provider,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
        )
        # 5.组织切片数据并写入知识库
        payloads = [
            DocumentPayload(
                source_type="knowledge_file",
                source_id=source.id,
                title=title,
                content=content,
                metadata={
                    **{key: value for key, value in chunk.items() if key != "content" and value is not None},
                    "display_title": source.name,
                    "filename": source.name,
                    "knowledge_document_id": str(source.id),
                    "checksum": source.checksum,
                    "file_size": source.file_size,
                    "content_type": source.content_type,
                    "chunk_index": index,
                    "chunk_chars": len(content),
                    "total_chunks": len(chunks),
                    "chunk_strategy": "structure_aware_v1",
                },
            )
            for index, (title, content, chunk) in enumerate(zip(titles, contents, chunks))
        ]
        # 先删除源文档之前的切片记录，再添加本次新切片及其向量
        delete_document_group(db, source.repo_id, source.id, source_type="knowledge_file", commit=False)
        add_documents(
            db,
            source.repo_id,
            payloads,
            commit=False,
            vectors=vectors,
            embedding_info={
                "embedding_provider": config.embedding_provider,
                "embedding_model": config.embedding_model,
                "embedding_dimensions": config.embedding_dimensions,
            },
        )
        # 6.更新处理状态
        source.status = "completed"
        source.error_message = None
        source.completed_at = datetime.now(timezone.utc)
        source.updated_at = source.completed_at
        db.commit()
    except Exception as exc: # 失败情况下 回滚数据库事务 并 保存状态和错误信息
        db.rollback() 
        source = db.get(KnowledgeSourceDocument, document_id)
        if source is not None:
            source.status = "failed" 
            source.error_message = str(exc)[:1000]
            source.updated_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()
