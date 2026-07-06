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


@dataclass(frozen=True)
class SandboxConfig:
    sandbox_id =str
    base_dir: Path = field(default_factory=lambda : Path("/tem/sandbox"))
    rw_host_paths : List[Path] = field(default_factory=list)
    ro_host_paths: List[Path] = field(default_factory=list) 
    hide_host_paths: List[Path] = field(default_factory=list)
    run_as_user: str = "nobody"
    init_timeout_sec: int = 5


class OverlayFSSandbox:
    def __init__(self,config:SandboxConfig) :
        self.config =config
        self.instance_path = Path(self.config.base_dir) / f"{self.config.sandbox_id}_{os.getpid()}"
        self.upper_dir = self.instance_path / "upper"
        self.work_dir = self.instance_path / "work"
        self.merged_dir = self.instance_path / "merged"
        self._init_proc : Optional[asyncio.subprocess.Process] = None #：它存的不是“进程 ID（数字）”，而是“完整的进程对象”
        self._exec_history: List[Dict[str,Any]] = []
        self._is_ready = False

    async def start(self) -> None:
        if self._init_proc is not None:
            return
        
        if sys.platform !="linux":
            raise SandboxStartError('"OverlayFS 沙箱仅支持 Linux 系统"')
        
        try:
            self.upper_dir.mkdir(parents=True,exist_ok=True)
            self.work_dir.mkdir(parents=True,exist_ok=True)
            self.merged_dir.mkdir(parents=True,exist_ok=True)
        except OSError as e:
            raise SandboxStartError(f"创建沙箱失败：{e}")
        
        init_script = self._build_init_script() 
        with tempfile.NamedTemporaryFile(mode="w",suffix="sh",delete=False) as f :
            f.write(init_script)
            script_path = f.name

        try:
            self._init_proc = await asyncio.create_subprocess_exec( #只有create是主进程，下面unshare等是子进程
                "unshare","--mount","--fork","--pid","/bin/bash",script_path, # --fork 把真正的沙箱脚本（/bin/bash script_path）生出来
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setpgrp   # 设为进程组组长，方便后续清理整组
            )
        except Exception as e :
            raise SandboxStartError(f"无法启动 unshare 进程: {e}")
        
        try:
            ready_line = await asyncio.wait_for(
               self._init_proc.stdout.readline(),
               timeout=self.config.init_timeout_sec
            )
        except asyncio.TimeoutError():
            await self._force_cleanup() 
            raise SandboxStartError("挂载超时,未收到Ready消息")
        
        if ready_line.decode().strip() != "READY":
            await self._force_cleanup()
            raise SandboxStartError(f"沙箱启动异常，未收到正确信号: {ready_line}")
        
        if self._init_proc.returncode is not None and self._init_proc.returncode != 0:
            stderr = await self._init_proc.stderr.read()
            raise SandboxStartError(f"沙箱进程已提前退出，错误输出: {stderr.decode()}")
        
        self._is_ready = True
        logger.info(f"沙箱 {self.config.sandbox_id} 启动成功,PID={self._init_proc.pid}")

        def _build_init_script(self) -> str:
            script_lines=[
                "#!bin/bash",
                "set -e",
                f"mkdir -p {self.upper_dir} {self.work_dir} {self.merged_dir}",
                f"mount -t overlay overlay -o lowerdir=/,upperdir={self.upper_dir},word = {self.word_dir} {self.merged_dir}"
            ] #含义：最终的挂载点（合并视图）。
              #作用：挂载完成后，当你访问 self.merged_dir（如 /tmp/sandbox_001/merged）时，你会看到“宿主机的所有文件（底层）+ 你在这沙箱里新改的文件（上层）”的融合结果。

            for path in sorted(self.config.rw_hide_paths,key=len,reverse=True):
                script_lines.append(f"mkdir -p {self.merdge_dir}{path}")
                script_lines.append(f"mount --bind /dev/null {self.merdge_dir}{path}")

                # 挂载读写映射
            for path in sorted(self.config.rw_host_paths, key=len, reverse=True):
                script_lines.append(f"mkdir -p {self.merged_dir}{path}")
                script_lines.append(f"mount --bind {path} {self.merged_dir}{path}")

            # 挂载只读映射
            for path in sorted(self.config.ro_host_paths, key=len, reverse=True):
                script_lines.append(f"mkdir -p {self.merged_dir}{path}")
                script_lines.append(f"mount --bind -o ro {path} {self.merged_dir}{path}")

            for pseudo in ["/proc", "/sys", "/dev", "/run"] :
                script_lines.append(f"mount --rbind {pseudo} {self.merged_dir}{pseudo}")

            script_lines.append(f"mkdir -p {self.merged_dir}/.pivot_old") # 在merged下创建pivot_old目录，用于下一步挂载
            script_lines.append(f"pivot_root {self.merged_dir} {self.merged_dir}/.pivot_old")
            # 把沙箱的根目录切换(挂载)到merged，把宿主根目录放到(挂载)pivot_old了，当在沙箱执行/时弹出的是merged而不是宿主机根目录，这样宿主根目录就隐藏起来了
            script_lines.append("umount -l /.pivot_old") # 删除旧的挂载点，宿主的根目录也不挂载到pivot_old了
            script_lines.append("rmdir /.pivot_old") # 删除空目录
            script_lines.append("cd /") #切换到根目录
            script_lines.append("echo 'READY'")
            script_lines.append("exec sleep infinity")
            return "\n".join(script_lines)
        
    async def exec_command(self, command: str, timeout_sec: int = 60) -> Dict[str, Any]:
        if not self._is_ready or self._init_proc is None:
            raise SandboxExecutionError("沙箱未就绪，无法执行命令")
        
        cmd=[
            "nsenter","-t",str(self._init_proc.pid),"-m","-U","-n",
            "sudo","u",self.config.run_as_user,"--",  # 从config里获得用户名，然后--是分隔符后面是真正的命令。
            "bin/bash","-c",command   # "-c"：让 Bash 读取后面紧跟的字符串，当成命令来执行
        ]

        try:
            # 全异步调用
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout,stderr = await asyncio.wait_for(proc.communicate(),timeout=timeout_sec)
                # proc.communicate()监听刚才创建的进程，把消息给stdout

            except asyncio.TimeoutError:
                 # 超时主动 kill 进程
                proc.kill()
                await proc.wait() # 因为超时了，所以强制把这个proc停止了
                raise SandboxExecutionError(f"命令执行超时（{timeout_sec}s）: {command}")
            
            output = {
                "stdout": stdout.decode(errors="replace"),  #decode是转换成汉字或因为等字符，replace是遇到无法识别的用�代替
                "stderr": stderr.decode(errors="replace"),
                "returncode": proc.returncode
            }
            self._exec_history.append(
                {
                    "command":command,
                    "timestamp":asyncio.get_event_loop().time(),
                    "output":output
                }
            )
            return output
        except FileNotFoundError as e:
            raise SandboxExecutionError(f"命令执行失败，找不到系统工具: {e}")
        
    async def cleanup(self) -> None:
        # 杀死进程
        if self._init_proc :
            try:
                os.killpg(os.getpgid(self._init_proc.pid),9) # type: ignore
            except ProcessLookupError:
                pass
            finally:
                self._is_ready = False
                self._init_proc = None
        # 卸载挂载
        try:
            subproc = await asyncio.create_subprocess_exec(
                "umount","-R",str(self.merged_dir),
                stderr=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL
            )
            await subproc.wait()  # 执行完卸载挂载操作的进程停止掉。
        except Exception as e :
            logger.warning(f"删除沙箱临时目录失败: {e}") # 向日志系统写入一条“警告”级别的记录

        self._exec_history.clear()
        logger.info(f"沙箱 {self.config.sandbox_id} 已清理")