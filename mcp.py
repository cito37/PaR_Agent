#将mcp工具转换成openai格式的tool进行的一系列操作

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, Self, TypeVar, Generic, get_type_hints, Callable
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from dataclasses import dataclass

# MCP官方库
from mcp import ClientSession, StdioServerParameters, types as mcp_types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
# 数据校验模型库
from pydantic import BaseModel, Field, field_validator


T=TypeVar("T")

@dataclass
class ToolResult(Generic[T]):
    success:bool
    message:str
    data: T | None=None
# 这里定义了使用tool后返回信息的格式，调用成功没，信息，数据是什么等。


def tool (name:str,description:str, params:dict,required:list[str]):
    openai_tool_schema={"type":"function","function":{"name":name,"description":description,"parameters":{"type":"object","properties":params,"required":required}}}
# 这写了我这个agent（大模型通用）的tool要有哪些属性，


# 这是把mcp提供的工具函数转换成大模型的tool格式
    def decorator(func:Callable) -> Callable:
    # callable是一个可调用类型，我要在这传入函数，这个函数必须是可调用的才能。
        setattr(func,"_tool_name",name)
        setattr(func,"_tool-schema",openai_tool_schema)
        return func
    
    return  decorator


# 统一标准接口，不必让agent知道这个工具是mcp、tool或其他提供的，
class BaseTool(ABC):
    @abstractmethod
    def get_tool(self)->list[dict]:
        ...
    @abstractmethod
    async def invoke(self,_tool_name:str,**kwargs) ->ToolResult[Any]:
        # LLM 根据上面的 schema 生成 JSON 参数，解析后解包成 **kwargs 传给 invoke
        ...
