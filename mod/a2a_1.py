from __future__ import annotations

import asyncio
from abc import ABC
from typing import Any, Self, TypeVar, Generic, Callable
from dataclasses import dataclass
from enum import StrEnum

import a2a_sdk
from pydantic import BaseModel, Field, field_validator

from abc import ABC, abstractmethod


T = TypeVar("T")

@dataclass
class ToolResult(Generic[T]):
    success:bool
    message:str
    data : T | None=None

def tool (name:str,descriptipn:str,params:dict,required:list[str]):
    schema = {
        "type": "function",
        "function": {
            "name":name,
            "description":descriptipn,
            "parameters": {"type":"object","properties": params,"required":required}
        }
    }

    def decorator(func:Callable) -> Callable:
        setattr(func,"_tool_name",name)
        setattr(func,"_tool_schema",schema)
        return func
    return decorator

class BaseTool(ABC):
    @abstractmethod
    def get_tools(self)->list[dict]:
        ...

    @abstractmethod
    def invoke(self,)