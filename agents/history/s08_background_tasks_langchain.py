#!/usr/bin/env python3
"""
s08_background_tasks_langchain.py - 后台任务

在后台线程中运行命令。在每次 LLM 调用之前排空通知队列以传递结果。

    主线程                     后台线程
    +-----------------+        +-----------------+
    | agent loop      |        | task executes   |
    | ...             |        | ...             |
    | [LLM call] <---+------- | enqueue(result) |
    |  ^drain queue   |        +-----------------+
    +-----------------+

    时间线:
    Agent ----[spawn A]----[spawn B]----[other work]----
                 |              |
                 v              v
              [A runs]      [B runs]        (并行)
                 |              |
                 +-- notification queue --> [results injected]

关键洞察: "即发即忘 -- agent 在命令运行期间不会阻塞。"
"""

import json
import os

from langchain_core.messages import HumanMessage, SystemMessage

from pathlib import Path
from dotenv import load_dotenv
from session_manage import SessionManager
from tools import CHILD_TOOLS, PARENT_TOOLS, TOOL_HANDLERS, WORKDIR, SKILL_LOADER, BACKGROUND_MANAGER
from llm_manage import create_llm_with_tools

# 加载环境变量
load_dotenv(override=True)

#对话历史目录
CHAT_HISTORY_DIR = WORKDIR / ".chathistory"
CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# 系统prompt
SYSTEM = f"""
你是一个专业的编程助手，负责处理与编程相关任务。

你的工作目录是 {WORKDIR} 。

遇到陌生领域时，使用 load_skill 加载专业技能知识。
Skills available：
{SKILL_LOADER.get_descriptions()}

## 核心能力
- （优先级低）简单任务且多步骤时，使用 todo 工具规划多步骤任务（标记 in_progress/completed）
- 使用 sub_agent 工具自动分发子任务给子智能体
- 优先使用工具而非纯文本回复
- 使用任务 task （task_create, task_update, task_list, task_get） 工具来规划和跟踪工作。
- 使用 background_run 工具在后台线程中运行命令，例如shell命令。
- 使用 check_background 工具检查后台命令任务状态或列出所有命令执行的任务。

使用## 何时使用 sub_agent 工具（子智能体）
当遇到以下情况时，**应主动调用 sub_agent 工具**，无需用户明确要求：

1. **多步骤独立任务**：任务可拆分为多个互不依赖的子任务并行执行
   - 示例："分析项目中所有Python文件的依赖关系"
   - 示例："为每个模块编写单元测试"

2. **探索性/信息收集任务**：需要读取多个文件才能回答
   - 示例："这个项目使用什么测试框架？"
   - 示例："列出所有配置文件并说明其作用"

3. **可能污染上下文的任务**：预计会产生大量工具调用或冗长输出
   - 示例："阅读整个代码库并总结架构"
   - 示例："搜索所有包含关键词的文件"

4. **耗时任务**：需要深入搜索、多次读写文件的复杂任务
   - 示例："找出所有未处理的TODO并生成报告"

**原则**：如果你判断某个子任务**可能需要多次工具调用**或**答案相对独立**，就应使用 sub_agent 工具。

**sub_agent 工具特点**：子智能体拥有独立上下文，不污染主对话，只返回最终摘要。
"""
# 子智能体系统prompt
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."

# 创建绑定了工具的 LLM 实例
llm_with_tools = create_llm_with_tools(PARENT_TOOLS)
# 子智能体 LLM 实例


