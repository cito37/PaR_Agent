from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Dict, Any

from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio
from mcp.types import TextContent, Tool, ListToolsResult, CallToolResult

from mod.sandbox import OverlayFSSandbox, SandboxConfig

logger = logging.getLogger(__name__)

sandbox_config = SandboxConfig(
    sandbox_id="mcp_shell",
    rw_host_paths=[],     # 可根据需要挂载宿主机可写目录
    ro_host_paths=[],     # 只读目录
    hide_host_paths=[],   # 隐藏目录
    run_as_user="nobody"  # 降权执行
)
sandbox = OverlayFSSandbox(config=sandbox_config)

app = Server("shell-sandbox")  #创建 MCP Server 实例


async def list_tools() -> List[Tool]:  # 列出所有工具
    """暴露给主进程的三个工具：执行、查看历史、停止沙箱"""
    return [
        Tool(
            name="exec_command",
            description="在隔离沙箱内执行一条 Shell 命令，返回 stdout 和 stderr",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令字符串"},
                    "timeout_sec": {"type": "number", "description": "超时时间（秒），默认 60"}
                },
                "required": ["command"]
            }
        ),
        Tool(
            name="view_history",
            description="查看沙箱内已执行过的命令历史，可按关键词过滤",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "可选关键词过滤"}
                }
            }
        ),
        Tool(
            name="stop_sandbox",
            description="停止并销毁当前沙箱环境，释放所有资源",
            inputSchema={"type": "object", "properties": {}}
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """处理来自主进程的工具调用请求"""
    if name == "exec_command":
        command = arguments.get("command", "")
        timeout = arguments.get("timeout_sec", 60)
        
        # 确保沙箱已启动  ，要在沙箱里面跑shell，一定要用沙箱隔离开。
        if not sandbox._is_ready:
            await sandbox.start()
        
        result = await sandbox.exec_command(command, timeout_sec=timeout)
        return [TextContent(type="text", text=f"STDOUT:\n{result['stdout']}\n\nSTDERR:\n{result['stderr']}\nRETURNCODE: {result['returncode']}")]
    
    elif name == "view_history":
        keyword = arguments.get("keyword")
        history = sandbox._exec_history
        if keyword:
            history = [h for h in history if keyword in h["command"]]
        return [TextContent(type="text", text=str(history))]
    
    elif name == "stop_sandbox":
        await sandbox.cleanup()
        return [TextContent(type="text", text="沙箱已停止并清理")]
    
    raise ValueError(f"未知工具: {name}")

# =============================================================================
# 3. 独立服务入口
# =============================================================================
async def main():
    """使用 stdio 传输启动 MCP Server"""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="shell-sandbox",
                server_version="0.1.0",
                capabilities=app.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())