#!/usr/bin/env python3
"""
todo_manager.py - 待办事项管理模块

待办事项管理模块负责待办事项的创建、查询、更新、删除等操作。
"""



# -- TodoManager: LLM 写入的结构化状态管理器 --
class TodoManager:
    """
    待办事项管理器类
    
    负责管理任务列表的状态，包括添加、更新和渲染任务。
    支持三种状态：pending(待处理)、in_progress(进行中)、completed(已完成)
    """
    
    def __init__(self):
        """初始化空的任务列表"""
        self.items = []

    def update(self, items: list) -> str:
        """
        更新待办事项列表
        
        参数:
            items: 任务列表，每个任务是一个包含 id、text、status 的字典
            items参数一定要和TOOLS中定义的input_schema中的items参数保持一致
            
        返回:
            渲染后的任务列表字符串
            
        异常:
            ValueError: 当任务数量超过20、任务文本为空、状态无效或同时有多个进行中任务时
        """
        # 限制最大任务数量为20
        if len(items) > 20:
            raise ValueError("Max 20 todos allowed")
        
        validated = []
        in_progress_count = 0
        
        # 遍历并验证每个任务项
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            
            # 验证任务文本不为空
            if not text:
                raise ValueError(f"Item {item_id}: text required")
            
            # 验证状态值有效
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            
            # 统计进行中任务数量
            if status == "in_progress":
                in_progress_count += 1
            
            validated.append({"id": item_id, "text": text, "status": status})
        
        # 确保只有一个进行中的任务
        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time")
        
        self.items = validated
        return self.render()

    def render(self) -> str:
        """
        渲染待办事项列表为可读字符串
        
        返回:
            格式化的任务列表字符串，包含进度统计
        """
        if not self.items:
            return "No todos."
        
        lines = []
        # 状态标记映射
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        
        for item in self.items:
            lines.append(f"{marker[item['status']]} #{item['id']}: {item['text']}")
        
        # 统计已完成任务数量
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        
        return "\n".join(lines)
