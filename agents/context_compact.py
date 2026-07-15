#!/usr/bin/env python3
"""
context_compact.py — 上下文压缩（v2 教程 s08 对齐版）

四层压缩管线 + reactive 兜底，编排顺序与 CC 源码一致：

    L3 budget → L1 snip → L2 micro → [token 超阈值?] → L4 summary
                                                            ↓
                                          [API prompt_too_long?]
                                                            ↓
                                                        reactive

L1/L2/L3 都是 0 API 调用；L4 用一次 LLM 摘要；reactive 是 API 报错的最后保险。
L4 / reactive 触发时把压缩前的完整 messages 写到 .transcripts/，便于事后追溯。

文件结构（自上而下读）：
  1. 配置常量（.env 可覆盖）
  2. 数据类（ContextStats / CompactResult）
  3. 消息工具（content 归一化、类型判断、序列化）— 模块级
  4. Token 工具（解析、格式化、估算）         — 模块级
  5. L1-L4 + reactive 压缩函数              — 模块级
  6. ContextCompact 编排器                   — 依赖注入 + 编排 + 兜底

公共 API：class ContextCompact 的方法；其余函数为内部实现。
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

from tool_base import TRANSCRIPT_DIRNAME, TOOL_RESULTS_DIRNAME


# ── 1. 配置常量（运行时可由 .env 覆盖） ──────────────────────────────
# 路径之外的可调参数一律 os.environ.get(KEY) or default 内联读取；
# 新增/修改时同步更新 .env 与 .env.example，详见 AGENTS.md。


def _int_env(key: str, default: int) -> int:
    return int(os.environ.get(key) or default)


def _float_env(key: str, default: float) -> float:
    return float(os.environ.get(key) or default)


DEFAULT_MAX_CONTEXT_TOKENS = _int_env("DEFAULT_MAX_CONTEXT_TOKENS", 1000000)
CONTEXT_LIMIT_CHARS = _int_env("CONTEXT_LIMIT_CHARS", 50000)

# L1 snip
SNIP_MAX_MESSAGES = _int_env("SNIP_MAX_MESSAGES", 50)
SNIP_KEEP_HEAD = _int_env("SNIP_KEEP_HEAD", 3)
SNIP_KEEP_TAIL = _int_env("SNIP_KEEP_TAIL", SNIP_MAX_MESSAGES - SNIP_KEEP_HEAD)

# L2 micro
KEEP_RECENT_TOOL_RESULTS = _int_env("KEEP_RECENT_TOOL_RESULTS", 3)

# L3 persist
PERSIST_THRESHOLD = _int_env("PERSIST_THRESHOLD", 30000)
MAX_TOOL_RESULT_BYTES = _int_env("MAX_TOOL_RESULT_BYTES", 200000)
PREVIEW_LENGTH = _int_env("PREVIEW_LENGTH", 2000)

# 触发阈值
SUMMARY_TRIGGER_RATIO = _float_env("SUMMARY_TRIGGER_RATIO", 0.80)
PROACTIVE_TRIGGER_RATIO = _float_env("PROACTIVE_TRIGGER_RATIO", 0.95)

# L4 / reactive 保留窗口
PRESERVE_RECENT_SUMMARY_MESSAGES = _int_env("PRESERVE_RECENT_SUMMARY_MESSAGES", 10)
REACTIVE_KEEP_TAIL = _int_env("REACTIVE_KEEP_TAIL", 5)
MAX_REACTIVE_RETRIES = _int_env("MAX_REACTIVE_RETRIES", 1)

# reactive 触发的错误关键字（API 报"prompt too long"）
REACTIVE_ERROR_MARKERS = (
    "prompt_too_long",
    "too many tokens",
    "context_length_exceeded",
    "context length exceeded",
    "maximum context length",
)

# L4 摘要 prompt 模板（9 段式结构化摘要）
_SUMMARY_PROMPT = """\
请将以下对话历史压缩为结构化摘要，保留后续继续任务所需的事实、决策、文件、错误和待办。

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


# ── 2. 数据类 ────────────────────────────────────────────────────────


@dataclass
class ContextStats:
    """上下文用量统计：用于 UI 展示与压缩决策。"""
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


# ── 3. 消息工具：content 归一化、类型判断、序列化 ───────────────────


def content_to_str(content) -> str:
    """把 LangChain 消息的 content 归一化为 str。"""
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


