#!/usr/bin/env python3
"""
子智能体学习
"""

import json
import os

from langchain_core.messages import HumanMessage
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from session_manage import SessionManager
from subagent import run_subagent
from tools import (
    PARENT_TOOLS,
    TOOL_HANDLERS,
    WORKDIR,
    SKILL_LOADER,
    BACKGROUND_MANAGER,
    set_todo_session,
    get_todo_manager,
)
from llm_manage import create_llm_with_tools

# 加载环境变量
load_dotenv(override=True)

#对话历史目录
CHAT_HISTORY_DIR = WORKDIR / ".chathistory"
CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# 系统prompt
SYSTEM = f"""
你是一个专业的编程助手，工作目录是 {WORKDIR}，所有操作仅限在该目录下进行。
请优先读取根目录下的CLAUDE.md或者AGENT.md来了解项目约束。
遇到复杂问题时可以先生成shell脚本或者python脚本，再执行。

## 上下文保护规则（最高优先级）
- 你有一个有限的上下文窗口，每次工具调用的输出都会消耗它。一旦耗尽，对话将无法继续
- **永远不要**对二进制文件（PDF、图片、压缩包等）使用 strings、cat、hexdump 命令
- **永远不要**一次性读取超过 500 行的文件，始终使用 limit 参数或 | head 控制
- **永远不要**让单次工具输出超过 5000 字符进入上下文，使用 | head -100 或 | tail 控制
- 读取 PDF 文件时，**必须**使用 read_pdf 工具，不要使用 bash 命令
- 当需要读取大量文件时，**必须**使用 sub_agent 来隔离上下文

## sub_agent 强制使用场景
以下场景**必须**使用 sub_agent，不得在主对话中直接执行：
1. 需要读取 3 个以上文件时
2. 需要读取 PDF 文件时
3. 需要执行 5 步以上工具调用时
4. 需要搜索/探索代码库或文档时
5. 需要实现具体功能时
6. 需要设计实现方案时

## todo 会话看板使用决策
todo_new_board 和 todo 是当前会话内的多组任务看板工具，用于把本对话里的复杂请求拆成可执行步骤并持续更新进度；它不是 workspace/team 级任务图。不是所有请求都必须使用，你需要在开始执行前自行判断是否启用 todo 看板。

建议启用 todo 看板的通用判断标准：
1. 任务目标需要拆成多个可验证步骤，而不是一次工具调用即可完成
2. 存在明确的阶段、阻塞条件或需要按顺序推进的工作流
3. 任务可能跨多轮对话、长时间执行，或需要在上下文压缩、应用退出、崩溃后恢复进度
4. 任务需要协调 sub_agent、后台任务、并行工作或多个产物，但仍属于当前会话内推进
5. 任务风险较高，需要记录执行状态、验证结果、失败原因或待用户决策的问题

不建议启用 todo 看板的情况：
1. 简单问答、解释、翻译、改写等无需工具或只需一步工具调用的请求
2. 用户明确要求只给建议、只分析、不执行
3. 创建 todo 本身会比任务执行更重，且不会提升可追踪性或可靠性

启用 todo 看板后的执行规范：
1. **新建组**：新的复杂用户指令开始时，第一轮工具调用优先使用 todo_new_board 创建新的任务组；title 概括当前用户指令；items 只包含新任务组的步骤，不要合并旧看板
2. **更新组**：todo 只更新当前活跃看板，items 必须是当前活跃看板的完整列表，不是增量补丁，也不包含历史看板的 items
3. **开工**：执行某个步骤前，调用 todo 将该项目标记为 in_progress；同一看板内只能有一个 in_progress
4. **收尾**：该步骤完成后，及时调用 todo 将其标记为 completed；失败或阻塞时保留未完成状态并在后续汇报中说明原因
5. **协作**：需要隔离上下文、并行探索或委派执行时，基于当前活跃 todo 看板边界分派 sub_agent
6. **核对**：使用了 todo 看板的任务，在最终回复前确认 todo 状态，必要时再次调用 todo 更新
7. **汇报**：最终汇总当前看板已完成 todo、关键产物、验证结果、未完成/阻塞项和需要用户决策的问题；历史已完成看板保留在 /todo 中

## sub_agent 工具范围控制
- 子智能体默认拥有执行工具权限，但不包含 todo_new_board 或 todo；会话看板只由主智能体维护
- 如需限制为只读操作，设置 allowed_tools=["bash","read_file","read_pdf"]
- 例如搜索信息、读取文档时，可限制工具范围避免误写

## 工具并行执行说明
- 所有工具（包括 sub_agent 和普通工具）都支持 `parallel` 参数。
- 当你在同一轮调用多个工具时，设置 `parallel=true` 的工具会**并行执行**以提升效率。
- 设置 `parallel=false` 或不设置的工具会**串行执行**（按顺序一个一个来）。
- 注意：并行执行时，请确保工具之间**没有写冲突**。例如不要在同一轮中同时 `write_file` 或 `edit_file` 同一个文件。
- 推荐做法：将相互依赖的操作设为 `parallel=false` 分先后执行，将独立的读操作设为 `parallel=true` 并行执行。

## sub_agent 使用例举
- 例如「同时搜索3个不同的目录」→ 3 个 sub_agent 都设 `parallel=true`
- 例如「读取 DRG_Docs 目录下所有 PDF 的标题和摘要」→ sub_agent(parallel="true")
- 例如「先分析API文档，再根据结果写前端」→ 第二个依赖第一个，设 `parallel=false`（默认串行）
- 例如「实现用户注册功能」→ sub_agent(parallel="false")
- 例如「只读方式搜索代码中的安全问题」→ sub_agent(allowed_tools=["bash","read_file","read_pdf"], parallel="true")

## 工作流程规范
面对复杂任务时，按以下流程执行：
1. **判断复杂度**：先判断是否需要 todo 看板、sub_agent 或普通工具即可完成
2. **规划执行**：如果启用 todo，新任务用 todo_new_board 建新看板组；已有活跃任务进度用 todo 更新；如果不启用，直接采用最小可行工具路径
3. **分发执行**：需要隔离上下文或并行处理时，再基于任务边界分发给 sub_agent
4. **完成更新**：使用 todo 时，每完成一步都更新状态；未使用 todo 时，也要在回复中清楚说明执行过程
5. **汇总决策**：收集工具和子智能体结果，汇报产物、验证结果、风险和需要用户确认的问题

Skills 可使用列表：
{SKILL_LOADER.get_descriptions()}
"""
# 创建绑定了工具的 LLM 实例
llm_with_tools = create_llm_with_tools(PARENT_TOOLS)


