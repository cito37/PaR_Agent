 # 定义了统一的响应格式，

from __future__ import annotations
from typing import Optional, Generic, TypeVar, Any
from pydantic import BaseModel

T = TypeVar("T")

class Response(BaseModel, Generic[T]):
    # 统一所有 API 接口的返回结构，前端只要解析 code 即可判断状态。
    code: int = 200
    msg: str = "success"
    data: Optional[T] = None

    @classmethod
    def success(cls, data: Optional[T] = None, msg: str = "success") -> Response[T]:
        return cls(code=200, msg=msg, data=data)

    @classmethod
    def fail(cls, msg: str = "fail", code: int = 400, data: Optional[T] = None) -> Response[T]:
        return cls(code=code, msg=msg, data=data)