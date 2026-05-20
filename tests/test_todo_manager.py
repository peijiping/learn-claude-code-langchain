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

    def test_new_file_initializes_empty_v2_state(self):
        manager, todo_file = self.make_manager()

        self.assertFalse(todo_file.exists())
        self.assertEqual(manager.boards, [])
        self.assertIsNone(manager.active_board_id)
        self.assertEqual(manager.next_board_id, 1)
        self.assertEqual(manager.render(), "No todos.")

    def test_rejects_old_top_level_items_format(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        todo_file = Path(tmp.name) / ".todo" / "session_1.json"
        todo_file.parent.mkdir(parents=True)
        todo_file.write_text(
            json.dumps({"items": [{"id": "1", "text": "旧格式", "status": "pending"}]}, ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "Unsupported old todo format"):
            TodoManager(todo_file)

    def test_create_board_writes_human_readable_utf8_json(self):
        manager, todo_file = self.make_manager()

        rendered = manager.create_board("需求整理", [
            {"id": "1", "text": "整理中文需求", "status": "in_progress"},
            {"id": "2", "text": "补充测试", "status": "pending"},
        ])

        content = todo_file.read_text(encoding="utf-8")
        payload = json.loads(content)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["active_board_id"], "board_1")
        self.assertEqual(payload["next_board_id"], 2)
        self.assertEqual(payload["boards"][0]["title"], "需求整理")
        self.assertEqual(payload["boards"][0]["items"][0]["text"], "整理中文需求")
        self.assertIn('"text": "整理中文需求"', content)
        self.assertNotIn("\\u", content)
        self.assertIn("[active] board_1: 需求整理", rendered)
        self.assertIn("[>] #1: 整理中文需求", rendered)

    def test_new_manager_loads_existing_session_file(self):
        manager, todo_file = self.make_manager()
        manager.create_board("恢复任务", [
            {"id": "1", "text": "恢复未完成任务", "status": "pending"},
        ])

        reloaded = TodoManager(todo_file)

        self.assertTrue(reloaded.has_open_items())
        self.assertEqual(reloaded.active_board_id, "board_1")
        self.assertIn("[ ] #1: 恢复未完成任务", reloaded.render())

    def test_create_second_board_preserves_first_and_switches_active(self):
        manager, todo_file = self.make_manager()
        manager.create_board("旧任务", [
            {"id": "1", "text": "旧步骤", "status": "completed"},
        ])

        manager.create_board("新任务", [
            {"id": "1", "text": "新步骤", "status": "in_progress"},
        ])

        payload = json.loads(todo_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["active_board_id"], "board_2")
        self.assertEqual(payload["next_board_id"], 3)
        self.assertEqual([board["title"] for board in payload["boards"]], ["旧任务", "新任务"])
        self.assertEqual(payload["boards"][0]["items"][0]["status"], "completed")
        self.assertEqual(payload["boards"][1]["items"][0]["status"], "in_progress")

    def test_update_only_updates_active_board(self):
        manager, todo_file = self.make_manager()
        manager.create_board("旧任务", [
            {"id": "1", "text": "旧步骤", "status": "completed"},
        ])
        manager.create_board("新任务", [
            {"id": "1", "text": "新步骤", "status": "pending"},
        ])

        rendered = manager.update([
            {"id": "1", "text": "新步骤", "status": "completed"},
        ])

        payload = json.loads(todo_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["boards"][0]["items"][0]["text"], "旧步骤")
        self.assertEqual(payload["boards"][0]["items"][0]["status"], "completed")
        self.assertEqual(payload["boards"][1]["items"][0]["text"], "新步骤")
        self.assertEqual(payload["boards"][1]["items"][0]["status"], "completed")
        self.assertIn("[active] board_2: 新任务", rendered)

    def test_update_requires_active_board(self):
        manager, _ = self.make_manager()

        with self.assertRaisesRegex(ValueError, "Call todo_new_board before todo"):
            manager.update([
                {"id": "1", "text": "孤立步骤", "status": "pending"},
            ])

    def test_render_shows_multiple_boards(self):
        manager, _ = self.make_manager()
        manager.create_board("旧任务", [
            {"id": "1", "text": "旧步骤", "status": "completed"},
        ])
        manager.create_board("新任务", [
            {"id": "1", "text": "新步骤", "status": "pending"},
        ])

        rendered = manager.render()

        self.assertIn("board_1: 旧任务", rendered)
        self.assertIn("[active] board_2: 新任务", rendered)
        self.assertIn("[x] #1: 旧步骤", rendered)
        self.assertIn("[ ] #1: 新步骤", rendered)

    def test_rejects_more_than_twenty_items(self):
        manager, _ = self.make_manager()

        with self.assertRaisesRegex(ValueError, "Max 20 todos allowed"):
            manager.create_board("太多任务", [
                {"id": str(i), "text": f"todo {i}", "status": "pending"}
                for i in range(21)
            ])

    def test_rejects_empty_text(self):
        manager, _ = self.make_manager()

        with self.assertRaisesRegex(ValueError, "text required"):
            manager.create_board("空任务", [{"id": "1", "text": " ", "status": "pending"}])

    def test_rejects_invalid_status(self):
        manager, _ = self.make_manager()

        with self.assertRaisesRegex(ValueError, "invalid status"):
            manager.create_board("非法状态", [{"id": "1", "text": "写测试", "status": "blocked"}])

    def test_rejects_multiple_in_progress_items(self):
        manager, _ = self.make_manager()

        with self.assertRaisesRegex(ValueError, "Only one task can be in_progress"):
            manager.create_board("多个进行中", [
                {"id": "1", "text": "第一步", "status": "in_progress"},
                {"id": "2", "text": "第二步", "status": "in_progress"},
            ])


if __name__ == "__main__":
    unittest.main()
