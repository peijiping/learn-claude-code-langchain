import json
import sys
import tempfile
import unittest
from pathlib import Path


AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from task_manager import TaskManager


class TaskManagerTest(unittest.TestCase):
    def make_manager(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return TaskManager(Path(tmp.name))

    def test_create_writes_human_readable_utf8_json(self):
        manager = self.make_manager()

        manager.create("DRG医院运营综述", "提取数据并撰写综述")

        content = (manager.dir / "task_1.json").read_text(encoding="utf-8")
        self.assertIn('"subject": "DRG医院运营综述"', content)
        self.assertIn('"description": "提取数据并撰写综述"', content)
        self.assertNotIn("\\u", content)

    def test_update_keeps_human_readable_utf8_json(self):
        manager = self.make_manager()
        manager.create("整理数据", "初始描述")

        result = manager.update(1, status="in_progress")

        content = (manager.dir / "task_1.json").read_text(encoding="utf-8")
        self.assertIn('"subject": "整理数据"', content)
        self.assertIn('"status": "in_progress"', content)
        self.assertNotIn("\\u", content)
        self.assertIn('"subject": "整理数据"', result)
        self.assertNotIn("\\u", result)

    def test_create_many_creates_root_and_ordered_child_tasks(self):
        manager = self.make_manager()

        result = manager.create_many(
            "DRG综述项目",
            "从文献到文章的完整流程",
            [
                {"subject": "扫描文献", "description": "列出 PDF 清单"},
                {"subject": "提取数据", "description": "读取 PDF 并提取统计数据"},
                {"subject": "撰写文章", "description": "输出 markdown 综述"},
            ],
        )

        payload = json.loads(result)
        self.assertEqual(payload["root"]["id"], 1)
        self.assertEqual([task["id"] for task in payload["tasks"]], [2, 3, 4])

        root = json.loads((manager.dir / "task_1.json").read_text(encoding="utf-8"))
        first = json.loads((manager.dir / "task_2.json").read_text(encoding="utf-8"))
        second = json.loads((manager.dir / "task_3.json").read_text(encoding="utf-8"))
        third = json.loads((manager.dir / "task_4.json").read_text(encoding="utf-8"))

        self.assertEqual(root["parent_id"], None)
        self.assertEqual(root["root_id"], 1)
        self.assertEqual(root["order"], 0)
        self.assertEqual(first["parent_id"], 1)
        self.assertEqual(first["root_id"], 1)
        self.assertEqual(first["order"], 1)
        self.assertEqual(second["blockedBy"], [2])
        self.assertEqual(first["blocks"], [3])
        self.assertEqual(third["blockedBy"], [3])
        self.assertEqual(second["blocks"], [4])

        all_text = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(manager.dir.glob("task_*.json"))
        )
        self.assertIn("DRG综述项目", all_text)
        self.assertIn("提取数据", all_text)
        self.assertNotIn("\\u", all_text)


if __name__ == "__main__":
    unittest.main()
