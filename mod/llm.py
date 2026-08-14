from __future__ import annotations

import asyncio
import os
import logging
from typing import Optional, List, Dict, Any, Literal
from pathlib import Path

# 依赖库
import yaml
from openai import AsyncOpenAI, APITimeoutError, APIStatusError
from pydantic import BaseModel, Field
from filelock import FileLock
from mod.mcp import MCPConfig
from mod.a2a import A2AConfig

logger = logging.getLogger(__name__)

# llm的config
class LLMConfig(BaseModel):
    base_url: str = "https://api.deepseek.com/anthropic"
    api_key: str = "sk-00236b86f5fb4ff2a2bc666d2212ea1b"
    model_name: str = "deepseek-v4pro"

# 配置读写取服务
class AppConfigService:
    def __init__(self,cofig_path: str = "config.yml"):
        self.config_path = Path(self.config_path)
        self._lock = FileLock(f"{self.config_path}.lock")
    
    # 读磁盘
    def load_config(self) -> Dict[str,Any]:
        with self._lock:
            try:
                if not self.config_path.exists():
                    return{"llm_config":{},"mcp_config":{},"a2a_config":{}}
                with  open(self.config_path,"r",encoding="UTF-8") as f:
                    return yaml.safe_load(f) or {} # 把yaml格式的f转换成py字典格式，如果没有就返回{}，yaml是cofing的格式
            except Exception as e:
                logger.error(f"读取cofig.yaml失败:{e}")
                return {}

# 保存数据进磁盘
    def save_config(self,config:Dict[str,Any]) ->None:
        with self._lock:
            try :
                if self.config_path.exists():
                    backup_path = self.config_path.with_suffix(".yml.bak") # 把名字后缀改成复制的
                    self.config_path.rename(backup_path)
                with open(self.config_path,"w",encoding="UTF-8") as f:
                    yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
                    # 原文件后缀被改成复制件了，所以config就没了，这里是生成了一个config(和原来的一样)
                logger.info("配置已保存")
            except Exception as e:
                logger.error(f"保存config文件失败:{e}")
                # 若写入失败，尝试将备份恢复
                backup_path = self.config_path.with_suffix(".yml.bak")
                if backup_path.exists():
                    backup_path.rename(self.config_path) 
    
    # 从config_path获取llm配置信息
    def get_llm_config(self) -> LLMConfig:
        raw = self.load_config() # 读取config数据
        llm_raw = raw.get("llm_config", {}) # 找到“llm_config”信息赋给llm_raw，若没有则给{}

        if os.getenv("OPENAI_BASE_URL"):   # 获得括号里的环境信息，
            llm_raw["base_url"] = os.getenv("OPENAI_BASE_URL")  # 环境信息赋给左边
        if os.getenv("OPENAI_API_KEY"):
            llm_raw["api_key"] = os.getenv("OPENAI_API_KEY") # 同上
        return LLMConfig(**llm_raw) # *号是把llm_raw转换为字典格式
    
    def get_mcp_config(self) -> MCPConfig:
        """解析 mcp_config 字段，返回 MCPConfig Pydantic 对象"""
        raw = self.load_config()
        mcp_raw = raw.get("mcp_config", {})
        # 传入 { "mcpServers": ... }，MCPConfig 会自动解析内部字典
        return MCPConfig(**mcp_raw)

    # ---------- 【新增】A2A 配置 ----------
    def get_a2a_config(self) -> A2AConfig:
        """解析 a2a_config 字段，返回 A2AConfig Pydantic 对象"""
        raw = self.load_config()
        a2a_raw = raw.get("a2a_config", {})
        # 传入 { "a2aServers": [...] }，A2AConfig 会自动解析内部列表
        return A2AConfig(**a2a_raw)
# llm客户端
class OpenAILLM:
    def __init__(self, config: LLMConfig):
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=30.0
        ) # timeout是时间的30s没反应重试

        self._model = config.model_name
        self._max_retries = 3  # llm可能调用失败，失败后重试，最多重试3次。
    async def invoke (self,messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Dict[Literal["type"], Literal["json_object", "text"]]] = None, # dict字典，key必须是type，value是二选一
        tool_choice: Optional[Literal["auto", "none"] | Dict[str, Any]] = None,
    ) -> Any:
        kwargs = {
            "model": self._model,
            "messages": messages,
            "parallel_tool_calls": False,  # 强约束
        }
        if tools: # 如果上传了说需要什么工具，则把kwargs改成这个工具
            kwargs["tools"] = tools
            # 默认允许模型自动选择，但可由上层通过 tool_choice 覆盖
            kwargs["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        if response_format:
            kwargs["response_format"] = response_format   
            
        last_exception = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.chat.completions.create(**kwargs)
                return response
            
            except (APITimeoutError,APIStatusError) as e:     # 这两种状态值得再试一次，
                last_exception = e
                if hasattr (e,"status_code") and e.status_code in (426,500,502,503,504):
                    # hasattr 检查用的，这里是在e里面检查有没有status_code
                    wait_sec = 2 **(attempt-1)  # 2的attempt-1次方,wait_sec是等待多少秒
                    logger.warning(f"第{attempt}LLM调用失败，wait_sec秒后重试")
                    if attempt < self._max_retries:
                        await asyncio.sleep(wait_sec)
                    continue #跳过这次循环
                else:  # 只有3次连接llm的机会，若走到else这步了意味着3次都用完了，直接返回错误了
                    raise
            
            except Exception as e:  # 上面的except是那两种错误方式还值得重试，这里就是两种值得重试方法意外的其他错误，就直接报错吧
                logger.error(f"LLM 调用发生未知异常 (第 {attempt} 次): {e}")
                if attempt < self._max_retries:
                    await asyncio.sleep(1)
                continue
        # 全部重试失败，抛出最后一次异常
        logger.error(f"LLM 调用重试 {self._max_retries} 次全部失败")
        raise last_exception or RuntimeError("LLM 调用失败，无具体异常信息")