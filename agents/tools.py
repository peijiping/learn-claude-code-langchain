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
from task_manager import TaskManager
# BackgroundManager 实例由 agent_full_v2.py 持有，这里只做引用传递。
# 采用 holder 模式（与下面的 TODO_MANAGER 一致），避免 tools.py 反向依赖 agent_full_v2 造成循环 import。
from message_bus import MessageBus, VALID_MSG_TYPES
from memories import MemoryStore
from tool_base import (
    BASE_TOOL,
    BASE_TOOL_HANDLERS,
    ROOT_DIR,
    SKILLS_DIR,
    TODO_DIR,
    INBOX_DIR,
    TEAM_DIR,
    WORKDIR,
    CHAT_HISTORY_DIR,
    MEMORY_DIR,
    todo_file_for_session,
)



# 创建全局 SkillLoader 实例
SKILLS = SkillLoader(SKILLS_DIR)
# 创建全局 MessageBus 实例
BUS = MessageBus(INBOX_DIR)
# 创建全局 TeammateManager 实例
# TEAM = TeammateManager(TEAM_DIR)
# 创建全局 MemoryStore 实例
MEMORY = MemoryStore(MEMORY_DIR)

# 创建全局 TaskManager 实例
TASK_MANAGER = TaskManager()



# ── BackgroundManager: 跨模块共享的后台任务管理器 ─────────────────
# agent_full_v2.py 在启动时把它的 background_manager 实例通过 set_background_manager 挂上来。
# check_background 工具的 handler 走 get_background_manager() 取同一实例，
# 保证查询口径和实际派发后台任务的实例是同一个（共享 background_tasks / background_results）。
_BACKGROUND_MANAGER_HOLDER: dict = {"current": None}


def set_background_manager(bm) -> None:
    """由 agent_full_v2.py 在模块加载完成后调用一次，挂上 BackgroundManager 实例。"""
    _BACKGROUND_MANAGER_HOLDER["current"] = bm


def get_background_manager():
    """获取 BackgroundManager 实例；未初始化时抛错，提示调用方先 set_background_manager。"""
    mgr = _BACKGROUND_MANAGER_HOLDER["current"]
    if mgr is None:
        raise RuntimeError(
            "BackgroundManager 未初始化。请先调用 set_background_manager(...)。"
        )
    return mgr



# ── TodoManager: 按 session 懒绑定的轻量级任务看板 ─────────────────
# todo 文件与 chat history 一一绑定（session_<N>.todo.json ↔ session_<N>.jsonl）。
# 不再做成模块级单例，而是用 holder 在切会话时重新指向对应文件。
# 工具 handler 仍以 dict 形式集中管理，"todo" 通过 get_todo_manager() 取当前会话实例。
_TODO_MANAGER_HOLDER: dict = {"current": None}


def set_todo_manager(session_num: int) -> "TodoManager":
    """
    切换 TodoManager 到指定 session 编号对应的 todo 文件。

    调用时机：
    - 启动时 init_session 后
    - /newsession、/switchsession N、/clearsession 后

    每次调用都会重新构造 TodoManager（构造时即从磁盘 load），
    这样上一个会话的内存状态与新会话完全隔离。
    """
    TODO_DIR.mkdir(parents=True, exist_ok=True)
    todo_file = todo_file_for_session(session_num)
    _TODO_MANAGER_HOLDER["current"] = TodoManager(todo_file)
    return _TODO_MANAGER_HOLDER["current"]


def get_todo_manager() -> "TodoManager":
    """
    获取当前会话的 TodoManager。

    未初始化（set_todo_manager 从未被调用）时抛错，提示调用方先去初始化。
    """
    mgr = _TODO_MANAGER_HOLDER["current"]
    if mgr is None:
        raise RuntimeError(
            "TodoManager 未初始化。请先调用 set_todo_manager(session_num) "
            "或在启动后使用 init_session。"
        )
    return mgr


# ============================================================
# 工具处理器映射
# ============================================================

# 建立工具名称到函数的映射
# 当大模型返回工具调用请求时，根据工具名找到对应的函数来执行
TOOL_HANDLERS = {
    **BASE_TOOL_HANDLERS,
    "todo":        lambda **kw: get_todo_manager().update(kw["items"], kw.get("fresh_start", False)),
    "load_skill":  lambda **kw: SKILLS.load_skill(kw["name"]),
    "list_skills": lambda **kw: SKILLS.list_skills(),
    "write_memory":   lambda **kw: MEMORY.write(kw["name"], kw["type"], kw["description"], kw["body"]),
    "forget_memory":  lambda **kw: MEMORY.forget(kw["name"]),
    "create_task": lambda **kw: TASK_MANAGER.run_create_task(
        subject=kw["subject"],
        description=kw.get("description", ""),
        blockedBy=kw.get("blockedBy"),
    ),
    "list_tasks": lambda **kw: TASK_MANAGER.run_list_tasks(),
    "get_task": lambda **kw: TASK_MANAGER.run_get_task(kw["task_id"]),
    "claim_task": lambda **kw: TASK_MANAGER.run_claim_task(kw["task_id"]),
    "complete_task": lambda **kw: TASK_MANAGER.run_complete_task(kw["task_id"]),
    # check_background：仅查询语义，不消费结果；可重复调用。
    # agent_full_v2.py 在每个 turn 开头以及 turn 内每轮 tool 执行后，
    # 会自动把已完成任务以 <task_notification> 注入上下文（消费语义），
    # 本工具是"主动查询"补充，用于模型想看还未被消费的任务当前状态。
    "check_background": lambda **kw: get_background_manager().check(kw.get("task_id")),
}

