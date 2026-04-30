#!/usr/bin/env python3

"""
tools.py - 工具定义和实现模块

此模块集中管理所有可用的工具，包括：
- 工具的 JSON Schema 定义
- 工具函数的具体实现
- 工具名称到函数的映射

其他模块可以通过导入此模块来使用这些工具。
"""

import json
import os
import subprocess
from pathlib import Path
from skills import SkillLoader
from task_manager import TaskManager
from todo_manager import TodoManager
from background_manager import BackgroundManager
from teammate_manager import TeammateManager, MessageBus, VALID_MSG_TYPES
from tools_base import safe_path, run_bash, run_read, run_write, run_edit


# 根目录
ROOT_DIR = Path.cwd()
# 技能目录
SKILLS_DIR = ROOT_DIR / "skills"

# 工作目录
WORKDIR = ROOT_DIR / "WorkSpace"
# 任务目录
TASKS_DIR = WORKDIR / ".tasks"
# 团队目录
TEAM_DIR = WORKDIR / ".team"
# 收件箱目录
INBOX_DIR = TEAM_DIR / ".inbox"



# 创建全局 SkillLoader 实例
SKILL_LOADER = SkillLoader(SKILLS_DIR)
# 创建全局 TodoManager 实例
TODO_MANAGER = TodoManager()
# 创建全局 TaskManager 实例
TASKS = TaskManager(TASKS_DIR)
# 创建全局 BackgroundManager 实例
BACKGROUND_MANAGER = BackgroundManager()
# 创建全局 MessageBus 实例
BUS = MessageBus(INBOX_DIR)
# 创建全局 TeammateManager 实例
TEAM = TeammateManager(TEAM_DIR)


# ============================================================
# 工具处理器映射
# ============================================================

# 建立工具名称到函数的映射
# 当大模型返回工具调用请求时，根据工具名找到对应的函数来执行
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo":       lambda **kw: TODO_MANAGER.update(kw["items"]),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("addBlockedBy"), kw.get("addBlocks")),
    "task_list":   lambda **kw: TASKS.list_all(),
    "task_get":    lambda **kw: TASKS.get(kw["task_id"]),
    "background_run":   lambda **kw: BACKGROUND_MANAGER.run(kw["command"]),
    "check_background": lambda **kw: BACKGROUND_MANAGER.check(kw.get("task_id")),
    "spawn_teammate":   lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":   lambda **kw: TEAM.list_all(),
    "send_message":     lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":       lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":        lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
}


# ============================================================
# 工具定义区域
# ============================================================

# 定义工具的 JSON Schema（描述工具的名称、参数等）
# 该工具是大模型初始化时给大模型传参用，告诉大模型有哪些工具可用
CHILD_TOOLS = [
    {
        "name": "bash",
        "description": "执行 shell 命令。",
        "input_schema": {"type": "object","properties": {"command": {"type": "string"}},"required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "读取文件内容。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},"required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "将内容写入文件。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "content": {"type": "string"}},"required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "替换文件中指定的文本内容。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},"required": ["path", "old_text", "new_text"]
        }
    },
    {
        "name": "todo",
        "description": "简单任务更新任务列表。用于跟踪多步骤任务的进度。",
        "input_schema": { "type": "object", "properties": {"items": {"type": "array","items": {"type": "object","properties": {"id": {"type": "string"},"text": {"type": "string"},"status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},"required": ["id", "text", "status"] }}},"required": ["items"]}
    },
    {
        "name": "load_skill", 
        "description": "加载指定名称的专业技能（skill）知识。",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "要加载的专业技能（skill）名称"}}, "required": ["name"]}
    },
    {"name": "task_create", "description": "Create a new task.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_update", "description": "Update a task's status or dependencies.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "addBlocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    {"name": "task_list", "description": "List all tasks with status summary.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "task_get", "description": "Get full details of a task by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
     {"name": "background_run", "description": "Run command in background thread. Returns task_id immediately.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "check_background", "description": "Check background task status. Omit task_id to list all.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
     {"name": "spawn_teammate", "description": "Spawn a persistent teammate that runs in its own thread.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates with name, role, status.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate's inbox.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
]

# -- Parent tools: base tools + task dispatcher --
PARENT_TOOLS = CHILD_TOOLS + [
    {"name": "sub_agent",
     "description": "分发子任务给子智能体。子智能体拥有独立上下文（不污染主对话），共享文件系统，只返回最终摘要。当任务需要多步骤操作、读取多个文件、收集信息或可能产生大量工具调用时使用。",
     "input_schema": {
        "type": "object",
        "properties": {"prompt": {"type": "string", "description": "给子智能体的任务描述，应具体说明要做什么"}, "description": {"type": "string", "description": "任务的简短描述，用于日志记录"}},
        "required": ["prompt"]}
    },
]
