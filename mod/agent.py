# from __future__ import annotations

# import asyncio
# import json
# import logging
# from abc import ABC, abstractmethod
# from typing import List, Optional, Dict, Any, AsyncGenerator, Union
# from mod.base import ToolResult

# # 依赖模块（按项目已定义的路径导入）
# from mod.prompt import (
#     SYSTEM_PROMPT,
#     PLAN_AGENT_PROMPT,
#     CREATE_PLAN_PROMPT,
#     UPDATE_PLAN_PROMPT,
#     REACT_SYSTEM_PROMPT,
#     REACT_EXEC_PROMPT,
#     REACT_SUMMARY_PROMPT,
# )
# from mod.memory import RedisMemory, ChatMessage
# from mod.mcp import BaseTool
# from mod.llm import OpenAILLM
# from mod.event import (
#     Plan, Step, PlanEvent, StepEvent, ToolEvent, 
#     MessageEvent, ErrorEvent, DoneEvent, WaitEvent
# )

# logger = logging.getLogger(__name__)

# class BaseAgent(ABC):
#     def __init__(
#         self,
#         name: str,
#         session_id: str,
#         llm: OpenAILLM,
#         memory: RedisMemory,
#         tools: List[BaseTool],
#         system_prompt: str = SYSTEM_PROMPT,
#     ):
#         self.name = name
#         self.session_id = session_id
#         self.llm = llm
#         self.memory = memory
#         self.tools = tools
#         self.system_prompt = system_prompt
#         # 【工程缓存】将工具的 Schema 提前展开为列表，避免每次循环都重新遍历调用 get_tools()
#         self._cached_tool_schemas = self._build_tool_schemas()

#     def _build_tool_schemas(self) -> List[Dict[str, Any]]:
#         """将全部工具（MCP/A2A/Shell/Message）的 Schema 合并成一个列表"""
#         schemas = []
#         for tool in self.tools:
#             schemas.extend(tool.get_tools())
#         return schemas
    
#     async def invoke(self, query: str) -> AsyncGenerator[Union[PlanEvent, StepEvent, ToolEvent, MessageEvent, ErrorEvent, WaitEvent, DoneEvent], None]:
#         # 将用户输入写入记忆
#         await self.memory.add_message(ChatMessage(role="user",context=query))
#         yield MessageEvent(role="user",message=query)

# # 计数，react的循环次数要小于100次
#         loop_count = 0
#         max_loops = 100
#         while loop_count < max_loops:
#             loop_count += 1

#             try:
#                 context = await self.memory.build_context()
#                 messages = [
#                     {"role": "system", "content": f"{self.system_prompt}"},
#                     {"role": "user", "content": f"当前任务状态：{json.dumps(context.get('task_state', {}))}"},
#                     {"role": "user", "content": f"对话摘要：{json.dumps(context.get('summary', {}))}"},
#                 ]
#                 for msg in context.get("recent_messages", []):
#                     messages.append({"role": msg.role, "content": msg.content})

#                 response = await self.llm.invoke(
#                     messages=messages,
#                     tools=self._cached_tool_schemas,
#                     # 如果当前是 PlanAgent，强制输出 JSON；ReActAgent 则不强制
#                     response_format={"type": "json_object"} if self.name == "planner" else None,
#                 )
#                 choice = response.choices[0]
#                 message = choice.message

#                 if message.tool_calls:  
#                     tool_call = message.tool_calls[0]  # 取工具列表中第一个工具
#                     tool_name = tool_call.function.name
#                     args_str = tool_call.function.arguments

#                     yield ToolEvent(  # 返回工具事件，工具事件里定义是这样。
#                         tool_call_id=tool_call.id,
#                         tool_name=tool_name,
#                         function_name=tool_name,
#                         function_args=json.loads(args_str),
#                         status="CALLING",
#                     )
#                     tool_result = await self._execute_tool(tool_name, args_str)

#                     yield ToolEvent(
#                         tool_call_id=tool_call.id,
#                         tool_name=tool_name,
#                         function_name=tool_name,
#                         function_args=json.loads(args_str),
#                         function_result=tool_result.data if tool_result.success else {"error": tool_result.message},
#                         status="CALLED",
#                     )

#                     await self.memory.add_message(ChatMessage(
#                         role="tool",
#                         context=f"工具执行结果：{json.dumps(tool_result.model_dump())}",
#                         tool_call_id=tool_call.id,
#                     ))

#                     if tool_result.data and isinstance(tool_result.data, dict) and tool_result.data.get("__wait__"):
#                         yield WaitEvent()
#                         return