# ============================================================
# 工具定义区域，初始化时传给大模型，告诉它有哪些工具可用
# ============================================================


# 工具定义遵循 OpenAI SDK 格式：每个工具用 type="function" 包装，参数定义在 function.parameters 下
TOOLS = [
    *BASE_TOOL,
    {"type": "function", "function": {
        "name": "todo",
        "description": "更新当前会话的待办列表。整体替换语义：传入完整的 items 数组即可。对复杂任务建议在动手前先调用一次（把计划铺开），执行中逐步把对应项标记为 in_progress / completed。fresh_start=True 表示开始新计划——会先丢弃当前列表里所有已完成的任务，适合在同一会话内切换到下一个独立任务时使用。",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "description": "完整的待办事项列表。", "items": {"type": "object", "properties": {
                "id": {"type": "string", "description": "任务标识，可省略，省略时按数组下标生成。"},
                "text": {"type": "string", "description": "任务内容（必填）。"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "任务状态；同一时刻只能有 1 个 in_progress。"},
            }, "required": ["text", "status"]}},
            "fresh_start": {"type": "boolean", "default": False, "description": "True 时表示开始新计划——先清掉当前列表里所有已完成的任务，再用 items 替换整个列表。"},
        }, "required": ["items"]}
    }},
    {"type": "function", "function": {
        "name": "load_skill", "description": "加载指定名称的专业技能（skill）知识。",
        "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "要加载的专业技能（skill）名称"}}, "required": ["name"]}
    }},
    {"type": "function", "function": {
        "name": "list_skills", "description": "获取当前所有可用技能（skill）的名称和简短描述列表，用于了解当前会话支持哪些技能。",
        "parameters": {"type": "object", "properties": {
            "parallel": {"type": "boolean", "default": False,
                "description": "True 时与同次响应中其他独立查询并行执行。"}
        }}
    }},
    # ── [改动 2] 新增：记忆工具 ──────────────────────────────
    {"type": "function", "function": {
        "name": "write_memory",
        "description": "Save a piece of information to persistent memory. "
                       "Use when the user states a preference, corrects you, "
                       "approves an approach, reveals a project fact, or asks you to remember something. "
                       "The memory will be available in future sessions.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Short kebab-case identifier, e.g. 'user-preference-tabs'"},
            "type": {"type": "string",
                      "enum": ["user", "feedback", "project", "reference"],
                      "description": "user=preference/habit, feedback=guidance/correction, project=fact/decision, reference=external pointer"},
            "description": {"type": "string", "description": "One-line summary shown in MEMORY.md index"},
            "body": {"type": "string", "description": "Full detail in markdown. Include context and rationale."}
        }, "required": ["name", "type", "description", "body"]}
    }},
    {"type": "function", "function": {
        "name": "forget_memory",
        "description": "Delete a memory by its name or filename. "
                       "Use when the user contradicts a saved memory or asks you to forget something.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Name of the memory to delete (slug or filename)"}
        }, "required": ["name"]}
    }},
    # ── [改动 3] 新增：任务管理工具 ──────────────────────────────
    {"type": "function", "function": {
        "name": "create_task",
        "description": "Create a new task with optional blockedBy dependencies.",
        "parameters": {"type": "object",
                       "properties": {
                           "subject": {"type": "string"},
                           "description": {"type": "string"},
                           "blockedBy": {"type": "array",
                                         "items": {"type": "string"}}},
                       "required": ["subject"]}
    }},
    {"type": "function", "function": {
        "name": "list_tasks",
        "description": "List all tasks with status, owner, and dependencies.",
        "parameters": {"type": "object", "properties": {
            "parallel": {"type": "boolean", "default": False,
                "description": "True 时与同次响应中其他独立查询并行执行。"}
        },
        "required": []}
    }},
    {"type": "function", "function": {
        "name": "get_task",
        "description": "Get full details of a specific task by ID.",
        "parameters": {"type": "object",
                       "properties": {
                           "task_id": {"type": "string"},
                           "parallel": {"type": "boolean", "default": False,
                               "description": "True 时与同次响应中其他独立查询并行执行。多个 get_task 一起发可提速。"}
                       },
                       "required": ["task_id"]}
    }},
    {"type": "function", "function": {
        "name": "claim_task",
        "description": "Claim a pending task. Sets owner, changes status to in_progress.",
        "parameters": {"type": "object",
                       "properties": {"task_id": {"type": "string"}},
                       "required": ["task_id"]}
    }},
    {"type": "function", "function": {
        "name": "complete_task",
        "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
        "parameters": {"type": "object",
                       "properties": {"task_id": {"type": "string"}},
                       "required": ["task_id"]}
    }},
    {"type": "function", "function": {
        "name": "check_background",
        "description": (
            "查询后台任务状态。仅查询语义，不消费结果，可重复调用。\n"
            "- 不传 task_id：列出所有后台任务的当前状态（running / completed / notified），已完成或已通知的任务会附带结果预览。\n"
            "- 传 task_id：返回该任务的详细状态；若已完成或已通知则返回完整结果。\n\n"
            "适用场景：\n"
            "1. 用户问后台任务执行得怎么样了时，主动查而不是再起一个新任务。\n"
            "2. 后台 sub_agent 已派发但还没收到 task_notification 时，确认是否还在跑。\n"
            "3. task_notification 已注入但 <summary> 截断不够用时，再用 task_id 取完整结果。\n"
            "4. 排查后台任务是否失败（status=error 也会出现在列表里）。\n\n"
            "注意：本工具只查询、不消费结果。优先用主循环自动注入的 task_notification 里的 <full_output>；"
            "如需重复取完整结果再调用本工具的 task_id 参数。"
        ),
        "parameters": {"type": "object",
                       "properties": {
                           "task_id": {"type": "string",
                                       "description": "可选，指定任务 ID（如 bg_0001）。不传则列出所有后台任务。"},
                           "parallel": {"type": "boolean", "default": False,
                               "description": "True 时与同次响应中其他独立查询并行执行。"}
                       }}
    }},
]



