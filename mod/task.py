from __future__ import annotations

import asyncio
import json
import uuid
import logging
from typing import List, Optional, Dict, Any, AsyncGenerator
from enum import StrEnum
from dataclasses import dataclass

from redis.asyncio import Redis

from mod.workflow import WorkFlow
from mod.event import EventUnion, DoneEvent, ErrorEvent
from mod.session import SessionRepo
from mod.mcp import BaseTool
from mod.llm import OpenAILLM

logger = logging.getLogger(__name__)

class TaskStatus(StrEnum):
    PENDING = "pending"      # 任务已创建，等待执行
    RUNNING = "running"      # 任务正在执行（WorkFlow 运行中）
    COMPLETED = "completed"  # 任务已成功完成
    FAILED = "failed"        # 任务执行失败
    CANCELED = "canceled"    # 任务被主动取消

@dataclass
class Task:
    task_id: str 
    session_id: str
    status: TaskStatus = TaskStatus.PENDING
    error_message: Optional[str] = None

class TaskManager:
    #主要负责对task的创建、workflow执行、
    def __init__(self,
        redis_client: Redis,
        llm_client: OpenAILLM,
        repo: SessionRepo,
        tools: List[BaseTool],
        sandbox_id: Optional[str] = None):
        self._redis = redis_client
        self._llm = llm_client
        self._repo = repo # 会话
        self._tools = tools
        self._sandbox_id = sandbox_id
        # 内存缓存：task_id -> Task 对象（用于快速查询状态，不做持久化）
        self._tasks: Dict[str, Task] = {} #存了当前的task状态（id、状态、错误信息等）
        # 后台运行的任务协程引用
        self._running_tasks: Dict[str, asyncio.Task] = {}

    async def create_task(
        self,
        session_id: str,
        user_message: str,
        files: Optional[List[str]] = None
    ) -> str:
        task_id = str(uuid.uuid4())
        task = Task(task_id=task_id, session_id=session_id)
        self._tasks[task_id] = task

        input_stream_key = f"input:{task_id}"  #这个input只是用来标记的字符
        await self._redis.xadd(
            input_stream_key,  # 往input_stream_key这个流里增加下面的消息
            {
                "type": "user_message",
                "content": user_message,
                "files": json.dumps(files or [])
            }
        )
        

