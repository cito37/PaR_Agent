from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any, Union
from enum import StrEnum

# SQLAlchemy 异步核心库
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Column, String, JSON, DateTime, Enum, select, delete, update

# Pydantic 数据模型
from pydantic import BaseModel, Field

# 【导入依赖】事件系统与记忆系统
from mod.event import EventUnion
from mod.memory import ChatMessage

logger = logging.getLogger(__name__)

class SessionStatus(StrEnum):
    """一句话作用：定义会话的生命周期状态机"""
    PENDING = "pending"      # 初始创建，或等待用户输入
    RUNNING = "running"      # 正在执行任务（流式输出中）
    WAITING = "waiting"      # 等待用户回复（如触发了 message_ask_user）
    COMPLETED = "completed"  # 所有步骤执行完成

class Session (BaseModel):
    int:str
    sandbox_id : Optional[str] = None
    task_id : Optional[str] = None
    title: str = "新对话"
    latest_message: str = ""                # 最后一条消息的摘要（用于列表展示）
    events: List[Dict[str, Any]] = Field(default_factory=list)  # 全部事件历史（用于前端恢复流式状态）
    files: List[str] = Field(default_factory=list)              # 上传文件/生成文件的路径列表
    memory: List[Dict[str, Any]] = Field(default_factory=list)  # 记忆检查点（同步写入的Redis记忆）
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    status: SessionStatus = SessionStatus.PENDING


class Base(DeclarativeBase):
    pass

# 建立一个数据库的表
class SessionModel(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sandbox_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(255), default="新对话")
    latest_message: Mapped[str] = mapped_column(String(1024), default="")
    # JSON 字段：存储结构化数据（事件列表、文件列表、记忆对象）
    events: Mapped[Dict[str, Any] | List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    files: Mapped[List[str]] = mapped_column(JSON, default=list)
    memory: Mapped[Dict[str, Any] | List[Dict[str, Any]]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus), default=SessionStatus.PENDING)

# 连接数据库
def get_db_engine_and_factory(db_path: str = "sessions.db") -> tuple[AsyncSession, async_sessionmaker]:
    database_url = f"sqlite+aiosqlite:///{db_path}" # 连接数据库url
    engine = create_async_engine(database_url, echo=False) #  echo=False是不把sql执行的语句打印在控制台上
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, async_session


