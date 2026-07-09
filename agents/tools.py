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


# 根目录
ROOT_DIR = Path.cwd()
# 技能目录
SKILLS_DIR = ROOT_DIR / "skills"

# 工作目录
WORKDIR = ROOT_DIR / "WorkSpace/task1"
# 待办目录与文件
TODO_DIR = WORKDIR / ".todo"
TODO_FILE = TODO_DIR / "todo.json"
# 团队目录
TEAM_DIR = WORKDIR / ".team"
# 收件箱目录
INBOX_DIR = WORKDIR / ".inbox"



# 创建全局 SkillLoader 实例
SKILL_LOADER = SkillLoader(SKILLS_DIR)
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

#函数实体区


def safe_path(p: str) -> Path:
    """
    验证路径是否在工作目录内，防止路径遍历攻击
    安全机制：
    - 将相对路径与工作目录拼接后转换为绝对路径
    - 检查最终路径是否仍然在 WORKDIR 内
    - 如果路径逃逸到 WORKDIR 之外，抛出 ValueError
    参数：
        p: 相对路径字符串
    返回：
        验证通过后的绝对路径(Path对象)
    异常：
        ValueError: 当路径试图逃逸到工作目录之外时抛出
                     例如：p = "../../etc/passwd" 会被拒绝
    """
    # 拼接工作目录和输入路径，并解析为绝对路径
    # .resolve() 会解析符号链接并返回绝对路径
    path = (WORKDIR / p).resolve()

    # is_relative_to() 检查 path 是否在 WORKDIR 的子目录中
    # 如果 path 是 "/etc/passwd" 或 "../other_dir" 等外部路径，则拒绝
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")

    return path

