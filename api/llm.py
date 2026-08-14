from __future__ import annotations

from fastapi import APIRouter, Depends
from mod.llm import AppConfigService, LLMConfig
from mod.mcp import MCPConfig
from mod.a2a import A2AConfig
from api.res import Response

router = APIRouter(prefix="/llm", tags=["llm"])

# 依赖注入
async def get_config_service() -> AppConfigService:
    raise NotImplementedError("请从 main.py 注入 AppConfigService")

@router.get("/")
async def get_llm_config(
    service: AppConfigService = Depends(get_config_service)
) -> Response[LLMConfig]:
    return Response.success(data=service.get_llm_config())

@router.get("/mcp")
async def get_mcp_config(
    service: AppConfigService = Depends(get_config_service)
) -> Response[MCPConfig]:
    return Response.success(data=service.get_mcp_config())

@router.get("/a2a")
async def get_a2a_config(
    service: AppConfigService = Depends(get_config_service)
) -> Response[A2AConfig]:
    return Response.success(data=service.get_a2a_config())