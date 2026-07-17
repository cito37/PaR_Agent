from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, AsyncGenerator, Union

# 依赖模块（按项目已定义的路径导入）
from mod.prompt import (
    SYSTEM_PROMPT,
    PLAN_AGENT_PROMPT,
    CREATE_PLAN_PROMPT,
    UPDATE_PLAN_PROMPT,
    REACT_SYSTEM_PROMPT,
    REACT_EXEC_PROMPT,
    REACT_SUMMARY_PROMPT,
)
from mod.memory import RedisMemory, ChatMessage
from mod.mcp import BaseTool
from mod.llm import OpenAILLM
from mod.event import (
    Plan, Step, PlanEvent, StepEvent, ToolEvent, 
    MessageEvent, ErrorEvent, DoneEvent, WaitEvent
)

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    def __init__(
        self,
        name: str,
        session_id: str,
        llm: OpenAILLM,
        memory: RedisMemory,
        tools: List[BaseTool],
        system_prompt: str = SYSTEM_PROMPT,
    ):
        self.name = name
        self.session_id = session_id
        self.llm = llm
        self.memory = memory
        self.tools = tools
        self.system_prompt = system_prompt
        # 【工程缓存】将工具的 Schema 提前展开为列表，避免每次循环都重新遍历调用 get_tools()
        self._cached_tool_schemas = self._build_tool_schemas()

    def _build_tool_schemas(self) -> List[Dict[str, Any]]:
        """将全部工具（MCP/A2A/Shell/Message）的 Schema 合并成一个列表"""
        schemas = []
        for tool in self.tools:
            schemas.extend(tool.get_tools())
        return schemas
    
    async def invoke(self, query: str) -> AsyncGenerator[Union[PlanEvent, StepEvent, ToolEvent, MessageEvent, ErrorEvent, WaitEvent, DoneEvent], None]:
        # 将用户输入写入记忆
        await self.memory.add_message(ChatMessage(role="user",context=query))
        yield MessageEvent(role="user",message=query)

# 计数，react的循环次数要小于100次
        loop_count = 0
        max_loops = 100
        while loop_count < max_loops:
            loop_count += 1

            try:
                context = await self.memory.build_context()
                messages = [
                    {"role": "system", "content": f"{self.system_prompt}"},
                    {"role": "user", "content": f"当前任务状态：{json.dumps(context.get('task_state', {}))}"},
                    {"role": "user", "content": f"对话摘要：{json.dumps(context.get('summary', {}))}"},
                ]
                for msg in context.get("recent_messages", []):
                    messages.append({"role": msg.role, "content": msg.content})
                
                response = await self.llm.invoke(
                    messages=messages,
                    tools=self._cached_tool_schemas,
                    # 如果当前是 PlanAgent，强制输出 JSON；ReActAgent 则不强制
                    response_format={"type": "json_object"} if self.name == "planner" else None,
                )
                choice = response.choices[0]
                message = choice.message