# 主 Agent 类化改造计划：agent_full_v2.py → Agent 类 + agent_cli.py 新入口 + 移除全局单例

## 0. 当前进度快照（2026-08-17 复核）

> 以下按实际文件状态核对，区分「已完成」与「待执行」，后续执行只做待办项。

| # | 改动项 | 状态 | 说明 |
|---|--------|------|------|
| 4.1 | tools.py 移除全局单例 | ✅ 已完成 | docstring 已更新，文件底部无 `TOOL_REGISTRY = ToolRegistry()` |
| 4.2 | agent_full_v2.py 类化 | ✅ 已完成 | 已是 `Agent` 类（`__init__` / `init_session` / `run_turn` / `new_session` / `switch_session` / `clear_session` / `agent_loop` 等齐全），底部保留 `__main__` 延迟调 `agent_cli.main` |
| 4.3 | agent_cli.py 新增入口 | ⏳ 待执行 | 文件尚不存在 |
| 4.4 | system_prompt.py 注入 tools | ⏳ 待执行 | 仍 `from tools import TOOL_REGISTRY`（L10）/ `memory=TOOL_REGISTRY.memory`（L30）/ `TOOL_REGISTRY.main_agent_tools`（L86）→ **当前 ImportError，模块不可导入** |
| 4.5 | teammate_manager.py 注入 tools | ⏳ 待执行 | 仍 `from tools import TOOL_REGISTRY`（L29）/ `TOOL_REGISTRY.run_bash/run_read/run_write/run_edit`（L306-318）→ **当前 ImportError** |
| 4.6 | AGENTS.md 更新 | ⏳ 待执行 | 需补「不再全局单例」与 agent_cli 主入口说明 |
| 4.7 | project_memory 更新 | ⏳ 待执行 | 记录 Agent 类化 + 移除单例 + 新入口 |

> ⚠️ 由于 4.4 / 4.5 未完成，`agent_full_v2.py` 现在 import 即失败（`system_prompt` → `tools.TOOL_REGISTRY` 不存在）。**执行阶段先从 4.4 / 4.5 修起，保证可导入，再补 4.3 入口。**

## 1. 背景与目标

当前 `agent_full_v2.py` 是「模块级全局状态 + 函数式 REPL」：`SYSTEM`、`llm_client`、`hook_system`、`Skills`、`subagent_runner`、`background_manager`、`recovery` 全部是模块级变量，`tools.py` 底部还有全局单例 `TOOL_REGISTRY = ToolRegistry()`。

这带来两个问题：

1. **只能存在一个 Agent**。s14 定时任务课程要求「每个定时任务单独启动一个会话」；未来的 TUI 要支持多会话（多实例）。当前一个进程只能有一份全局状态，定时任务触发时会和正在进行的对话抢同一份 `history_messages` / `todo_manager` / `background_manager`，产生运行时冲突。
2. **全局单例让依赖关系不可控**。`system_prompt.py`、`teammate_manager.py` 直接 import 全局 `TOOL_REGISTRY`，无法注入不同实例。

### 本次改造目标

1. 移除 `tools.py` 底部的全局单例 `TOOL_REGISTRY`。
2. 把 `agent_full_v2.py` 改造成 **`Agent` 类**：所有依赖、会话状态全部收敛为实例属性，不保留任何模块级可变单例。
3. 新增 `agent_cli.py`：作为新的交互入口，实例化 `Agent` 并驱动 REPL 对话（input 循环 + 斜杠命令 + readline 配置）。
4. 为 s14 定时任务（每任务独立会话）和未来 TUI 多实例预留清晰接缝：`init_session()` + `run_turn(query)`。

> 本次只做「类化改造 + 新入口」，**不实现** s14 cron 调度器本身，也不实现 TUI。这两块是后续工作，但 Agent 类会为它们提供多实例能力。

---

## 2. 现状分析（探索结论）

### 2.1 agent_full_v2.py 当前结构

