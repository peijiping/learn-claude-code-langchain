#!/usr/bin/env python3
"""
V2版本的通用智能体学习
"""

import json
import os

from langchain_core.messages import HumanMessage, ToolMessage
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from session_manage import SessionManager
from subagent import SubAgent
from tools import (
    BASE_TOOL,
    MAIN_AGENT_TOOLS,
    TOOL_HANDLERS,
    WORKDIR,
    SKILL_LOADER,
    BACKGROUND_MANAGER,
    get_todo_manager,
)
from llm_manage import create_llm_with_tools

from check_permission import check_permission

from hooks import HookSystem

# 钩子实例：主循环的 hook_system 在模块级只实例化一次
hook_system = HookSystem()
hook_system.register_default_hooks()

try:
    import readline  # 导入 GNU readline 库，用于增强命令行输入功能
    # 关闭终端特殊字符绑定，避免干扰输入
    readline.parse_and_bind('set bind-tty-special-chars off')
    # 启用输入元字符（支持 UTF-8/中文等非 ASCII 字符的输入）
    readline.parse_and_bind('set input-meta on')
    # 启用输出元字符（支持 UTF-8/中文等非 ASCII 字符的输出显示）
    readline.parse_and_bind('set output-meta on')
    # 关闭元字符转换，防止中文字符被转义
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass  # 如果 readline 不可用（如 Windows 环境），则跳过配置



# 加载环境变量
load_dotenv(override=True)

#对话历史目录
CHAT_HISTORY_DIR = WORKDIR / ".chathistory"
CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# 子智能体单实例：复用工具集/处理器/hooks，避免每次 sub_agent 调用都重新实例化
SUB_AGENT_RUNNER = SubAgent(BASE_TOOL, TOOL_HANDLERS, hook_system)


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
- 子智能体默认拥有执行工具权限，但不包含 todo 工具；待办列表只由主智能体维护
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

# 三、待办列表（todo）

todo 是单会话待办列表工具，用于把复杂请求拆成可执行步骤并持续更新进度。
数据持久化为单个 JSON 文件，会话内有效；不支持跨 session 恢复，也不支持任务间的依赖图。

## 何时启用
**建议启用：**
1. 任务需要拆成多个可验证步骤
2. 任务可能跨多轮对话，需要明确"现在到哪一步"
3. 风险较高，需要记录执行状态避免跑偏
4. 收尾前需要给用户一份进度汇总

**不必启用：**
- 简单问答、解释、改写等无需工具或只需一步的请求
- 用户明确要求只给建议、不执行
- 列计划比任务本身更重

## 执行规范
1. **列计划**：动手前先用 todo 把步骤铺开（全部 pending 状态）
2. **开工**：开始某一步时把对应项标记为 in_progress；同一时刻只能有 1 个 in_progress
3. **收尾**：完成后及时标记 completed；不要保留已完成的项占用视觉
4. **新计划**：fresh_start=True 表示开始新计划——会先清掉当前已完成的项，再用新的 items 整体替换
5. **核对**：复杂任务最终回复前调用 todo 一次（即使没变化）以触发 render，便于汇总进度

# 四、工作流程

面对复杂任务时，按以下流程执行：
1. **判断复杂度**：是否需要任务看板、sub_agent，还是普通工具即可
2. **规划执行**：启用待办则先用 todo 列计划；已有进度用 todo 更新；不启用则直接走最小工具路径
3. **分发执行**：需要隔离上下文或并行处理时，基于任务边界分派 sub_agent
4. **完成更新**：使用任务看板时每步更新状态；未使用时在回复中说明执行过程
5. **汇总决策**：收集工具和子智能体结果，汇报产物、验证结果、风险和待确认问题

Skills 可使用列表：
{SKILL_LOADER.get_descriptions()}
"""
# 创建绑定了工具的 LLM 实例
llm_with_tools = create_llm_with_tools(MAIN_AGENT_TOOLS)


# 最大智能体循环迭代次数，防止无限循环导致程序卡死
MAX_AGENT_ITERATIONS = 100


def _execute_tool_call(tool_call: dict) -> dict:
    """执行单个工具调用（sub_agent 或普通工具），返回结果字典"""
    tool_name = tool_call["name"]
    tool_args = tool_call["args"]
    tool_id = tool_call["id"]

    if tool_name == "sub_agent":
        allowed_tools = tool_args.get("allowed_tools")
        print(f">> sub_agent ({tool_args.get('description', '')}): {tool_args['prompt'][:80]}")
        tool_output = SUB_AGENT_RUNNER.spawn_subagent(
            tool_args["prompt"], allowed_tools=allowed_tools
        )
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
    rounds_since_todo = 0  # 记录距离上次调用 todo 工具的轮数，用于 nag reminder

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
        session_manager.maybe_compact_context(history_messages, session_file)

        llm_response = llm_with_tools.invoke(history_messages)
        # 加入大模型回复到历史消息中
        history_messages.append(llm_response)
        session_manager.append_message_to_session(session_file, llm_response)
        print(f"[本轮回复] {llm_response.content}")

        if not hasattr(llm_response, "tool_calls") or not llm_response.tool_calls:
            #增加一个hook，用于在大模型回复中检查是否需要强制结束当前轮次
            force = hook_system.trigger("Stop", history_messages)
            if force:
                history_messages.append({"role": "user", "content": force})
                continue
            return

        print(f"》》》》》》》》[本轮 tool_calls 数量] {len(llm_response.tool_calls)}")
        print(llm_response.tool_calls)
        print("*********")
        # 所有工具调用都根据 parallel 参数分组，并行组用线程池执行，串行组按顺序执行
        tool_call_results = []
        used_todo = False
        for tool_call in llm_response.tool_calls:
            if tool_call["name"] == "todo":
                used_todo = True
            # 目前先不要并行执行，后续要优化并行执行方式，当前无论何种情况都是按顺序串行执行。
            # s03 变更：执行前先经过权限管道检查
            # if not check_permission(tool_call):
            #     results.append({"type": "tool_result", "tool_use_id": tool_call["id"],
            #                     "content": "Permission denied."})
            #     continue
            # s04 change: hook replaces hard-coded check_permission()
            blocked = hook_system.trigger("PreToolUse", tool_call)
            if blocked:
                tool_call_results.append({"type": "tool_result", "tool_use_id": tool_call["id"],
                                "content": str(blocked)})
                continue
            # 执行工具调用或 sub_agent 调用
            tool_call_result = _execute_tool_call(tool_call)
            tool_call_results.append(tool_call_result)
            # s04: post hook
            hook_system.trigger("PostToolUse", tool_call, tool_call_result)  

        # 并行执行 parallel=true 的工具,
        # 目前先注释掉，不支持并行执行，因为当前的并行并未考虑到当同时返回多个工具时，多个工具有并行和顺序执行的执行顺序情况，目前仅为同时并行或同时串行。


        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if get_todo_manager().has_open_items() and rounds_since_todo >= 3:
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
            print(get_todo_manager().render())
            continue

        if query.strip() == "/compact":
            session_manager.maybe_compact_context(history_messages, session_file, manual=True)
            continue
        # s04: pre hook
        hook_system.trigger("UserPromptSubmit", query)
        
        history_messages.append(HumanMessage(content=query))
        session_manager.append_message_to_session(session_file, history_messages[-1])
        session_manager.maybe_compact_context(history_messages, session_file)
        # 执行智能体主循环
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
