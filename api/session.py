from __future__ import annotations

import asyncio
import uuid
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

from mod.session import SessionRepo, Session, SessionStatus, get_db_engine_and_factory, SessionModel
from mod.task import TaskManager
from mod.llm import OpenAILLM, AppConfigService
from mod.mcp import BaseTool
from api.res import Response


router = APIRouter(prefix="/session", tags=["会话模式"])

async def get_repo() -> SessionRepo:
    raise NotImplementedError("请从 main.py 注入 SessionRepo")

async def get_task_manager() -> TaskManager:
    raise NotImplementedError("请从 main.py 注入 TaskManager")

@router.post("/")
async def create_session(
    title: Optional[str] = None,
    repo: SessionRepo = Depends(get_repo)
) -> Response[Session]:
    session_id = str(uuid.uuid4())
    session = Session(
        id=session_id,
        title=title or "新对话",
        status=SessionStatus.PENDING
        
    )
    await repo.save(session)
    return Response.success(data=session)

# 获取会话列表
@router.get("/")
async def list_sessions(
    limit: int = 50,
    offset: int = 0,
    repo: SessionRepo = Depends(get_repo)
) -> Response[List[Session]]:
    sessions = await repo.get_all(limit=limit, offset=offset)
    return Response.success(data=sessions)


# 建立sse长连接，从redis拉取事件推给前端
@router.post("/{session_id}/chat")
async def chat_stream(
    session_id: str,
    message: str,
    files: Optional[List[str]] = None,
    repo: SessionRepo = Depends(get_repo),
    task_manager: TaskManager = Depends(get_task_manager)
):
    session = await repo.get_by_id(session_id) # 根据id查会话存在不
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    await repo.update_status(session_id, SessionStatus.RUNNING)
    task_id = await task_manager.create_task(session_id, message, files)


    async def event_generator():
        last_event_id = "0"  # Redis Stream 初始读取 ID
        try:
            while True:
                # 从 Redis 输出流中拉取新事件（非阻塞，每 0.5 秒轮询一次）
                events, new_last_id = await task_manager.get_events_from_stream(task_id, last_event_id)
                if events:
                    for evt in events:
                        # 解析事件数据
                        data = evt["data"]
                        # 如果收到 __done__ 标记，结束流
                        if data.get("status") == "__done__":
                            await repo.update_status(session_id, SessionStatus.COMPLETED)
                            yield {"event": "done", "data": "任务完成"}
                            return
                        # 如果收到 __error__ 标记，结束流并报错
                        if data.get("status") == "__error__":
                            await repo.update_status(session_id, SessionStatus.COMPLETED)
                            yield {"event": "error", "data": data.get("error", "未知错误")}
                            return
                        # 正常事件推送
                        if "event" in data:
                            yield {"event": "event", "data": data["event"]}
                    last_event_id = new_last_id
                
                # 如果没有新事件，等待 0.5 秒继续轮询（避免高频循环消耗 CPU）
                await asyncio.sleep(0.5)
                
        except asyncio.CancelledError:
            # 前端断开连接时，清理任务资源
            await task_manager.cleanup_task(task_id)
            logger.info(f"SSE 连接断开，已清理任务 {task_id}")
            raise
        finally:
            # 确保连接断开后清理
            await task_manager.cleanup_task(task_id)

    # 返回 SSE 流式响应
    return EventSourceResponse(event_generator())