- **模块级全局**：`MODEL`/`FALLBACK_MODEL`（env）、`SYSTEM`（SystemPromptBuilder）、`llm_client`（LLMClient().llm）、`hook_system`（HookSystem + register_default_hooks）、`Skills`（SkillLoader）、`subagent_runner`（SubAgent）、`background_manager`（BackgroundManager）、`TOOL_REGISTRY.set_background_manager(...)`、`recovery`（ErrorRecovery）、`MAX_AGENT_ITERATIONS`。
- **函数**：`_make_executor`、`_execute_tool_call`、`_inject_todo_reminder`、`agent_loop(history_messages, session_file, session_manager)`、`main()`（REPL）。
- **REPL 命令**：`/help`、`/q`、`/newsession`、`/switchsession N`、`/clearsession`、`/tasks`、`/compact`、`/skills`。
- `readline` 中文输入配置在模块顶部（属 CLI 职责，应移到 agent_cli.py）。
- 全仓库没有任何 `from agent_full_v2 import ...`（只有 hooks.py / system_prompt.py 里的注释提到文件名），可以放心重构。

### 2.2 引用 `TOOL_REGISTRY` 的模块（agents/ 根目录，共 3 个）

| 模块 | 用法 | 改动 |
|------|------|------|
| `agent_full_v2.py` | `main_agent_tools` / `handlers` / `execute` / `set_todo_manager` / `get_todo_manager` / `set_background_manager` | 改为 `self.tools.xxx` |
| `system_prompt.py` | 构造默认参数 `memory=TOOL_REGISTRY.memory`；`_get_tools()` 用 `TOOL_REGISTRY.main_agent_tools` | 构造函数注入 `tools`，`_get_tools()` 用 `self.tools` |
| `teammate_manager.py` | `_exec()` 里 `TOOL_REGISTRY.run_bash/run_read/run_write/run_edit` | 构造函数注入 `tools`（默认 `ToolRegistry()` 实例），`_exec()` 用 `self.tools.xxx` |

其余模块（`subagent` / `hooks` / `error_recovery` / `session_manage` / `background_manager` / `todo_manager` / `task_manager` / `memories` / `message_bus` / `skills` / `llm_manage`）**均为类、无全局可变单例**，不需要改。

### 2.3 可复用的类（均已实例友好）

`ToolRegistry`、`SessionManager`、`SubAgent(base_tools, handlers, hook_system)`、`BackgroundManager`、`ErrorRecovery(primary_model, fallback_model)`、`SystemPromptBuilder`、`SkillLoader`、`HookSystem`、`LLMClient` 全部是普通类，可直接作实例属性。

### 2.4 s14 教学代码的局限（为什么需要多实例）

s14 教学版（`anthropic_v2/s14_cron_scheduler/s14_code.py`）是**单 agent**：cron 任务触发后注入**同一份** `session_history` 的 `messages`，用 `agent_lock` 串行化。用户的目标是「每个定时任务单独启动一个会话」，即每个 cron 触发时新建一个 `Agent`（或新会话）独立执行——这必须建立在 Agent 可多实例化的基础上。

---

## 3. 目标架构

```
agent_cli.py（新入口，REPL）          ── 交互驱动
   └── Agent（agent_full_v2.py 类化）  ── 引擎：持有一切状态
         ├── self.tools          ToolRegistry（实例，非全局单例）
         ├── self.skills         SkillLoader
         ├── self.system_prompt  SystemPromptBuilder（注入 tools/memory/skills）
         ├── self.llm_client     LLMClient().llm
         ├── self.hook_system    HookSystem
         ├── self.background_manager  BackgroundManager（挂到 self.tools 的 holder）
         ├── self.subagent_runner     SubAgent(self.tools.base_tools, self.tools.handlers, self.hook_system)
         ├── self.recovery       ErrorRecovery
         ├── self.session_manager     SessionManager（init_session 时创建）
         └── 会话状态 self.session_num / self.session_file / self.history_messages

未来：s14 cron 每触发一次 → Agent().init_session(resume=False) + Agent().run_turn("[Scheduled] ...")
未来：TUI 每会话 → 一个独立 Agent 实例
```

每个 `Agent` 实例拥有**独立的** ToolRegistry / todo holder / background holder / hook_system / subagent_runner / session 状态，实例之间互不干扰。

---

## 4. 具体改动（分文件）

### 4.1 `agents/tools.py` — 移除全局单例

- 删除文件末尾 `TOOL_REGISTRY = ToolRegistry()` 这一行（及上方注释）。
- 更新模块 docstring：说明「不再提供全局单例，由调用方（Agent）实例化，保证多实例隔离」。
- `ToolRegistry` 类本身**不改**（已实例友好；holder 模式保留，供每个 Agent 各自 `set_background_manager` / `set_todo_manager`）。

