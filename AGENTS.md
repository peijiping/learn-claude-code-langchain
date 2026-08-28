# AGENTS.md — 给 AI Coding Agent 的指引

## 项目本质

个人学习仓库，从 0 到 1 理解 Coding Agent 的 Harness 构建。沿着 v1 → v2 → v2.1 教程路径，用 **OpenAI SDK 重写**验证所有设计模式（原用 langchain，为更贴近底层逻辑已切换）。

**核心心法**：Agency（感知-推理-行动）来自模型训练，Harness 是让模型在特定领域干活的脚手架。

## 代码布局

| 路径 | 性质 | 操作 |
|------|------|------|
| `agents/*.py`（根目录） | 🛠️ 我用 OpenAI SDK 重写的 v2 实现 | **主入口，读写修改** |
| `anthropic/` | ✅ v1 教程代码（Anthropic SDK） | 只读不写 |
| `anthropic_v2/` | ✅ v2 教程代码（Anthropic SDK） | 只读不写 |
| `anthropic_v2.1/` | 🚧 v2.1 教程代码（教程更新版，后续将把 s15-s17 内容更新进 agents/ 实现） | 只读不写 |
| `history/`、`mcp_servers/` | 早期版本归档 / 本地 MCP 测试服务器 | 只读不写 |

## 主入口

- `agents/agent_cli.py` — **唯一入口**（`python agents/agent_cli.py`），实例化 `Agent` 驱动 REPL（input 循环 + 斜杠命令 + readline 配置）。
- `agents/agent_full_v2.py` — 主智能体引擎，`Agent` 类（基于 OpenAI SDK `chat.completions.create()` + function calling），持有全部依赖与会话状态，支持多实例隔离。**不作为启动入口**（已移除 `__main__`），由 agent_cli.py 导入。

为 s14 定时任务（每任务独立会话）与未来 TUI 多会话预留接缝：

```python
agent = Agent()
agent.init_session(resume=False)   # 新会话（cron 用）
agent.run_turn("[Scheduled] ...")  # 非交互单轮
```

同级模块：`tools.py` / `agent_cli.py` / `subagent.py` / `skills.py` / `todo_manager.py` / `task_manager.py` / `background_manager.py` / `compact.py` / `session_manage.py` / `message_bus.py` / `teammate_manager.py` / `llm_manage.py`

## 核心模式

```
agent_loop(messages):
    while stop_reason != "tool_use":
        response = model.invoke(messages, tools)
        处理 tool_calls → 注入 tool_result → 继续循环
```

所有机制（子智能体、技能加载、任务系统、后台、队友协作、上下文压缩、MCP）都围绕这个循环叠加。

## 学习进度

- ✅ v1 12 课已学完
- ✅ v2 20 课已学完
- 🚧 v2.1 教程更新版进行中（待把 s15-s17 内容更新进 agents/ 实现）


## 注意事项

### `.claudeignore` 文件规则

`.claudeignore` 中列出的文件/目录**不要读取**，包括但不限于：

- `.venv/`、`node_modules/`、`__pycache__/`、`*.pyc` — 虚拟环境与字节码
- `.pytest_cache/`、`.ruff_cache/`、`.mypy_cache/` — 测试/检查缓存
- `dist/`、`build/`、`*.egg-info/` — 构建产物
- `.env`、`.env*.local`、`*.pem`、`*.key` — 敏感配置/密钥
- `.DS_Store`、`Thumbs.db` — 系统文件
- `.idea/`、`.vscode/`、`.reasonix/` — 编辑器/工具配置
- `*.log`、`*.whl`、`*.so` — 日志/大文件
- `WorkSpace/` — 实验留档（非项目源码）
- `skills/` — 技能文件（非代码实现）
- `analysis/`、`analysis_progress.md` — 分析产物

这些是临时文件、缓存、敏感信息或系统文件，**不是项目开发需要的代码**。读取它们会浪费上下文、暴露敏感信息、干扰推理。

### 其他

- 别删 `anthropic/`、`anthropic_v2/` 和 `anthropic_v2.1/` 下的教程代码——它们是学习对照材料，只读不动。
- 当我问"看教程"时，去 `anthropic/`（v1）、`anthropic_v2/`(v2）或 `anthropic_v2.1/`（v2.1）找对应的课程代码。
- 我自己的实现若有 bug，优先修 `agents/agent_full_v2.py` 及其同级模块。
- **路径定义统一管理**：所有工作目录相关常量（`WORKDIR`、`TODO_DIR`、`TEAM_DIR`、`INBOX_DIR`、`CHAT_HISTORY_DIR`、`TRANSCRIPT_DIRNAME`、`TOOL_RESULTS_DIRNAME` 等）一律在 `agents/paths.py` 顶部集中定义，其他模块通过 `from paths import ...` 引用，禁止在业务模块内重复声明。
- **工具统一走 ToolRegistry（实例，无全局单例）**：`agents/tools.py` 的 `ToolRegistry` 类统一管理所有工具（原 `tool_base.py` 已合并删除），由 `Agent`（`agent_full_v2.py`）实例化并持有为 `self.tools`。**不再提供全局单例 `TOOL_REGISTRY`**，多实例各持一份。工具定义用 `self.tools.main_agent_tools`（子智能体用 `self.tools.base_tools`）、处理器用 `self.tools.handlers`、执行用 `self.tools.execute(name, **args)`；基础工具方法（`run_bash` / `run_read` / `run_write` / `run_edit` / `run_glob` / `safe_path`）与 todo/background holder（`set_todo_manager` / `get_todo_manager` / `set_background_manager`）均通过该实例调用。其他模块（如 `teammate_manager` / `system_prompt`）需要工具时，由调用方注入 `ToolRegistry` 实例（构造参数），禁止再 import 被删除的 `tool_base` 或全局单例。
- **可调参数走 `.env`**：纯路径之外的运行时可调参数（如 `context_compact.py` 中的 `CONTEXT_LIMIT_CHARS`、`SNIP_MAX_MESSAGES`、`SUMMARY_TRIGGER_RATIO`、`MAX_REACTIVE_RETRIES` 等）一律声明在 `.env`（默认值同时给到 `.env.example` 的注释示例），代码中通过 `os.environ.get(KEY) or default`（整数用 `int(...)`、浮点用 `float(...)`，与 `llm_manage.py` 风格保持一致）内联读取；新增/修改这类参数时，必须同步更新 `.env` 与 `.env.example`。
