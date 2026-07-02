from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any, Literal
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

class SandboxError(Exception):
    """沙箱层所有异常的基类"""
    pass

class SandboxStartError(SandboxError):
    """沙箱启动失败"""
    pass

class SandboxExecutionError(SandboxError):
    """命令执行过程异常（超时、权限、进程消失）"""
    pass


