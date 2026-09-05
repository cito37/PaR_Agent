from __future__ import annotations

import uuid
from typing import Any, List, Dict

from mod.mcp import BaseTool, ToolResult
from mod.memory import RedisMemory, ChatMessage
from mod.llm import OpenAILLM

class SubagentTool(BaseTool):

    def __init__(self, llm: OpenAILLM, redis_client):
        self._llm = llm
        self._redis_client = redis_client

    def get_tools(self) -> List[Dict[str, Any]]:
        return [{
            "type": "function",
            "function": {
                "name": "run_subagent",
                "description": "将一段独立的探索任务（如查询函数、读取文件）交给子代理，子代理拥有独立上下文，仅返回精炼结论，防止主上下文被原始日志污染。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "需要子代理完成的探索性任务描述"
                        }
                    },
                    "required": ["task"]
                }
            }
        }]

    async def invoke(self, tool_name: str, **kwargs) -> ToolResult[Any]:
        if tool_name == "run_subagent":
            task = kwargs.get("task")
            if not task:
                return ToolResult(success=False, message="缺少任务描述", data=None)

            sub_session_id = f"sub_{uuid.uuid4()}"
            sub_memory = RedisMemory(
                redis_client=self._redis_client,
                session_id=sub_session_id,
                llm_client=self._llm
            )

            sub_prompt = f"""
你是一个代码搜索子代理。你的任务：{task}
请直接给出最精炼的结论（例如：函数定义位置、报错原因、文件内容）。
不要输出冗长的原始代码或日志，只输出总结。
"""
            await sub_memory.add_message(ChatMessage(role="user", content=sub_prompt))

            try:
                response = await self._llm.invoke(
                    messages=[{"role": "user", "content": sub_prompt}]
                )
                result = response.choices[0].message.content
                return ToolResult(success=True, message="子代理已完成探索", data=result)
            except Exception as e:
                return ToolResult(success=False, message=f"子代理执行失败: {str(e)}", data=None)

        return ToolResult(success=False, message=f"未知工具: {tool_name}", data=None)