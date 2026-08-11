from __future__ import annotations

from enum import StrEnum
from datetime import datetime
from typing import List, Optional, Any, Union
from pydantic import BaseModel, Field

class ExecStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"  # 新增：等待用户输入

class PlanStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DONE = "done"

class Step(BaseModel):
    """单个步骤的结构定义"""
    id: str
    description: str
    status: ExecStatus = ExecStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    success: bool = False
    files: List[str] = Field(default_factory=list)

class Plan(BaseModel):
    """整体计划的结构定义"""
    id: str
    title: str
    goal: str
    lang: str = "zh"
    steps: List[Step] = Field(default_factory=list)
    message: Optional[str] = None
    status: PlanStatus = PlanStatus.CREATED
    error: Optional[str] = None

    @property
    def done(self) -> bool:
        return all(step.status == ExecStatus.COMPLETED for step in self.steps)

    def next_step(self) -> Optional[Step]:  # 找到一个等待着的plan任务做
        for step in self.steps:
            if step.status == ExecStatus.PENDING:
                return step
        return None


# 事件类
class BaseEvent(BaseModel):
    """所有事件的基类，提供统一的 timestamp 和 id"""
    id: str = Field(default_factory=lambda: datetime.now().isoformat())
    type: str  # 子类必须填写具体类型
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

class PlanEvent(BaseEvent):
    type: str = "plan"
    plan: Plan
    status: PlanStatus

class StepEvent(BaseEvent):
    type: str = "step"
    step: Step
    status: ExecStatus  # STARTED / DONE / FAILED / WAITING

class ToolEvent(BaseEvent):
    type: str = "tool"
    tool_call_id: str
    tool_name: str
    function_name: str
    function_args: dict
    function_result: Optional[Any] = None
    status: str  # "CALLING" 或 "CALLED"

class MessageEvent(BaseEvent):
    type: str = "message"
    role: str  # "user" 或 "assistant"
    message: str
    files: List[str] = Field(default_factory=list)

class TitleEvent(BaseEvent):
    type: str = "title"
    title: str

class WaitEvent(BaseEvent):
    type: str = "wait"
    """触发该事件时，工作流暂停等待用户输入"""

class ErrorEvent(BaseEvent):
    type: str = "error"
    error: str

class DoneEvent(BaseEvent):
    type: str = "done"
    """所有步骤执行完毕，工作流正常结束"""

# 它是告诉程序：“接下来我发出去（或收到）的事件，只能是这 7 种之一，你给我按这 7 种标准来分辨
EventUnion = Union[
    PlanEvent,
    StepEvent,
    ToolEvent,
    MessageEvent,
    WaitEvent,
    ErrorEvent,
    DoneEvent
]