#                     continue
            
#                 else:
#                     # 2.4 LLM 没有调用工具，直接输出回答
#                     content = message.content
#                     yield MessageEvent(role="assistant", message=content)
#                     await self.memory.add_message(ChatMessage(role="assistant", content=content))
#                     break
                
#             except Exception as e:
#                 logger.exception(f"Agent {self.name} 循环执行异常")
#                 yield ErrorEvent(error=f"Agent 执行异常: {str(e)}")
#                 break

#         if loop_count >= max_loops:
#             yield ErrorEvent(error="Agent 执行循环次数超过上限（100次），可能陷入死循环")

#     async def _execute_tool(self, tool_name: str, args_str: str) -> Any:
#         try:
#             args = json.loads(args_str)
#         except json.JSONDecodeError:
#             return ToolResult(success=False, message="无法解析工具参数 JSON", data=None)
        
#         for tool in self.tools:
#             if tool.has_tool(tool_name):
#                 result = await tool.invoke(tool_name, **args)
#                 return result
#         return ToolResult(success=False, message=f"未找到工具: {tool_name}", data=None)

#     async def add_to_memory(self, role: str, context: str, metadata: dict = None):
#         """快捷方法，向记忆追加一条消息"""
#         await self.memory.add_message(ChatMessage(
#             role=role,
#             context=context,
#             metadata=metadata or {}
#         ))
#     async def roll_back_memory(self, count: int = 1) -> List[ChatMessage]:
#         # 回滚最后 N 条记忆（用于工具调用失败时的状态恢复；外面有个大循环，每次循环都是count-1
#         return await self.memory.roll_back(count=count)

# class PlanAgent(BaseAgent):
#     def __init__(self, session_id: str, llm: OpenAILLM, memory: RedisMemory):
#         super().__init__(
#             name="planner",
#             session_id=session_id,
#             llm=llm,
#             memory=memory,
#             tools=[],
#             system_prompt=SYSTEM_PROMPT + "\n" + PLAN_AGENT_PROMPT
#         )

#     async def create_plan(self, message: str, files: List[str] = None) -> Plan:
#         """
#         根据用户消息生成初始计划。
#         返回 Plan 对象，并产出 PlanEvent。
#         """
#         prompt = CREATE_PLAN_PROMPT.format(message=message, files=json.dumps(files or []))
#         # 构建只包含 system + user 的简单对话
#         messages = [
#             {"role": "system", "content": self.system_prompt},
#             {"role": "user", "content": prompt}
#         ]
#         try:
#             # 强制要求 JSON 输出
#             response = await self.llm.invoke(
#                 messages=messages,
#                 response_format={"type": "json_object"},
#                 tools=None
#             )
#             content = response.choices[0].message.content
#             plan_data = json.loads(content)
#             # 构造 Plan 对象
#             plan = Plan(**plan_data)
#             return plan
#         except Exception as e:
#             logger.error(f"PlanAgent 创建计划失败: {e}")
#             raise ValueError(f"规划生成异常: {e}")
        
# # 更新计划，plan有很多步，每执行完一步，把完成的删了就更新plan
#     async def update_plan(self, plan: Plan, step: Step) -> Plan:
#         prompt = UPDATE_PLAN_PROMPT.format(
#             plan=plan.model_dump_json(),
#             step=step.model_dump_json()
#         )
#         messages = [
#             {"role": "system", "content": self.system_prompt},
#             {"role": "user", "content": prompt}
#         ]
#         try:
#             response = await self.llm.invoke(
#                 messages=messages,
#                 response_format={"type": "json_object"},
#                 tools=None
#             )
#             content = response.choices[0].message.content
#             updated_plan_data = json.loads(content)
#             return Plan(**updated_plan_data)
#         except Exception as e:
#             logger.error(f"PlanAgent 更新计划失败: {e}")
#             raise ValueError(f"计划更新异常: {e}")
        
# class ReActAgent(BaseAgent):
#     def __init__(self, session_id: str, llm: OpenAILLM, memory: RedisMemory, tools: List[BaseTool]):
#         super().__init__(
#             name="re-act",
#             session_id=session_id,
#             llm=llm,
#             memory=memory,
#             tools=tools,
#             system_prompt=SYSTEM_PROMPT + "\n" + REACT_SYSTEM_PROMPT
#         )
#     # 按照plan单个执行step
#     async def execute_step(self, plan: Plan, step: Step, user_message: str, files: List[str] = None) -> Step:   
#          # 1. 构造执行该步骤的提示词
#         prompt = REACT_EXEC_PROMPT.format(
#             step=step.model_dump_json(),
#             message=user_message,
#             files=json.dumps(files or []),
#             lang=plan.lang
#         )
#         await self.memory.add_message(ChatMessage(role="user", content=prompt))
#         final_result = None
#         step_status = "running"

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, AsyncGenerator, Union

