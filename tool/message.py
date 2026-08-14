from __future__ import annotations

from typing import Any, List, Dict

from mod.mcp import BaseTool, ToolResult
from mod.event import WaitEvent

class MessageTool(BaseTool):
    def get_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "message_notify_user",
                    "description": "向用户发送一条一次性通知，无需用户回复。通常用于报告阶段性结果。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "要发送给用户的通知内容"
                            }
                        },
                        "required": ["text"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "message_ask_user",
                    "description": "向用户提问，并等待用户输入回复。Agent 将在此处暂停执行，直到收到用户回复。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "向用户提出的问题"
                            }
                        },
                        "required": ["text"]
                    }
                }
            }
        ]
    async def invoke(self, tool_name: str, **kwargs) -> ToolResult[Any]:
        """根据工具名执行对应的交互逻辑"""
        if tool_name == "message_notify_user":
            text = kwargs.get("text", "")
            return ToolResult(
                success=True,
                message=f"通知已发送: {text}",
                data={"type": "notification", "text": text}
            )

        elif tool_name == "message_ask_user":
            text = kwargs.get("text", "")
            # 【工程关键】标记 __wait__ 为 True，上层 agent.py 检测到此标记会抛出 WaitEvent
            return ToolResult(
                success=True,
                message=f"等待用户回复: {text}",
                data={"__wait__": True, "text": text}
            )

        return ToolResult(success=False, message=f"未知消息工具: {tool_name}", data=None)