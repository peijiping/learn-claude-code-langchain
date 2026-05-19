#!/usr/bin/env python3
"""
subagent.py - 通用型子智能体模块

提供独立的子智能体执行环境，用于在隔离的上下文中执行子任务。
子智能体默认拥有全部工具权限，通过 prompt 引导行为，而非通过类型限制。
支持通过 system_prompt 和 allowed_tools 参数自定义子智能体的能力和角色。
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from llm_manage import create_llm_with_tools
from tools import CHILD_TOOLS_SUBAGENT, TOOL_HANDLERS, WORKDIR


DEFAULT_SYSTEM_PROMPT = f"""你是一个通用型子智能体，工作目录是 {WORKDIR}。

## 核心规则
1. **任务导向**：严格按照任务描述完成指定工作，不要发散
2. **输出控制**：每次工具调用都要限制输出量。读取文件时使用 limit 参数，bash 命令用 | head 限制行数
3. **PDF 读取**：必须使用 read_pdf 工具读取 PDF，不要使用 strings/cat 等命令
4. **摘要优先**：你的输出是给主智能体看的，只返回关键发现和结果，不要返回原始数据
5. **安全操作**：执行写入或删除操作前，确认目标路径在工作目录内
6. **看板边界**：不要创建或更新 todo 看板；会话看板由主智能体统一维护

## 输出格式
完成任务后，用以下格式返回摘要：
### 结果
[任务完成的关键结果]
### 要点
- [关键发现或实现细节]
### 注意
[需要注意的问题或后续工作]"""


def _extract_content(response) -> str:
    """从 LLM 响应中提取文本内容"""
    if not hasattr(response, "content"):
        return ""
    if isinstance(response.content, list):
        return "".join(b.text for b in response.content if hasattr(b, "text"))
    return str(response.content) if response.content else ""


def _truncate_messages(messages: list, max_history_rounds: int = 6) -> list:
    """
    截断消息历史，防止上下文无限膨胀。

    策略：
    - 始终保留 SystemMessage（第0条）和第一条 HumanMessage（任务描述）
    - 对历史对话轮次（LLM回复 + 工具结果）进行滑动窗口截断，只保留最近 N 轮
    - 对超长工具结果消息进行压缩替换

    参数:
        messages: 当前消息列表
        max_history_rounds: 保留的最大历史轮次（每轮 = 1条LLM回复 + 1条工具结果）

    返回:
        截断后的消息列表
    """
    if len(messages) <= 2:
        return messages

    # 保留头部：SystemMessage + 第一条 HumanMessage
    head = messages[:2]
    # 历史部分：从第2条开始
    tail = messages[2:]

    # 每轮历史包含 2 条消息：LLM回复 + 工具结果HumanMessage
    # 只保留最近 max_history_rounds * 2 条
    max_tail_len = max_history_rounds * 2
    if len(tail) > max_tail_len:
        tail = tail[-max_tail_len:]
        # 在截断处插入提示，告知 LLM 前面的历史已被省略
        truncation_notice = HumanMessage(
            content="[系统提示：前面的工具执行历史已省略，以下是最近的操作记录。请基于已有信息继续完成任务并返回摘要。]"
        )
        tail = [truncation_notice] + tail

    # 对 tail 中每条消息做长度检查，超长则压缩
    compressed_tail = []
    for msg in tail:
        content = getattr(msg, "content", "")
        if isinstance(content, str) and len(content) > 15000:
            # 超长消息：保留首尾，中间截断
            head_text = content[:5000]
            tail_text = content[-5000:]
            compressed = (
                f"{head_text}\n\n"
                f"... [内容已截断，原长度 {len(content)} 字符，保留首 5000 + 尾 5000] ...\n\n"
                f"{tail_text}"
            )
            compressed_tail.append(
                HumanMessage(content=compressed) if isinstance(msg, HumanMessage) else type(msg)(content=compressed)
            )
        else:
            compressed_tail.append(msg)

    return head + compressed_tail


def run_subagent(
    prompt: str,
    system_prompt: str | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """
    运行通用型子智能体任务

    该函数创建一个独立的子智能体来执行特定任务，具有以下特点：
    1. 独立的上下文环境，不受父智能体上下文污染
    2. 默认拥有全部工具权限，通过 prompt 引导行为
    3. 可通过 system_prompt 自定义角色和行为约束
    4. 可通过 allowed_tools 限制可用工具范围
    5. 循环调用工具直到完成或达到安全限制（30轮）
    6. 只返回最终的任务摘要，而非完整执行过程

    参数:
        prompt: 需要子智能体执行的任务描述
        system_prompt: 自定义系统提示，为 None 时使用默认通用提示
        allowed_tools: 允许使用的工具名称列表，为 None 时使用全部工具。
                       例如 ["bash", "read_file", "read_pdf"] 限制为只读工具集

    返回:
        str: 任务执行结果的摘要文本，如果无结果则返回 "(no summary)"
    """
    if allowed_tools is not None:
        sub_tools = [t for t in CHILD_TOOLS_SUBAGENT if t["name"] in allowed_tools]
    else:
        sub_tools = CHILD_TOOLS_SUBAGENT

    sub_system = system_prompt or DEFAULT_SYSTEM_PROMPT

    sub_messages = [SystemMessage(content=sub_system)]
    sub_messages.append(HumanMessage(content=prompt))
    sub_llm_with_tools = create_llm_with_tools(sub_tools)

    tools_label = f"{len(sub_tools)} tools" if allowed_tools else "all child tools"
    print(f"  [subagent] 开始执行任务 ({tools_label}): {prompt[:80]}...")

    for iteration in range(30):
        # 每 3 轮截断一次上下文，防止消息无限膨胀
        if iteration > 0 and iteration % 3 == 0:
            original_len = len(sub_messages)
            sub_messages = _truncate_messages(sub_messages, max_history_rounds=6)
            if len(sub_messages) < original_len:
                print(f"  [subagent] 上下文截断: {original_len} 条 -> {len(sub_messages)} 条")

        try:
            sub_response = sub_llm_with_tools.invoke(sub_messages)
        except Exception as e:
            error_msg = f"子智能体 API 调用失败 (第 {iteration + 1} 轮): {type(e).__name__}: {e}"
            print(f"  [subagent] {error_msg}")
            return error_msg

        sub_messages.append(sub_response)

        if not hasattr(sub_response, "tool_calls") or not sub_response.tool_calls:
            content = _extract_content(sub_response)
            return content or "(no summary)"

        results = []
        for tool_call in sub_response.tool_calls:
            if tool_call["name"]:
                handler = TOOL_HANDLERS.get(tool_call["name"])
                if handler:
                    try:
                        output = handler(**tool_call["args"])
                    except Exception as e:
                        output = f"Error executing {tool_call['name']}: {e}"
                else:
                    output = f"Unknown tool: {tool_call['name']}"
                results.append({
                    "type": "tool_result",
                    "tool_name": tool_call["name"],
                    "tool_args": tool_call["args"],
                    "tool_id": tool_call["id"],
                    "tool_output": str(output)[:5000],
                })

        sub_messages.append(HumanMessage(content=json.dumps(results)))
        print(f"  [subagent] 第 {iteration + 1} 轮，执行了 {len(results)} 个工具调用")

    # 30 轮到达上限，尝试从最后一轮响应中提取内容返回
    content = _extract_content(sub_response)
    if content:
        return f"[达到最大轮次限制，返回最后一轮摘要]\n{content}"
    return "(no summary: 达到最大轮次限制且最后一轮无内容)"
