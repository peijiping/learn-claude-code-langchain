#!/usr/bin/env python3

"""
tools.py - 工具定义和实现模块

此模块集中管理所有可用的工具，包括：
- 工具的 JSON Schema 定义
- 工具函数的具体实现
- 工具名称到函数的映射

其他模块可以通过导入此模块来使用这些工具。
"""

from pathlib import Path
from skills import SkillLoader
from todo_manager import TodoManager
from background_manager import BackgroundManager
from teammate_manager import TeammateManager
from message_bus import MessageBus, VALID_MSG_TYPES
from tool_base import (
    BASE_TOOL,
    BASE_TOOL_HANDLERS,
    ROOT_DIR,
    TODO_FILE,
    INBOX_DIR,
    TEAM_DIR,
    SKILL_LOADER,
    WORKDIR,
)




# 创建全局 TodoManager 实例
TODO_MANAGER = TodoManager(TODO_FILE)
# 创建全局 BackgroundManager 实例
BACKGROUND_MANAGER = BackgroundManager()
# 创建全局 MessageBus 实例
BUS = MessageBus(INBOX_DIR)
# 创建全局 TeammateManager 实例
TEAM = TeammateManager(TEAM_DIR)


def get_todo_manager() -> TodoManager:
    """返回全局 TodoManager 实例。"""
    return TODO_MANAGER


# ============================================================
# 工具处理器映射
# ============================================================

# 建立工具名称到函数的映射
# 当大模型返回工具调用请求时，根据工具名找到对应的函数来执行
TOOL_HANDLERS = {
    **BASE_TOOL_HANDLERS,
    "todo":        lambda **kw: TODO_MANAGER.update(kw["items"], kw.get("fresh_start", False)),
 
}

# ============================================================
# 工具定义区域，初始化时传给大模型，告诉它有哪些工具可用
# ============================================================


# 工具的 JSON Schema（描述工具的名称、参数等）
TOOLS = [
    *BASE_TOOL,
    {"name": "todo", "description": "更新当前会话的待办列表。整体替换语义：传入完整的 items 数组即可。对复杂任务建议在动手前先调用一次（把计划铺开），执行中逐步把对应项标记为 in_progress / completed。fresh_start=True 表示开始新计划——会先丢弃当前列表里所有已完成的任务，适合在同一会话内切换到下一个独立任务时使用。",
     "input_schema": {"type": "object", "properties": {
         "items": {"type": "array", "description": "完整的待办事项列表。", "items": {"type": "object", "properties": {
             "id": {"type": "string", "description": "任务标识，可省略，省略时按数组下标生成。"},
             "text": {"type": "string", "description": "任务内容（必填）。"},
             "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "任务状态；同一时刻只能有 1 个 in_progress。"},
         }, "required": ["text", "status"]}},
         "fresh_start": {"type": "boolean", "default": False, "description": "True 时表示开始新计划——先清掉当前列表里所有已完成的任务，再用 items 替换整个列表。"},
     }, "required": ["items"]}
    },
]



#主智能体工具
MAIN_AGENT_TOOLS = [
    *TOOLS,
    {"name": "sub_agent",
     "description": "分发子任务给通用型子智能体。子智能体拥有独立上下文（不污染主对话），共享文件系统，只返回最终摘要。子智能体默认拥有执行工具权限，但不包含 task 系列工具；任务看板只由主智能体维护。当任务需要多步骤操作、读取多个文件、收集信息或可能产生大量工具调用时使用。如果多个子任务之间没有依赖关系，设置 parallel=true 让它们并行执行以提升效率。串行时设为 false。\n\n可通过 allowed_tools 限制子智能体的工具范围，例如只允许只读操作。\n\n示例：\n- sub_agent(prompt=\"读取 DRG_Docs 目录下所有 PDF 的标题和摘要\", parallel=\"true\")\n- sub_agent(prompt=\"实现用户注册功能\", parallel=\"false\")\n- sub_agent(prompt=\"分析当前代码架构并设计重构方案\", parallel=\"false\")\n- sub_agent(prompt=\"只读方式搜索代码中的安全问题\", allowed_tools=[\"bash\",\"read_file\",\"read_pdf\"], parallel=\"true\")",
     "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "给子智能体的任务描述，应具体说明要做什么"},
            "description": {"type": "string", "description": "任务的简短描述，用于日志记录"},
            "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "限制子智能体可用的工具名称列表。不设置则默认使用全部工具。例如 [\"bash\",\"read_file\",\"read_pdf\"] 限制为只读工具集"},
            "parallel": {"type": "boolean", "enum": ["true", "false"], "description": "值为true、false，是否与其他 sub_agent 并行执行。"}
        },
        "required": ["prompt","parallel"]}
    }
]
