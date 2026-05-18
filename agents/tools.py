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
import threading
import uuid
from pathlib import Path
from skills import SkillLoader
from task_manager import TaskManager
# from todo_manager import TodoManager
from background_manager import BackgroundManager
from teammate_manager import TeammateManager
from message_bus import MessageBus, VALID_MSG_TYPES
from tools_base import safe_path, run_bash, run_read, run_read_pdf, run_write, run_edit


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
INBOX_DIR = WORKDIR / ".inbox"



# 创建全局 SkillLoader 实例
SKILL_LOADER = SkillLoader(SKILLS_DIR)
# 创建全局 TodoManager 实例
# TODO_MANAGER = TodoManager()
# 创建全局 TaskManager 实例
TASKS = TaskManager(TASKS_DIR)
# 创建全局 BackgroundManager 实例
BACKGROUND_MANAGER = BackgroundManager()
# 创建全局 MessageBus 实例
BUS = MessageBus(INBOX_DIR)
# 创建全局 TeammateManager 实例
TEAM = TeammateManager(TEAM_DIR)



# =============================================================================
# 请求跟踪器 - 用于关联异步请求与响应
# =============================================================================

# 关闭请求跟踪器：存储所有发出的关闭请求，键为request_id，值为请求详情
# 包含字段：target(目标团队成员), status(pending/approved/rejected)
shutdown_requests = {}

# 计划审批请求跟踪器：存储待审批的计划请求
# 包含字段：from_(发送者), status(pending/approved/rejected)
plan_requests = {}

# 线程锁：保护共享数据结构的线程安全
# 所有对 shutdown_requests 和 plan_requests 的访问都需要先获取此锁
_tracker_lock = threading.Lock()

# =============================================================================
# 团队协作协议处理器 - Lead专用
# =============================================================================

# -- Lead-specific protocol handlers --
def handle_shutdown_request(teammate: str) -> str:
    """
    向指定团队成员发送优雅关闭请求

    作为 Lead 智能体使用的协议工具，用于请求某个团队成员（teammate）优雅地关闭。
    流程如下：
    1. 生成唯一的请求 ID（request_id），用于后续跟踪请求状态
    2. 在 shutdown_requests 跟踪器中注册该请求，初始状态为 "pending"
    3. 通过消息总线（BUS）向目标成员发送类型为 "shutdown_request" 的消息
    4. 返回操作结果字符串，包含请求 ID 以便后续查询状态

    参数:
        teammate: 目标团队成员名称，指定要关闭哪个成员

    返回:
        str: 操作结果消息，包含生成的 request_id，格式如：
             "Shutdown request a1b2c3d4 sent to 'worker_b'"

    注意:
        - 本函数仅发送请求，不等待响应
        - 调用后可通过 _check_shutdown_status() 查询请求的处理状态
        - 目标成员收到请求后，应通过 shutdown_response 工具回复审批结果
    """
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    """
    审批团队成员提交的计划请求

    作为 Lead 智能体使用的协议工具，用于审批团队成员提交的计划（plan）。
    流程如下：
    1. 根据 request_id 从 plan_requests 跟踪器中查找待审批的计划请求
    2. 如果未找到对应的请求记录，返回错误信息
    3. 更新请求状态为 "approved"（批准）或 "rejected"（拒绝）
    4. 通过消息总线（BUS）向请求发起者发送审批结果通知
    5. 返回操作结果字符串

    参数:
        request_id: 计划请求的唯一标识符，用于定位待审批的请求
        approve: 审批结果，True 表示批准，False 表示拒绝
        feedback: 审批反馈意见（可选），用于向请求发起者说明审批理由或修改建议

    返回:
        str: 操作结果消息，格式如：
             - "Plan approved for 'worker_a'"
             - "Plan rejected for 'worker_a'"
             - "Error: Unknown plan request_id 'xxxx'"

    注意:
        - request_id 必须存在于 plan_requests 中，否则返回错误
        - 审批结果会通过消息总线异步通知请求发起者
        - 反馈意见（feedback）会作为审批响应消息的内容发送给对方
    """
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"


def _check_shutdown_status(request_id: str) -> str:
    """
    查询关闭请求的当前处理状态（内部工具）

    根据 request_id 从 shutdown_requests 跟踪器中查询指定关闭请求的当前状态。
    用于 Lead 智能体在发出关闭请求后，轮询或确认目标成员的处理结果。

    参数:
        request_id: 关闭请求的唯一标识符，由 handle_shutdown_request() 生成

    返回:
        str: JSON 格式的状态信息，包含以下字段：
             - 正常情况：{"target": "成员名", "status": "pending|approved|rejected"}
             - 未找到：{"error": "not found"}

    注意:
        - 本函数以下划线开头，标记为内部使用工具
        - 状态字段说明：
          - pending:   请求已发送，等待目标成员响应
          - approved:  目标成员已批准关闭请求
          - rejected:  目标成员拒绝关闭请求
        - 如果 request_id 不存在于跟踪器中，返回 {"error": "not found"}
    """
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))




# ============================================================
# 工具处理器映射
# ============================================================

# 建立工具名称到函数的映射
# 当大模型返回工具调用请求时，根据工具名找到对应的函数来执行
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "read_pdf":   lambda **kw: run_read_pdf(kw["path"], kw.get("max_pages", 5), kw.get("chars_per_page", 3000)),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # "todo":       lambda **kw: TODO_MANAGER.update(kw["items"]),
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
    "shutdown_request":  lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval":     lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
}


# ============================================================
# 工具定义区域
# ============================================================