# 最大智能体循环迭代次数，防止无限循环导致程序卡死
MAX_AGENT_ITERATIONS = 50


def _execute_tool_call(tool_call: dict) -> dict:
    """执行单个工具调用（sub_agent 或普通工具），返回结果字典"""
    tool_name = tool_call["name"]
    tool_args = tool_call["args"]
    tool_id = tool_call["id"]

    if tool_name == "sub_agent":
        allowed_tools = tool_args.get("allowed_tools")
        print(f">> sub_agent ({tool_args.get('description', '')}): {tool_args['prompt'][:80]}")
        tool_output = run_subagent(tool_args["prompt"], allowed_tools=allowed_tools)
        print(f">> sub_agent 执行结果: {tool_output[:200]}...")
    elif tool_name in TOOL_HANDLERS:
        print(f">> 工具 {tool_name}({tool_args})")
        tool_output = TOOL_HANDLERS[tool_name](**tool_args)
        print(f">> 工具 {tool_name} 执行结果: {tool_output}")
    else:
        tool_output = f"Error: Unknown tool {tool_name}"
        print(f">> 工具 {tool_name} 执行结果: {tool_output}")
    print("-" * 20)

    return {
        "type": "tool_result",
        "tool_name": tool_name,
        "tool_args": tool_args,
        "tool_id": tool_id,
        "tool_output": tool_output,
    }


