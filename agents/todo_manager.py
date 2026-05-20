#!/usr/bin/env python3
"""
todo_manager.py - 待办事项管理模块

待办事项管理模块负责待办事项的创建、查询、更新、删除等操作。
"""



import json
from pathlib import Path


# -- TodoManager: LLM 写入的结构化状态管理器 --
class TodoManager:
    """
    待办事项管理器类
    
    负责管理任务列表的状态，包括添加、更新和渲染任务。
    支持三种状态：pending(待处理)、in_progress(进行中)、completed(已完成)
    """
    
    def __init__(self, todo_file: Path):
        """初始化会话绑定的多看板待办文件"""
        self.todo_file = todo_file
        self.boards = []
        self.active_board_id = None
        self.next_board_id = 1
        self.load()

    def load(self) -> list:
        """
        从磁盘加载当前会话待办看板。

        返回:
            当前待办看板列表
        """
        self.todo_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.todo_file.exists():
            self.boards = []
            self.active_board_id = None
            self.next_board_id = 1
            return self.boards

        payload = json.loads(self.todo_file.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "items" in payload:
            raise ValueError("Unsupported old todo format; delete the file and create a new board")
        if not isinstance(payload, dict) or payload.get("version") != 2:
            raise ValueError(f"Invalid todo file format: {self.todo_file}")

        boards = payload.get("boards")
        active_board_id = payload.get("active_board_id")
        next_board_id = payload.get("next_board_id")
        if not isinstance(boards, list):
            raise ValueError(f"Invalid todo file format: {self.todo_file}")
        if active_board_id is not None and not isinstance(active_board_id, str):
            raise ValueError(f"Invalid todo file format: {self.todo_file}")
        if not isinstance(next_board_id, int) or next_board_id < 1:
            raise ValueError(f"Invalid todo file format: {self.todo_file}")

        validated_boards = []
        board_ids = set()
        for board in boards:
            if not isinstance(board, dict):
                raise ValueError(f"Invalid todo file format: {self.todo_file}")
            board_id = str(board.get("id", "")).strip()
            title = str(board.get("title", "")).strip()
            if not board_id or not title or board_id in board_ids:
                raise ValueError(f"Invalid todo file format: {self.todo_file}")
            board_ids.add(board_id)
            validated_boards.append({
                "id": board_id,
                "title": title,
                "items": self._validate_items(board.get("items", [])),
            })

        if active_board_id is not None and active_board_id not in board_ids:
            raise ValueError(f"Invalid todo file format: {self.todo_file}")

        self.boards = validated_boards
        self.active_board_id = active_board_id
        self.next_board_id = next_board_id
        return self.boards

    def _save(self) -> None:
        """保存当前会话待办看板到磁盘"""
        self.todo_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "active_board_id": self.active_board_id,
            "next_board_id": self.next_board_id,
            "boards": self.boards,
        }
        self.todo_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _validate_items(self, items: list) -> list:
        """
        校验并规范化单个看板内的待办事项列表。

        参数:
            items: 任务列表，每个任务是一个包含 id、text、status 的字典

        返回:
            规范化后的任务列表

        异常:
            ValueError: 当任务数量超过20、任务文本为空、状态无效或同时有多个进行中任务时
        """
        if not isinstance(items, list):
            raise ValueError("items must be a list")

        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")

        validated = []
        in_progress_count = 0

        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"Item {i + 1}: must be an object")
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))

            if not text:
                raise ValueError(f"Item {item_id}: text required")

            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")

            if status == "in_progress":
                in_progress_count += 1

            validated.append({"id": item_id, "text": text, "status": status})

        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")

        return validated

    def _active_board(self) -> dict:
        if not self.active_board_id:
            raise ValueError("Call todo_new_board before todo")

        for board in self.boards:
            if board["id"] == self.active_board_id:
                return board

        raise ValueError(f"Active todo board not found: {self.active_board_id}")

    def create_board(self, title: str, items: list) -> str:
        """
        新建一组待办看板并设为当前活跃看板。

        参数:
            title: 看板标题
            items: 新看板完整待办事项列表

        返回:
            渲染后的全部看板
        """
        title = str(title).strip()
        if not title:
            raise ValueError("title required")

        board_id = f"board_{self.next_board_id}"
        board = {
            "id": board_id,
            "title": title,
            "items": self._validate_items(items),
        }
        self.boards.append(board)
        self.active_board_id = board_id
        self.next_board_id += 1
        self._save()
        return self.render()

    def update(self, items: list) -> str:
        """
        更新当前活跃看板的待办事项列表。

        参数:
            items: 当前活跃看板的完整待办事项列表

        返回:
            渲染后的全部看板
        """
        active_board = self._active_board()
        active_board["items"] = self._validate_items(items)
        self._save()
        return self.render()

    def has_open_items(self) -> bool:
        """
        判断当前会话是否存在未完成待办事项。

        返回:
            True 表示存在 pending 或 in_progress 项
        """
        return any(
            item["status"] != "completed"
            for board in self.boards
            for item in board["items"]
        )

    def render(self) -> str:
        """
        渲染待办事项列表为可读字符串
        
        返回:
            格式化的任务列表字符串，包含进度统计
        """
        if not self.boards:
            return "No todos."

        lines = []
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}

        for board_index, board in enumerate(self.boards):
            active_prefix = "[active] " if board["id"] == self.active_board_id else ""
            done = sum(1 for item in board["items"] if item["status"] == "completed")
            total = len(board["items"])
            lines.append(f"{active_prefix}{board['id']}: {board['title']} ({done}/{total} completed)")
            for item in board["items"]:
                lines.append(f"{marker[item['status']]} #{item['id']}: {item['text']}")
            if board_index != len(self.boards) - 1:
                lines.append("")

        return "\n".join(lines)