# 对session进行增删改查
class SessionRepo:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def save(self, session: Session) -> Session:  # 先查一下有没有，有就更新，没有就创建
        stmt = select(SessionModel).where(SessionModel.id == session.id)
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            for key, value in session.model_dump().items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            obj = existing

        else:
            db_model = SessionModel(
                    id=session.id,
                    sandbox_id=session.sandbox_id,
                    task_id=session.task_id,
                    title=session.title,
                    latest_message=session.latest_message,
                    events=session.events,
                    files=session.files,
                    memory=session.memory,
                    status=session.status,
                    created_at=datetime.fromisoformat(session.created_at),
                    updated_at=datetime.fromisoformat(session.updated_at)
                )
            self._session.add(db_model)
            obj = db_model

            await self._session.commit()
            await self._session.refresh(obj) 
            return self._to_pydantic(obj)
        
    async def get_all(self, limit: int = 50, offset: int = 0) -> List[Session]:
            """获取所有会话列表，按更新时间倒序排列"""
            stmt = select(SessionModel).order_by(SessionModel.updated_at.desc()).offset(offset).limit(limit)
            result = await self._session.execute(stmt)
            return [self._to_pydantic(row) for row in result.scalars().all()]

    async def get_by_id(self, session_id: str) -> Optional[Session]:
            """根据会话ID查询单条记录"""
            stmt = select(SessionModel).where(SessionModel.id == session_id)
            result = await self._session.execute(stmt)
            db_model = result.scalar_one_or_none()
            return self._to_pydantic(db_model) if db_model else None

    async def delete(self, session_id: str) -> bool:
            """删除指定会话"""
            stmt = delete(SessionModel).where(SessionModel.id == session_id)
            result = await self._session.execute(stmt)
            await self._session.commit()
            return result.rowcount > 0  
    

    # 如果session存在，就直接更改（更新）之前的字段内容就行
    async def update_title(self, session_id: str, title: str) -> bool:
        stmt = update(SessionModel).where(SessionModel.id == session_id).values(title=title)
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount > 0

    async def update_status(self, session_id: str, status: SessionStatus) -> bool:
        stmt = update(SessionModel).where(SessionModel.id == session_id).values(status=status)
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount > 0

    async def update_latest_message(self, session_id: str, message: str) -> bool:
        stmt = update(SessionModel).where(SessionModel.id == session_id).values(latest_message=message)
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount > 0

    async def add_event(self, session_id: str, event: EventUnion) -> bool:
        """
        追加一个事件到事件历史列表。
        EventUnion 是第 5 步定义的联合类型，此处序列化为字典存入 JSON 字段。
        """
        session_obj = await self.get_by_id(session_id)
        if not session_obj:
            return False
        
        # 将 Pydantic 事件转为字典
        event_dict = event.model_dump()
        # 获取现有事件列表并追加
        current_events = session_obj.events
        current_events.append(event_dict)
        
        stmt = update(SessionModel).where(SessionModel.id == session_id).values(events=current_events)
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount > 0

    async def add_file(self, session_id: str, file_path: str) -> bool:
        """向会话的文件列表中添加一条文件路径"""
        session_obj = await self.get_by_id(session_id)
        if not session_obj:
            return False
        
        current_files = session_obj.files
        if file_path not in current_files:
            current_files.append(file_path)
            stmt = update(SessionModel).where(SessionModel.id == session_id).values(files=current_files)
            result = await self._session.execute(stmt)
            await self._session.commit()
            return result.rowcount > 0
        return True

    async def remove_file(self, session_id: str, file_path: str) -> bool:
        """从文件列表中移除一条文件路径"""
        session_obj = await self.get_by_id(session_id)
        if not session_obj:
            return False
        
        current_files = session_obj.files
        if file_path in current_files:
            current_files.remove(file_path)
            stmt = update(SessionModel).where(SessionModel.id == session_id).values(files=current_files)
            result = await self._session.execute(stmt)
            await self._session.commit()
            return result.rowcount > 0
        return True

    async def save_memory(self, session_id: str, memory_dict: Dict[str, Any]) -> bool:
        """
        将记忆系统的快照（由 RedisMemory.to_dict() 生成）存入数据库的 memory 字段。
        这相当于一个"检查点"，用于程序重启后恢复记忆。
        """
        stmt = update(SessionModel).where(SessionModel.id == session_id).values(memory=memory_dict)
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount > 0

    async def get_memory(self, session_id: str) -> Optional[Dict[str, Any]]:
        """读取数据库中的 memory 检查点数据"""
        session_obj = await self.get_by_id(session_id)
        return session_obj.memory if session_obj else None    


# 返回一个干净、安全、全是普通 Python 类型（字符串、列表、字典）的 Session Pydantic 对象。
# 把这个对象交给业务层去用，极其安全，不用担心触发数据库字段的未定义错误。
    def _to_pydantic(self, db_model: SessionModel) -> Session:
        """将 ORM 模型转换为业务层 Pydantic 模型"""
        return Session(
            id=db_model.id,
            sandbox_id=db_model.sandbox_id,
            task_id=db_model.task_id,
            title=db_model.title,
            latest_message=db_model.latest_message,
            events=db_model.events or [],
            files=db_model.files or [],
            memory=db_model.memory or {},
            created_at=db_model.created_at.isoformat(),
            updated_at=db_model.updated_at.isoformat() if db_model.updated_at else datetime.now().isoformat(),
            status=db_model.status
        )