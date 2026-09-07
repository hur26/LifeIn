"""存储层:引擎、会话、ORM 基类。

单一 PostgreSQL + pgvector(ADR-006),不引第二个存储。
分工按 ADR-016:简单 CRUD 用 SQLAlchemy,记忆层那些 JSONB 与向量的分析查询直接写 SQL。

**表结构的真相在迁移脚本里,不在 ORM 类里。** 06 的 DDL 带了若干 CHECK 约束
(facts 必须有 provenance、L3 只能由 user_input 触发、L2 必须有回滚信息),
这些是安全机制,不能靠 ORM 声明去表达 —— 所以迁移写 DDL,ORM 只做映射。
`Base.metadata.create_all()` 在本项目里**永远不要调用**,它会绕过那些约束。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from lifein.config import Settings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    """ORM 基类。所有表都带 user_id —— 铁律 1,P0 只有一个用户也不许省。"""


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is None:
        s = settings or get_settings()
        _engine = create_engine(
            s.database_url,
            pool_pre_ping=True,  # 自托管单机,连接可能被防火墙静默掐断
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """一个工作单元一个事务。

    06 §2.7 有一条硬要求:写入 target_table 与更新 pending_confirmations.status
    必须在**同一个事务**里,否则会出现"确认了但没写进去"或者"写了两次"。
    那类操作一律套在这个上下文里,不要各自 commit。
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """丢弃已建立的引擎。测试用,生产代码不该调。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