### 4.2 `agents/agent_full_v2.py` — 核心：函数式 → `Agent` 类

保留文件名与 REPL 入口的向后兼容（见下「__main__ 兼容」），但主体改为类。

**模块顶部**：
- 删除 `readline` 配置块（移到 agent_cli.py）。
- 保留 `load_dotenv(override=True)`、`MODEL`/`FALLBACK_MODEL`（env 读取）。
- 导入改为：`from tools import ToolRegistry`（不再有 TOOL_REGISTRY 单例）。

**类定义**：

```python
class Agent:
    """主智能体引擎：持有全部依赖与会话状态，支持多实例。"""

    MAX_AGENT_ITERATIONS = 100

    def __init__(self, *, skills=None, memory=None, tools=None, ...):
        # env
        self.model = os.environ.get("OPENAI_MODEL_ID", "")
        self.fallback_model = os.environ.get("FALLBACK_MODEL_ID", "")

        # 依赖（默认惰性构造，允许外部注入 → 多实例可共享/自定义）
        self.skills = skills if skills is not None else SkillLoader(SKILLS_DIR)
        self.tools = tools if tools is not None else ToolRegistry(skills=self.skills)
        self.memory = memory if memory is not None else self.tools.memory

        self.hook_system = HookSystem()
        self.hook_system.register_default_hooks()

        self.background_manager = BackgroundManager()
        self.tools.set_background_manager(self.background_manager)  # 实例级 holder，非全局

        self.subagent_runner = SubAgent(
            self.tools.base_tools, self.tools.handlers, self.hook_system)

        self.system_prompt = SystemPromptBuilder(
            workdir=WORKDIR,
            skills=self.skills,
            memory=self.memory,
            tools=self.tools,
            chat_history_dir=CHAT_HISTORY_DIR,
        )

        self.llm_client = LLMClient().llm
        self.recovery = ErrorRecovery(
            primary_model=self.model, fallback_model=self.fallback_model)

        # 会话状态（init_session 填充）
        self.session_manager = None
        self.session_num = None
        self.session_file = None
        self.history_messages = []
```

> 说明：`LLMClient()` / `HookSystem()` / `ErrorRecovery()` 每次构造都是新实例，天然多实例安全。`self.memory` 走 `self.tools.memory`（项目级磁盘记忆，多实例共享同一 `.memory/` 是预期行为）。

**原函数 → 实例方法**（`TOOL_REGISTRY` → `self.tools`，`background_manager` → `self.background_manager`，`subagent_runner` → `self.subagent_runner`，`hook_system` → `self.hook_system`，`recovery` → `self.recovery`，`llm_client` → `self.llm_client`，`session_manager`/`session_file`/`history_messages` → `self.*`）：

- `_make_executor(self, tool_name, tool_args)` → 用 `self.subagent_runner.spawn_subagent(...)` / `self.tools.execute(...)`
- `_execute_tool_call(self, tool_call)` → 用 `self.background_manager`
- `_inject_todo_reminder(self)` → 用 `self.tools.get_todo_manager()`（不再传参，从 self 取）
- `agent_loop(self)` → 无参（内部读 `self.history_messages` / `self.session_file` / `self.session_manager`）；`TOOL_REGISTRY.main_agent_tools` → `self.tools.main_agent_tools`

**新增公共方法**（为 CLI / 未来 cron / TUI 提供接缝）：

```python
def init_session(self, resume=True) -> int:
    """创建/恢复会话：构建 SessionManager → 初始化 → 绑定 todo → 注入 reminder。"""
    if self.session_manager is None:
        self.session_manager = SessionManager(
            CHAT_HISTORY_DIR, self.system_prompt.build_system_prompt())
    if resume:
        self.session_num, self.session_file, self.history_messages = \
            self.session_manager.init_session()
    else:
        self.session_num, self.session_file, self.history_messages = \
            self.session_manager.create_initialized_session()
    self.tools.set_todo_manager(self.session_num)
    self._inject_todo_reminder()
    return self.session_num

def run_turn(self, user_query: str) -> str:
    """跑一轮非交互对话（CLI / cron / TUI 共用）。返回最终 assistant 回复文本。"""
    self.hook_system.trigger("UserPromptSubmit", user_query)
    self.history_messages.append({"role": "user", "content": user_query})
    self.session_manager.append_message_to_session(self.session_file, self.history_messages[-1])
    self.session_manager.maybe_compact_context(self.history_messages, self.session_file)
    self.agent_loop()
    # 取最后一条 assistant 内容返回（agent_loop 内部仍会打印 thinking/本轮回复，保持现状 UX）
    last = self.history_messages[-1].get("content", "")
    if isinstance(last, list):
        return "".join(b.get("text", "") for b in last if isinstance(b, dict))
    return str(last)
```