def message_to_text(msg) -> str:
    """从 LangChain 消息 / dict / 其他对象里取出文本 content（统一为 str）。"""
    if hasattr(msg, "content"):
        return content_to_str(msg.content)
    if isinstance(msg, dict):
        return content_to_str(msg.get("content", ""))
    return str(msg)


def is_ai_with_tool_use(msg) -> bool:
    return isinstance(msg, AIMessage) and bool(getattr(msg, "tool_calls", None))


def is_tool_result(msg) -> bool:
    return isinstance(msg, ToolMessage)


def is_workspace_instruction(msg) -> bool:
    """判断 msg 是否为 SessionManager 启动时写入的 workspace 规则注入消息。"""
    return (
        isinstance(msg, HumanMessage)
        and isinstance(msg.content, str)
        and (
            "以下是 workspace/CLAUDE.md 内容：" in msg.content
            or "以下是 workspace/AGENT.md 内容：" in msg.content
        )
    )


def message_to_dict(msg) -> dict:
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
        return {"role": "tool", "content": msg.content, "tool_call_id": msg.tool_call_id}
    return {"role": "unknown", "content": str(msg)}


# ── 4. Token 工具：解析、格式化、估算 ──────────────────────────────


def parse_max_context_tokens(value: Optional[str], default: int) -> int:
    """解析 '200K' / '1M' / '196608' 这类字符串为 token 整数。"""
    if not value:
        return default
    text = str(value).strip().upper()
    if not text:
        return default

    multiplier = 1
    if text.endswith("M"):
        multiplier, text = 1_000_000, text[:-1]
    elif text.endswith("K"):
        multiplier, text = 1_000, text[:-1]

    try:
        tokens = int(float(text) * multiplier)
    except ValueError:
        return default
    return tokens if tokens > 0 else default


def format_token_count(tokens: int) -> str:
    """整百万显示 '1M'，整千显示 '200K'，其他保持原样。"""
    if tokens >= 1_000_000 and tokens % 1_000_000 == 0:
        return f"{tokens // 1_000_000}M"
    if tokens >= 1_000 and tokens % 1_000 == 0:
        return f"{tokens // 1_000}K"
    return str(tokens)


def estimate_tokens(messages: list) -> int:
    """粗略估算消息列表的 token 数。
    启发式系数：中文字 1.5、英文词 1.3、其他 0.5；每条消息另加 4（role + 格式）。
    """
    total = 0
    for msg in messages:
        total += 4
        text = message_to_text(msg)
        chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
        english = len(re.findall(r"[a-zA-Z]+", text))
        other = max(0, len(text) - chinese - english)
        total += int(chinese * 1.5 + english * 1.3 + other * 0.5)

        if is_ai_with_tool_use(msg):
            total += len(json.dumps(msg.tool_calls, ensure_ascii=False)) // 4
    return total


# ── 5a. 边界调整：避开拆开 tool_use / tool_result ─────────────────


def _find_last_ai_index(messages: list) -> int:
    """返回最后一条 AIMessage 的索引；找不到返回 -1。"""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], AIMessage):
            return i
    return -1


def _advance_past_tool_results(messages: list, start: int) -> int:
    """把 start 推过紧随其后的 ToolMessage（避免把 tool_use 与 tool_result 拆开）。"""
    while start < len(messages) and is_tool_result(messages[start]):
        start += 1
    return start


def _retreat_before_orphan_tool_result(messages: list, start: int) -> int:
    """start 是 ToolMessage 且 start-1 是 AIMessage(tool_calls) 时，回退 1 格避免孤立。"""
    if (
        0 < start < len(messages)
        and is_tool_result(messages[start])
        and is_ai_with_tool_use(messages[start - 1])
    ):
        return start - 1
    return start


# ── 5b. L1: snip_compact —— 裁掉中间消息 ─────────────────────────


def snip_compact(messages: list, max_messages: int = SNIP_MAX_MESSAGES) -> list:
    """消息条数 > max_messages 时，保留头 SNIP_KEEP_HEAD + 尾 SNIP_KEEP_TAIL 条，中间用占位 HumanMessage 替代。"""
    if len(messages) <= max_messages:
        return messages

    head_end = _advance_past_tool_results(messages, SNIP_KEEP_HEAD)
    tail_start = _retreat_before_orphan_tool_result(messages, len(messages) - SNIP_KEEP_TAIL)
    if head_end >= tail_start:
        return messages

    placeholder = HumanMessage(content=f"[snipped {tail_start - head_end} messages from conversation middle]")
    return [*messages[:head_end], placeholder, *messages[tail_start:]]


