import json
import sys
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage


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


if __name__ == "__main__":
    unittest.main()