会话管理方法（映射原 REPL 斜杠命令，供 agent_cli 调用）：

- `new_session(self) -> tuple[int, str]`：`create_initialized_session()` + `set_todo_manager`，返回 `(num, 提示语)`
- `switch_session(self, num: int)`：`switch_session(num)` + `set_todo_manager` + `_inject_todo_reminder`（沿用原 try/except 与报错信息）
- `clear_session(self) -> int`：`clear_session` + `get_todo_manager().update([], fresh_start=False)` + 重新 load
- `show_tasks(self) -> str`：`get_todo_manager().render()`
- `compact(self)`：`session_manager.maybe_compact_context(..., manual=True)`
- `show_skills(self) -> str`：`self.skills.list_skills()`
- `context_label(self) -> str`：`session_manager.format_context_label(history_messages)`

**删除**：原模块级 `main()` REPL 函数（移到 agent_cli.py）。文件底部保留向后兼容入口：

```python
if __name__ == "__main__":
    from agent_cli import main as cli_main
    cli_main()
```

> 用函数内延迟 import，避免 agent_cli.py 与 agent_full_v2.py 循环导入。

### 4.3 `agents/agent_cli.py` — 新增交互入口

```python
#!/usr/bin/env python3
"""agent_cli.py - 主智能体命令行交互入口

实例化 Agent 并驱动 REPL：input 循环 + 斜杠命令 + readline 中文输入配置。
"""
import sys
from dotenv import load_dotenv
from agent_full_v2 import Agent

# readline 中文输入配置（从原 agent_full_v2.py 顶部迁移过来）
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass


def main():
    load_dotenv(override=True)
    agent = Agent()
    agent.init_session()  # resume 最近会话或新建

    while True:
        try:
            label = agent.context_label()
            query = input(f"\033[36m[session_{agent.session_num} ({label})] >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        cmd = query.strip().lower()
        if cmd == "/help":
            print("可用命令: /q /newsession /switchsession N /clearsession /tasks /compact /skills")
            continue
        if cmd in ("/q", "/exit", ""):
            break
        if cmd == "/newsession":
            num, _ = agent.new_session()
            print(f"\033[33m已创建新会话: session_{num}.jsonl\033[0m")
            continue
        if cmd.startswith("/switchsession "):
            # 调用 agent.switch_session(num)，沿用原 try/except 报错
            ...
            continue
        if cmd == "/clearsession":
            deleted = agent.clear_session()
            print(f"\033[33m已清空当前会话，删除了 {deleted} 条历史消息\033[0m")
            continue
        if cmd == "/tasks":
            print(agent.show_tasks())
            continue
        if cmd == "/compact":
            agent.compact()
            continue
        if cmd == "/skills":
            print(f"当前可用技能:\n{agent.show_skills()}")
            continue

        # 普通用户输入 → 跑一轮
        reply = agent.run_turn(query)
        print(reply)
        print()


if __name__ == "__main__":
    main()
```

> `/switchsession N` 的具体实现直接搬原 `main()` 的 try/except 逻辑，调用 `agent.switch_session(n)`。

### 4.4 `agents/system_prompt.py` — 注入 tools，去掉全局单例

- 删除 `from tools import TOOL_REGISTRY`。
- 构造函数改为：

```python
def __init__(
    self,
    workdir: Path = WORKDIR,
    skills: SkillLoader = None,
    memory=None,
    tools=None,          # 新增：ToolRegistry 实例（Agent 传入）
    chat_history_dir: Path = CHAT_HISTORY_DIR,
    workspace_instruction_files: tuple[str, ...] = None,
):
    self.tools = tools
    self.skills = skills if skills else SkillLoader(SKILLS_DIR)
    self.memory = memory if memory is not None else (self.tools.memory if self.tools else None)
    ...
```

