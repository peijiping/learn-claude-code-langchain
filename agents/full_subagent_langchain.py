#!/usr/bin/env python3
"""
子智能体学习
"""

import json
import os

from langchain_core.messages import HumanMessage, ToolMessage
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
    get_task_manager,
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
遇到复杂问题时可以先生成 shell 脚本或 python 脚本再执行。
请优先读取根目录下的 CLAUDE.md 或 AGENT.md 来了解项目约束。

# 一、上下文保护规则（最高优先级）

你有一个有限的上下文窗口，每次工具调用的输出都会消耗它。一旦耗尽，对话将无法继续。

- **永远不要**对二进制文件（PDF、图片、压缩包等）使用 strings、cat、hexdump 命令
- **永远不要**一次性读取超过 500 行的文件，始终使用 limit 参数或 | head 控制
- **永远不要**让单次工具输出超过 5000 字符进入上下文，使用 | head -100 或 | tail 控制
- 读取 PDF 文件时，**必须**使用 read_pdf 工具
- 当需要读取大量文件时，**必须**使用 sub_agent 来隔离上下文

# 二、工具与并发机制

## 2.1 sub_agent（子智能体）

### 强制使用场景
以下场景**必须**使用 sub_agent，不得在主对话中直接执行：
1. 需要读取 3 个以上文件
2. 需要读取 PDF 文件
3. 需要执行 5 步以上工具调用
4. 需要搜索/探索代码库或文档
5. 需要实现具体功能
6. 需要设计实现方案

### 工具范围控制
- 子智能体默认拥有执行工具权限，但不包含 task 系列工具（task_create、task_create_many、task_update）；任务看板只由主智能体维护
- 如需限制为只读操作，设置 allowed_tools=["bash","read_file","read_pdf"]

### 使用示例
- 「同时搜索 3 个不同目录」→ 3 个 sub_agent 都设 `parallel=true`
- 「读取 DRG_Docs 下所有 PDF 标题和摘要」→ sub_agent(parallel=true)
- 「先分析 API 文档，再根据结果写前端」→ 第二个依赖第一个，设 `parallel=false`（串行）
- 「实现用户注册功能」→ sub_agent(parallel=false)
- 「只读搜索代码安全问题」→ sub_agent(allowed_tools=["bash","read_file","read_pdf"], parallel=true)

## 2.2 并行执行 vs 后台执行

系统提供两种并发机制，适用场景不同，务必区分：

### 并行执行（`parallel=true`）
- **同步等待**：同一轮发起多个工具调用，等全部完成后才将结果一起喂给 LLM
- **适用场景**：独立的读操作、多个 sub_agent 探索不同目录、互不依赖的工具调用
- **注意**：有写冲突的操作不要并行（如同一文件同时 write_file 和 edit_file）

### 后台执行（`background_run` + `check_background`）
- **异步即发即忘**：提交命令后立即返回 task_id，不阻塞 agent 继续下一轮
- **跨轮通知**：结果在后续轮次自动注入上下文
- **适用场景**：长时间命令（>30 秒），如 npm install、编译、启动服务、跑测试套件
- **不要用 parallel 跑 background_run**：并行仍会阻塞等待，失去异步意义

### 判断速查
| 场景 | 用哪个 |
|------|--------|
| 读多个文件、探索多个目录 | parallel=true |
| 命令预计 5 秒内完成 | parallel=true |
| 命令可能超过 30 秒 | background_run |
| 需要边跑边干其他事 | background_run |
| sub_agent 间无依赖 | parallel=true |

# 三、任务看板（task）

task_create、task_create_many、task_update、task_list、task_get 是 workspace 级任务看板工具，用于把复杂请求拆成可执行步骤、管理依赖关系并持续更新进度。

## 何时启用
**建议启用：**
1. 任务需要拆成多个可验证步骤
2. 存在明确的阶段、阻塞条件或串行依赖
3. 任务可能跨多轮对话、长时间执行，或需要在崩溃后恢复进度
4. 需要协调 sub_agent、后台任务、并行工作或多个产物
5. 风险较高，需记录执行状态、验证结果和失败原因

**不必启用：**
- 简单问答、解释、改写等无需工具或只需一步的请求
- 用户明确要求只给建议、不执行
- 创建看板比任务本身更重

## 执行规范
1. **新建任务组**：新的复杂指令开始时，优先用 task_create_many 创建总任务和子任务；steps 只包含新任务组的步骤
2. **更新任务**：task_update 更新单个任务状态；执行前标记 in_progress，完成后标记 completed
3. **开工约束**：同一任务组内只能有一个 in_progress
4. **收尾**：完成后及时标记 completed；失败或阻塞保留未完成状态并说明原因
5. **协作**：需要隔离上下文或并行探索时，基于任务边界分派 sub_agent
6. **核对**：复杂任务最终回复前调用 task_list 核对完成状态
7. **汇报**：汇总已完成任务、关键产物、验证结果、未完成/阻塞项和待决策问题

# 四、工作流程

面对复杂任务时，按以下流程执行：
1. **判断复杂度**：是否需要任务看板、sub_agent，还是普通工具即可
2. **规划执行**：启用任务看板则用 task_create_many 创建任务组；已有进度用 task_update 更新；不启用则直接走最小工具路径
3. **分发执行**：需要隔离上下文或并行处理时，基于任务边界分派 sub_agent
4. **完成更新**：使用任务看板时每步更新状态；未使用时在回复中说明执行过程
5. **汇总决策**：收集工具和子智能体结果，汇报产物、验证结果、风险和待确认问题