# 假设 ToolResult 已在 mod.base 中定义（由用户提供）
from mod.base import ToolResult

from mod.prompt import (
    SYSTEM_PROMPT,
    PLAN_AGENT_PROMPT,
    CREATE_PLAN_PROMPT,
    UPDATE_PLAN_PROMPT,
    REACT_SYSTEM_PROMPT,
    REACT_EXEC_PROMPT,
    REACT_SUMMARY_PROMPT,
)
from mod.memory import RedisMemory, ChatMessage
from mod.mcp import BaseTool
from mod.llm import OpenAILLM
from mod.event import (
    Plan, Step, PlanEvent, StepEvent, ToolEvent,
    MessageEvent, ErrorEvent, DoneEvent, WaitEvent
)

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    def __init__(
        self,
        name: str,
        session_id: str,
        llm: OpenAILLM,
        memory: RedisMemory,
        tools: List[BaseTool],
        system_prompt: str = SYSTEM_PROMPT,
    ):
        self.name = name
        self.session_id = session_id
        self.llm = llm
        self.memory = memory
        self.tools = tools
        self.system_prompt = system_prompt
        self._cached_tool_schemas = self._build_tool_schemas()

    def _build_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas = []
        for tool in self.tools:
            schemas.extend(tool.get_tools())
        return schemas

    async def invoke(self, query: str) -> AsyncGenerator[
        Union[PlanEvent, StepEvent, ToolEvent, MessageEvent, ErrorEvent, WaitEvent, DoneEvent], None
    ]:
        # 将用户输入写入记忆（修正参数名 content）
        await self.memory.add_message(ChatMessage(role="user", content=query))
        yield MessageEvent(role="user", message=query)

        loop_count = 0
        max_loops = 100

        while loop_count < max_loops:
            loop_count += 1

            try:
                context = await self.memory.build_context()
                # 将任务状态和摘要融入 system prompt，避免在对话历史中插入冗余 user 消息
                task_state_str = json.dumps(context.get('task_state', {}))
                summary_str = json.dumps(context.get('summary', {}))
                system_with_context = f"{self.system_prompt}\n\n## 当前任务状态\n{task_state_str}\n\n## 对话摘要\n{summary_str}"
                messages = [{"role": "system", "content": system_with_context}]

                # 追加近期消息
                for msg in context.get("recent_messages", []):
                    messages.append({"role": msg.role, "content": msg.content})

                response = await self.llm.invoke(
                    messages=messages,
                    tools=self._cached_tool_schemas,
                    response_format={"type": "json_object"} if self.name == "planner" else None,
                )
                choice = response.choices[0]
                message = choice.message

                if message.tool_calls:
                    tool_call = message.tool_calls[0]
                    tool_name = tool_call.function.name
                    args_str = tool_call.function.arguments

                    yield ToolEvent(
                        tool_call_id=tool_call.id,
                        tool_name=tool_name,
                        function_name=tool_name,
                        function_args=json.loads(args_str),
                        status="CALLING",
                    )

                    tool_result = await self._execute_tool(tool_name, args_str)

                    yield ToolEvent(
                        tool_call_id=tool_call.id,
                        tool_name=tool_name,
                        function_name=tool_name,
                        function_args=json.loads(args_str),
                        function_result=tool_result.data if tool_result.success else {"error": tool_result.message},
                        status="CALLED",
                    )

                    # 修正参数名 content
                    await self.memory.add_message(ChatMessage(
                        role="tool",
                        content=f"工具执行结果：{json.dumps(tool_result.model_dump())}",
                        tool_call_id=tool_call.id,
                    ))

                    if tool_result.data and isinstance(tool_result.data, dict) and tool_result.data.get("__wait__"):
                        yield WaitEvent()
                        return

                    continue  # 继续下一轮循环

                else:
                    content = message.content
                    yield MessageEvent(role="assistant", message=content)
                    await self.memory.add_message(ChatMessage(role="assistant", content=content))
                    break  # 无工具调用，本次任务完成

            except Exception as e:
                logger.exception(f"Agent {self.name} 循环执行异常")
                yield ErrorEvent(error=f"Agent 执行异常: {str(e)}")
                break

        if loop_count >= max_loops:
            yield ErrorEvent(error="Agent 执行循环次数超过上限（100次），可能陷入死循环")

    async def _execute_tool(self, tool_name: str, args_str: str) -> ToolResult:
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            return ToolResult(success=False, message="无法解析工具参数 JSON", data=None)

        for tool in self.tools:
            if tool.has_tool(tool_name):
                result = await tool.invoke(tool_name, **args)
                return result
        return ToolResult(success=False, message=f"未找到工具: {tool_name}", data=None)

    async def add_to_memory(self, role: str, content: str, metadata: dict = None):
        """快捷方法，向记忆追加一条消息（修正参数名 content）"""
        await self.memory.add_message(ChatMessage(
            role=role,
            content=content,
            metadata=metadata or {}
        ))

    async def roll_back_memory(self, count: int = 1) -> List[ChatMessage]:
        """回滚最后 N 条记忆"""
        return await self.memory.roll_back(count=count)


