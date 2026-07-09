#!/usr/bin/env python3
"""
compact.py - 上下文压缩模块

集中处理上下文 token 统计、工具结果剪枝、结构化摘要压缩和 skill 重新注入。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


DEFAULT_MAX_CONTEXT_TOKENS = 196_608
TOOL_PRUNE_TRIGGER_RATIO = 0.95
SUMMARY_TRIGGER_RATIO = 0.80
PRESERVE_RECENT_TOOL_MESSAGES = 20
PRESERVE_RECENT_SUMMARY_MESSAGES = 10
MAX_TOOL_MESSAGES_PRUNED = 50
LEGACY_TOOL_OUTPUT_MAX_LENGTH = 500


@dataclass
class ContextStats:
    used_tokens: int
    max_tokens: int
    used_percent: float
    remaining_percent: float
    max_label: str


@dataclass
class CompactResult:
    messages: list
    changed: bool
    operations: dict = field(default_factory=dict)
    before: Optional[ContextStats] = None
    after: Optional[ContextStats] = None


class CompactManager:
    """负责上下文压缩和 token 统计。"""

    def __init__(
        self,
        max_context_tokens: Optional[int] = None,
        summarizer: Optional[Callable[[str], str]] = None,
        skill_loader=None,
    ):
        self.max_context_tokens = max_context_tokens or self.parse_max_context_tokens(
            os.environ.get("MAX_CONTEXT_TOKENS"),
            DEFAULT_MAX_CONTEXT_TOKENS,
        )
        self.summarizer = summarizer
        self.skill_loader = skill_loader

    @staticmethod
    def parse_max_context_tokens(value: Optional[str], default: int = DEFAULT_MAX_CONTEXT_TOKENS) -> int:
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
        if tokens >= 1_000_000 and tokens % 1_000_000 == 0:
            return f"{tokens // 1_000_000}M"
        if tokens >= 1_000 and tokens % 1_000 == 0:
            return f"{tokens // 1_000}K"
        return str(tokens)

    def estimate_tokens(self, messages: list) -> int:
        total_tokens = 0

        for msg in messages:
            total_tokens += 4
            content = self._message_content_for_count(msg)
            if isinstance(content, str):
                chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", content))
                english_words = len(re.findall(r"[a-zA-Z]+", content))
                other_chars = max(0, len(content) - chinese_chars - english_words)
                total_tokens += int(chinese_chars * 1.5 + english_words * 1.3 + other_chars * 0.5)
            else:
                total_tokens += len(json.dumps(content, ensure_ascii=False)) // 4

            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                total_tokens += len(json.dumps(msg.tool_calls, ensure_ascii=False)) // 4

        return total_tokens

    def _message_content_for_count(self, msg):
        if hasattr(msg, "content"):
            content = msg.content
            if isinstance(content, list):
                return json.dumps(content, ensure_ascii=False)
            return content
        if isinstance(msg, dict):
            return msg.get("content", "")
        return str(msg)

    def context_stats(self, messages: list) -> ContextStats:
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
        stats = self.context_stats(messages)
        return f"max：{stats.max_label}，used：{stats.used_tokens}，{int(stats.remaining_percent)}%"

    def compact_if_needed(self, messages: list, force: bool = False) -> CompactResult:
        before = self.context_stats(messages)
        operations = {
            "tool_messages_pruned": 0,
            "legacy_tool_outputs_pruned": 0,
            "summary_messages_replaced": 0,
            "skills_reloaded": [],
        }

        if not force and before.used_percent < TOOL_PRUNE_TRIGGER_RATIO * 100:
            return CompactResult(messages=messages, changed=False, operations=operations, before=before, after=before)

        loaded_skills = self.detect_loaded_skills(messages)
        current_messages = messages
        changed = False

        pruned = self.prune_old_tool_results(current_messages)
        current_messages = pruned.messages
        operations.update(pruned.operations)
        changed = changed or pruned.changed

        after_prune = self.context_stats(current_messages)
        if force or after_prune.used_percent >= SUMMARY_TRIGGER_RATIO * 100:
            summarized = self._replace_history_with_summary(current_messages)
            current_messages = summarized.messages
            operations.update(summarized.operations)
            changed = changed or summarized.changed

        if changed and loaded_skills:
            current_messages, reloaded = self._reload_skills(current_messages, loaded_skills)
            operations["skills_reloaded"] = reloaded

        after = self.context_stats(current_messages)
        return CompactResult(messages=current_messages, changed=changed, operations=operations, before=before, after=after)

    def prune_old_tool_results(self, messages: list) -> CompactResult:
        result = list(messages)
        limit = max(0, len(result) - PRESERVE_RECENT_TOOL_MESSAGES)
        pruned_tool_messages = 0
        pruned_legacy_outputs = 0

        for index in range(limit):
            if pruned_tool_messages >= MAX_TOOL_MESSAGES_PRUNED:
                break

            msg = result[index]
            if isinstance(msg, ToolMessage) and not self._is_pruned_tool_message(msg):
                result[index] = ToolMessage(
                    content=f"[工具结果已剪枝，原始输出约 {len(str(msg.content))} 字符，tool_call_id={msg.tool_call_id}]",
                    tool_call_id=msg.tool_call_id,
                )
                pruned_tool_messages += 1
            elif isinstance(msg, HumanMessage):
                compressed_msg, changed_count = self._prune_legacy_human_tool_result(msg)
                if changed_count:
                    result[index] = compressed_msg
                    pruned_legacy_outputs += changed_count

        changed = pruned_tool_messages > 0 or pruned_legacy_outputs > 0
        return CompactResult(
            messages=result,
            changed=changed,
            operations={
                "tool_messages_pruned": pruned_tool_messages,
                "legacy_tool_outputs_pruned": pruned_legacy_outputs,
            },
        )

    def _is_pruned_tool_message(self, msg: ToolMessage) -> bool:
        return isinstance(msg.content, str) and "已剪枝" in msg.content

    def _prune_legacy_human_tool_result(self, msg: HumanMessage) -> tuple[HumanMessage, int]:
        content = msg.content
        if not isinstance(content, str) or '"tool_result"' not in content:
            return msg, 0

        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return msg, 0

        if not isinstance(payload, list):
            return msg, 0

        changed_count = 0
        for item in payload:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            output = item.get("tool_output")
            if isinstance(output, str) and "已剪枝" not in output and len(output) > LEGACY_TOOL_OUTPUT_MAX_LENGTH:
                item["tool_output"] = (
                    output[:LEGACY_TOOL_OUTPUT_MAX_LENGTH]
                    + f"... [工具结果已剪枝，原始输出约 {len(output)} 字符]"
                )
                changed_count += 1

        if changed_count == 0:
            return msg, 0

        return HumanMessage(content=json.dumps(payload, ensure_ascii=False)), changed_count

    def summarize_history(self, messages: list) -> str:
        prompt = self._build_summary_prompt(messages)
        if self.summarizer is not None:
            return self.summarizer(prompt)

        from llm_manage import create_llm

        response = create_llm(max_tokens=4000).invoke([HumanMessage(content=prompt)])
        content = getattr(response, "content", response)
        if isinstance(content, list):
            return "\n".join(str(block) for block in content)
        return str(content)

    def _build_summary_prompt(self, messages: list) -> str:
        transcript = "\n\n".join(self._format_message_for_summary(msg) for msg in messages)
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

    def _format_message_for_summary(self, msg) -> str:
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

        content = getattr(msg, "content", str(msg))
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)

        extra = ""
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            extra = "\ntool_calls: " + json.dumps(msg.tool_calls, ensure_ascii=False)
        if isinstance(msg, ToolMessage):
            extra = f"\ntool_call_id: {msg.tool_call_id}"
        return f"[{role}]\n{content}{extra}"

    def _replace_history_with_summary(self, messages: list) -> CompactResult:
        if len(messages) <= PRESERVE_RECENT_SUMMARY_MESSAGES + 1:
            return CompactResult(messages=messages, changed=False, operations={"summary_messages_replaced": 0})

        prefix_end = self._protected_prefix_end(messages)
        recent_start = max(prefix_end, len(messages) - PRESERVE_RECENT_SUMMARY_MESSAGES)
        recent_start = self._expand_recent_start_for_tool_pairs(messages, recent_start)
        if recent_start <= prefix_end:
            return CompactResult(messages=messages, changed=False, operations={"summary_messages_replaced": 0})

        to_summarize = messages[prefix_end:recent_start]
        if not to_summarize:
            return CompactResult(messages=messages, changed=False, operations={"summary_messages_replaced": 0})

        summary = self.summarize_history(to_summarize)
        summary_msg = HumanMessage(content=f"<context_summary>\n{summary}\n</context_summary>")
        result = [*messages[:prefix_end], summary_msg, *messages[recent_start:]]
        return CompactResult(
            messages=result,
            changed=True,
            operations={"summary_messages_replaced": len(to_summarize)},
        )

    def _protected_prefix_end(self, messages: list) -> int:
        prefix_end = 1 if messages and isinstance(messages[0], SystemMessage) else 0
        if len(messages) > prefix_end and self._is_workspace_instruction_message(messages[prefix_end]):
            prefix_end += 1
        return prefix_end

    def _is_workspace_instruction_message(self, msg) -> bool:
        return (
            isinstance(msg, HumanMessage)
            and isinstance(msg.content, str)
            and (
                "以下是 workspace/CLAUDE.md 内容：" in msg.content
                or "以下是 workspace/AGENT.md 内容：" in msg.content
            )
        )

    def _expand_recent_start_for_tool_pairs(self, messages: list, recent_start: int) -> int:
        start = recent_start
        while start > 0 and isinstance(messages[start], ToolMessage):
            previous_ai = self._find_previous_ai_with_tool_call(messages, start, messages[start].tool_call_id)
            if previous_ai is None or previous_ai >= start:
                break
            start = previous_ai
        return start

    def _find_previous_ai_with_tool_call(self, messages: list, before_index: int, tool_call_id: str) -> Optional[int]:
        for index in range(before_index - 1, -1, -1):
            msg = messages[index]
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                if any(tc.get("id") == tool_call_id for tc in msg.tool_calls):
                    return index
            if isinstance(msg, HumanMessage):
                break
        return None

    def detect_loaded_skills(self, messages: list) -> list[str]:
        loaded = []
        pending = {}

        for msg in messages:
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "load_skill":
                        name = tool_call.get("args", {}).get("name")
                        call_id = tool_call.get("id")
                        if name and call_id:
                            pending[call_id] = name
            elif isinstance(msg, ToolMessage) and msg.tool_call_id in pending:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if "Error:" not in content and pending[msg.tool_call_id] not in loaded:
                    loaded.append(pending[msg.tool_call_id])

        return loaded

    def _reload_skills(self, messages: list, skill_names: list[str]) -> tuple[list, list[str]]:
        if self.skill_loader is None:
            return messages, []

        sections = []
        reloaded = []
        for name in skill_names:
            content = self.skill_loader.load_skill(name)
            if isinstance(content, str) and not content.startswith("Error:"):
                sections.append(content)
                reloaded.append(name)

        if not sections:
            return messages, []

        prefix_end = self._protected_prefix_end(messages)
        recent_start = max(prefix_end, len(messages) - PRESERVE_RECENT_SUMMARY_MESSAGES)
        recent_start = self._expand_recent_start_for_tool_pairs(messages, recent_start)
        reload_msg = HumanMessage(content="<reloaded_skills>\n" + "\n\n".join(sections) + "\n</reloaded_skills>")
        return [*messages[:recent_start], reload_msg, *messages[recent_start:]], reloaded
