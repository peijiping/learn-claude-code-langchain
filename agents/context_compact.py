#!/usr/bin/env python3
"""
context_compact.py - 上下文压缩模块（v2 教程 s08 对齐版）

实现"s08 Context Compact"四层压缩管线：
  L1: snip_compact        —— 消息条数 > 50 时，裁掉中间一段
  L2: micro_compact       —— 把"较旧"的 tool_result 替换成占位文本
  L3: tool_result_budget  —— 把超大的 tool_result 落盘，前端只保留预览
  L4: compact_history     —— 调用 LLM 对整段历史做摘要压缩
  兜底: reactive_compact  —— API 返回 prompt_too_long 时触发

设计原则：便宜优先，昂贵兜底。L1/L2/L3 都是 0 API 调用；L4 用一次 LLM 摘要；
reactive 是 API 报错的最后保险。

实时落盘：本模块在 L4 / reactive 触发时把压缩前的完整 messages 写到 .transcripts/，
保证即便摘要丢失关键信息也能从磁盘回溯（与 session_manager 的 session jsonl 互为补充：
session 是"每条消息逐条落盘"用于断点续传，transcript 是"压缩前全量快照"用于事后追溯）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


# ───────────────────────────────────────────────────────────────────
# 常量
# ───────────────────────────────────────────────────────────────────

# 默认上下文窗口大小（Claude Sonnet/Opus 为 200K，取 196608 作为安全上限）
DEFAULT_MAX_CONTEXT_TOKENS = 196_608

# 触发 L4 摘要压缩的字符数阈值（与 CC 源码保持量级一致；教学版用字符数估算）
CONTEXT_LIMIT_CHARS = 50_000

# 触发 L1 snip 压缩的消息条数阈值
SNIP_MAX_MESSAGES = 50
SNIP_KEEP_HEAD = 3
SNIP_KEEP_TAIL = 47  # = SNIP_MAX_MESSAGES - SNIP_KEEP_HEAD

# L2 micro_compact：保留最近 N 条 tool_result 不动
KEEP_RECENT_TOOL_RESULTS = 3

# L3 tool_result_budget：单条 tool_result 超过这个大小就落盘
PERSIST_THRESHOLD = 30_000
# L3 tool_result_budget：单条 user 消息内所有 tool_result 的总字节上限
MAX_TOOL_RESULT_BYTES = 200_000
# L3 落盘后保留的预览字符数
PREVIEW_LENGTH = 2_000

# 触发 L4 摘要压缩的字符数比率（与现有逻辑兼容：使用率 ≥ 80% 时启动）
SUMMARY_TRIGGER_RATIO = 0.80
# 触发 L1-L3 主动压缩的使用率阈值（≥ 95% 时启动）
PROACTIVE_TRIGGER_RATIO = 0.95

# L4 摘要时，保留前缀（SystemMessage + workspace 指令）的截止位置
# L4 摘要时，保留最后 N 条消息原文不被摘要（保证最近对话完整）
PRESERVE_RECENT_SUMMARY_MESSAGES = 10

# reactive 兜底时，保留最后 N 条消息原文
REACTIVE_KEEP_TAIL = 5

# reactive 兜底的最大重试次数
MAX_REACTIVE_RETRIES = 1

# L4 / reactive 时 transcript 落盘的目录名
TRANSCRIPT_DIRNAME = ".transcripts"
# L3 落盘大 tool_result 的目录名
TOOL_RESULTS_DIRNAME = ".task_outputs/tool-results"


# ───────────────────────────────────────────────────────────────────
# 上下文统计 / 压缩结果 数据类
# ───────────────────────────────────────────────────────────────────


@dataclass
class ContextStats:
    """上下文用量统计，用于 UI 展示与压缩决策。"""
    used_tokens: int
    max_tokens: int
    used_percent: float
    remaining_percent: float
    max_label: str


@dataclass
class CompactResult:
    """一次压缩/剪枝操作的结果。"""
    messages: list
    changed: bool
    operations: dict = field(default_factory=dict)
    before: Optional[ContextStats] = None
    after: Optional[ContextStats] = None


# ───────────────────────────────────────────────────────────────────
# 工具函数：消息内容归一化、消息类型判断
# ───────────────────────────────────────────────────────────────────


def _content_to_str(content) -> str:
    """把 LangChain 消息的 content 归一化为 str。

    - str: 原样返回
    - list: 多模态内容（TextBlock / dict 等），拼接所有 text 字段
    - 其他: 走 str()
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", block)))
            else:
                parts.append(getattr(block, "text", str(block)))
        return "\n".join(parts)
    return str(content)