def run_bash(command: str) -> str:
    """
    执行shell命令并返回结果
    安全特性：
    - 危险命令黑名单检查：禁止 rm -rf /, sudo, shutdown, reboot 等高危操作
    - 超时保护：命令执行超过120秒会自动终止
    - 输出截断：结果最多返回50000字符，防止内存溢出
    参数：
        command: 要执行的shell命令字符串
    返回：
        命令成功：返回标准输出+标准错误的合并内容（最多50000字符）
        命令失败：返回格式 "Error: command failed with return code X\\n错误信息"
        超时：返回 "Error: Timeout (120s)"
        危险命令：返回 "Error: Dangerous command blocked"
    """
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
        if is_binary_content(out):
            return "Error: 输出包含大量二进制数据，请使用专用工具（如 pymupdf 读取 PDF）而非 strings/cat/hexdump 等原始命令。"
        if r.returncode != 0:
            return f"Error: 命令执行失败，返回码 {r.returncode}\n{smart_truncate(out, 50000)}"
        return smart_truncate(out, 50000) if out else "(command executed successfully, no output)"
    except subprocess.TimeoutExpired:
        # 命令执行超时（超过120秒）
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    """
    读取文件内容
    功能特性：
    - 使用 safe_path 进行安全路径验证
    - 支持行数限制：只读取前limit行，避免大文件撑爆内存
    - 当文件被截断时，显示剩余行数提示
    - 自动截断超长内容至50000字符
    参数：
        path: 要读取的文件路径（相对路径）
        limit: 可选，限制读取的行数。默认None表示读取全部
    返回：
        成功：文件内容字符串（可能被截断）
        失败：格式 "Error: {异常信息}"
    """
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_read_pdf(path: str, max_pages: int = 5, chars_per_page: int = 3000) -> str:
    """
    使用 pymupdf 安全读取 PDF 文件，分页提取文本
    功能特性：
    - 使用 safe_path 进行安全路径验证
    - 分页提取，每页限制字符数
    - 限制最大读取页数
    - 总输出截断至 30000 字符
    参数：
        path: PDF 文件路径（相对路径）
        max_pages: 最大读取页数，默认5页
        chars_per_page: 每页最大字符数，默认3000
    返回：
        成功：PDF 文本内容
        失败：格式 "Error: {异常信息}"
    """
    try:
        fp = safe_path(path)
        if not fp.exists():
            return f"Error: File not found: {path}"
        if not str(fp).lower().endswith('.pdf'):
            return f"Error: Not a PDF file: {path}"
        try:
            import fitz
        except ImportError:
            return "Error: pymupdf 未安装。请运行: python3 -m pip install pymupdf"
        doc = fitz.open(str(fp))
        total_pages = len(doc)
        results = [f"PDF: {path}, 总页数: {total_pages}"]
        read_pages = min(max_pages, total_pages)
        for i in range(read_pages):
            text = doc[i].get_text().strip()
            if text:
                results.append(f"--- 第 {i+1} 页 ---")
                results.append(text[:chars_per_page])
            else:
                results.append(f"--- 第 {i+1} 页 --- (无可提取文本，可能为扫描件)")
        if total_pages > read_pages:
            results.append(f"... (还有 {total_pages - read_pages} 页未读取，可增大 max_pages 参数)")
        doc.close()
        return "\n".join(results)[:30000]

    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """
    写入内容到文件
    功能特性：
    - 使用 safe_path 进行安全路径验证
    - 自动创建父目录：如果父目录不存在会递归创建
    - 覆盖写入：目标文件已存在会被覆盖
    - 返回写入字节数，便于验证
    参数：
        path: 要写入的文件路径（相对路径）
        content: 要写入的内容字符串
    返回：
        成功：格式 "Wrote {字节数} bytes to {路径}"
        失败：格式 "Error: {异常信息}"
    """
    try:
        fp = safe_path(path)
        # 自动创建父目录
        # parents=True: 递归创建所有不存在的父目录
        # exist_ok=True: 如果目录已存在不报错
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 写入内容（覆盖模式）
        fp.write_text(content)
        return f"已写入： {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """
    替换文件中的指定文本
    功能特性：
    - 使用 safe_path 进行安全路径验证
    - 精确替换：只替换第一处匹配（使用 count=1）
    - 先检查再写入：验证old_text存在后才执行替换
    - 原子性保证：读取和写入之间可能存在竞态条件
    参数：
        path: 要编辑的文件路径（相对路径）
        old_text: 要被替换的原文本（必须是完整的连续字符串）
        new_text: 替换后的新文本
    返回：
        成功：格式 "Edited {路径}"
        失败（文本未找到）：格式 "Error: Text not found in {路径}"
        失败（其他）：格式 "Error: {异常信息}"
    """
    try:
        fp = safe_path(path)
        # 读取文件全部内容
        content = fp.read_text()
        # 检查要替换的文本是否存在于文件中
        if old_text not in content:
            return f"Error: Text not found in {path}"
        # 执行替换：只替换第一处匹配
        # replace(old_text, new_text, 1) 中的 1 表示只替换一次
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"已编辑： {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    """
    使用 glob 模块搜索匹配的文件路径
    功能特性：
    - 使用 safe_path 进行安全路径验证
    - 仅返回相对于工作目录的路径
    参数：
        pattern: 要匹配的文件路径模式（支持 glob 模式）
    返回：
        成功：匹配的文件路径列表（每个路径占一行）
        失败：格式 "Error: {异常信息}"
    """
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ============================================================
# 工具处理器映射
# ============================================================

# 建立工具名称到函数的映射
# 当大模型返回工具调用请求时，根据工具名找到对应的函数来执行
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "run_read":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "run_read_pdf":   lambda **kw: run_read_pdf(kw["path"], kw.get("max_pages", 5), kw.get("chars_per_page", 3000)),
    "run_write": lambda **kw: run_write(kw["path"], kw["content"]),
    "run_edit":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "run_glob":  lambda **kw: run_glob(kw["pattern"]),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "list_skills": lambda **kw: SKILL_LOADER.get_descriptions(),
    "todo":        lambda **kw: TODO_MANAGER.update(kw["items"], kw.get("fresh_start", False)),
 
}

# ============================================================
# 工具定义区域，初始化时传给大模型，告诉它有哪些工具可用
# ============================================================


# 基础工具，主要是子智能体可用的工具
BASE_TOOL = [
    {
        "name": "bash","description": "执行 shell 命令。",
        "input_schema": {"type": "object","properties": {"command": {"type": "string"}},"required": ["command"]}
    },
    {
        "name": "run_read","description": "读取文件内容。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},"required": ["path"]}
    },
    {
        "name": "run_read_pdf","description": "使用 pymupdf 安全读取 PDF 文件，分页提取文本。读取 PDF 时必须使用此工具，不要使用 bash 的 strings/cat 等命令。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string","description": "PDF 文件路径"},"max_pages": {"type": "integer","description": "最大读取页数，默认5"},"chars_per_page": {"type": "integer","description": "每页最大字符数，默认3000"}},"required": ["path"]}
    },
    {
        "name": "run_write","description": "将内容写入文件。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "content": {"type": "string"}},"required": ["path", "content"]}
    },
    {
        "name": "run_edit","description": "替换文件中指定的文本内容。",
        "input_schema": {"type": "object","properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},"required": ["path", "old_text", "new_text"]}
    },
    {"name": "run_glob","description": "使用 glob 模式匹配文件路径。",
     "input_schema": {"type": "object","properties": {"pattern": {"type": "string","description": "要匹配的文件路径模式"}}, "required": ["pattern"]}
    },
    {"name": "load_skill", "description": "加载指定名称的专业技能（skill）知识。",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "要加载的专业技能（skill）名称"}}, "required": ["name"]}
    },
    {"name": "list_skills", "description": "获取当前所有可用技能（skill）的名称和简短描述列表，用于了解当前会话支持哪些技能。",
     "input_schema": {"type": "object", "properties": {}}
    },
]

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
