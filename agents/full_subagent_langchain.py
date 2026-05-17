#!/usr/bin/env python3
"""
子智能体学习
"""

import json
import os

from langchain_core.messages import HumanMessage, SystemMessage
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from session_manage import SessionManager
from subagent import run_subagent
from tools import PARENT_TOOLS, TOOL_HANDLERS, WORKDIR, SKILL_LOADER, BACKGROUND_MANAGER, BUS, TEAM, TASKS_DIR
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

## 工具并行执行说明
- 所有工具（包括 sub_agent 和普通工具）都支持 `parallel` 参数。
- 当你在同一轮调用多个工具时，设置 `parallel=true` 的工具会**并行执行**以提升效率。
- 设置 `parallel=false` 或不设置的工具会**串行执行**（按顺序一个一个来）。
- 注意：并行执行时，请确保工具之间**没有写冲突**。例如不要在同一轮中同时 `write_file` 或 `edit_file` 同一个文件。
- 推荐做法：将相互依赖的操作设为 `parallel=false` 分先后执行，将独立的读操作设为 `parallel=true` 并行执行。

## sub_agent 使用时机，当子任务满足以下任一条件时，主动使用 sub_agent 分发：
- 需要读取多个文件探索或收集信息时
- 需要多次工具调用或答案相对独立，就使用 sub_agent。
- 可能产生大量工具调用，会污染主对话上下文时。
- 并行能大幅提升效率，但仅限任务相互独立时使用。
## sub_agent 使用时机例举
- 例如「同时搜索3个不同的目录」→ 3 个 sub_agent 都设 `parallel=true`
- 例如「先分析API文档，再根据结果写前端」→ 第二个依赖第一个，设 `parallel=false`（默认串行）

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
        print(f">> sub_agent ({tool_args.get('description', '')}): {tool_args['prompt'][:80]}")
        tool_output = run_subagent(tool_args["prompt"])
        print(f">> sub_agent 执行结果: {tool_output}")
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
        for tool_call in llm_response.tool_calls:
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

        print("》》》》》》》》")
        # 加入工具执行结果到历史消息中
        history_messages.append(HumanMessage(content=json.dumps(tool_call_results)))
        session_manager.append_message_to_session(session_file, history_messages[-1])


def main():
    session_manager = SessionManager(CHAT_HISTORY_DIR, SYSTEM)
    session_num, session_file, history_messages = session_manager.init_session()
    
    while True:
        try:
            remaining_percent = session_manager.get_remaining_token_percent(history_messages)
            query = input(f"\033[36m[session_{session_num} ({int(remaining_percent)}%)] >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        
        if query.strip().lower() in ("q", "exit", ""):
            break
        
        if query.strip().lower() == "@newsession":
            session_num, session_file = session_manager.create_new_session()
            history_messages = [SystemMessage(content=SYSTEM)]
            session_manager.append_message_to_session(session_file, history_messages[0])
            print(f"\033[33m已创建新会话: session_{session_num}.jsonl\033[0m")
            continue
        
        if query.strip().lower().startswith("@switchsession "):
            try:
                target_num = int(query.strip().split()[1])
                session_num, session_file, history_messages = session_manager.switch_session(target_num)
                print(f"\033[33m已切换到会话: session_{session_num}.jsonl ({len(history_messages)} 条消息)\033[0m")
            except (ValueError, IndexError):
                print("\033[31m用法: @switchsession <数字>\033[0m")
            except FileNotFoundError as e:
                print(f"\033[31m{e}\033[0m")
            continue
        
        if query.strip().lower() == "@clearsession":
            deleted_count = session_manager.clear_session(session_file)
            history_messages = [SystemMessage(content=SYSTEM)]
            print(f"\033[33m已清空当前会话，删除了 {deleted_count} 条历史消息\033[0m")
            continue
        
        # 团队命令
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        # 收件箱命令
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue

        if query.strip() == "/tasks":
            TASKS_DIR.mkdir(exist_ok=True)
            for f in sorted(TASKS_DIR.glob("task_*.json")):
                t = json.loads(f.read_text())
                marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
                owner = f" @{t['owner']}" if t.get("owner") else ""
                print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
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
