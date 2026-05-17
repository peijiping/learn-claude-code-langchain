#!/usr/bin/env python3
"""
subagent.py - 子智能体模块

提供独立的子智能体执行环境，用于在隔离的上下文中执行子任务。
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from llm_manage import create_llm_with_tools
from tools import TOOLS, TOOL_HANDLERS, WORKDIR

SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


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
    sub_messages = [SystemMessage(content=SUBAGENT_SYSTEM)]
    sub_messages.append(HumanMessage(content=prompt))
    sub_llm_with_tools = create_llm_with_tools(TOOLS)
    for _ in range(30):
        sub_response = sub_llm_with_tools.invoke(sub_messages)
        sub_messages.append(sub_response)
        if not hasattr(sub_response, "tool_calls") or not sub_response.tool_calls:
            if hasattr(sub_response, "content"):
                if isinstance(sub_response.content, list):
                    return "".join(b.text for b in sub_response.content if hasattr(b, "text")) or "(no summary)"
                return sub_response.content or "(no summary)"
            return "(no summary)"

        results = []
        for tool_call in sub_response.tool_calls:
            if tool_call["name"]:
                handler = TOOL_HANDLERS.get(tool_call["name"])
                output = handler(**tool_call["args"]) if handler else f"Unknown tool: {tool_call["name"]}"
                results.append({
                    "type": "tool_result",
                    "tool_name": tool_call["name"],
                    "tool_args": tool_call["args"],
                    "tool_id": tool_call["id"],
                    "tool_output": str(output)[:50000],
                })
        sub_messages.append(HumanMessage(content=json.dumps(results)))
    if hasattr(sub_response, "content"):
        if isinstance(sub_response.content, list):
            return "".join(b.text for b in sub_response.content if hasattr(b, "text")) or "(no summary)"
        return sub_response.content or "(no summary)"
    return "(no summary)"