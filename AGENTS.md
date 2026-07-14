# AGENTS.md — 给 AI Coding Agent 的指引

## 项目本质

个人学习仓库，从 0 到 1 理解 Coding Agent 的 Harness 构建。沿着 v1 → v2 教程路径，用 **langchain 重写**验证所有设计模式。

**核心心法**：Agency（感知-推理-行动）来自模型训练，Harness 是让模型在特定领域干活的脚手架。

## 代码布局

| 路径 | 性质 | 操作 |
|------|------|------|
| `agents/*.py`（根目录） | 🛠️ 我用 langchain 重写的 v2 实现 | **主入口，读写修改** |
| `agents/anthropic/` | ✅ v1 教程代码（Anthropic SDK） | 只读不写 |
| `agents/anthropic_v2/` | 🚧 v2 教程代码（Anthropic SDK） | 只读不写 |

## 主入口

`agents/agent_full_v2.py` — 基于 `ChatOpenAI.bind_tools()` 的 REPL 循环。

同级模块：`tools.py` / `subagent.py` / `skills.py` / `todo_manager.py` / `task_manager.py` / `background_manager.py` / `compact.py` / `session_manage.py` / `message_bus.py` / `teammate_manager.py` / `llm_manage.py`

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
- 🚧 v2 20 课进行中（已到 MCP 插件接入）
- ⏳ 待做：Hooks 插桩、LangGraph 迁移

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

- 别删 `agents/anthropic/` 和 `agents/anthropic_v2/` 下的教程代码——它们是学习对照材料，只读不动。
- 当我问"看教程"时，去 `agents/anthropic/`（v1）或 `agents/anthropic_v2/`（v2）找对应的课程代码。
- 我自己的实现若有 bug，优先修 `agents/agent_full_v2.py` 及其同级模块。