# ── 5c. L2: micro_compact —— 旧 tool_result 用占位文本替换 ────────


_MICRO_PLACEHOLDER = "[Earlier tool result compacted. Re-run if needed.]"


def micro_compact(messages: list, keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> int:
    """把"较旧"且长度 > 120 字符的 ToolMessage 替换为占位文本；返回被替换条数。
    不直接删除是为了保持 OpenAI/Anthropic API 约束：每条 tool_use 必须有对应 tool_result。
    """
    tool_results = [(i, m) for i, m in enumerate(messages) if is_tool_result(m)]
    if len(tool_results) <= keep_recent:
        return 0
    replaced = 0
    for _, msg in tool_results[:-keep_recent]:
        if len(content_to_str(msg.content)) > 120 and msg.content != _MICRO_PLACEHOLDER:
            msg.content = _MICRO_PLACEHOLDER
            replaced += 1
    return replaced


# ── 5d. L3: tool_result_budget —— 超大工具输出落盘 ─────────────────


def persist_large_output(tool_use_id: str, output: str, tool_results_dir: Path) -> str:
    """把超大工具输出写到磁盘，返回"路径 + 2KB 预览"的占位文本。已存在则跳过写入。"""
    if len(output) <= PERSIST_THRESHOLD:
        return output
    tool_results_dir.mkdir(parents=True, exist_ok=True)
    path = tool_results_dir / f"{tool_use_id}.txt"
    if not path.exists():
        try:
            path.write_text(output, encoding="utf-8")
        except OSError:
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
) -> int:
    """监控最后一条 AIMessage 之后的所有 ToolMessage 总字节数；超大者落盘，返回落盘条数。

    LangChain 适配：s08 教程的"最后一条 user 消息里所有 tool_result"在这里对应
    "最后一条 AIMessage 之后的所有 ToolMessage"。
    """
    if tool_results_dir is None:
        tool_results_dir = Path.cwd() / TOOL_RESULTS_DIRNAME

    last_ai = _find_last_ai_index(messages)
    if last_ai < 0:
        return 0

    tool_msgs = [m for m in messages[last_ai + 1:] if is_tool_result(m)]
    if not tool_msgs:
        return 0

    total = sum(len(content_to_str(m.content)) for m in tool_msgs)
    if total <= max_bytes:
        return 0

    # 按体积从大到小排序，优先落盘最大的
    ranked = sorted(tool_msgs, key=lambda m: len(content_to_str(m.content)), reverse=True)
    persisted = 0
    for msg in ranked:
        if total <= max_bytes:
            break
        content_str = content_to_str(msg.content)
        if len(content_str) <= persist_threshold:
            continue
        new_content = persist_large_output(msg.tool_call_id, content_str, tool_results_dir)
        total = total - len(content_str) + len(new_content)
        msg.content = new_content
        persisted += 1
    return persisted


# ── 5e. L4: compact_history —— LLM 整段摘要 ───────────────────────


def _format_message_for_summary(msg) -> str:
    """把单条消息格式化为 '[role]\\ncontent[\\ntool_calls/tool_call_id]' 形式。"""
    type_to_role = {
        SystemMessage: "system",
        HumanMessage: "human",
        AIMessage: "ai",
        ToolMessage: "tool",
    }
    role = type_to_role.get(type(msg), getattr(msg, "type", msg.__class__.__name__))
    extra = ""
    if is_ai_with_tool_use(msg):
        extra = "\ntool_calls: " + json.dumps(msg.tool_calls, ensure_ascii=False)
    elif is_tool_result(msg):
        extra = f"\ntool_call_id: {msg.tool_call_id}"
    return f"[{role}]\n{content_to_str(msg.content)}{extra}"


def _build_summary_prompt(messages: list) -> str:
    """构建摘要 prompt（9 段式结构化模板）。"""
    transcript = "\n\n".join(_format_message_for_summary(m) for m in messages)
    return _SUMMARY_PROMPT.format(transcript=transcript)