def _is_ai_with_tool_use(msg) -> bool:
    """判断消息是否为带 tool_calls 的 AIMessage。"""
    return isinstance(msg, AIMessage) and bool(getattr(msg, "tool_calls", None))


def _is_tool_result_message(msg) -> bool:
    """判断消息是否为工具结果（ToolMessage）。

    教学版 s08 用的是 dict 列表结构（user 消息里嵌 tool_result），
    我们用 LangChain 的 ToolMessage 单类结构，所以判断类型即可。
    """
    return isinstance(msg, ToolMessage)


def _get_recent_ai_index(messages: list) -> int:
    """从尾部往前找最后一条 AIMessage 的索引；找不到返回 -1。

    用于定位"最近一轮"工具调用范围（AIMessage 之后的所有 ToolMessage）。
    """
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], AIMessage):
            return index
    return -1


# ───────────────────────────────────────────────────────────────────
# L1: snip_compact —— 裁掉中间消息
# ───────────────────────────────────────────────────────────────────


def snip_compact(messages: list, max_messages: int = SNIP_MAX_MESSAGES) -> list:
    """当消息条数 > max_messages 时，保留头 3 + 尾 47，中间用一个占位 HumanMessage 替代。

    关键技巧：调整边界时不能把"含 tool_use 的 AIMessage"和它对应的 ToolMessage 拆开——
    否则 LLM 会看到没有回执的 tool_use，行为异常。
    调整后窗口无重叠（head_end >= tail_start）则放弃压缩。
    """
    if len(messages) <= max_messages:
        return messages

    head_end, tail_start = SNIP_KEEP_HEAD, len(messages) - SNIP_KEEP_TAIL

    # 头边界调整：head_end-1 是含 tool_use 的 AIMessage 时，把 head_end 推过紧随的 ToolMessage
    if head_end > 0 and _is_ai_with_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1

    # 尾边界调整：tail_start 是 ToolMessage 但 tail_start-1 是 AIMessage(tool_calls)，
    # 说明对应的 tool_use 已被切到中间，回退 1 格避免孤立
    if (
        tail_start > 0
        and tail_start < len(messages)
        and _is_tool_result_message(messages[tail_start])
        and _is_ai_with_tool_use(messages[tail_start - 1])
    ):
        tail_start -= 1

    if head_end >= tail_start:
        return messages

    snipped = tail_start - head_end
    placeholder = HumanMessage(content=f"[snipped {snipped} messages from conversation middle]")
    return [*messages[:head_end], placeholder, *messages[tail_start:]]


# ───────────────────────────────────────────────────────────────────
# L2: micro_compact —— 旧 tool_result 用占位文本替换
# ───────────────────────────────────────────────────────────────────


def collect_tool_result_messages(messages: list) -> list[tuple[int, ToolMessage]]:
    """扫描所有消息，把 ToolMessage 的 (index, message) 元组收集起来。"""
    return [(i, m) for i, m in enumerate(messages) if isinstance(m, ToolMessage)]