- `_get_tools()` 中 `TOOL_REGISTRY.main_agent_tools` → `self.tools.main_agent_tools`；若 `self.tools is None` 抛 `RuntimeError("SystemPromptBuilder 需要传入 ToolRegistry 实例")`。

### 4.5 `agents/teammate_manager.py` — 注入 tools

- 删除 `from tools import TOOL_REGISTRY`，改 `from tools import ToolRegistry`。
- `__init__` 增加参数 `tools: ToolRegistry = None`，`self.tools = tools or ToolRegistry()`（实例级默认构造，非全局单例，符合「不在 tools.py 里搞全局单例」的要求）。
- `_exec()` 里 `TOOL_REGISTRY.run_bash(...)` → `self.tools.run_bash(...)`（run_read / run_write / run_edit 同理）。

### 4.6 `AGENTS.md` — 更新工具规则说明

- 「工具统一走 ToolRegistry」条目改为：`tools.py` 的 `ToolRegistry` 类**不再提供全局单例**，由 `Agent`（`agent_full_v2.py`）实例化并持有；`main_agent_tools` / `handlers` / `execute` / todo-background holder 均通过 `self.tools.xxx` 调用。
- 新增「主入口」说明：`agent_cli.py` 为新的交互入口（`python agent_cli.py`），内部实例化 `Agent` 驱动 REPL；`agent_full_v2.py` 为引擎（Agent 类）。

### 4.7 项目记忆（project_memory.md）

- 记录本次「Agent 类化 + 移除全局单例 + 新增 agent_cli.py」的结构变更、调用约定，以及为 s14/TUI 预留的 `init_session` / `run_turn` 接缝。

---

## 5. 设计决策与假设

1. **类放哪**：按用户要求，`Agent` 类放在 `agent_full_v2.py`（保留文件名），新交互入口 `agent_cli.py`。
2. **向后兼容**：`agent_full_v2.py` 底部保留 `if __name__ == "__main__"` 延迟调用 `agent_cli.main()`，保证 `python agent_full_v2.py` 仍可用；新推荐入口是 `python agent_cli.py`。
3. **teammate_manager 用 `ToolRegistry()` 默认实例**：这是构造器内的实例级默认值，不是模块级全局单例，不违背「不在 tools.py 搞全局单例」的要求；且该模块暂未被主链路引用，独立可用。
4. **会话/记忆语义不变**：多实例共享同一磁盘（`.chathistory/`、`.memory/`、`.tasks/`），这是预期（项目级数据）；每个 Agent 的 todo/background/session 状态在内存中彼此隔离。
5. **打印行为保持现状**：`agent_loop` 内保留 thinking/回复打印；`run_turn` 额外返回最终回复文本，由 CLI 打印（与原 `main()` 在 loop 后再打印一次的行为一致）。
6. **本次不做** s14 cron 调度器与 TUI，仅通过 `init_session(resume=False)` + `run_turn(query)` 预留接缝。

---

## 6. 验证步骤

1. `py_compile`：`agent_full_v2.py`、`agent_cli.py`、`tools.py`、`system_prompt.py`、`teammate_manager.py`。
2. Grep 确认 agents/ 根目录无 `TOOL_REGISTRY` 残留引用（`history/` 备份除外）。
3. 冒烟：`python agent_cli.py` 能启动并显示 session 提示符；输入一句简单 query（如「你好」）能收到回复；`/skills`、`/tasks`、`/newsession`、`/switchsession` 等命令正常。
4. 冒烟：`python agent_full_v2.py` 仍能进入同一 REPL（向后兼容入口）。
5. 多实例隔离冒烟：在脚本里连建两个 `Agent()` 实例，分别 `init_session(resume=False)`，确认各自 session_num / todo / background 互不干扰（可临时用一段测试代码，不落库）。

---

## 7. 后续衔接（本次不实现，仅说明）

- **s14 定时任务**：新增 `cron_manager.py` 时，每个 cron 触发 → `agent = Agent(); agent.init_session(resume=False); agent.run_turn(f"[Scheduled] {prompt}")`，天然独立会话，不再与 REPL 抢状态。
- **TUI 多会话**：每个会话面板持有一个 `Agent` 实例，共享磁盘数据、隔离内存状态。