# 定义工具的 JSON Schema（描述工具的名称、参数等）
# 该工具是大模型初始化时给大模型传参用，告诉大模型有哪些工具可用
TOOLS = [
    {
        "name": "bash","description": "执行 shell 命令。",
        "input_schema": {"type": "object","properties": {"command": {"type": "string"}},"required": ["command"]}
    },
    {
        "name": "read_file","description": "读取文件内容。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},"required": ["path"]}
    },
    {
        "name": "read_pdf","description": "使用 pymupdf 安全读取 PDF 文件，分页提取文本。读取 PDF 时必须使用此工具，不要使用 bash 的 strings/cat 等命令。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string","description": "PDF 文件路径"},"max_pages": {"type": "integer","description": "最大读取页数，默认5"},"chars_per_page": {"type": "integer","description": "每页最大字符数，默认3000"}},"required": ["path"]}
    },
    {
        "name": "write_file","description": "将内容写入文件。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "content": {"type": "string"}},"required": ["path", "content"]}
    },
    {
        "name": "edit_file","description": "替换文件中指定的文本内容。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},"required": ["path", "old_text", "new_text"]}
    },
    # {
    #     "name": "todo",
    #     "description": "简单任务更新任务列表。用于跟踪多步骤任务的进度。",
    #     "input_schema": { "type": "object", "properties": {"items": {"type": "array","items": {"type": "object","properties": {"id": {"type": "string"},"text": {"type": "string"},"status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},"required": ["id", "text", "status"] }}},"required": ["items"]}
    # },
    {"name": "load_skill", "description": "加载指定名称的专业技能（skill）知识。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "要加载的专业技能（skill）名称"}}, "required": ["name"]}
    },
    {"name": "task_create", "description": "创建一个新任务。",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_update", "description": "更新任务的状态或依赖关系。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "addBlocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    {"name": "task_list", "description": "列出所有任务及其状态摘要。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "task_get", "description": "根据ID获取任务的完整详情。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
     {"name": "background_run", "description": "在后台线程中运行命令，立即返回task_id。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "check_background", "description": "检查后台任务状态，省略task_id以列出所有任务。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
     {"name": "spawn_teammate", "description": "生成一个在独立线程中运行的持久化队友。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "列出所有队友的名称、角色和状态。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "向队友的收件箱发送消息。",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "读取并清空负责人的收件箱。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "向所有队友发送消息。",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "请求队友优雅关闭，返回request_id用于跟踪。",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "通过request_id检查关闭请求的状态。",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "批准或拒绝队友的计划，提供request_id、approve和可选的feedback。",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
]



# 子智能体的工具描述，工具是大模型初始化时给大模型传参用，告诉大模型有哪些工具可用
CHILD_TOOLS_SUBAGENT = [
    {
        "name": "bash","description": "执行 shell 命令。",
        "input_schema": {"type": "object","properties": {"command": {"type": "string"}},"required": ["command"]}
    },
    {
        "name": "read_file","description": "读取文件内容。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},"required": ["path"]}
    },
    {
        "name": "read_pdf","description": "使用 pymupdf 安全读取 PDF 文件，分页提取文本。读取 PDF 时必须使用此工具，不要使用 bash 的 strings/cat 等命令。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string","description": "PDF 文件路径"},"max_pages": {"type": "integer","description": "最大读取页数，默认5"},"chars_per_page": {"type": "integer","description": "每页最大字符数，默认3000"}},"required": ["path"]}
    },
    {
        "name": "write_file","description": "将内容写入文件。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "content": {"type": "string"}},"required": ["path", "content"]}
    },
    {
        "name": "edit_file","description": "替换文件中指定的文本内容。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},"required": ["path", "old_text", "new_text"]}
    },
    {
        "name": "load_skill", "description": "加载指定名称的专业技能（skill）知识。",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "要加载的专业技能（skill）名称"}}, "required": ["name"]}
    },
    {
        "name": "task_create", "description": "创建一个新任务。",
        "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}
    },
    {
        "name": "task_update", "description": "更新任务的状态或依赖关系。",
        "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "addBlocks": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}
    },
    {
        "name": "task_list", "description": "列出所有任务及其状态摘要。",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "task_get", "description": "根据ID获取任务的完整详情。",
        "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}
    },
    {
        "name": "background_run", "description": "在后台线程中运行命令，立即返回task_id。",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    },
    {
        "name": "check_background", "description": "检查后台任务状态，省略task_id以列出所有任务。",
        "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}
    },
    
]

# -- Parent tools: base tools + task dispatcher --
PARENT_TOOLS = CHILD_TOOLS_SUBAGENT + [
    {"name": "sub_agent",
     "description": "分发子任务给通用型子智能体。子智能体拥有独立上下文（不污染主对话），共享文件系统，只返回最终摘要。子智能体默认拥有全部工具权限，通过 prompt 描述引导其行为。当任务需要多步骤操作、读取多个文件、收集信息或可能产生大量工具调用时使用。如果多个子任务之间没有依赖关系，设置 parallel=true 让它们并行执行以提升效率。串行时设为 false。\n\n可通过 allowed_tools 限制子智能体的工具范围，例如只允许只读操作。\n\n示例：\n- sub_agent(prompt=\"读取 DRG_Docs 目录下所有 PDF 的标题和摘要\", parallel=\"true\")\n- sub_agent(prompt=\"实现用户注册功能\", parallel=\"false\")\n- sub_agent(prompt=\"分析当前代码架构并设计重构方案\", parallel=\"false\")\n- sub_agent(prompt=\"只读方式搜索代码中的安全问题\", allowed_tools=[\"bash\",\"read_file\",\"read_pdf\"], parallel=\"true\")",
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