#执行主体
def agent_loop(history_messages: list, session_file: Path, session_manager: SessionManager):

    iteration = 0  # 循环迭代计数
    rounds_since_todo = 0  # 记录距离上次更新待办事项的轮数

    while True:
        iteration += 1
        if iteration > MAX_AGENT_ITERATIONS:
            print(f"\033[31m[警告] 智能体循环达到最大迭代次数 ({MAX_AGENT_ITERATIONS})，强制结束\033[0m")
            break

        # 在 LLM 调用之前，排空后台通知并注入为系统消息
        notifs = BACKGROUND_MANAGER.drain_notifications()
        if notifs and history_messages:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            history_messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
            history_messages.append({"role": "assistant", "content": "Noted background results."})

        # 在调用 LLM 前截断上下文、替换旧消息中过长的工具消息为占位符，确保不超过限制
        # history_messages[:] = session_manager.trim_messages_to_limit(history_messages)
        history_messages[:] = session_manager.trim_messages_with_tool_compression(history_messages)

        llm_response = llm_with_tools.invoke(history_messages)
        # 加入大模型回复到历史消息中
        history_messages.append(llm_response)
        session_manager.append_message_to_session(session_file, llm_response)

        if not hasattr(llm_response, "tool_calls") or not llm_response.tool_calls:
            return
        
        print(f"》》》》》》》》[本轮 tool_calls 数量] {len(llm_response.tool_calls)}")
        print(llm_response.tool_calls)
        print("*********")
        # 所有工具调用都根据 parallel 参数分组，并行组用线程池执行，串行组按顺序执行
        tool_call_results = []
        parallel_calls = []
        sequential_calls = []
        used_todo = False
        for tool_call in llm_response.tool_calls:
            if tool_call["name"] in ("todo", "todo_new_board"):
                used_todo = True
            if tool_call["args"].get("parallel", False):
                parallel_calls.append(tool_call)
            else:
                sequential_calls.append(tool_call)

        # 并行执行 parallel=true 的工具
        if parallel_calls:
            print()
            with ThreadPoolExecutor(max_workers=len(parallel_calls)) as executor:
                future_map = {executor.submit(_execute_tool_call, tc): tc for tc in parallel_calls}
                for future in as_completed(future_map):
                    tool_call_results.append(future.result())

        # 串行执行 parallel=false 或未设置的工具
        for tool_call in sequential_calls:
            tool_call_results.append(_execute_tool_call(tool_call))

        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if get_todo_manager().has_open_items() and rounds_since_todo >= 3:
            tool_call_results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})

        print("》》》》》》》》")
        # 加入工具执行结果到历史消息中
        history_messages.append(HumanMessage(content=json.dumps(tool_call_results, ensure_ascii=False)))
        session_manager.append_message_to_session(session_file, history_messages[-1])


def main():
    session_manager = SessionManager(CHAT_HISTORY_DIR, SYSTEM)
    session_num, session_file, history_messages = session_manager.init_session()
    set_todo_session(session_num)
    
    while True:
        try:
            remaining_percent = session_manager.get_remaining_token_percent(history_messages)
            query = input(f"\033[36m[session_{session_num} ({int(remaining_percent)}%)] >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        
        if query.strip().lower() in ("q", "exit", ""):
            break
        
        if query.strip().lower() == "@newsession":
            session_num, session_file, history_messages = session_manager.create_initialized_session()
            set_todo_session(session_num)
            print(f"\033[33m已创建新会话: session_{session_num}.jsonl\033[0m")
            continue
        
        if query.strip().lower().startswith("@switchsession "):
            try:
                target_num = int(query.strip().split()[1])
                session_num, session_file, history_messages = session_manager.switch_session(target_num)
                set_todo_session(session_num)
                print(f"\033[33m已切换到会话: session_{session_num}.jsonl ({len(history_messages)} 条消息)\033[0m")
            except (ValueError, IndexError):
                print("\033[31m用法: @switchsession <数字>\033[0m")
            except FileNotFoundError as e:
                print(f"\033[31m{e}\033[0m")
            continue
        
        if query.strip().lower() == "@clearsession":
            deleted_count = session_manager.clear_session(session_file)
            history_messages = session_manager.load_session_history(session_file)
            print(f"\033[33m已清空当前会话，删除了 {deleted_count} 条历史消息\033[0m")
            continue
        
        if query.strip() == "/todo":
            print(get_todo_manager().render())
            continue

        if query.strip() == "/tasks":
            print("当前主执行文件已改用会话级 /todo 看板；workspace 级 task 看板保留给后续团队智能体示例。")
            continue

        history_messages.append(HumanMessage(content=query))
        session_manager.append_message_to_session(session_file, history_messages[-1])
        agent_loop(history_messages, session_file, session_manager)
        response_content = history_messages[-1].content
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        else:
            print(response_content)
        print()


if __name__ == "__main__":
    main()
