#!/usr/bin/env python3
"""
s10_team_protocols.py - 团队协议

关闭协议和计划审批协议，两者使用相同的 request_id 关联模式。
基于 s09 的团队消息机制构建。

    关闭状态机: pending -> approved | rejected

    负责人                          队友
    +---------------------+          +---------------------+
    | shutdown_request     |          |                     |
    | {                    | -------> | 接收请求            |
    |   request_id: abc    |          | 决定: 批准?         |
    | }                    |          |                     |
    +---------------------+          +---------------------+
                                             |
    +---------------------+          +-------v-------------+
    | shutdown_response    | <------- | shutdown_response   |
    | {                    |          | {                   |
    |   request_id: abc    |          |   request_id: abc   |
    |   approve: true      |          |   approve: true     |
    | }                    |          | }                   |
    +---------------------+          +---------------------+
            |
            v
    status -> "shutdown", 线程停止

    计划审批状态机: pending -> approved | rejected

    队友                            负责人
    +---------------------+          +---------------------+
    | plan_approval        |          |                     |
    | submit: {plan:"..."}| -------> | 审核计划文本         |
    +---------------------+          | 批准/拒绝?          |
                                     +---------------------+
                                             |
    +---------------------+          +-------v-------------+
    | plan_approval_resp   | <------- | plan_approval       |
    | {approve: true}      |          | review: {req_id,    |
    +---------------------+          |   approve: true}     |
                                     +---------------------+

    追踪器: {request_id: {"target|from": name, "status": "pending|..."}}

关键洞察: "相同的 request_id 关联模式，两个不同的应用领域。"
"""

import json
import os

from langchain_core.messages import HumanMessage, SystemMessage

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
你是一个专业的编程助手和团队负责人，工作目录是 {WORKDIR}。

## 团队管理
- spawn_teammate 创建持久化团队成员（独立线程运行）
- send_message / read_inbox / broadcast 与团队成员通信
- list_teammates 查看所有成员状态
- send_message 支持 message、shutdown_request、plan_approval_response 等类型
- shutdown_request 发送关闭请求后，如果队友未响应，不要重复轮询。尝试 send_message 再次沟通，或使用 force_shutdown 强制关闭
- 不要连续多次调用相同的查询工具（如 shutdown_response、read_inbox、list_teammates），如果连续 2-3 次得到相同结果，说明需要换一种方式

## sub_agent 使用时机
当子任务满足以下任一条件时，主动使用 sub_agent 分发：
1. 可拆分为多个独立子任务并行执行
2. 需要读取多个文件探索或收集信息
3. 可能产生大量工具调用，会污染主对话上下文

**原则**：子任务可能需要多次工具调用或答案相对独立，就使用 sub_agent。子智能体拥有独立上下文，只返回最终摘要。

## 场景选择指南：Teammate vs Sub-agent

| 场景 | 用哪个 | 原因 |
|------|--------|------|
| "持续监控日志文件，有异常时通知我" | **spawn_teammate** | 需要长期运行、持续交互 |
| "帮我查一下这个目录的结构和文件内容" | **sub_agent** | 一次性任务，不污染主对话 |
| "写一个单元测试，然后 review 代码" | **sub_agent** | 独立子任务，完成后返回结果即可 |
| "你负责后端API，我负责前端，我们协作开发" | **spawn_teammate** | 需要持续协作、双向沟通 |
| "同时搜索3个不同的目录" | **sub_agent** | 可并行分发多个独立子任务 |

Skills available：
{SKILL_LOADER.get_descriptions()}
"""
# 创建绑定了工具的 LLM 实例
llm_with_tools = create_llm_with_tools(PARENT_TOOLS)


# 最大智能体循环迭代次数，防止无限循环导致程序卡死
MAX_AGENT_ITERATIONS = 50

#执行主体
def agent_loop(history_messages: list, session_file: Path, session_manager: SessionManager):

    rounds_since_todo = 0  # 记录距离上次更新待办事项的轮数
    recent_tool_patterns = []  # 记录最近每轮调用的工具名集合，用于检测重复循环
    repetitive_warning_injected = False  # 是否已注入重复循环警告
    iteration = 0  # 循环迭代计数

    while True:
        iteration += 1
        if iteration > MAX_AGENT_ITERATIONS:
            print(f"\033[31m[警告] 智能体循环达到最大迭代次数 ({MAX_AGENT_ITERATIONS})，强制结束\033[0m")
            break

        # 读取收件箱消息
        inbox = BUS.read_inbox("lead")
        if inbox:
            # 处理 shutdown_response 类型的消息，更新关闭请求状态
            for msg in inbox:
                if msg.get("type") == "shutdown_response":
                    req_id = msg.get("request_id", "")
                    approve = msg.get("approve", False)
                    result = process_shutdown_response(req_id, approve)
                    print(f"\033[33m[收件箱] {result}\033[0m")
                    # 如果批准关闭，强制终止队友线程
                    if approve:
                        teammate = None
                        with _tracker_lock:
                            req = shutdown_requests.get(req_id)
                            if req:
                                teammate = req.get("target")
                        if teammate:
                            force_shutdown(teammate)
                            print(f"\033[33m[收件箱] 已强制关闭 '{teammate}'\033[0m")

            history_messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            history_messages.append({
                "role": "assistant",
                "content": "Noted inbox messages.",
            })

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

        # 记录本轮调用的工具名集合，用于重复循环检测
        current_tool_names = {tc["name"] for tc in llm_response.tool_calls}
        recent_tool_patterns.append(current_tool_names)
        if len(recent_tool_patterns) > 6:
            recent_tool_patterns.pop(0)

        # 检测重复循环：连续 4 轮调用完全相同的工具集
        is_repetitive = (
            len(recent_tool_patterns) >= 4
            and all(p == recent_tool_patterns[-1] for p in recent_tool_patterns[-4:])
        )

        # 如果已注入过警告但仍在重复，提前退出循环
        if is_repetitive and repetitive_warning_injected:
            print(f"\033[31m[检测到重复循环] 连续多轮调用相同工具 {current_tool_names}，强制结束\033[0m")
            # 注入强制结束提示
            tool_call_results = [{
                "type": "text",
                "text": "<system-break>You have been calling the same tools repeatedly. Provide your final response now without calling more tools.</system-break>"
            }]
            history_messages.append(HumanMessage(content=json.dumps(tool_call_results)))
            session_manager.append_message_to_session(session_file, history_messages[-1])
            return

        # 此处用循环是因为大模型可能一次调用多个工具，每个工具都需要单独执行，每个工具的执行结果都需要加入到results中
        tool_call_results = []
        used_todo = False
        for tool_call in llm_response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_id = tool_call["id"]
            print("-" * 20)
            print(tool_call)
            print("----------")
            if tool_name == "sub_agent":
                # 调用子智能体来执行任务
                print(f"> sub_agent ({tool_args['description']}): {tool_args['prompt'][:80]}")
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

        # 如果检测到重复循环，注入警告
        if is_repetitive and not repetitive_warning_injected:
            tool_call_results.insert(0, {
                "type": "text",
                "text": "<warning>You are calling the same tools repeatedly without progress. If you continue this pattern, the loop will be terminated. Try a different approach or provide your final response.</warning>"
            })
            repetitive_warning_injected = True

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