def summarize_history(
    messages: list,
    summarizer: Optional[Callable[[str], str]] = None,
) -> str:
    """调用 summarizer 对消息列表生成结构化摘要文本；缺省时临时构造 LLM。"""
    prompt = _build_summary_prompt(messages)
    if summarizer is not None:
        return summarizer(prompt)
    from llm_manage import create_llm

    response = create_llm(max_tokens=4000).invoke([HumanMessage(content=prompt)])
    return content_to_str(response.content) or "(empty summary)"


def write_transcript(messages: list, transcript_dir: Path) -> Path:
    """把当前完整历史写到 .transcripts/transcript_<timestamp>.jsonl。"""
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(message_to_dict(msg), ensure_ascii=False, default=str) + "\n")
    return path


def _protected_prefix_end(messages: list) -> int:
    """返回受保护前缀的结束位置：SystemMessage + workspace 指令注入（不能进摘要）。"""
    end = 1 if messages and isinstance(messages[0], SystemMessage) else 0
    if len(messages) > end and is_workspace_instruction(messages[end]):
        end += 1
    return end


def _find_ai_with_tool_call(messages: list, before_index: int, tool_call_id: str) -> Optional[int]:
    """从 before_index 往前找含指定 tool_call_id 的 AIMessage；遇到 HumanMessage 停止（跨轮无意义）。"""
    for i in range(before_index - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, HumanMessage):
            return None
        if is_ai_with_tool_use(msg) and any(tc.get("id") == tool_call_id for tc in msg.tool_calls):
            return i
    return None


def _expand_recent_start_for_tool_pairs(messages: list, recent_start: int) -> int:
    """把保留后缀的起点向前扩展，让每个 ToolMessage 都能找到对应 tool_call（避免孤立 tool_result）。"""
    start = recent_start
    while start > 0 and is_tool_result(messages[start]):
        prev_ai = _find_ai_with_tool_call(messages, start, messages[start].tool_call_id)
        if prev_ai is None or prev_ai >= start:
            break
        start = prev_ai
    return start


