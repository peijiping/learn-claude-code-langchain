#!/usr/bin/env python3
"""
task_manager.py - 任务管理模块

任务管理模块负责任务的创建、查询、更新、删除等操作。
支持任务依赖关系管理，任务状态包括：pending（待处理）、in_progress（进行中）、completed（已完成）。
"""

import json
from pathlib import Path


# -- TaskManager: 支持依赖关系图的CRUD操作，数据持久化为JSON文件 --
class TaskManager:
    """
    任务管理器类
    
    提供任务的增删改查功能，支持任务之间的依赖关系管理。
    每个任务以独立的JSON文件形式存储在指定目录中。
    """
    
    def __init__(self, tasks_dir: Path):
        """
        初始化任务管理器
        
        Args:
            tasks_dir: 任务数据存储目录的路径
        """
        self.dir = tasks_dir  # 任务文件存储目录
        self.dir.mkdir(exist_ok=True)  # 如果目录不存在则创建
        self._next_id = self._max_id() + 1  # 初始化下一个任务ID

    def _max_id(self) -> int:
        """
        获取当前最大的任务ID
        
        遍历任务目录中的所有任务文件，提取ID并返回最大值。
        如果没有任务文件，返回0。
        
        Returns:
            当前最大的任务ID
        """
        # 从文件名中提取任务ID（格式：task_{id}.json）
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> dict:
        """
        加载指定ID的任务数据
        
        Args:
            task_id: 任务ID
            
        Returns:
            任务数据的字典形式
            
        Raises:
            ValueError: 当任务不存在时抛出
        """
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())

    def _save(self, task: dict):
        """
        保存任务数据到文件
        
        Args:
            task: 任务数据的字典，必须包含 'id' 字段
        """
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        """
        创建新任务
        
        Args:
            subject: 任务主题/标题
            description: 任务描述（可选）
            
        Returns:
            JSON格式的任务数据字符串
            
        任务数据结构：
            - id: 任务唯一标识
            - subject: 任务主题
            - description: 任务描述
            - status: 任务状态（pending/in_progress/completed）
            - blockedBy: 阻塞当前任务的前置任务ID列表
            - blocks: 后置任务ID列表
            - owner: 任务负责人
        """
        task = {
            "id": self._next_id,  # 分配新ID
            "subject": subject,   # 任务主题
            "description": description,  # 任务描述
            "status": "pending",  # 默认状态为待处理
            "blockedBy": [],      # 被哪些前置任务阻塞
            "blocks": [],         # 后置任务ID列表
            "owner": "",          # 任务负责人
        }
        self._save(task)  # 保存到文件
        self._next_id += 1  # 递增ID计数器
        return json.dumps(task, indent=2)

    def get(self, task_id: int) -> str:
        """
        获取指定任务的信息
        
        Args:
            task_id: 任务ID
            
        Returns:
            JSON格式的任务数据字符串
        """
        return json.dumps(self._load(task_id), indent=2)

    def update(self, task_id: int, status: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        """
        更新任务信息
        
        Args:
            task_id: 要更新的任务ID
            status: 新的任务状态（可选）
            add_blocked_by: 要添加的阻塞当前任务的前置任务ID列表（可选）
            add_blocks: 要添加的后置任务ID列表（可选）
            
        Returns:
            JSON格式的更新后任务数据字符串
            
        Raises:
            ValueError: 当状态值无效时抛出
        """
        task = self._load(task_id)  # 加载现有任务数据
        
        # 更新任务状态
        if status:
            # 验证状态值是否有效
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            # 当任务完成时，从所有其他任务的blockedBy列表中移除该任务
            if status == "completed":
                self._clear_dependency(task_id)
        
        # 添加阻塞当前任务的任务（当前任务依赖于这些任务）
        if add_blocked_by:
            # 使用set去重后转回list
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        
        # 添加被当前任务阻塞的任务（这些任务依赖于当前任务）
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
            # 双向更新：同时更新被阻塞任务的blockedBy列表
            for blocked_id in add_blocks:
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    # 如果被阻塞的任务不存在，忽略错误
                    pass
        
        self._save(task)  # 保存更新后的任务
        return json.dumps(task, indent=2)

    def _clear_dependency(self, completed_id: int):
        """
        清除任务依赖关系
        
        当任务完成时，从所有其他任务的blockedBy列表中移除该任务ID。
        这样可以解除其他任务对该已完成任务的依赖。
        
        Args:
            completed_id: 已完成的任务ID
        """
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def list_all(self) -> str:
        """
        列出所有任务
        
        以格式化的字符串形式返回所有任务的列表。
        每个任务显示状态标记、ID、主题和阻塞信息。
        
        Returns:
            格式化的任务列表字符串
            
        状态标记说明：
            - [ ]: pending（待处理）
            - [>]: in_progress（进行中）
            - [x]: completed（已完成）
            - [?]: 未知状态
        """
        tasks = []
        # 读取所有任务文件
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text()))
        
        if not tasks:
            return "No tasks."
        
        lines = []
        for t in tasks:
            # 根据状态选择对应的标记
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            # 如果有阻塞信息，显示阻塞该任务的任务ID列表
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
        
        return "\n".join(lines)
