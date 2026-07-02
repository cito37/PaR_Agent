from __future__ import annotations

import asyncio
from typing import Any, Self
from contextlib import AsyncExitStack
import httpx
from pydantic import BaseModel, Field
from mod.mcp import ToolResult,BaseTool

class A2AServerConfig(BaseModel):
    id :str
    base_url:str
    enable:bool

class A2AConfig(BaseModel):
    a2aServer:list[A2AServerConfig]=Field(default_factory=list)

# 管理连接和调用
class A2AClientManager:
    def __init__(self,config:A2AConfig):
        self.config = config
        self._stack = AsyncExitStack()
        self._client: httpx.AsyncClient | None=None
        self._agent_cards:dict[str,dict]={}

    async def init (self )->Self:
        self._client = await self._stack.enter_async_context (
            httpx.AsyncClient(timeout=30.0)
        )
        await self._fetch_all_agent_cards()
        return self
    
    async def _fetch_all_agent_cards(self):
        for server in self.config.a2aServer:
            if not server.enable:
                continue
            try:
                url = f"{server.base_url.rstrip('/')}/.well-know/agent-card.json"
                # 后面的 /.well-known/agent-card.json 是 A2A 协议规定的标准固定路径（全球 A2A 协议都默认去这个位置找“名片”）
                resp= await self._client.get(url)
                card= resp.json()
                self._agent_cards[server.id] = card

            except Exception as e:
                print(f"[WARN] A2A 服务 {server.id} 拉取卡片失败: {e}")
#                 # 为什么一定要做上面这一步（把所有名片都先拿好）？
# 因为 LLM（大模型） 在选工具时，需要知道“当前有哪些远程 Agent 可以用”。如果你不提前缓存这些名片，那么每次调用远程 Agent 之前，都得先发一次 HTTP 请求去拿名片，效率极低，而且万一远程 Agent 临时挂了，还会引发连环报错。
# 所以，在程序启动时，一口气把所有远程 Agent 的“名片”全拿回来放在内存里，是 A2A 调用最标准、最高效的做法。后面 invoke() 发请求时，直接用这个缓存里的信息去判断目标是否存在、怎么调用即可
    def get_agent_cards(self) -> list[dict]:
        return list(self._agent_cards.values())
    
    async def invoke(self,agent_id:str,query:str) -> dict:
        server = next((s for s in self.config.a2aServer if s.id == agent_id),None)#next是返回第一个满足条件的，若都没有就返回最后的None
        if not server or not server.enable:
            raise  ValueError(f"A2A 服务 {agent_id} 不存在或未启用")
        payload={
            "jsonrpc":"2.0",
            "method":"message/send",
            "params":{
                "message":{
                    "role":"user",
                    "context":query
                }
            },
            "id":1
        }
        url=f"{server.base_url.rstrip("/")}/rpc"
        resp = await self._client.post(url,json=payload)
        resp.raise_for_status # 给发件agent回复一个状态码
        return resp.json()
    
    async def cleanup(self):
        await self._stack.aclose()
        self._agent_cards.clear()


class A2ATool(BaseTool):
    def __init__(self,manager:A2AClientManager):
        self._manager=manager

    def get_tools(self) -> list[dict]:
        # """返回两个工具定义：查询卡片 + 调用 Agent"""
        return [
             {
                "type": "function",
                "function": {
                    "name": "get_remote_agent_cards",
                    "description": "获取所有可用的远程智能体能力描述，用于了解哪些 Agent 可以提供帮助",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            },
             {
                "type": "function",
                "function": {
                    "name": "call_remote_agent",
                    "description": "向指定 ID 的远程智能体发送一条文本消息，并获取其回复",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agent_id": {
                                "type": "string",
                                "description": "目标远程智能体的唯一 ID(来自 get_remote_agent_cards)"
                            },
                            "query": {
                                "type": "string",
                                "description": "发给远程智能体的查询文本"
                            }
                        },
                        "required": ["agent_id", "query"]
                    }
                }
            }
        ]
    async def invoke(self,tool_name:str,**kwargs)->ToolResult[Any]:
        if tool_name=="get_remote_agent_cards":
            card=self._manager.get_agent_cards()
            return ToolResult(
                success=True,
                message="成功获取远程agent列表",
                data=card
            )
        if tool_name == "call_remote_agent":
            vaild_args = self.filter_params(self._manager.invoke,kwargs)
            agent_id = vaild_args.get("agent_id")
            query = vaild_args.get("query")
            if not agent_id or not query:
                return ToolResult(
                    success=False,
                    message="缺少 agent_id 或 query 参数",
                    data=None
                )
            try:
                result = await self._manager.invoke(agent_id,query)
                return ToolResult(
                    success=True,
                    message=f"远程 Agent {agent_id} 调用成功",
                    data=result
                )
            except Exception as e:
                return ToolResult(
                    success=False,
                    message=f"远程 Agent {agent_id} 调用失败",
                    data=None
                )
            
        return ToolResult(
                    success=False,
                    message=f"未知A2A工具{tool_name}",
                    data=None
        )