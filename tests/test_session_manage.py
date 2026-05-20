import json
import sys
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage


AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from session_manage import SessionManager


class SessionManagerTest(unittest.TestCase):
    def make_manager(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace_dir = Path(tmp.name) / "WorkSpace"
        chat_history_dir = workspace_dir / ".chathistory"
        return SessionManager(chat_history_dir, "system prompt"), workspace_dir

    def read_session_rows(self, session_file: Path):
        return [
            json.loads(line)
            for line in session_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_new_session_without_workspace_instruction_files_keeps_system_only(self):
        manager, _ = self.make_manager()

        _, session_file, messages = manager.init_session()

        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0], SystemMessage)
        self.assertEqual(messages[0].content, "system prompt")
        self.assertEqual(self.read_session_rows(session_file), [
            {"type": "system", "content": "system prompt"}
        ])

    def test_new_session_appends_claude_then_agent_content_as_human_message(self):
        manager, workspace_dir = self.make_manager()
        (workspace_dir / "CLAUDE.md").write_text("claude rules", encoding="utf-8")
        (workspace_dir / "AGENT.md").write_text("agent rules", encoding="utf-8")

        _, session_file, messages = manager.init_session()

        self.assertEqual(len(messages), 2)
        self.assertIsInstance(messages[0], SystemMessage)
        self.assertIsInstance(messages[1], HumanMessage)
        self.assertIn("以下是 workspace/CLAUDE.md 内容：", messages[1].content)
        self.assertIn("claude rules", messages[1].content)
        self.assertIn("以下是 workspace/AGENT.md 内容：", messages[1].content)
        self.assertIn("agent rules", messages[1].content)
        self.assertLess(messages[1].content.index("CLAUDE.md"), messages[1].content.index("AGENT.md"))

        rows = self.read_session_rows(session_file)
        self.assertEqual([row["type"] for row in rows], ["system", "human"])
        self.assertEqual(rows[1]["content"], messages[1].content)

    def test_existing_session_is_loaded_without_duplicate_workspace_instruction_message(self):
        manager, workspace_dir = self.make_manager()
        (workspace_dir / "CLAUDE.md").write_text("claude rules", encoding="utf-8")
        _, session_file, first_messages = manager.init_session()

        _, _, loaded_messages = manager.init_session()

        self.assertEqual(len(first_messages), 2)
        self.assertEqual(len(loaded_messages), 2)
        self.assertEqual(self.read_session_rows(session_file), [
            {"type": "system", "content": first_messages[0].content},
            {"type": "human", "content": first_messages[1].content},
        ])

    def test_clear_session_rebuilds_initial_messages_with_workspace_instruction_files(self):
        manager, workspace_dir = self.make_manager()
        (workspace_dir / "CLAUDE.md").write_text("claude rules", encoding="utf-8")
        _, session_file, _ = manager.init_session()
        manager.append_message_to_session(session_file, HumanMessage(content="hello"))

        deleted_count = manager.clear_session(session_file)
        messages = manager.load_session_history(session_file)

        self.assertEqual(deleted_count, 2)
        self.assertEqual(len(messages), 2)
        self.assertIsInstance(messages[0], SystemMessage)
        self.assertIsInstance(messages[1], HumanMessage)
        self.assertIn("claude rules", messages[1].content)

    def test_save_session_history_rewrites_file_to_match_memory_messages(self):
        manager, _ = self.make_manager()
        _, session_file, messages = manager.init_session()
        messages.append(HumanMessage(content="hello"))
        messages.append(ToolMessage(content="tool result", tool_call_id="tool-1"))

        manager.save_session_history(session_file, messages)
        loaded_messages = manager.load_session_history(session_file)

        self.assertEqual(len(loaded_messages), len(messages))
        self.assertIsInstance(loaded_messages[-1], ToolMessage)
        self.assertEqual(loaded_messages[-1].content, "tool result")
        self.assertEqual(loaded_messages[-1].tool_call_id, "tool-1")
        self.assertEqual(self.read_session_rows(session_file)[-1], {
            "type": "tool",
            "content": "tool result",
            "tool_call_id": "tool-1",
        })

    def test_compact_messages_updates_memory_and_session_file_consistently(self):
        manager, _ = self.make_manager()
        manager.compact_manager.max_context_tokens = 100
        manager.compact_manager.summarizer = lambda _prompt: "session summary"
        _, session_file, messages = manager.init_session()
        messages.extend(HumanMessage(content=f"old {i} " + ("x" * 80)) for i in range(12))
        messages.extend(HumanMessage(content=f"recent {i}") for i in range(10))
        manager.save_session_history(session_file, messages)

        result = manager.compact_messages_if_needed(messages, session_file, force=True)
        loaded_messages = manager.load_session_history(session_file)

        self.assertTrue(result.changed)
        self.assertEqual([m.content for m in messages], [m.content for m in loaded_messages])
        self.assertTrue(any(
            isinstance(message, HumanMessage) and "<context_summary>" in message.content
            for message in loaded_messages
        ))


if __name__ == "__main__":
    unittest.main()
