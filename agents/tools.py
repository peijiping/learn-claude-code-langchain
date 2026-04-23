#!/usr/bin/env python3

"""
tools.py - 工具定义和实现模块

此模块集中管理所有可用的工具，包括：
- 工具的 JSON Schema 定义
- 工具函数的具体实现
- 工具名称到函数的映射

其他模块可以通过导入此模块来使用这些工具。
"""

import os
import subprocess
from pathlib import Path
from skills import SkillLoader
from task_manager import TaskManager
from todo_manager import TodoManager
from background_manager import BackgroundManager


# 根目录
ROOT_DIR = Path.cwd()
# 技能目录
SKILLS_DIR = ROOT_DIR / "skills"

# 工作目录
WORKDIR = ROOT_DIR / "WorkSpace"
# 任务目录
TASKS_DIR = WORKDIR / ".tasks"



# 创建全局 SkillLoader 实例
SKILL_LOADER = SkillLoader(SKILLS_DIR)
# 创建全局 TodoManager 实例
TODO_MANAGER = TodoManager()
# 创建全局 TaskManager 实例
TASKS = TaskManager(TASKS_DIR)
# 创建全局 BackgroundManager 实例
BACKGROUND_MANAGER = BackgroundManager()


# ============================================================
# 工具函数实现区域
# ============================================================

def safe_path(p: str) -> Path:
    """验证路径是否在工作目录内，防止路径遍历攻击"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """执行 shell 命令并返回结果"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120
        )
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            return f"Error: command failed with return code {r.returncode}\n{out}"
        return out[:50000] if out else "(command executed successfully, no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    """读取文件内容"""
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入内容到文件"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """替换文件中的指定文本"""
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


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
