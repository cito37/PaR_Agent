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
    PENDING = "pending"  # 任务已创建，等待执行
    RUNNING = "running"  # 任务正在执行（WorkFlow 运行中）
    COMPLETED = "completed"  # 任务已成功完成
    FAILED = "failed"  # 任务执行失败
    CANCELED = "canceled"  # 任务被主动取消


@dataclass
class Task:
    task_id: str
    session_id: str
    status: TaskStatus = TaskStatus.PENDING
    error_message: Optional[str] = None


class TaskManager:
    # 主要负责对task的创建、workflow执行、
    def __init__(
        self,
        redis_client: Redis,
        llm_client: OpenAILLM,
        repo: SessionRepo,
        tools: List[BaseTool],
        sandbox_id: Optional[str] = None,
    ):
        self._redis = redis_client
        self._llm = llm_client
        self._repo = repo  # 会话
        self._tools = tools
        self._sandbox_id = sandbox_id
        # 内存缓存：task_id -> Task 对象（用于快速查询状态，不做持久化）
        self._tasks: Dict[str, Task] = {}  # 存了当前的task状态（id、状态、错误信息等）
        # 后台运行的任务协程引用
        self._running_tasks: Dict[str, asyncio.Task] = {}

    async def create_task(
        self, session_id: str, user_message: str, files: Optional[List[str]] = None
    ) -> str:
        task_id = str(uuid.uuid4())
        task = Task(task_id=task_id, session_id=session_id)
        self._tasks[task_id] = task

        input_stream_key = f"input:{task_id}"  # 这个input只是用来标记的字符
        await self._redis.xadd(
            input_stream_key,  # 往input_stream_key这个流里增加下面的消息
            {
                "type": "user_message",
                "content": user_message,
                "files": json.dumps(files or []),
            },
        )

        worker_task = asyncio.create_task(
            self._run_workflow(task_id, session_id, user_message, files)
        )
        self._running_tasks[task_id] = worker_task

        logger.info(f"创建任务成功: task_id={task_id}, session_id={session_id}")
        return task_id

    async def _run_workflow(
        self,
        task_id: str,
        session_id: str,
        user_message: str,
        files: Optional[List[str]],
    ) -> None:
        self._tasks[task_id].status = TaskStatus.RUNNING

        flow = WorkFlow(
            session_id=session_id,
            llm_client=self._llm,
            redis_client=self._redis,
            tools=self._tools,
            repo=self._repo,
            sandbox_id=self._sandbox_id,
        )
        output_stream_key = f"output:{task_id}"
        try:
            async for event in flow.invoke(
                user_message, files
            ):  # 引用了workflow的invoke
                event_json = event.model_dump_json()
                await self._redis.xadd(output_stream_key, {"event": event_json})

            await self._redis.xadd(output_stream_key, {"status": "__done__"})
            self._tasks[task_id].status = TaskStatus.COMPLETED
            logger.info(f"任务 {task_id} 执行完成")

        except Exception as e:
            logger.exception(f"任务 {task_id} 执行异常")
            # 发送 __error__ 标记信号
            await self._redis.xadd(
                output_stream_key, {"status": "__error__", "error": str(e)}
            )
            self._tasks[task_id].status = TaskStatus.FAILED
            self._tasks[task_id].error_message = str(e)

        finally:
            # 协程执行完毕，从运行字典中移除引用，释放内存
            self._running_tasks.pop(task_id, None)

    async def get_task_status(self, task_id: str) -> Optional[TaskStatus]:
        # 询任务当前状态
        task = self._tasks.get(task_id)
        return task.status if task else None

    async def get_events_from_stream(  # 从流里拿事件
        self, task_id: str, last_event_id: str = "0"
    ) -> tuple[List[Dict[str, Any]]]:
        output_stream_key = f"output:{task_id}"
        try:
            # XREAD COUNT 10：每次最多取 10 条，防止一次性拉取过多导致内存溢出
            # BLOCK 0：如果没有新消息，立即返回空列表（非阻塞），由上层 SSE 循环控制等待时间
            result = await self._redis.xread(
                {output_stream_key: last_event_id}, count=10, block=0
            )
            if not result:
                return [], last_event_id
            # Redis XREAD 返回的格式是: [[stream_key, [(id, {data}), ...]]]
            stream_data = result[0][1]
            events = []
            latest_id = last_event_id

            for msg_id, msg_data in stream_data:
                latest_id = msg_id
                # 提取事件数据（第 14 步可以判断是 event 字段还是 status 字段）
                events.append({"id": msg_id, "data": msg_data})

            return events, latest_id
        except Exception as e:
            logger.error(f"从 Redis 读取事件失败: {e}")
            return [], last_event_id

    async def cancel_task(self, task_id: str) -> bool:
        # 消正在运行的任务（强制终止后台协程
        if task_id not in self._running_tasks:
            return False

        task = self._running_tasks[task_id]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._tasks[task_id].status = TaskStatus.CANCELED
        self._running_tasks.pop(task_id, None)
        return True

    async def cleanup_task(self, task_id: str) -> None:

        # 句话作用：任务彻底完成后，清理 Redis 中的输入/输出流数据。
        # 常由 API 层在 DoneEvent 或 ErrorEvent 后主动调用，防止 Redis 堆积过期数据。

        try:
            await self._redis.delete(f"input:{task_id}", f"output:{task_id}")
            self._tasks.pop(task_id, None)
            self._running_tasks.pop(task_id, None)
            logger.info(f"已清理任务 {task_id} 的 Redis 数据")
        except Exception as e:
            logger.error(f"清理任务 {task_id} 失败: {e}")
