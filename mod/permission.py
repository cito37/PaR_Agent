from __future__ import annotations

import shlex
from typing import Optional, Dict, Any
from enum import StrEnum

# 定义审批结果
class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    NEED_INPUT = "need_input"   # 需要用户手动确认

class PermissionGate:
    def __init__(self):
        # 定义高危命令关键词（命中即触发人工审批）
        self._dangerous_keywords = [
            "rm", "sudo", "git reset --hard", "git clean", 
            "drop table", "truncate", "kill -9", "shutdown", 
            "reboot", "mkfs", "dd if="
        ]
        # 定义只需要 WRITE 权限的操作（如写文件）
        self._write_keywords = ["write_file", "apply_patch"]

    def check(self, tool_name: str, args: Dict[str, Any]) -> ApprovalStatus:
        """
        根据工具名和参数判断是否放行。
        :return: APPROVED（直接执行） / NEED_INPUT（需要用户确认）
        """
        # 1. 如果是文件写入类工具，属于中等风险，直接放行（但需记录）
        if tool_name in self._write_keywords:
            return ApprovalStatus.APPROVED

        # 2. 如果是执行命令类工具，检查命令内容
        if tool_name == "exec_command":
            command = args.get("command", "")
            # 解析命令，防止引号绕过检测
            try:
                tokens = shlex.split(command)
            except ValueError:
                # 命令格式无法解析，视为高危
                return ApprovalStatus.NEED_INPUT

            # 遍历关键词，检测高危命令
            for token in tokens:
                # 匹配 sudo、rm 等关键词
                base_token = token.split("=")[0]  # 处理 "VAR=value" 形式
                if base_token in self._dangerous_keywords:
                    return ApprovalStatus.NEED_INPUT
                # 匹配 git reset --hard 组合
                if "git" in tokens and ("reset" in tokens or "clean" in tokens):
                    return ApprovalStatus.NEED_INPUT

        # 3. 其他情况默认放行
        return ApprovalStatus.APPROVED