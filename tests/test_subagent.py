import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
sys.path.insert(0, str(AGENTS_DIR))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "https://example.com/v1")
os.environ.setdefault("OPENAI_MODEL_ID", "deepseek-v4-flash")

from subagent import run_subagent


class FakeSubagentLLM:
    def __init__(self):
        self.calls = 0
        self.second_call_messages = None

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "a.py"}, "id": "call-read"},
                    {"name": "bash", "args": {"command": "pwd"}, "id": "call-bash"},
                ],
            )

        self.second_call_messages = list(messages)
        return AIMessage(content="done")


class MultiRoundSubagentLLM:
    def __init__(self, tool_rounds: int = 4):
        self.calls = 0
        self.tool_rounds = tool_rounds
        self.messages_by_call = []

    def invoke(self, messages):
        self.calls += 1
        self.messages_by_call.append(list(messages))
        if self.calls <= self.tool_rounds:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": f"round-{self.calls}.py"},
                        "id": f"call-{self.calls}",
                    }
                ],
            )

        return AIMessage(content="done")


class SubagentToolMessageTest(unittest.TestCase):
    def test_subagent_replies_to_each_tool_call_with_tool_message(self):
        fake_llm = FakeSubagentLLM()

        with patch("subagent.create_llm_with_tools", return_value=fake_llm), patch.dict(
            "subagent.TOOL_HANDLERS",
            {
                "read_file": lambda **_kw: "file contents",
                "bash": lambda **_kw: "shell output",
            },
            clear=True,
        ):
            result = run_subagent("inspect files", allowed_tools=["read_file", "bash"])

        self.assertEqual(result, "done")
        trailing_messages = fake_llm.second_call_messages[-2:]
        self.assertTrue(all(isinstance(msg, ToolMessage) for msg in trailing_messages))
        self.assertEqual(
            [msg.tool_call_id for msg in trailing_messages],
            ["call-read", "call-bash"],
        )

    def test_subagent_keeps_full_message_history_across_tool_rounds(self):
        fake_llm = MultiRoundSubagentLLM(tool_rounds=10)

        with patch("subagent.create_llm_with_tools", return_value=fake_llm), patch.dict(
            "subagent.TOOL_HANDLERS",
            {"read_file": lambda **_kw: "file contents"},
            clear=True,
        ):
            result = run_subagent("inspect files", allowed_tools=["read_file"])

        self.assertEqual(result, "done")
        final_call_messages = fake_llm.messages_by_call[-1]
        self.assertEqual(len(final_call_messages), 22)
        self.assertEqual(len([msg for msg in final_call_messages if isinstance(msg, AIMessage)]), 10)
        self.assertEqual(len([msg for msg in final_call_messages if isinstance(msg, ToolMessage)]), 10)
        self.assertFalse(
            any("历史已省略" in getattr(msg, "content", "") for msg in final_call_messages)
        )

    def test_subagent_keeps_full_tool_output_in_message_history(self):
        fake_llm = FakeSubagentLLM()
        long_output = "x" * 6000

        with patch("subagent.create_llm_with_tools", return_value=fake_llm), patch.dict(
            "subagent.TOOL_HANDLERS",
            {
                "read_file": lambda **_kw: long_output,
                "bash": lambda **_kw: "shell output",
            },
            clear=True,
        ):
            result = run_subagent("inspect files", allowed_tools=["read_file", "bash"])

        self.assertEqual(result, "done")
        first_tool_message = fake_llm.second_call_messages[-2]
        self.assertIn(long_output, first_tool_message.content)


if __name__ == "__main__":
    unittest.main()
