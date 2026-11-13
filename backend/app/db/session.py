from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase): # SQLalchemy提供的ORM声明式基类
    """为项目中的 ORM 模型提供统一的基类，让 SQLAlchemy 能够识别和管理这些模型对应的数据库表"""
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True) # engine对象
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False) # session工厂
"""
settings.database_url：指定连接哪个数据库，以及采用什么数据库驱动。例如，实际项目可能连接 PostgreSQL 或 SQLit
pool_pre_ping=True：从连接池获取连接时，先检查连接是否仍然有效，减少复用失效连接造成的错误
bind=engine 表示这个工厂创建出的 Session 默认使用前面定义的 engine
autoflush=False	执行查询前，不自动将部分待处理的 ORM 变更 flush 到数据库
autocommit=False	不启用旧式的自动提交模式；事务一般由 Session 按正常事务机制管理
"""

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal() # 实例
    try:
        yield db
    finally:
        db.close()