# -- Subagent: fresh context, filtered tools, summary-only return --
def run_subagent(prompt: str) -> str:
    """
    运行子智能体任务

    该函数创建一个独立的子智能体来执行特定任务，具有以下特点：
    1. 独立的上下文环境，不受父智能体上下文污染
    2. 使用受限的工具集（CHILD_TOOLS），比父智能体权限更小
    3. 循环调用工具直到完成或达到安全限制（30轮）
    4. 只返回最终的任务摘要，而非完整执行过程

    参数:
        prompt: 需要子智能体执行的任务描述

    返回:
        str: 任务执行结果的摘要文本，如果无结果则返回 "(no summary)"
    """
    sub_messages = [SystemMessage(content=SUBAGENT_SYSTEM)]  # fresh context，为子智能体创建全新的上下文环境
    sub_messages.append(HumanMessage(content=prompt))  # 将用户输入的任务描述作为人类消息添加
    # 子智能体创建独立的 LLM 实例，使用受限的子工具集
    sub_llm_with_tools = create_llm_with_tools(CHILD_TOOLS)
    # 安全限制：最多循环30次，防止无限循环
    for _ in range(30):
        # 调用子 LLM 获取响应
        sub_response = sub_llm_with_tools.invoke(sub_messages)
        # 将 LLM 响应添加到消息历史
        sub_messages.append(sub_response)
        # 检查是否需要调用工具，如果没有工具调用则说明任务完成
        if not hasattr(sub_response, "tool_calls") or not sub_response.tool_calls:
            return ""

        # 存储所有工具执行的结果
        results = []
        # 遍历需要执行的工具调用
        for tool_call in sub_response.tool_calls:
            if tool_call["name"]:
                # 获取对应的工具处理器
                handler = TOOL_HANDLERS.get(tool_call["name"])
                # 执行工具调用，传入工具参数
                output = handler(**tool_call["args"]) if handler else f"Unknown tool: {tool_call["name"]}"
                # 将工具执行结果包装成工具结果消息，限制内容长度不超过50000字符
                results.append({
                "type": "tool_result",
                "tool_name": tool_call["name"],
                "tool_args": tool_call["args"],
                "tool_id": tool_call["id"],
                "tool_output": str(output)[:50000],
                })
        # 将工具执行结果作为人类消息添加回对话上下文
        sub_messages.append(HumanMessage(content=json.dumps(results)))
    # 循环结束或达到限制时，提取并返回最终响应中的文本内容作为摘要
    return "".join(b.text for b in sub_response.content if hasattr(b, "text")) or "(no summary)"


#执行主体
def agent_loop(history_messages: list, session_file: Path, session_manager: SessionManager):

    rounds_since_todo = 0  # 记录距离上次更新待办事项的轮数

    while True:

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

        #此处用循环是因为大模型可能一次调用多个工具，每个工具都需要单独执行，每个工具的执行结果都需要加入到results中
        tool_call_results = []
        used_todo = False
        for tool_call in llm_response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_id = tool_call["id"]
            print("-" * 20)
            print(tool_call)
            print("----------")
            if tool_name == "task":
                # 调用子智能体来执行任务
                print(f"> task ({tool_args['description']}): {tool_args['prompt'][:80]}")
                tool_output = run_subagent(tool_args["prompt"])
            else:
                # 调用父智能体的工具
                if tool_name in TOOL_HANDLERS:
                    # 调用对应的工具函数来执行工具
                    tool_output = TOOL_HANDLERS[tool_name](**tool_args)
                else:
                    tool_output = f"Error: Unknown tool {tool_name}"
            
            print(f"工具 {tool_name} 执行结果: {tool_output}")
            print("-" * 20)
            tool_call_results.append({
                "type": "tool_result",
                "tool_name": tool_name,
                "tool_args": tool_args,
                "tool_id": tool_id,
                "tool_output": tool_output,
            })

            # 检查是否使用了 todo 工具
            if tool_name == "todo":
                used_todo = True
                rounds_since_todo = 0  # 重置计数器

        # 更新待办事项计数器
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        
        # 如果连续 3 轮没有更新待办事项，注入提醒
        if rounds_since_todo >= 3:
            tool_call_results.insert(0, {"type": "text", "text": "<reminder>Update your todos.</reminder>"})

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