def micro_compact(messages: list, keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> list:
    """替换所有"较旧"且"长度 > 120 字符"的 ToolMessage 的 content。

    占位文本提示 LLM：旧结果已丢弃，若需要可重新调用工具。
    不直接删除是为了保持 OpenAI/Anthropic API 的约束：每条 tool_use 必须有对应 tool_result。
    """
    tool_results = collect_tool_result_messages(messages)
    if len(tool_results) <= keep_recent:
        return messages
    for _, msg in tool_results[:-keep_recent]:
        content_str = _content_to_str(msg.content)
        if len(content_str) > 120:
            msg.content = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# ───────────────────────────────────────────────────────────────────
# L3: tool_result_budget —— 超大工具输出落盘
# ───────────────────────────────────────────────────────────────────


def persist_large_output(
    tool_use_id: str,
    output: str,
    tool_results_dir: Path,
) -> str:
    """把超大工具输出写到磁盘，返回"路径 + 2KB 预览"的占位文本。

    若文件已存在则跳过写入（避免重复劳动）。
    """
    if len(output) <= PERSIST_THRESHOLD:
        return output
    tool_results_dir.mkdir(parents=True, exist_ok=True)
    path = tool_results_dir / f"{tool_use_id}.txt"
    if not path.exists():
        try:
            path.write_text(output, encoding="utf-8")
        except OSError:
            # 落盘失败时仍返回原内容，避免压垮上下文
            return output
    return (
        f"<persisted-output>\n"
        f"Full output: {path}\n"
        f"Preview:\n{output[:PREVIEW_LENGTH]}\n"
        f"</persisted-output>"
    )


def tool_result_budget(
    messages: list,
    max_bytes: int = MAX_TOOL_RESULT_BYTES,
    persist_threshold: int = PERSIST_THRESHOLD,
    tool_results_dir: Optional[Path] = None,
) -> list:
    """监控"最近一轮"AIMessage 之后的所有 ToolMessage 的总字节数。

    超过 max_bytes 时，按从大到小依次把超大块落盘，直到总量达标。
    注意：只处理"最近一轮"——历史消息已经在更早的循环里被预算控制过。

    LangChain 适配：s08 教程的"最后一条 user 消息里所有 tool_result"在 langchain
    框架下对应"最后一条 AIMessage 之后的所有 ToolMessage"。
    """
    if tool_results_dir is None:
        tool_results_dir = Path.cwd() / TOOL_RESULTS_DIRNAME

    last_ai_index = _get_recent_ai_index(messages)
    if last_ai_index < 0:
        return messages

    # 收集最后一条 AIMessage 之后的所有 ToolMessage
    tool_msgs = [m for m in messages[last_ai_index + 1:] if isinstance(m, ToolMessage)]
    if not tool_msgs:
        return messages

    total = sum(len(_content_to_str(m.content)) for m in tool_msgs)
    if total <= max_bytes:
        return messages

    # 按体积从大到小排序，优先落盘最大的
    ranked = sorted(tool_msgs, key=lambda m: len(_content_to_str(m.content)), reverse=True)
    for msg in ranked:
        if total <= max_bytes:
            break
        content_str = _content_to_str(msg.content)
        if len(content_str) <= persist_threshold:
            continue
        new_content = persist_large_output(msg.tool_call_id, content_str, tool_results_dir)
        new_size = len(new_content)
        total = total - len(content_str) + new_size
        msg.content = new_content
    return messages


# ───────────────────────────────────────────────────────────────────
# L4: compact_history —— LLM 整段摘要
# ───────────────────────────────────────────────────────────────────


def write_transcript(messages: list, transcript_dir: Path) -> Path:
    """在压缩前把当前完整历史写到 .transcripts/transcript_<timestamp>.jsonl。

    万一摘要丢失关键信息，可以从磁盘恢复。这是与 session jsonl 互补的
    "全量快照"——session 是逐条追加的运行日志，transcript 是压缩前一次性快照。
    """
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            row = _message_to_dict(msg)
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def _message_to_dict(msg) -> dict:
    """把 LangChain 消息转成 dict 便于 jsonl 落盘。"""
    if isinstance(msg, SystemMessage):
        return {"role": "system", "content": msg.content}
    if isinstance(msg, HumanMessage):
        return {"role": "human", "content": msg.content}
    if isinstance(msg, AIMessage):
        row = {"role": "ai", "content": msg.content}
        if getattr(msg, "tool_calls", None):
            row["tool_calls"] = msg.tool_calls
        if getattr(msg, "id", None):
            row["id"] = msg.id
        return row
    if isinstance(msg, ToolMessage):
        return {
            "role": "tool",
            "content": msg.content,
            "tool_call_id": msg.tool_call_id,
        }
    return {"role": "unknown", "content": str(msg)}


def _format_message_for_summary(msg) -> str:
    """把单条消息格式化为 '[role]\\ncontent' 形式，供摘要 prompt 拼接。"""
    if isinstance(msg, SystemMessage):
        role = "system"
    elif isinstance(msg, HumanMessage):
        role = "human"
    elif isinstance(msg, AIMessage):
        role = "ai"
    elif isinstance(msg, ToolMessage):
        role = "tool"
    else:
        role = getattr(msg, "type", msg.__class__.__name__)

    content = _content_to_str(msg.content)
    extra = ""
    if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
        extra = "\ntool_calls: " + json.dumps(msg.tool_calls, ensure_ascii=False)
    if isinstance(msg, ToolMessage):
        extra = f"\ntool_call_id: {msg.tool_call_id}"
    return f"[{role}]\n{content}{extra}"


def _build_summary_prompt(messages: list) -> str:
    """构建摘要 prompt。"""
    transcript = "\n\n".join(_format_message_for_summary(msg) for msg in messages)
    return f"""请将以下对话历史压缩为结构化摘要，保留后续继续任务所需的事实、决策、文件、错误和待办。

摘要必须使用以下 9 个章节标题：
1. Primary Request and Intent — 用户的请求和意图
2. Key Technical Concepts — 关键技术概念
3. Files and Code Sections — 涉及的文件和代码片段
4. Errors and fixes — 遇到的错误和修复
5. Problem Solving — 问题解决过程
6. All user messages — 所有用户消息（非工具结果）
7. Pending Tasks — 待完成的任务
8. Current Work — 当前进行的工作
9. Optional Next Step — 可选的下一步

对话历史：
{transcript}
"""


def summarize_history(
    messages: list,
    summarizer: Optional[Callable[[str], str]] = None,
) -> str:
    """调用 summarizer 对消息列表生成结构化摘要文本。

    若外部注入了 summarizer（便于测试或使用更便宜的模型），直接调用；
    否则临时用 llm_manage.create_llm(max_tokens=4000) 构造一个 LLM 执行摘要。
    """
    prompt = _build_summary_prompt(messages)
    if summarizer is not None:
        return summarizer(prompt)

    from llm_manage import create_llm

    response = create_llm(max_tokens=4000).invoke([HumanMessage(content=prompt)])
    return _content_to_str(response.content) or "(empty summary)"


def _protected_prefix_end(messages: list) -> int:
    """返回受保护前缀的结束位置。

    受保护前缀包含：
      1. SystemMessage（角色人设/系统提示）—— 不能进摘要，否则模型丢失人设
      2. 紧随其后的 workspace/CLAUDE.md 或 AGENT.md 注入消息
    """
    prefix_end = 1 if messages and isinstance(messages[0], SystemMessage) else 0
    if len(messages) > prefix_end and _is_workspace_instruction_message(messages[prefix_end]):
        prefix_end += 1
    return prefix_end


def _is_workspace_instruction_message(msg) -> bool:
    """判断消息是否为 workspace 规则注入消息（由 SessionManager 启动时写入）。"""
    return (
        isinstance(msg, HumanMessage)
        and isinstance(msg.content, str)
        and (
            "以下是 workspace/CLAUDE.md 内容：" in msg.content
            or "以下是 workspace/AGENT.md 内容：" in msg.content
        )
    )


def _expand_recent_start_for_tool_pairs(messages: list, recent_start: int) -> int:
    """把保留后缀的起点向前扩展，确保每个 ToolMessage 都能找到对应的 tool_call。

    如果保留后缀里含 ToolMessage 但其 tool_call 已经在摘要范围里，模型会看到
    孤立的 tool_result 而无法理解它为什么出现。
    """
    start = recent_start
    while start > 0 and isinstance(messages[start], ToolMessage):
        previous_ai = _find_previous_ai_with_tool_call(messages, start, messages[start].tool_call_id)
        if previous_ai is None or previous_ai >= start:
            break
        start = previous_ai
    return start


def _find_previous_ai_with_tool_call(messages: list, before_index: int, tool_call_id: str) -> Optional[int]:
    """从 before_index 往前找包含指定 tool_call_id 的 AIMessage。

    遇到 HumanMessage 时停止（人类消息标志着又一轮对话的开始，
    跨轮次的 tool_call 配对已经无意义）。
    """
    for index in range(before_index - 1, -1, -1):
        msg = messages[index]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            if any(tc.get("id") == tool_call_id for tc in msg.tool_calls):
                return index
        if isinstance(msg, HumanMessage):
            break
    return None


def compact_history(
    messages: list,
    summarizer: Optional[Callable[[str], str]] = None,
    transcript_dir: Optional[Path] = None,
) -> list:
    """把中间一段 messages 压缩为单条摘要 HumanMessage。

    - 保留前缀：SystemMessage + workspace 指令消息
    - 保留后缀：最后 PRESERVE_RECENT_SUMMARY_MESSAGES 条消息原文
    - 后缀起点向前扩展到匹配的 tool_call AI 消息，避免孤立 tool_result
    - 压缩前先 write_transcript 做全量快照，便于事后追溯
    """
    if transcript_dir is None:
        transcript_dir = Path.cwd() / TRANSCRIPT_DIRNAME

    transcript_path = write_transcript(messages, transcript_dir)

    if len(messages) <= PRESERVE_RECENT_SUMMARY_MESSAGES + 1:
        return messages

    prefix_end = _protected_prefix_end(messages)
    recent_start = max(prefix_end, len(messages) - PRESERVE_RECENT_SUMMARY_MESSAGES)
    recent_start = _expand_recent_start_for_tool_pairs(messages, recent_start)
    if recent_start <= prefix_end:
        return messages

    to_summarize = messages[prefix_end:recent_start]
    if not to_summarize:
        return messages

    summary = summarize_history(to_summarize, summarizer=summarizer)
    summary_msg = HumanMessage(content=f"<context_summary>\n{summary}\n</context_summary>")
    result = [*messages[:prefix_end], summary_msg, *messages[recent_start:]]
    print(f"[transcript saved: {transcript_path}]")
    return result


# ───────────────────────────────────────────────────────────────────
# 兜底: reactive_compact —— API 报错时触发
# ───────────────────────────────────────────────────────────────────


def reactive_compact(
    messages: list,
    summarizer: Optional[Callable[[str], str]] = None,
    transcript_dir: Optional[Path] = None,
    keep_tail: int = REACTIVE_KEEP_TAIL,
) -> list:
    """兜底压缩：把最早一段历史让 LLM 摘要，保留最近 keep_tail 条原样。

    与 L4 的差异：L4 是"主动预防"（token 估算超阈值时触发）；
    reactive 是"被动兜底"（API 真正返回 prompt_too_long 时触发）。

    边界同样要避开把 tool_use 和它对应 tool_result 拆开的情况。
    压缩前也写 transcript，与 L4 保持一致的"全量快照"策略。
    """
    if transcript_dir is None:
        transcript_dir = Path.cwd() / TRANSCRIPT_DIRNAME

    transcript_path = write_transcript(messages, transcript_dir)

    tail_start = max(0, len(messages) - keep_tail)
    # 边界回退：若要保留的第一条是 ToolMessage 且上一条含 tool_use，多保留 1 条
    if (
        tail_start > 0
        and tail_start < len(messages)
        and _is_tool_result_message(messages[tail_start])
        and _is_ai_with_tool_use(messages[tail_start - 1])
    ):
        tail_start -= 1

    summary = summarize_history(messages[:tail_start], summarizer=summarizer)
    print(f"[transcript saved: {transcript_path}]")
    return [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]


# ───────────────────────────────────────────────────────────────────
# ContextCompactManager：编排四层管线 + reactive 兜底
# ───────────────────────────────────────────────────────────────────


class ContextCompactManager:
    """四层压缩管线编排器（ContextCompactManager）。

    工作流程（由 compact_if_needed 编排）：
      1. L3 tool_result_budget —— 把超大 tool_result 落盘
      2. L1 snip_compact       —— 裁掉中间消息
      3. L2 micro_compact      —— 旧 tool_result 占位
      4. L4 compact_history    —— LLM 摘要（仅当 token 估算仍超阈值时）

    reactive 兜底（由 handle_api_error 编排）：
      - API 返回 prompt_too_long 时调用 reactive_compact
      - 默认最多重试 1 次，再次失败把异常上抛

    实时落盘：本模块自身不直接落盘 session jsonl；调用方（SessionManager）
    在拿到 CompactResult.changed=True 后调 save_session_history 同步磁盘。
    L4 / reactive 触发的 transcript 快照由本模块在内部完成。
    """

    def __init__(
        self,
        max_context_tokens: Optional[int] = None,
        summarizer: Optional[Callable[[str], str]] = None,
        transcript_dir: Optional[Path] = None,
        tool_results_dir: Optional[Path] = None,
    ):
        self.max_context_tokens = max_context_tokens or self.parse_max_context_tokens(
            os.environ.get("MAX_CONTEXT_TOKENS"),
            DEFAULT_MAX_CONTEXT_TOKENS,
        )
        self.summarizer = summarizer
        self.transcript_dir = transcript_dir or Path.cwd() / TRANSCRIPT_DIRNAME
        self.tool_results_dir = tool_results_dir or Path.cwd() / TOOL_RESULTS_DIRNAME
        self._reactive_retries = 0

    # ── 解析 / 格式化 ─────────────────────────────────────────────

    @staticmethod
    def parse_max_context_tokens(value: Optional[str], default: int = DEFAULT_MAX_CONTEXT_TOKENS) -> int:
        """解析 '200K' / '1M' / '196608' 这类字符串为 token 整数。"""
        if value is None:
            return default

        text = str(value).strip().upper()
        if not text:
            return default

        multiplier = 1
        if text.endswith("M"):
            multiplier = 1_000_000
            text = text[:-1]
        elif text.endswith("K"):
            multiplier = 1_000
            text = text[:-1]

        try:
            parsed = float(text)
        except ValueError:
            return default

        tokens = int(parsed * multiplier)
        return tokens if tokens > 0 else default

    @staticmethod
    def format_token_count(tokens: int) -> str:
        """把 token 整数格式化为可读形式：整百万显示为 '1M'，整千显示为 '200K'，其他保持原样。"""
        if tokens >= 1_000_000 and tokens % 1_000_000 == 0:
            return f"{tokens // 1_000_000}M"
        if tokens >= 1_000 and tokens % 1_000 == 0:
            return f"{tokens // 1_000}K"
        return str(tokens)

    # ── token 估算（与旧版兼容） ──────────────────────────────────

    def estimate_tokens(self, messages: list) -> int:
        """粗略估算消息列表的 token 数。

        启发式系数：
          - 中文字符按 1.5 token 计
          - 英文单词按 1.3 token 计
          - 其他字符按 0.5 token 计
        每条消息额外加 4 token 作为 role + 格式开销。
        AI 消息的 tool_calls 单独按 JSON 长度估算。
        """
        total_tokens = 0

        for msg in messages:
            total_tokens += 4
            content_str = _content_to_str(self._message_content(msg))
            chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", content_str))
            english_words = len(re.findall(r"[a-zA-Z]+", content_str))
            other_chars = max(0, len(content_str) - chinese_chars - english_words)
            total_tokens += int(chinese_chars * 1.5 + english_words * 1.3 + other_chars * 0.5)

            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                total_tokens += len(json.dumps(msg.tool_calls, ensure_ascii=False)) // 4

        return total_tokens

    def _message_content(self, msg):
        """取消息 content 用于 token 估算。"""
        if hasattr(msg, "content"):
            return msg.content
        if isinstance(msg, dict):
            return msg.get("content", "")
        return str(msg)

    def context_stats(self, messages: list) -> ContextStats:
        """统计当前 messages 的使用率/剩余百分比等指标。"""
        used_tokens = self.estimate_tokens(messages)
        used_percent = min(100.0, (used_tokens / self.max_context_tokens) * 100)
        remaining_percent = max(0.0, 100.0 - used_percent)
        return ContextStats(
            used_tokens=used_tokens,
            max_tokens=self.max_context_tokens,
            used_percent=used_percent,
            remaining_percent=remaining_percent,
            max_label=self.format_token_count(self.max_context_tokens),
        )

    def format_context_label(self, messages: list) -> str:
        """生成 'max：200K，used：12xxx，xx%' 形式的简短标签，供 UI 状态栏展示。"""
        stats = self.context_stats(messages)
        return f"max：{stats.max_label}，used：{stats.used_tokens}，{int(stats.remaining_percent)}%"

    # ── 四层管线：L3 → L1 → L2 → [L4?] ──────────────────────────

    def compact_if_needed(self, messages: list, force: bool = False) -> CompactResult:
        """根据上下文使用率自动决定是否压缩。供 agent 主循环在每轮调用前调用。

        顺序与 CC 源码一致：budget → snip → micro → [token 超阈值] → auto。
        force=True 时跳过阈值判断强制走完整流程（手动 /compact 时使用）。
        """
        before = self.context_stats(messages)
        operations = {
            "tool_results_persisted": 0,
            "messages_snip_compacted": 0,
            "tool_results_micro_compacted": 0,
            "summary_messages_replaced": 0,
            "transcript_written": None,
        }

        # 未达到 PROACTIVE 触发阈值且非 force，直接返回
        if not force and before.used_percent < PROACTIVE_TRIGGER_RATIO * 100:
            return CompactResult(
                messages=messages,
                changed=False,
                operations=operations,
                before=before,
                after=before,
            )

        current = messages
        changed = False

        # L3：把超大 tool_result 落盘（in-place 修改 messages）
        if self._step_budget(current):
            operations["tool_results_persisted"] = self._count_persisted(current)
            changed = True

        # L1：裁中间消息
        snipped = snip_compact(current)
        if len(snipped) != len(current):
            operations["messages_snip_compacted"] = len(current) - len(snipped)
            current = snipped
            changed = True

        # L2：旧 tool_result 占位
        micro_before = sum(
            1 for m in current
            if isinstance(m, ToolMessage)
            and _content_to_str(m.content) != "[Earlier tool result compacted. Re-run if needed.]"
        )
        micro_compact(current)
        micro_after = sum(
            1 for m in current
            if isinstance(m, ToolMessage)
            and _content_to_str(m.content) != "[Earlier tool result compacted. Re-run if needed.]"
        )
        if micro_before != micro_after:
            operations["tool_results_micro_compacted"] = micro_before - micro_after
            changed = True

        # L4：仍超阈值（或 force）则用 LLM 摘要
        after_pre = self.context_stats(current)
        if force or after_pre.used_percent >= SUMMARY_TRIGGER_RATIO * 100:
            new_messages = compact_history(
                current,
                summarizer=self.summarizer,
                transcript_dir=self.transcript_dir,
            )
            if len(new_messages) != len(current):
                operations["summary_messages_replaced"] = len(current) - len(new_messages)
                operations["transcript_written"] = str(self.transcript_dir / "transcript_*.jsonl")
                current = new_messages
                changed = True

        after = self.context_stats(current)
        return CompactResult(
            messages=current,
            changed=changed,
            operations=operations,
            before=before,
            after=after,
        )

    def _step_budget(self, messages: list) -> bool:
        """L3 tool_result_budget 的薄包装；返回是否发生了变更。"""
        before = sum(
            len(_content_to_str(m.content))
            for m in messages
            if isinstance(m, ToolMessage)
        )
        tool_result_budget(
            messages,
            max_bytes=MAX_TOOL_RESULT_BYTES,
            persist_threshold=PERSIST_THRESHOLD,
            tool_results_dir=self.tool_results_dir,
        )
        after = sum(
            len(_content_to_str(m.content))
            for m in messages
            if isinstance(m, ToolMessage)
        )
        return after < before

    def _count_persisted(self, messages: list) -> int:
        """统计当前 messages 里被 L3 标记为 persisted-output 的 ToolMessage 条数。"""
        count = 0
        for m in messages:
            if isinstance(m, ToolMessage):
                content_str = _content_to_str(m.content)
                if "<persisted-output>" in content_str:
                    count += 1
        return count

    # ── 兜底：reactive compact（API 报错时调用） ──────────────────

    def handle_api_error(self, messages: list, error: Exception) -> Optional[CompactResult]:
        """API 报错时尝试 reactive compact 兜底。

        - 仅在错误信息含 'prompt_too_long' / 'too many tokens' / 'context_length_exceeded' 时触发
        - 默认最多重试 MAX_REACTIVE_RETRIES（1）次
        - 再次失败返回 None，由调用方决定如何处理（通常是抛出去）

        返回 None 表示不可重试，调用方应抛出原异常。
        返回 CompactResult 表示已执行 reactive compact（changed=True），
        调用方应在压缩后的 messages 上重试一次 LLM 调用。
        """
        err_str = str(error).lower()
        is_prompt_too_long = any(
            marker in err_str
            for marker in (
                "prompt_too_long",
                "too many tokens",
                "context_length_exceeded",
                "context length exceeded",
                "maximum context length",
            )
        )
        if not is_prompt_too_long:
            return None
        if self._reactive_retries >= MAX_REACTIVE_RETRIES:
            return None

        new_messages = reactive_compact(
            messages,
            summarizer=self.summarizer,
            transcript_dir=self.transcript_dir,
        )
        self._reactive_retries += 1
        return CompactResult(
            messages=new_messages,
            changed=True,
            operations={
                "reactive_compact_triggered": True,
                "transcript_written": str(self.transcript_dir / "transcript_*.jsonl"),
            },
        )

    def reset_reactive_counter(self) -> None:
        """LLM 调用成功后重置 reactive 兜底计数器。"""
        self._reactive_retries = 0
