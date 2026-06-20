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

# 
    def has_tool(self,tool_name:str)->bool:
        for _,func in self._list_tool_methods():
            tool_n = getattr(func,tool_name,None)
            if tool_name ==tool_n or tool_name==func._name_:
                return True
        return False

    def filter_params(self,method:Callable,kwargs:dict)->dict:
        type_hint_map = get_type_hints(method)
        # get_type_hint读取一个函数 / 类上所有带类型注解的参数、返回值，返回字典
        vail_param_name = set(type_hint_map())
        return{k:v for k,v in kwargs.items() if k in vail_param_name}
    # kwargs 是大模型输出的参数字典，可能带一堆多余、不存在的参数。
    # 只保留函数定义里写了类型注解的参数，剔除多余 key。


# 遍历工具方法筛选出有schema标记的工具加入list_tool（被tool装饰过就会有schema）
    def _list_tool_methods(self) -> list[tuple[str,Callable]]:
        tool_list =[]
        for attr_name in dir(self):
            # dir会返回该对象所有属性、方法名的字符串列表
            attr_val = getattr(self,attr_name)
            # getattr(obj, name, default)：读取对象属性
            if callable(attr_val) and hasattr(attr_val,"_tool_schema"):
            #  hasattr用来检查对象身上有没有某个自定义属性
                tool_list.append((attr_name,attr_val))
        return tool_list
    



    # 枚举用来固定仅允许的 3 种传输类型，
class MCPTransport(StrEnum):
        STDIO = "stdio"   # 本地子进程标准输入输出（shell沙箱使用）
        SSE = "sse"       # 服务端推送事件（远程http流式）
        STREAMABLE_HTTP = "streamable_http"  # 标准流式http传输
        # 这三个是 MCP（Model Context Protocol）官方标准规定的全部 3 种标准传输层

# 这是mcp数据的模型，里面包含只允许的3种传输类型，每个类型传输的字段
class McpServerConfig(BaseModel):
    transport:MCPTransport
    enable:bool
    desciption:str=""
    env:dict[str,str]=Field(default_factory=dict)
    # 下面这两个字段是stdio传输需要的字段
    command:str | None=None
    args:list[str]=Field(default_factory=list)
    # 下面这两个字段是http传输需要的字段
    url:str | None=None
    headers: dict[str,str]=Field(default_factory=dict)

    @field_validator("*")   # 实例化自动执行校验函数，*代表下面所有字段都进自动校验，
    def validate_transport_fields(cls, values: dict):   #values是总结了上面所有配置字段的字典
        transport_type = values.get("transport")  #从字典里用关键词取值，transport有三个类型，下面是对三个类型是否包含必要字段的检验，不包含就报错。
        if transport_type in (MCPTransport.SSE,MCPTransport.STREAMABLE_HTTP):
            if not values.get("url"):
                raise ValueError(f"传输模式{transport_type}必须填写url配置")
        if transport_type in MCPTransport.STDIO:
            if not values.get("command"):
                raise ValueError (f"传输模式{transport_type}必须填写url配置")
        return values
    
class McpConfig(BaseModel):
    mcpServers: dict[str, McpServerConfig] = Field(default_factory=dict)