class PlanAgent(BaseAgent):
    def __init__(self, session_id: str, llm: OpenAILLM, memory: RedisMemory):
        super().__init__(
            name="planner",
            session_id=session_id,
            llm=llm,
            memory=memory,
            tools=[],
            system_prompt=SYSTEM_PROMPT + "\n" + PLAN_AGENT_PROMPT
        )

    async def create_plan(self, message: str, files: List[str] = None) -> Plan:
        prompt = CREATE_PLAN_PROMPT.format(message=message, files=json.dumps(files or []))
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]
        try:
            response = await self.llm.invoke(
                messages=messages,
                response_format={"type": "json_object"},
                tools=None
            )
            content = response.choices[0].message.content
            plan_data = json.loads(content)
            plan = Plan(**plan_data)
            return plan
        except Exception as e:
            logger.error(f"PlanAgent 创建计划失败: {e}")
            raise ValueError(f"规划生成异常: {e}")

    async def update_plan(self, plan: Plan, step: Step) -> Plan:
        prompt = UPDATE_PLAN_PROMPT.format(
            plan=plan.model_dump_json(),
            step=step.model_dump_json()
        )
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]
        try:
            response = await self.llm.invoke(
                messages=messages,
                response_format={"type": "json_object"},
                tools=None
            )
            content = response.choices[0].message.content
            updated_plan_data = json.loads(content)
            return Plan(**updated_plan_data)
        except Exception as e:
            logger.error(f"PlanAgent 更新计划失败: {e}")
            raise ValueError(f"计划更新异常: {e}")


class ReActAgent(BaseAgent):
    def __init__(self, session_id: str, llm: OpenAILLM, memory: RedisMemory, tools: List[BaseTool]):
        super().__init__(
            name="re-act",
            session_id=session_id,
            llm=llm,
            memory=memory,
            tools=tools,
            system_prompt=SYSTEM_PROMPT + "\n" + REACT_SYSTEM_PROMPT
        )

    async def execute_step(self, plan: Plan, step: Step, user_message: str, files: List[str] = None) -> Step:
        prompt = REACT_EXEC_PROMPT.format(
            step=step.model_dump_json(),
            message=user_message,
            files=json.dumps(files or []),
            lang=plan.lang
        )
        # 修正参数名 content
        await self.memory.add_message(ChatMessage(role="user", content=prompt))

        # 此处调用父类 invoke 即可，但父类 invoke 会从用户消息开始循环，这符合预期
        # 我们只需要等待 invoke 完成，并更新 step 状态
        # 注意：由于 invoke 是生成器，我们需要消费它直到结束
        try:
            async for event in self.invoke(prompt):
                # 在这里可以处理事件，但具体事件处理由上层（workflow）负责
                # 我们只需要知道 invoke 执行完毕，并判断最终状态
                pass
            # 循环正常结束，说明没有 WaitEvent，且没有异常
            step.status = "completed"
            step.success = True
        except Exception as e:
            step.status = "failed"
            step.error = str(e)
        return step

    async def summarize(self, plan: Plan) -> str:
        """所有步骤完成后，汇总生成最终回答"""
        steps_results = []
        for s in plan.steps:
            steps_results.append({
                "id": s.id,
                "description": s.description,
                "result": s.result,
                "success": s.success,
                "error": s.error
            })

        prompt = REACT_SUMMARY_PROMPT.format(
            plan=plan.model_dump_json(),
            steps_results=json.dumps(steps_results, ensure_ascii=False)
        )

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]

        try:
            response = await self.llm.invoke(messages=messages)
            summary = response.choices[0].message.content
            return summary
        except Exception as e:
            logger.error(f"ReActAgent 汇总失败: {e}")
            return f"总结失败: {str(e)}"