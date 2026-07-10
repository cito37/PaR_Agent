from __future__ import annotations

import json
import logging
from typing import List, Optional, Dict, Any, TYPE_CHECKING
from datetime import datetime
from pydantic import BaseModel, Field
from redis.asyncio import Redis

# 引入类型标注，避免循环导入
if TYPE_CHECKING:
    from mod.llm import OpenAILLM

#目前没有写创建llm的代码，

logger = logging.getLogger(__name__) # getLogger创建一个日志，日志的名字是（_name_)是当前文件名字

# 定义数据模型，规定后面返回、传输的数据格式。
class ChatMessage(BaseModel):
    role:str
    context:str
    timestamp:str = Field(default_factory=lambda: datetime.now().isoformat())
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class TaskState(BaseModel):
    goal: str = ""
    title: str = ""
    lang: str = "zh"
    todo: List[str] = Field(default_factory=list)
    done: List[str] = Field(default_factory=list)
    current_step_id: Optional[str] = None
    status: str = "idle"

class SemanticFact(BaseModel):
    """语义事实模型，便于后期扩展向量化"""
    type: str  # e.g., "preference", "project_config", "user_identity"
    content: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


#  记忆类初始化
class RedisMemory:
    def __init__(
        self,
        redis_client: Redis,
        session_id: str,
        llm_client: Optional[OpenAILLM] = None,
        recent_limit: int = 50,
        summary_trigger: int = 100
    ):
        self._client = redis_client
        self._session_id = session_id
        self._llm = llm_client  # 用于自动生成总结的外部 LLM 客户端
        self._limit = recent_limit  # 只保留50条 第一层
        self._trigger = summary_trigger   # 处罚压缩机制 第二层

        self._key_recent = f"mem:recent:{session_id}" # 拼接的字符串 
        self._key_summary = f"mem:summary:{session_id}"
        self._key_state = f"mem:state:{session_id}"
        self._key_facts = f"mem:facts:{session_id}"

        self._fallback_buffer: List[ChatMessage] = [] # 当redis写入失败时，先把数据存到这个列表里。


# 第一层记忆，只保留50条最近消息
    async def add_message(self, message: ChatMessage) -> None:
        try:
            data = message.model_dump_json() # 把message转换为json字符
            pipe = self._client.pipeline() # 开通了一个管道，这个管道可以做到一次性把多个数据传到redis，如果不用管道则需要一条条传给redis
            pipe.lpush(self._key_recent,data) # 把data左边插入(也就是插到头部)到self._key_recent
            pipe.ltrim(self._key_recent,0,self._limit-1)
            pipe.llen(self._key_recent) # 获取当前条数
            results = await pipe.execute() # 这里是执行了传输，把上面三个pipe都传给redis了，不过llen会返回当前条数，条数存在results[2]

            if results[2] >= self._trigger and self._llm is not None: # 只有记录超过100条和llm客户端创建成功才启动压缩函数。
                await self._compress_with_llm()
        except Exception as e:
            logger.warning(f"Redis 写入失败，转入 fallback 缓存: {e}")
            self._fallback_buffer.append(message)
    
    #获取50条，
    async def  get_recent_messages(self,limit:int =50) ->List[ChatMessage]:
        try:
            raw_list = await self._client.lrange(self._key_recent, 0, limit - 1)
            msgs = [ChatMessage.model_validate_json(m) for m in raw_list] # A.model_validate_json(b)函数是把参数b转换为A类型
            return msgs[::-1]
        except Exception :
            if self._fallback_buffer:
               return self._fallback_buffer[-limit:]
            return []
    
    # 第二层，第一层超过100条消息时进行压缩摘要
    async def _compress_with_llm(self) -> None:
        if not self._llm:
            logger.warning("未注入 LLM 客户端，无法自动总结")
            return
        
        try:
            msgs = await self.get_recent_messages(limit=self._limit) # 这里有矛盾，标记
            if not msgs:
                return
            prompt = f"""
            请对以下对话历史进行结构化总结，提炼出关键上下文、项目信息、用户偏好。
            直接输出 JSON 格式，不要包含 markdown 代码块。
            对话历史：
            {json.dumps([m.model_dump() for m in msgs], ensure_ascii=False)}
            """
            summary_response = await self._llm.invoke(message=[{"role"="user","content"=prompt}],response_format={"type": "json_object"})
            summary_json = summary_response.choices[0].message.content
            pipe = self._client.pipeline()
            pipe.set(self._key_summary, summary_json) # set是负责存值的，存到self._key_summary，若这有旧值直接覆盖
            pipe.delete(self._key_recent) # 把第一层的数据清除了
            await pipe.execute()
            logger.info(f"记忆已触发压缩，生成总结: {summary_json[:100]}...")

        except Exception as e:
            logger.error(f"记忆压缩失败: {e}")
        
    async def get_summary(self) -> Optional[Dict[str, Any]]:
        try:
            raw = await self._client.get(self._key_summary)
            if raw:
                return json.loads(raw) # 这是把raw换回python格式字符串
        except Exception:
            pass
        return None
    
#第三层，记录任务的工作状态
    async def update_task_state(self, state: TaskState) -> None:
        try:
            await self._client.hset(self._key_state, mapping=state.model_dump())
             # hset:hash传入，把state转换为python字典保存到redis的_key_state里
        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")