#主智能体工具
MAIN_AGENT_TOOLS = [
    *TOOLS,
    {"type": "function", "function": {
        "name": "sub_agent",
        "description": "分发子任务给通用型子智能体。子智能体拥有独立上下文（不污染主对话），共享文件系统，只返回最终摘要。子智能体默认拥有执行工具权限，但不包含 task 系列工具；任务看板只由主智能体维护。当任务需要多步骤操作、读取多个文件、收集信息或可能产生大量工具调用时使用。\n\n⚠️ 强制规则（必须遵守，违例会阻塞主循环浪费时间）：\n凡是「批量 / 全量 / 跨多个文件 / 跨整个目录 / 预计耗时 > 30 秒」的任务，**必须传 run_in_background=true** 丢到后台线程异步执行，立即返回任务 ID，结果通过后续轮次的 <task_notification> 收回。绝对不要同步等待这类任务完成。\n判断标准（命中任意一条就必须后台）：\n  - 涉及 ≥ 2 个文件 / 整个目录 / 全部 N 个 X\n  - prompt 含「全部 / 全量 / 批量 / 跑一遍 / 扫描 / 审计 / 构建 / 测试套件」等关键词\n  - 需要多步骤工具调用且总耗时可能 > 30 秒\n允许同步（run_in_background 默认 false）的场景：\n  - 单个文件的快速查询、单步工具调用\n  - 必须等前序结果才能继续的下一步操作\n\n如果多个子任务之间没有依赖关系，设置 parallel=true 让它们并行执行以提升效率；串行时设为 false。注意：run_in_background 与 parallel 互斥——已传 run_in_background=true 时不要再传 parallel。\n\n可通过 allowed_tools 限制子智能体的工具范围，例如只允许只读操作。\n\n示例：\n- sub_agent(prompt=\"读取 DRG_Docs 目录下全部 19 个 PDF 的标题和摘要\", run_in_background=true)  ← 批量全目录，必须后台\n- sub_agent(prompt=\"实现用户注册功能\", parallel=\"false\")\n- sub_agent(prompt=\"分析当前代码架构并设计重构方案\", parallel=\"false\")\n- sub_agent(prompt=\"只读方式搜索代码中的安全问题\", allowed_tools=[\"bash\",\"read_file\",\"read_pdf\"], parallel=\"true\")\n- sub_agent(prompt=\"跑全量测试并报告失败用例\", parallel=\"false\", run_in_background=true)",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "给子智能体的任务描述，应具体说明要做什么"},
                "description": {"type": "string", "description": "任务的简短描述，用于日志记录"},
                "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "限制子智能体可用的工具名称列表。不设置则默认使用全部工具。例如 [\"bash\",\"read_file\",\"read_pdf\"] 限制为只读工具集"},
                "parallel": {"type": "boolean", "description": "是否与其他 sub_agent 并行执行。"},
                "run_in_background": {"type": "boolean", "default": False,
                    "description": "True 时把子任务丢到后台线程异步执行，立即返回后台任务 ID；"
                                   "结果通过 <task_notification> 在后续轮次通知。"
                                   "与 parallel 互斥：传 True 时不再走并行/串行等待桶。"}
            },
            "required": ["prompt", "parallel"]
        }
    }}
]