Skills 可使用列表：
{SKILL_LOADER.get_descriptions()}
"""
# 创建绑定了工具的 LLM 实例
llm_with_tools = create_llm_with_tools(PARENT_TOOLS)


# 最大智能体循环迭代次数，防止无限循环导致程序卡死
MAX_AGENT_ITERATIONS = 100


def maybe_compact_context(
    history_messages: list,
    session_file: Path,
    session_manager: SessionManager,
    manual: bool = False,
) -> None:
    """
    检查并按阈值执行上下文压缩。

    manual=True 用于 /compact：仍遵守触发阈值，未达阈值时只提示当前状态。
    """
    stats = session_manager.compact_manager.context_stats(history_messages)
    if not manual and stats.used_percent < 95:
        return

    print(
        f"\033[33m[上下文压缩] 正在检查上下文：当前 {stats.used_tokens}/{stats.max_label} tokens，"
        f"剩余 {int(stats.remaining_percent)}%\033[0m"
    )
    session_manager.compact_messages_if_needed(
        history_messages,
        session_file,
        force=False,
        announce=True,
    )


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
    rounds_since_task = 0  # 记录距离上次更新任务看板的轮数

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

        # 在调用 LLM 前检查上下文，达到阈值时阻塞执行压缩并同步会话文件。
        maybe_compact_context(history_messages, session_file, session_manager)

        llm_response = llm_with_tools.invoke(history_messages)
        # 加入大模型回复到历史消息中
        history_messages.append(llm_response)
        session_manager.append_message_to_session(session_file, llm_response)
        print(f"[本轮回复] {llm_response.content}")

        if not hasattr(llm_response, "tool_calls") or not llm_response.tool_calls:
            return
        
        print(f"》》》》》》》》[本轮 tool_calls 数量] {len(llm_response.tool_calls)}")
        print(llm_response.tool_calls)
        print("*********")
        # 所有工具调用都根据 parallel 参数分组，并行组用线程池执行，串行组按顺序执行
        tool_call_results = []
        parallel_calls = []
        sequential_calls = []
        used_task = False
        for tool_call in llm_response.tool_calls:
            if tool_call["name"] in ("task_create", "task_create_many", "task_update", "task_list", "task_get"):
                used_task = True
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

        rounds_since_task = 0 if used_task else rounds_since_task + 1
        if get_task_manager().has_open_items() and rounds_since_task >= 3:
            tool_call_results.insert(0, {"type": "text", "text": "<reminder>Update your tasks.</reminder>"})

        print("》》》》》》》》")
        # 加入工具执行结果到历史消息中（必须为 ToolMessage，否则 OpenAI 会报 400）
        for tc in llm_response.tool_calls:
            # 找到对应 tool_call_id 的执行结果
            result = next(
                (r for r in tool_call_results if r.get("tool_id") == tc["id"]),
                None,
            )
            if result is None:
                tool_content = json.dumps(
                    {"error": f"No result found for tool_call_id {tc['id']}"},
                    ensure_ascii=False,
                )
            else:
                tool_content = json.dumps(result, ensure_ascii=False)
            tool_msg = ToolMessage(content=tool_content, tool_call_id=tc["id"])
            history_messages.append(tool_msg)
            session_manager.append_message_to_session(session_file, tool_msg)


def main():
    session_manager = SessionManager(CHAT_HISTORY_DIR, SYSTEM)
    session_manager.compact_manager.skill_loader = SKILL_LOADER
    session_num, session_file, history_messages = session_manager.init_session()
    
    while True:
        try:
            context_label = session_manager.format_context_label(history_messages)
            query = input(f"\033[36m[session_{session_num} ({context_label})] >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        
        if query.strip().lower() in ("/q", "/exit", ""):
            break
        
        if query.strip().lower() == "/newsession":
            session_num, session_file, history_messages = session_manager.create_initialized_session()
            print(f"\033[33m已创建新会话: session_{session_num}.jsonl\033[0m")
            continue
        
        if query.strip().lower().startswith("/switchsession "):
            try:
                target_num = int(query.strip().split()[1])
                session_num, session_file, history_messages = session_manager.switch_session(target_num)
                print(f"\033[33m已切换到会话: session_{session_num}.jsonl ({len(history_messages)} 条消息)\033[0m")
            except (ValueError, IndexError):
                print("\033[31m用法: /switchsession <数字>\033[0m")
            except FileNotFoundError as e:
                print(f"\033[31m{e}\033[0m")
            continue
        
        if query.strip().lower() == "/clearsession":
            deleted_count = session_manager.clear_session(session_file)
            history_messages = session_manager.load_session_history(session_file)
            print(f"\033[33m已清空当前会话，删除了 {deleted_count} 条历史消息\033[0m")
            continue
        
        if query.strip() == "/tasks":
            print(get_task_manager().render())
            continue

        if query.strip() == "/compact":
            maybe_compact_context(history_messages, session_file, session_manager, manual=True)
            continue

        history_messages.append(HumanMessage(content=query))
        session_manager.append_message_to_session(session_file, history_messages[-1])
        maybe_compact_context(history_messages, session_file, session_manager)
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
