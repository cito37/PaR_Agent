from __future__ import annotations

import asyncio
import logging
import uuid
from enum import StrEnum
from typing import AsyncGenerator, Optional, List, Dict, Any

from mod.event import (
    Plan, Step, ExecStatus, PlanEvent, StepEvent, 
    ToolEvent, MessageEvent, WaitEvent, ErrorEvent, DoneEvent,TitleEvent, EventUnion
)
from mod.agent import PlanAgent, ReActAgent
from mod.session import SessionRepo, SessionStatus
from mod.memory import RedisMemory, ChatMessage
from mod.mcp import BaseTool

logger = logging.getLogger(__name__)

class FlowStatus(StrEnum):
    IDLE = "idle"              # 等待用户输入
    PLANNING = "planning"      # PlanAgent 正在拆解任务
    EXECUTING = "executing"    # ReActAgent 正在执行当前步骤
    UPDATING = "updating"      # PlanAgent 正在更新剩余计划
    SUMMARIZING = "summarizing" # ReActAgent 正在生成最终汇总
    COMPLETED = "completed"    # 全部完成

class WorkFlow:
    def __init__(
        self,
        session_id: str,
        llm_client,
        redis_client,
        tools: List[BaseTool],
        repo: SessionRepo,
        sandbox_id: Optional[str] = None
    ):
        self.session_id = session_id
        self.repo = repo
        self.sandbox_id = sandbox_id

        self.memory = RedisMemory(
            redis_client=redis_client,
            session_id=session_id,
            llm_client=llm_client  # 允许 RedisMemory 触发自动总结
        )

        # 【工程关键】初始化两个 Agent，并注入相同的 memory 和 llm
        self.planner = PlanAgent(
            session_id=session_id,
            llm=llm_client,
            memory=self.memory
        )
        self.executor = ReActAgent(
            session_id=session_id,
            llm=llm_client,
            memory=self.memory,
            tools=tools
        )
        self.current_plan: Optional[Plan] = None
        self.current_step: Optional[Step] = None
        self.status: FlowStatus = FlowStatus.IDLE
    
    async def invoke(
        self,
        user_message: str,
        files: List[str] = None
    ) -> AsyncGenerator[EventUnion, None]:
        self.status = FlowStatus.PLANNING
        yield MessageEvent(role="user", message=user_message)

        try:
            plan = await self.planner.create_plan(message=user_message, files=files)
            self.current_plan = plan
            yield PlanEvent(plan=plan, status="created")
            yield TitleEvent(title=plan.title)

            await self.memory.add_message(
                ChatMessage(role="assistant", content=f"已生成计划：{plan.model_dump_json()}")
            )

            while not plan.done:
                # 2.1 取出下一个待执行步骤
                step = plan.next_step()
                if not step:
                    break
                self.current_step = step
                self.status = FlowStatus.EXECUTING
                yield StepEvent(step=step, status=ExecStatus.RUNNING)

                executed_step = await self.executor.execute_step( # 调用了react agent的执行
                    plan=plan,
                    step=step,
                    user_message=user_message,
                    files=files
                )

                if executed_step.status == ExecStatus.WAITING:
                    yield WaitEvent()
                    self.status = FlowStatus.IDLE
                    # 等待用户输入后，WorkFlow 将会被外部重新调用
                    return

                for idx, s in enumerate(plan.steps):
                    if s.id == step.id:
                        plan.steps[idx] = executed_step
                        break
                
                yield StepEvent(step=executed_step, status=executed_step.status)

                #存入memory
                memory_snapshot = await self.memory.to_dict()
                await self.repo.save_memory(self.session_id, memory_snapshot)
                logger.info(f"检查点落盘成功，当前记忆条目数: {len(memory_snapshot)}")

                if executed_step.status == ExecStatus.FAILED:
                    yield ErrorEvent(error=f"步骤 {step.id} 执行失败: {executed_step.error}")
                    self.status = FlowStatus.COMPLETED
                    return

                self.status = FlowStatus.UPDATING
                updated_plan = await self.planner.update_plan(plan=plan, step=executed_step)
                self.current_plan = updated_plan
                plan = updated_plan
                yield PlanEvent(plan=plan, status="updated")

            if plan.done:
                self.status = FlowStatus.SUMMARIZING
                summary = await self.executor.summarize(plan)
                yield MessageEvent(role="assistant", message=summary)

                # 将最终总结写入记忆
                await self.memory.add_message(
                    ChatMessage(role="assistant", content=summary)
                )
                # 【最终落盘】执行最后一次检查点落盘
                final_snapshot = await self.memory.to_dict()
                await self.repo.save_memory(self.session_id, final_snapshot)

                # ------------- 阶段 4：完成（COMPLETED） -------------
            self.status = FlowStatus.COMPLETED
            await self.repo.update_status(self.session_id, SessionStatus.COMPLETED)
            yield DoneEvent()

        except Exception as e:
            logger.exception("WorkFlow 执行异常")
            yield ErrorEvent(error=f"工作流异常: {str(e)}")
            self.status = FlowStatus.COMPLETED
            await self.repo.update_status(self.session_id, SessionStatus.COMPLETED)

    async def resume(self, user_message: str) -> AsyncGenerator[EventUnion, None]:
        """
        一句话作用：当触发了 WaitEvent 后，用户回复时调用此方法恢复工作流。
        """
        # 将用户新输入推入记忆
        await self.memory.add_message(ChatMessage(role="user", content=user_message))
        # 将当前状态设为 IDLE，重新调用 invoke 时状态机会从原地恢复
        self.status = FlowStatus.IDLE
        async for event in self.invoke(user_message):
            yield event