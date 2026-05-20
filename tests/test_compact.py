import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from compact import CompactManager


class FakeSkillLoader:
    def get_content(self, name: str) -> str:
        return f"<skill name=\"{name}\">body for {name}</skill>"


class CompactManagerTest(unittest.TestCase):
    def make_manager(self, max_tokens: int = 1000, summary: str = "summary text") -> CompactManager:
        return CompactManager(
            max_context_tokens=max_tokens,
            summarizer=lambda _prompt: summary,
            skill_loader=FakeSkillLoader(),
        )

    def test_reads_max_context_tokens_from_env_and_formats_context_label(self):
        with patch.dict(os.environ, {"MAX_CONTEXT_TOKENS": "1M"}, clear=False):
            manager = CompactManager()

        self.assertEqual(manager.max_context_tokens, 1_000_000)

        with patch.object(manager, "estimate_tokens", return_value=247_284):
            self.assertEqual(
                manager.format_context_label([HumanMessage(content="hi")]),
                "max：1M，used：247284，75%",
            )

    def test_prunes_at_most_50_old_tool_messages_outside_latest_20(self):
        manager = self.make_manager(max_tokens=1000)
        messages = [SystemMessage(content="system")]
        for i in range(80):
            messages.append(ToolMessage(content="x" * 100, tool_call_id=f"tool-{i}"))

        result = manager.prune_old_tool_results(messages)

        pruned = [
            msg for msg in result.messages
            if isinstance(msg, ToolMessage) and "已剪枝" in msg.content
        ]
        self.assertEqual(len(pruned), 50)
        self.assertEqual([msg.tool_call_id for msg in result.messages[-20:]], [f"tool-{i}" for i in range(60, 80)])
        self.assertTrue(all("已剪枝" not in msg.content for msg in result.messages[-20:]))
        self.assertEqual(result.operations["tool_messages_pruned"], 50)

    def test_prunes_legacy_human_tool_result_json(self):
        manager = self.make_manager(max_tokens=1000)
        legacy = HumanMessage(content=json.dumps([
            {
                "type": "tool_result",
                "tool_name": "read_file",
                "tool_id": "abc",
                "tool_output": "x" * 1000,
            }
        ], ensure_ascii=False))

        recent = [HumanMessage(content=f"recent {i}") for i in range(20)]

        result = manager.prune_old_tool_results([SystemMessage(content="system"), legacy, *recent])

        payload = json.loads(result.messages[1].content)
        self.assertIn("已剪枝", payload[0]["tool_output"])
        self.assertEqual(result.operations["legacy_tool_outputs_pruned"], 1)

    def test_summary_prompt_contains_required_sections(self):
        prompts = []
        manager = CompactManager(max_context_tokens=1000, summarizer=lambda prompt: prompts.append(prompt) or "summary")
        manager.summarize_history([
            HumanMessage(content="用户需求"),
            ToolMessage(content="tool output", tool_call_id="tool-1"),
        ])

        prompt = prompts[0]
        for section in [
            "Primary Request and Intent",
            "Key Technical Concepts",
            "Files and Code Sections",
            "Errors and fixes",
            "Problem Solving",
            "All user messages",
            "Pending Tasks",
            "Current Work",
            "Optional Next Step",
        ]:
            self.assertIn(section, prompt)
        self.assertIn("用户需求", prompt)

    def test_compact_summarizes_middle_history_and_preserves_boundaries(self):
        manager = self.make_manager(max_tokens=100)
        workspace = HumanMessage(content="以下是 workspace/CLAUDE.md 内容：\nrules")
        middle = [HumanMessage(content=f"old {i} " + ("x" * 80)) for i in range(12)]
        recent = [HumanMessage(content=f"recent {i}") for i in range(10)]
        messages = [SystemMessage(content="system"), workspace, *middle, *recent]

        result = manager.compact_if_needed(messages, force=True)

        self.assertIsInstance(result.messages[0], SystemMessage)
        self.assertIs(result.messages[1], workspace)
        self.assertIn("<context_summary>", result.messages[2].content)
        self.assertEqual([m.content for m in result.messages[-10:]], [m.content for m in recent])
        self.assertEqual(result.operations["summary_messages_replaced"], len(middle))

    def test_compact_does_not_split_ai_tool_call_pairs_at_recent_boundary(self):
        manager = self.make_manager(max_tokens=100)
        ai_msg = AIMessage(content="")
        ai_msg.tool_calls = [{"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}]
        tool_msg = ToolMessage(content="tool result", tool_call_id="call-1")
        messages = [
            SystemMessage(content="system"),
            *[HumanMessage(content=f"old {i} " + ("x" * 80)) for i in range(8)],
            ai_msg,
            tool_msg,
            *[HumanMessage(content=f"recent {i}") for i in range(9)],
        ]

        result = manager.compact_if_needed(messages, force=True)

        self.assertIn(ai_msg, result.messages)
        self.assertIn(tool_msg, result.messages)
        self.assertLess(result.messages.index(ai_msg), result.messages.index(tool_msg))

    def test_loaded_skills_are_reloaded_after_compression(self):
        manager = self.make_manager(max_tokens=100)
        ai_msg = AIMessage(content="")
        ai_msg.tool_calls = [{"name": "load_skill", "args": {"name": "pdf"}, "id": "load-1"}]
        tool_msg = ToolMessage(content="<skill name=\"pdf\">body</skill>", tool_call_id="load-1")
        messages = [
            SystemMessage(content="system"),
            ai_msg,
            tool_msg,
            *[HumanMessage(content=f"old {i} " + ("x" * 80)) for i in range(12)],
            *[HumanMessage(content=f"recent {i}") for i in range(10)],
        ]

        result = manager.compact_if_needed(messages, force=True)

        reloaded = [
            msg for msg in result.messages
            if isinstance(msg, HumanMessage) and "<reloaded_skills>" in msg.content
        ]
        self.assertEqual(len(reloaded), 1)
        self.assertIn("body for pdf", reloaded[0].content)
        self.assertLess(result.messages.index(reloaded[0]), len(result.messages) - 10)
        self.assertEqual(result.operations["skills_reloaded"], ["pdf"])


if __name__ == "__main__":
    unittest.main()
