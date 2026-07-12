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

        # 锁的key，还没有value
        lock_key = f"mem:lock:compress:{self._session_id}"

        try:
            # 1. 尝试获取锁（SETNX：只有 Key 不存在时才返回 True）
            acquired = await self._client.setnx(lock_key, "1") # 这是给lock_key的value为1
            if not acquired:
                # 如果锁已存在，说明已经有别的协程在压缩，或者上一次压缩失败后锁未清理（兜底处理）
                logger.info("压缩锁已被占用，跳过本次压缩，防止并发或重试风暴")
                return

            # 2. 给锁加上自动过期时间（30 秒），防止因进程崩溃导致锁永远无法释放（死锁）
            await self._client.expire(lock_key, 30)

            # 3. 获取当前窗口内的全部消息
            msgs = await self.get_recent_messages(limit=self._limit)
            if not msgs:
                return

            # 4. 构造 Prompt 并调用 LLM
            prompt = f"""
            请对以下对话历史进行结构化总结，提炼出关键上下文、项目信息、用户偏好。
            直接输出 JSON 格式，不要包含 markdown 代码块。

            对话历史：
            {json.dumps([m.model_dump() for m in msgs], ensure_ascii=False)}
"""
            summary_response = await self._llm.invoke(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
            summary_json = summary_response.choices[0].message.content

            # 5. LLM 成功响应后，开始原子化写总结 + 清空50条近期窗口
            pipe = self._client.pipeline()
            pipe.set(self._key_summary, summary_json)
            pipe.delete(self._key_recent)
            await pipe.execute()

            logger.info(f"记忆压缩成功，已生成摘要: {summary_json[:100]}...")

        except Exception as e:
            # 【防御性补丁】哪怕发生异常，锁也会在 30 秒后自动过期（expire 兜底）
            # 并且因为我们在最后才会 delete 最近消息，所以即使失败，Layer 1 数据也完全安全，不会丢失。
            logger.error(f"记忆压缩失败，锁将在 30 秒后自动释放: {e}")

        finally:
            # 【安全兜底】无论如何，尝试手动释放锁（删除 lock_key）
            # 如果前面 setnx 失败，acquired 为 False，这里会跳过
            if acquired:
                await self._client.delete(lock_key)
        
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
    
    async def get_task_state(self) -> Optional[TaskState]:
        try:
            raw = await self._client.hgetall(self._key_state)
            if raw:
                decode = {k.decode():v.decode() for k ,v in raw.item()}
                
                if "todo"in decode:
                    decode["todo"] = json.load(decode["todo"])
                if "done" in decode:
                    decode["done"] = json.load(decode{"done"})

                return TaskState(**decode)
        except Exception as e:
            logger.error(f"读取任务状态失败: {e}")
        return None    
    
    # 第四层 记录用户偏好
async def add_fact(self, fact: SemanticFact) -> None:
    try:
        await self._client.hset(
                self._key_facts,
                fact.type,
                fact.model_dump_json()
            )
    except Exception as e:
            logger.error(f"存入语义事实失败: {e}")

async def get_facts(self, fact_type: Optional[str] = None) -> List[SemanticFact]:
    try:
            if fact_type:
                raw = await self._client.hget(self._key_facts, fact_type)
                if raw:
                    return [SemanticFact.model_validate_json(raw)]
                return []
            else:
                raw_dict = await self._client.hgetall(self._key_facts)
                facts = []
                for _, v in raw_dict.items():
                    facts.append(SemanticFact.model_validate_json(v.decode()))
                return facts
    except Exception as e:
            logger.error(f"读取语义事实失败: {e}")
            return []
    
# 上下文拼凑
async def build_context(self) -> Dict[str, Any]:
    recent_msgs = await self.get_recent_messages()
    summary = await self.get_summary()
    task_state = await self.get_task_state()
    facts = await self.get_facts()

    return {
            "summary": summary or {},           # Layer 2 对话摘要
            "recent_messages": recent_msgs,     # Layer 1 近期窗口
            "task_state": task_state,           # Layer 3 任务状态
            "semantic_facts": facts             # Layer 4 知识事实
        }


#  兼容旧接口（供 roll_back 等使用）
async def roll_back(self, count: int = 1) -> List[ChatMessage]:
        """从 Layer 1 移除最后 N 条消息，用于回退错误工具调用"""
        if count <= 0:
            return []
        try:
            # LPOP count 从头部移除 count 条
            raw_list = await self._client.lpop(self._key_recent, count)
            if raw_list:
                return [ChatMessage.model_validate_json(m) for m in raw_list]
            return []
        except Exception:
            return []

async def to_dict(self) -> Dict[str, Any]:
        """导出全部记忆状态（包含 4 层），用于前端预览或调试"""
        return {
            "summary": await self.get_summary(),
            "recent": [m.model_dump() for m in await self.get_recent_messages()],
            "state": (await self.get_task_state()).model_dump() if await self.get_task_state() else None,
            "facts": [f.model_dump() for f in await self.get_facts()]
        }