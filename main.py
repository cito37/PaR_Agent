from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

# ---- 导入项目模块 ----
from mod.session import SessionRepo, get_db_engine_and_factory
from mod.llm import AppConfigService, OpenAILLM
from mod.mcp import MCPClientManager, MCPTool, MCPConfig
from mod.a2a import A2AClientManager, A2ATool, A2AConfig
from tool.message import MessageTool  # 第 13 步写好的 MessageTool

from mod.task import TaskManager
from api.session import router as session_router, get_repo, get_task_manager
from api.llm import router as llm_router, get_config_service
from mod.base import ToolResult  # 用于类型提示，非必须

logger = logging.getLogger(__name__)


#  应用程序生命周期管理（启动/关闭）
@asynccontextmanager
async def lifespan(app: FastAPI):
    """一句话作用：在 FastAPI 启动时初始化所有组件，关闭时清理资源。"""
    logger.info("正在启动 Agent 服务...")

    # 1.1 加载配置
    config_service = AppConfigService()
    app.state.config_service = config_service

    # 1.2 初始化 LLM 客户端
    llm_config = config_service.get_llm_config()
    llm_client = OpenAILLM(config=llm_config)
    app.state.llm_client = llm_client

    # 1.3 初始化 Redis（生产环境建议从配置/环境变量读取 URL）
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = Redis.from_url(redis_url, decode_responses=True)
    app.state.redis_client = redis_client
    await redis_client.ping()  # 测试连接

    # 1.4 初始化数据库（SQLite）
    engine, async_session_maker = get_db_engine_and_factory("sessions.db")
    async with engine.begin() as conn:
        # 自动创建表结构
        await conn.run_sync(lambda sync_conn: sync_conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, sandbox_id TEXT, task_id TEXT, title TEXT, latest_message TEXT, events JSON, files JSON, memory JSON, created_at DATETIME, updated_at DATETIME, status TEXT)"
        ))
    app.state.async_session_maker = async_session_maker
    repo = SessionRepo(async_session_maker)
    app.state.repo = repo

    # 1.5 初始化 MCP 客户端管理器（建立工具连接）
    mcp_config = config_service.get_mcp_config()
    mcp_manager = await MCPClientManager(mcp_config).init()
    app.state.mcp_manager = mcp_manager

    # 1.6 初始化 A2A 客户端管理器（发现远程 Agent）
    a2a_config = config_service.get_a2a_config()
    a2a_manager = await A2AClientManager(a2a_config).init()
    app.state.a2a_manager = a2a_manager

    # 1.7 组装工具列表（Agent 的“手脚”）
    tools = [
        MCPTool(mcp_manager),
        A2ATool(a2a_manager),
        MessageTool(),
    ]
    app.state.tools = tools

    # 1.8 初始化任务管理器（TaskManager）
    task_manager = TaskManager(
        redis_client=redis_client,
        llm_client=llm_client,
        repo=repo,
        tools=tools,
        sandbox_id=None  # 暂不绑定沙箱 ID
    )
    app.state.task_manager = task_manager

    # 1.9 设置依赖注入覆盖（将真实实例挂载到 API 路由中）
    def get_repo_override():
        return app.state.repo
    app.dependency_overrides[get_repo] = get_repo_override

    def get_task_manager_override():
        return app.state.task_manager
    app.dependency_overrides[get_task_manager] = get_task_manager_override

    def get_config_service_override():
        return app.state.config_service
    app.dependency_overrides[get_config_service] = get_config_service_override

    logger.info("全部组件初始化完成，服务已就绪。")
    yield

    # ---- 关闭阶段 ----
    logger.info("正在关闭服务，清理资源...")
    await app.state.mcp_manager.cleanup()
    await app.state.a2a_manager.cleanup()
    await redis_client.close()
    await engine.dispose()


# 2. 创建 FastAPI 实例并挂载路由
app = FastAPI(
    title="cito_agent",
    description="多智能体工作流系统 (PlanAgent + ReActAgent)",
    version="0.1.0",
    lifespan=lifespan
)

# 2.1 CORS 配置（允许前端跨域访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境请替换为前端域名白名单
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2.2 挂载路由
app.include_router(session_router)
app.include_router(llm_router)

# 3. 健康检查接口（可选）

@app.get("/health")
async def health_check():
    """用于探针检查服务状态"""
    return {"status": "ok"}

# 4. 启动入口
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True  # 开发环境开启热重载
    )