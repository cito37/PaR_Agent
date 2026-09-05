import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import List, Dict, Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from mod.event import ErrorEvent, DoneEvent
from mod.workflow import WorkFlow


async def run_single_case(case: Dict[str, Any], workflow_factory) -> bool:
   
    flow = workflow_factory(case.get("session_id", "eval_session"))
    event_types = []
    error_occurred = False

    try:
        async for event in flow.invoke(case["input"], files=case.get("files", [])):
            event_types.append(event.type)
            if isinstance(event, ErrorEvent):
                error_occurred = True
                print(f"出现错误: {event.error}")
            if isinstance(event, DoneEvent):
                break
    except Exception as e:
        print(f"执行异常: {e}")
        error_occurred = True

    # 成功条件：没有报错，且出现了完成事件
    success = (not error_occurred) and ("done" in event_types)
    return success


async def main():
    # 1. 加载测试用例（JSON 文件）
    cases_file = Path(__file__).parent / "test_cases.json"
    if not cases_file.exists():
        print(f"测试用例文件不存在: {cases_file}")
        return

    with open(cases_file, "r", encoding="utf-8") as f:
        cases = json.load(f)

    # 2. 逐条运行测试用例
    total = len(cases)
    passed = 0
    print(f"开始评测，共 {total} 个用例...\n")

    for case in cases:
        print(f"运行用例: {case['id']} - {case['name']}")

        is_success = await run_single_case(case, build_workflow)
        if is_success:
            passed += 1
            print(f"通过\n")
        else:
            print(f"失败\n")

    # 3. 汇总结果
    success_rate = (passed / total) * 100 if total > 0 else 0
    print("=" * 40)
    print(f"评测完成: 共 {total} 个用例, 通过 {passed} 个, 失败 {total - passed} 个")
    print(f"成功率: {success_rate:.1f}%")


def build_workflow(session_id: str) -> WorkFlow:
    from mod.llm import OpenAILLM, AppConfigService
    from mod.memory import RedisMemory
    from mod.session import SessionRepo
    raise NotImplementedError("请根据项目依赖注入实现 build_workflow 函数")


if __name__ == "__main__":
    asyncio.run(main())