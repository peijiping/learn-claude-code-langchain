import json
import sys
import tempfile
import unittest
from pathlib import Path


AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from todo_manager import TodoManager


class TodoManagerTest(unittest.TestCase):
    def make_manager(self, filename: str = "session_1.json"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        todo_file = Path(tmp.name) / ".todo" / filename
        return TodoManager(todo_file), todo_file

    def test_update_writes_human_readable_utf8_json(self):
        manager, todo_file = self.make_manager()

        rendered = manager.update([
            {"id": "1", "text": "整理中文需求", "status": "in_progress"},
            {"id": "2", "text": "补充测试", "status": "pending"},
        ])

        content = todo_file.read_text(encoding="utf-8")
        payload = json.loads(content)
        self.assertEqual(payload["items"][0]["text"], "整理中文需求")
        self.assertIn('"text": "整理中文需求"', content)
        self.assertNotIn("\\u", content)
        self.assertIn("[>] #1: 整理中文需求", rendered)

    def test_new_manager_loads_existing_session_file(self):
        manager, todo_file = self.make_manager()
        manager.update([
            {"id": "1", "text": "恢复未完成任务", "status": "pending"},
        ])

        reloaded = TodoManager(todo_file)

        self.assertTrue(reloaded.has_open_items())
        self.assertIn("[ ] #1: 恢复未完成任务", reloaded.render())

    def test_rejects_more_than_twenty_items(self):
        manager, _ = self.make_manager()

        with self.assertRaisesRegex(ValueError, "Max 20 todos allowed"):
            manager.update([
                {"id": str(i), "text": f"todo {i}", "status": "pending"}
                for i in range(21)
            ])

    def test_rejects_empty_text(self):
        manager, _ = self.make_manager()

        with self.assertRaisesRegex(ValueError, "text required"):
            manager.update([{"id": "1", "text": " ", "status": "pending"}])

    def test_rejects_invalid_status(self):
        manager, _ = self.make_manager()

        with self.assertRaisesRegex(ValueError, "invalid status"):
            manager.update([{"id": "1", "text": "写测试", "status": "blocked"}])

    def test_rejects_multiple_in_progress_items(self):
        manager, _ = self.make_manager()

        with self.assertRaisesRegex(ValueError, "Only one task can be in_progress"):
            manager.update([
                {"id": "1", "text": "第一步", "status": "in_progress"},
                {"id": "2", "text": "第二步", "status": "in_progress"},
            ])


if __name__ == "__main__":
    unittest.main()
