#!/usr/bin/env python3
"""
s02_tool_use_langchain.py - Tools

The agent loop from s01 didn't change. We just added tools to the array
and a dispatch map to route calls.

    +----------+      +-------+      +------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch    |
    |  prompt  |      |       |      | {                |
    +----------+      +---+---+      |   bash: run_bash |
                          ^          |   read: run_read |
                          |          |   write: run_wr  |
                          +----------+   edit: run_edit |
                          tool_result| }                |
                                     +------------------+

Key insight: "The loop didn't change at all. I just added tools."
"""

import json
import os

from langchain_core.messages import HumanMessage, SystemMessage

from pathlib import Path
from dotenv import load_dotenv
from session_manage import SessionManager
from tools import TOOLS, TOOL_HANDLERS, WORKDIR
from llm_manage import create_llm_with_tools

# 加载环境变量
load_dotenv(override=True)

#对话历史目录
CHAT_HISTORY_DIR = WORKDIR / ".chathistory"
CHAT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

# 系统prompt
SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain.所有操作都必须在工作目录下进行。"

# 创建绑定了工具的 LLM 实例
llm_with_tools = create_llm_with_tools(TOOLS)


#执行主体
def agent_loop(history_messages: list, session_file: Path, session_manager: SessionManager):
    while True:
        # 在调用 LLM 前截断上下文，确保不超过限制
        history_messages[:] = session_manager.trim_messages_to_limit(history_messages)

        llm_response = llm_with_tools.invoke(history_messages)
        # 加入大模型回复到历史消息中
        history_messages.append(llm_response)
        session_manager.append_message_to_session(session_file, llm_response)

        if not hasattr(llm_response, "tool_calls") or not llm_response.tool_calls:
            return

        #此处用循环是因为大模型可能一次调用多个工具，每个工具都需要单独执行，每个工具的执行结果都需要加入到results中
        tool_call_results = []
        for tool_call in llm_response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_id = tool_call["id"]
            print("-" * 20)
            print(tool_call)
            print("----------")
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