def compact_history(
    messages: list,
    summarizer: Optional[Callable[[str], str]] = None,
    transcript_dir: Optional[Path] = None,
) -> list:
    """把中间一段 messages 压缩为单条摘要 HumanMessage。
    保留前缀：SystemMessage + workspace 指令。
    保留后缀：最后 PRESERVE_RECENT_SUMMARY_MESSAGES 条原文。
    压缩前先 write_transcript 做全量快照。
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
    print(f"[transcript saved: {transcript_path}]")
    return [
        *messages[:prefix_end],
        HumanMessage(content=f"<context_summary>\n{summary}\n</context_summary>"),
        *messages[recent_start:],
    ]


# ── 5f. reactive_compact —— API 报错时的兜底压缩 ──────────────────


def reactive_compact(
    messages: list,
    summarizer: Optional[Callable[[str], str]] = None,
    transcript_dir: Optional[Path] = None,
    keep_tail: int = REACTIVE_KEEP_TAIL,
) -> list:
    """兜底压缩：把最早一段历史让 LLM 摘要，保留最近 keep_tail 条原样。
    与 L4 的差异：L4 是"主动预防"（token 估算超阈值时触发），reactive 是"被动兜底"。
    """
    if transcript_dir is None:
        transcript_dir = Path.cwd() / TRANSCRIPT_DIRNAME

    transcript_path = write_transcript(messages, transcript_dir)
    tail_start = _retreat_before_orphan_tool_result(messages, max(0, len(messages) - keep_tail))
    summary = summarize_history(messages[:tail_start], summarizer=summarizer)
    print(f"[transcript saved: {transcript_path}]")
    return [HumanMessage(content=f"[Reactive compact]\n\n{summary}"), *messages[tail_start:]]


# ── 6. ContextCompact 编排器 ────────────────────────────────────────


def _empty_operations() -> dict:
    """operations 字典的零值模板，session_manage 用 ops.get(key) 是否非 0 来决定是否打印。"""
    return {
        "tool_results_persisted": 0,
        "messages_snip_compacted": 0,
        "tool_results_micro_compacted": 0,
        "summary_messages_replaced": 0,
        "transcript_written": None,
    }


def _is_prompt_too_long_error(error: Exception) -> bool:
    return any(m in str(error).lower() for m in REACTIVE_ERROR_MARKERS)


class ContextCompact:
    """四层压缩管线编排器。

    编排顺序：L3 budget → L1 snip → L2 micro → [token 超阈值?] → L4 summary。
    兜底：API 返回 prompt_too_long 时触发 reactive_compact（最多 MAX_REACTIVE_RETRIES 次）。
    """

    def __init__(
        self,
        max_context_tokens: Optional[int] = None,
        summarizer: Optional[Callable[[str], str]] = None,
        transcript_dir: Optional[Path] = None,
        tool_results_dir: Optional[Path] = None,
    ):
        self.max_context_tokens = (
            max_context_tokens
            or parse_max_context_tokens(os.environ.get("MAX_CONTEXT_TOKENS"), DEFAULT_MAX_CONTEXT_TOKENS)
        )
        self.summarizer = summarizer
        self.transcript_dir = transcript_dir or Path.cwd() / TRANSCRIPT_DIRNAME
        self.tool_results_dir = tool_results_dir or Path.cwd() / TOOL_RESULTS_DIRNAME
        self._reactive_retries = 0

    # ── 上下文统计 ──────────────────────────────────────────────

    def context_stats(self, messages: list) -> ContextStats:
        used = estimate_tokens(messages)
        used_percent = min(100.0, (used / self.max_context_tokens) * 100)
        return ContextStats(
            used_tokens=used,
            max_tokens=self.max_context_tokens,
            used_percent=used_percent,
            remaining_percent=max(0.0, 100.0 - used_percent),
            max_label=format_token_count(self.max_context_tokens),
        )

    def format_context_label(self, messages: list) -> str:
        s = self.context_stats(messages)
        return f"max：{s.max_label}，used：{s.used_tokens}，{int(s.remaining_percent)}%"

    # ── 编排：四层管线 ────────────────────────────────────────

    def compact_if_needed(self, messages: list, force: bool = False) -> CompactResult:
        """根据使用率自动决定是否压缩。force=True 时跳过阈值判断（手动 /compact 用）。"""
        before = self.context_stats(messages)
        operations = _empty_operations()

        if not force and before.used_percent < PROACTIVE_TRIGGER_RATIO * 100:
            return CompactResult(messages=messages, changed=False,
                                 operations=operations, before=before, after=before)

        current, changed = messages, False

        # L3 budget —— 把超大 tool_result 落盘
        persisted = tool_result_budget(
            current, max_bytes=MAX_TOOL_RESULT_BYTES,
            persist_threshold=PERSIST_THRESHOLD, tool_results_dir=self.tool_results_dir,
        )
        if persisted:
            operations["tool_results_persisted"] = persisted
            changed = True

        # L1 snip —— 裁中间消息
        snipped = snip_compact(current)
        if len(snipped) != len(current):
            operations["messages_snip_compacted"] = len(current) - len(snipped)
            current, changed = snipped, True

        # L2 micro —— 旧 tool_result 占位
        micro_count = micro_compact(current)
        if micro_count:
            operations["tool_results_micro_compacted"] = micro_count
            changed = True

        # L4 summary —— 仍超阈值（或 force）则用 LLM 摘要
        if force or self.context_stats(current).used_percent >= SUMMARY_TRIGGER_RATIO * 100:
            new_messages = compact_history(
                current, summarizer=self.summarizer, transcript_dir=self.transcript_dir,
            )
            if len(new_messages) != len(current):
                operations["summary_messages_replaced"] = len(current) - len(new_messages)
                operations["transcript_written"] = str(self.transcript_dir / "transcript_*.jsonl")
                current, changed = new_messages, True

        return CompactResult(
            messages=current, changed=changed, operations=operations,
            before=before, after=self.context_stats(current),
        )

    # ── 兜底：reactive（API 报错时调用） ────────────────────────

    def handle_api_error(self, messages: list, error: Exception) -> Optional[CompactResult]:
        """API 报错时尝试 reactive compact 兜底。返回 None 表示不可重试，调用方应抛出原异常。"""
        if not _is_prompt_too_long_error(error):
            return None
        if self._reactive_retries >= MAX_REACTIVE_RETRIES:
            return None

        new_messages = reactive_compact(
            messages, summarizer=self.summarizer, transcript_dir=self.transcript_dir,
        )
        self._reactive_retries += 1
        return CompactResult(
            messages=new_messages, changed=True,
            operations={
                "reactive_compact_triggered": True,
                "transcript_written": str(self.transcript_dir / "transcript_*.jsonl"),
            },
        )

    def reset_reactive_counter(self) -> None:
        """LLM 调用成功后重置 reactive 兜底计数器。"""
        self._reactive_retries = 0
