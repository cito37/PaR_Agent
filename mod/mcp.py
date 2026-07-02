#将mcp工具转换成openai格式的tool进行的一系列操作
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, Self, TypeVar, Generic, get_type_hints, Callable#类型标注
from collections.abc import Awaitable
from contextlib import AsyncExitStack#异步管理资源的容器（栈）
from dataclasses import dataclass

# MCP官方库
from mcp import ClientSession, StdioServerParameters,  types as mcp_types
# 来创建、持有和操作与 MCP 服务的通信会话，StdioServerParameters包装的，
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
# 数据校验模型库
from pydantic import BaseModel, Field, field_validator
from typing import cast # 强制转换cast（str，变量1），把变量1转换为str


T=TypeVar("T")
# TypeVar泛形占位符，创建一个可变类型占位符

@dataclass
# 是装饰器（数据类语法糖，来自标准库 dataclasses）,作用：自动给类生成 __init__、__repr__、__eq__、__hash__ 等样板代码，
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
        setattr(func,"_tool_name",name)#给这个函数加属性
        setattr(func,"_tool-schema",openai_tool_schema)
        return func
    
    return  decorator


# 统一标准接口，不必让agent知道这个工具是mcp、tool或其他提供的，这里没有写具体内容，只定义了，后期用直接继承然后再额外加数据
class BaseTool(ABC):
    @abstractmethod
    def get_tools(self)->list[dict]:
        ...
    @abstractmethod
    async def invoke(self,_tool_name:str,**kwargs) ->ToolResult[Any]:
        # LLM 根据上面的 schema 生成 JSON 参数，解析后解包成 **kwargs 传给 invoke
        ...


    def has_tool(self,tool_name:str)->bool:
        for _,func in self._list_tool_methods():
            tool_n = getattr(func,tool_name,None)
            if tool_name ==tool_n or tool_name==func._name_:
                return True
        return False
    
 # kwargs 是大模型输出的参数字典，可能带一堆多余、不存在的参数。
    # 只保留函数定义里写了类型注解的参数，剔除多余 key。
    def filter_params(self,method:Callable,kwargs:dict) -> dict:
        type_hint_map = get_type_hints(method)
        # get_type_hint读取一个函数/类上所有带类型注解的参数、返回值，返回字典
        vail_param_name = set(type_hint_map)
        return{k:v for k,v in kwargs.items() if k in vail_param_name}
   

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
    command: str | None = None
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
        if transport_type == MCPTransport.STDIO :
            if not values.get("command"):
                raise ValueError (f"传输模式{transport_type}必须填写url配置")
        return values
    
class McpConfig(BaseModel):
    mcpServers: dict[str, McpServerConfig] = Field(default_factory=dict)



# 
class MCPClientManager:
    def __init__ (self,config:McpConfig):
        self.config = config
        self._stack = AsyncExitStack()#最后会释放这个stack
        self._sessions : dict[str,ClientSession] ={}
        self._cached_tools: dict[str,dict] = {}
    
    async def init (self) ->Self :
        await self.connect_mcp_servers()
        return self
    
    # 连接
    async def connect_mcp_servers(self):
        for server_name,server_cfg in self.config.mcpServers.items():
            if not server_cfg.enable:
                continue
            if server_cfg.transport == MCPTransport.STDIO:
                await self.connect_stdio_mcp_server(server_name, server_cfg)
            elif server_cfg.transport == MCPTransport.STREAMABLE_HTTP:
                await self.connect_streamable_http_mcp_server(server_name, server_cfg)
         # SSE模式可后续扩展，当前暂不实现
            # 连接建立完成后拉取该服务所有工具存入缓存
            await self.cache_mcp_tools(server_name, self._sessions[server_name])        


    async def connect_stdio_mcp_server(self,server_name,cfg:McpServerConfig):
        stdio_params = StdioServerParameters( # StdioServerParameters这是mcp提供的自动包装的程序
            command = cast(str,cfg.command),
            args= cfg.args,
            env = {**cfg.env} # **塞入新字典，防止原数据被篡改
        )
        # enter_async_context：托管会话资源，退出自动关闭连接
        transport = await self._stack.enter_async_context(stdio_client(stdio_params))
        # stdio_client：MCP SDK导入函数，接收StdioServerParameters，创建本地子进程传输对象
        read_stream,write_stream = transport  
        session = await self._stack.enter_async_context(ClientSession(read_stream,write_stream))# ClientSession：MCP SDK导入类，传入读写流建立通信会话
        await session.initialize()
        self._sessions[server_name] = session

    async def connect_streamable_http_mcp_server(self,server_name,cfg:McpServerConfig):
        transport=await self._stack.enter_async_context(streamable_http_client(cast(str,cfg.url),headers=cfg.headers))
        read_stream, write_stream = transport
        session = await self._stack.enter_async_context(ClientSession(read_stream,write_stream))
        await session.initialize()
        self._sessions[server_name] = session
    
    async def cache_mcp_tools(self, server_name: str, session: ClientSession):
        tool_list_resp = await session.list_tools()
        for mcp_tool in tool_list_resp.tools:
            global_tool_name = f"mcp_{server_name}_{mcp_tool.name}"
            self._cached_tools[global_tool_name]={
                "type" : "function",
                "function"  : {
                    "name": global_tool_name,
                    "description" : mcp_tool.description,
                    "parameters": mcp_tool.inputSchema}
            }

    def get_all_tools(self) ->list[dict]:
        return list(self._cached_tools.values())
    
    async def invoke(self,full_tool_name:str,args:dict)-> ToolResult[list[dict]]:
        # 校验缓存里是否有这个工具
        if not full_tool_name in self._cached_tools:
            return ToolResult[list[dict]](
                success=False,
                message= f"mcp不存在{full_tool_name}",
                data=[]
            )
        # 对于没有缓存，llm传过来的tool名字存在的判断
        prefix = "mcp_"
        raw_name = full_tool_name.removeprefix(prefix)
        server_id,origin_tool_name = raw_name.split("_",1)
        target_session = self._sessions.get(server_id)
        if not target_session:
            return ToolResult[list[dict]](
                success=False,
                message= f"mcp{target_session}未建立连接",
                data=[]
            )
        try:
            # MCP协议调用远程工具
            mcp_resp: mcp_types.CallToolResult = await target_session.call_tool(origin_tool_name, arguments=args)
            content_data = [item.model_dump() for item in mcp_resp.content]
            return ToolResult[list[dict]](
                success=True,
                message="MCP远程工具调用成功",
                data=content_data
            )
        except Exception as err:
            # 捕获所有异常，统一封装错误返回
            return ToolResult[list[dict]](
                success=False,
                message=f"MCP调用异常：{str(err)}",
                data=[]
            )
    async def cleanup(self):
            # 一句话作用：关闭全部MCP连接、子进程，清空缓存，释放所有资源，程序退出时调用
            await self._stack.aclose()
            self._sessions.clear()
            self._cached_tools.clear()


# 继承前面的BaseTool抽象接口，这地方传入数据了，就是包装层
class MCPTool(BaseTool):
    def __init__(self,manager: MCPClientManager):
        self._manager = manager

    def get_tools(self)->list[dict]:
        return self._manager.get_all_tools()
    
    async def invoke(self, tool_name: str, **kwargs) -> ToolResult[Any]:
        # 过滤多余参数，只保留合法参数
        valid_args = self.filter_params(self._manager.invoke, kwargs)
        # 转发调用
        return await self._manager.invoke(tool_name, valid_args)