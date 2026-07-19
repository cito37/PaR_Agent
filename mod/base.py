from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, TypeVar, Generic, get_type_hints, Callable
from dataclasses import dataclass

T = TypeVar("T")

@dataclass
class ToolResult(Generic[T]):
    success: bool
    message: str
    data: T | None = None

def tool(name: str, description: str, params: dict, required: list[str]):
    openai_tool_schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params,
                "required": required
            }
        }
    }
    def decorator(func: Callable) -> Callable:
        setattr(func, "_tool_name", name)
        setattr(func, "_tool_schema", openai_tool_schema)
        return func
    return decorator

class BaseTool(ABC):
    @abstractmethod
    def get_tools(self) -> list[dict]:
        ...

    @abstractmethod
    async def invoke(self, tool_name: str, **kwargs) -> ToolResult[Any]:
        ...

    def has_tool(self, tool_name: str) -> bool:
        for _, func in self._list_tool_methods():
            if getattr(func, "_tool_name", None) == tool_name:
                return True
        return False

    def filter_params(self, method: Callable, kwargs: dict) -> dict:
        type_hint_map = get_type_hints(method)
        valid_param_name = set(type_hint_map)
        return {k: v for k, v in kwargs.items() if k in valid_param_name}

    def _list_tool_methods(self) -> list[tuple[str, Callable]]:
        tool_list = []
        for attr_name in dir(self):
            attr_val = getattr(self, attr_name)
            if callable(attr_val) and hasattr(attr_val, "_tool_schema"):
                tool_list.append((attr_name, attr_val))
        return tool_list