# 我的 Claude Code Agent 学习仓库

> 从 0 到 1 学习 Claude Code Agent Harness 工程，沿着 **v1 → v2** 演进路径，最终目标：**自己动手做出一套完整的智能体**。

---

## 这个仓库在做什么

这是我的个人学习仓库，跟随 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 的脉络，理解并复刻 Claude Code 这样的 Coding Agent 是怎么一层一层搭起来的。

**核心心法**：

> Agency（感知—推理—行动的能力）来自模型训练，不是来自外部代码编排。
> 我们大多数人不训练模型，我们构建 **Harness** —— 那层让模型能在特定领域里干活的脚手架。

所以本仓库学的不是"如何让 LLM 变聪明"，而是：

- 如何把 LLM 包成一个能在 IDE / 终端 / 文件系统里干活的 agent
- 如何给 agent 加工具、加记忆、加规划、加协作
- 如何用尽量少的代码复现 Claude Code 的核心机制

## 两套代码，分清角色

仓库里**有两套代码，定位不同**，别搞混：

| 代码位置 | 性质 | 技术栈 | 角色 |
|----------|------|--------|------|
| [`anthropic/`](./anthropic) | ✅ 已学完的 **v1 教程**（原样保留） | 原生 Anthropic SDK | 学习材料，只读不改 |
| [`anthropic_v2/`](./anthropic_v2) | ✅ s01–s19 已学完的 **v2 教程**（原样保留，s20 待学） | 原生 Anthropic SDK | 学习材料，只读不改 |
| [`agents/agent_full_v2.py`](./agents/agent_full_v2.py) + `agents/*.py` | 🛠️ **我自己用 OpenAI SDK 重写的 v2 智能体** | OpenAI SDK | 自己造的，**这是主入口** |

**原教程的代码全是原生 Anthropic SDK 写的**，没碰 langchain。我自己的那一份起初按 v2 设计、用 langchain 翻译实现，后来为了更深入理解底层交互，**去掉了 langchain 全部依赖，改用原生 OpenAI SDK 直接调用**。现在 `agents/` 根目录用的是 bare `openai` 库——`client.chat.completions.create()` + `response.choices[0].message.tool_calls`，没有任何 langchain 抽象层。

`agents/` 根目录下的模块就是我的 OpenAI SDK 版实现：

- `agent_full_v2.py` —— **v2 智能体引擎**（Agent 类，多实例支持）
- `agent_cli.py` —— **REPL 交互入口**（实例化 Agent + CronScheduler）
- `llm_manage.py` —— 兼容 reasoning 模型的 `OpenAI` 原生客户端封装
- `system_prompt.py` —— System Prompt 运行时组装（静态/动态分段 + cache boundary）
- `session_manage.py` —— 会话管理（新建 / 切换 / 清空 / 持久化）
- `subagent.py` —— 子智能体（隔离上下文的探索者）
- `tools.py` —— 工具注册表（ToolRegistry 类，合并原 tool_base）
- `cron_scheduler.py` —— Cron 定时调度器（s14，CronScheduler 类）
- `skills.py` —— skill loader（按需加载知识）
- `memories.py` —— Tool 驱动持久化记忆（`write_memory` / `forget_memory`，MEMORY.md 索引常驻）
- `todo_manager.py` —— TodoWrite（短清单）
- `task_manager.py` —— 文件式 Task System（`blockedBy` / `blocks` 依赖图）
- `background_manager.py` —— 后台任务（线程池 + 通知队列）
- `context_compact.py` —— 三层上下文压缩（micro / auto / 阈值触发）
- `error_recovery.py` —— 错误恢复状态机（429/503 退避 / max_tokens 升级 / prompt 超长压缩 / 兜底 abort）
- `hooks.py` —— Hook 系统（UserPromptSubmit / PreToolUse / PostToolUse / Stop 四类事件）
- `check_permission.py` —— 权限三闸门（硬拒绝 / 规则匹配 / 用户确认）
- `paths.py` —— 所有路径常量的单一事实来源（WORKDIR / TODO_DIR / TEAM_DIR / MCP_CONFIG 等）
- `worktree.py` —— Worktree 目录隔离（s18）
- `mcp_manager.py` —— **真实 MCP 客户端**（s19，基于官方 `mcp` SDK 替代 mock 版）
- `message_bus.py` —— 队友间 JSONL 邮箱
- `teammate_manager.py` —— 持久队友 + idle 自循环
- `history/v1`、`history/v2` —— 之前写过的 v1 / v2 早期版本归档

> 教程代码归教程，自己写的归自己写。两边不混用，便于回看官方实现 vs 自己实现。

## 核心模式：Agent Loop

不管 v1 还是 v2，整个 agent 都建立在一个最朴素的循环之上：

```python
def agent_loop(messages):
    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        messages.append(msg)

        if not msg.tool_calls:
            return

        results = []
        for tc in msg.tool_calls:
            output = TOOL_HANDLERS[tc.function.name](**json.loads(tc.function.arguments))
            results.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": output,
            })
        messages.extend(results)
```

每一节、每一个机制，都是在这个 loop 外面**加一层**。loop 本身永远不变。

---

## 学习路径

### ✅ v1（已完成） —— 12 节课，一条朴素的主线

代码在 [`anthropic/`](./anthropic) 目录。

| 课程 | 主题 | 一句话心法 |
|------|------|------------|
| s01 | Agent Loop | 一个循环 + Bash = 一个 Agent |
| s02 | Tool Use | 加一个工具 = 加一个 handler |
| s03 | TodoWrite | 没有计划的 agent 会漂移 |
| s04 | Subagent | 大任务拆分，每个子任务一个干净上下文 |
| s05 | Skill Loading | 知识按需加载，不前置塞入 |
| s06 | Context Compact | 上下文会满，要腾出空间 |
| s07 | Task System | 大目标拆成小任务，排序，落盘 |
| s08 | Background Tasks | 慢操作后台跑，agent 继续思考 |
| s09 | Agent Teams | 一个干不完就分给队友 |
| s10 | Team Protocols | 队友之间要有共同通信协议 |
| s11 | Autonomous Agents | 队友自己看任务板领活干 |
| s12 | Worktree Isolation | 各干各的目录，互不干扰 |

最后由 `s_full.py` 把 s01–s11 全部串起来，得到一个完整可跑的 v1 Capstone。

中文学习笔记放在 `anthropic/docs/zh/`。

### ✅ v2（s01–s19 已学完，s20 待学） —— 20 节课，更完整的 Harness

代码在 [`anthropic_v2/`](./anthropic_v2) 目录。

v2 把 v1 的 12 节课扩展到 20 节，引入了 v1 没单拆出来的关键能力 —— **权限系统、Hooks、记忆子系统、错误恢复、Cron 调度、MCP 插件**，并按"动手 → 复杂任务 → 记忆恢复 → 长任务 → 协作 → 扩展装配"的链路重排了顺序，更贴近真实工程。

| 阶段 | 课程 | 新增能力 | 状态 |
|------|------|----------|------|
| **Stage 1 · 让 Agent 动手** | s01 Agent Loop / s02 Tool Use / **s03 Permission** / **s04 Hooks** | 工具 + 权限 + 扩展点 | ✅ 已学完 |
| **Stage 2 · 处理复杂任务** | s05 TodoWrite / s06 Subagent / s08 Context Compact | 计划 + 子任务 + 上下文压缩 | ✅ 已学完 |
| **Stage 3 · 记忆与恢复** | **s09 Memory** / **s10 System Prompt** / **s11 Error Recovery** | 记忆 + 提示词装配 + 错误恢复 | ✅ 已学完 |

> **注意**：s09 教程代码是"事后分析"模式（每轮结束额外调 LLM 抽取记忆），我自己的实现改成了 **Tool 驱动模式** — 模型通过 `write_memory`/`forget_memory` 工具即时写入，更贴合真实 CC 的行为。详见 [`s09_code_cc.py`](anthropic_v2/s09_memory/s09_code_cc.py)。
| **Stage 4 · 跑长任务** | s12 Task System / s13 Background Tasks / **s14 Cron Scheduler** | 任务系统 + 后台 + 定时 | ✅ 已学完 |
| **Stage 5 · 多人协作** | s15 Agent Teams / s16 Team Protocols / s17 Autonomous Agents / s18 Worktree Isolation | 团队 + 协议 + 自组织 + 隔离 | ✅ 已学完 |
| **Stage 6 · 扩展装配** | s07 Skill Loading / **s19 MCP Plugin** / s20 Comprehensive | 技能 + MCP + 集成 | ✅ s19 完成 · s20 待学 |

> **s19 说明**：教程代码是 mock handler 模拟外部 server，我的实现换成**真实 MCP 客户端**——基于官方 `mcp` SDK，单开连接池、动态工具池、真实 LLM 全链路调用验证通过（详见下方"已落地的核心机制 → 18"）；并补充了 `${VAR}` 密钥插值（不落盘）、断线自愈、Resources 只读工具、工具标注 + 破坏性审批门控、远程服务器鉴权。对标主流智能体（Claude Code）MCP，当前仍缺：Server 市场 + 一键安装、Prompts 读取、Sampling、流式输出、工具冲突消解。

v2 的特点是每节都是独立文件夹：`README.md`（中文）+ `README.en.md`（英文）+ `code.py`（可运行）+ `images/`（SVG 图）。

---

## 目录结构

```
learn-claude-code-main/
├── agents/
│   │
│   │  # === 🛠️ 我自己用 OpenAI SDK 重写的 v2 智能体（主入口在这里）===
│   ├── agent_full_v2.py          # ⭐ v2 智能体引擎（Agent 类，多实例支持）
│   ├── agent_cli.py              # ⭐ REPL 交互入口（实例化 Agent + CronScheduler）
│   ├── llm_manage.py             # OpenAI 原生客户端封装（兼容 reasoning 模型）
│   ├── system_prompt.py          # System Prompt 运行时组装（s10）
│   ├── session_manage.py         # 会话管理
│   ├── subagent.py               # 子智能体
│   ├── tools.py                   # 工具注册表（ToolRegistry 类）
│   ├── cron_scheduler.py         # Cron 定时调度器（s14，CronScheduler 类）
│   ├── skills.py                 # skill loader
│   ├── memories.py               # Tool 驱动持久化记忆（s09）
│   ├── todo_manager.py           # TodoWrite
│   ├── task_manager.py           # 文件式任务系统
│   ├── background_manager.py     # 后台任务 + 通知
│   ├── context_compact.py        # 上下文压缩（三层）
│   ├── error_recovery.py         # 错误恢复状态机（s11）
│   ├── hooks.py                  # Hook 系统（s04）
│   ├── check_permission.py       # 权限三闸门（s03，尚未接入主循环）
│   ├── paths.py                  # 所有路径常量单一来源＋ensure_dirs()
│   ├── worktree.py               # Worktree 隔离（s18）
│   ├── mcp_manager.py            # 真实 MCP 客户端（s19，官方 mcp SDK）
│   ├── message_bus.py            # 队友邮箱
│   └── teammate_manager.py       # 队友 + idle 循环
│
├── anthropic/                    # ✅ v1 教程代码：12 节课 + s_full Capstone（只读不动）
│   ├── s01_agent_loop.py
│   ├── s02_tool_use.py
│   ├── ...
│   ├── s12_worktree_task_isolation.py
│   ├── s_full.py                  # v1 完整版
│   └── docs/zh/                   # v1 中文笔记
│
├── anthropic_v2/                 # ✅ v2 教程代码：20 节课 + Web 平台（s01–s19 已学，只读不动）
│   ├── s01_agent_loop/            # 每节一个文件夹
│   ├── s02_tool_use/
│   ├── ...
│   ├── s19_mcp_plugin/            # s19：MCP 外接工具
│   ├── s20_comprehensive/         # v2 终点（待学）
│   ├── web/                       # Next.js 学习平台
│   ├── tests/                     # smoke tests
│   └── README.md                  # v2 教程总入口
│
├── history/                      # 早期版本归档（v1 / v2 旧实现留档）
├── mcp_servers/                  # 本地 MCP 测试服务器（echo_server.py）
├── skills/                        # v1 s05 用的 skill 文件
├── tests/                         # v1 模块单元测试
├── WorkSpace/                     # 用 agent 跑过的实际任务留档
│   ├── task1/                     # DRG 论文综述生成
│   └── task2/                     # 病历结构化提取
├── pyproject.toml                 # Python 3.13 + openai / pydantic
├── uv.lock
└── README.md                      # 你正在读这个
```

---

## 学习目标 & 已完成项

**目标**：跟着 v1 → v2 教程，**用 OpenAI SDK 重新实现**一套完整可跑的 Coding Agent（起初基于 langchain，后全部剥离改用原生 SDK），理解 Harness 每一层的底层交互细节。

**已落地的核心机制**（`agents/` 根目录）：

1. **核心循环** —— `agent_full_v2.py::agent_loop`，基于 `OpenAI.chat.completions.create(...)` + `tools` 参数，多轮 `invoke` 直至 `tool_calls` 为空为止。
2. **工具集** —— `tools.py` 注册 bash / read / write / edit / read_pdf / 任务看板 / 后台 / skill / sub_agent 等。
3. **并发** —— 同一轮内 `parallel=true` 的工具用 `ThreadPoolExecutor` 并行跑，串行的按顺序。
4. **后台任务** —— `background_manager.py` 起线程跑长命令，结果通过通知队列在下轮注入。
5. **任务看板** —— `task_manager.py` 文件式 + 依赖图（`blockedBy` / `blocks`）。
6. **TodoWrite** —— `todo_manager.py` 短清单 + nag 提醒；todo 文件与 session 绑定（`.todo/session_<N>.todo.json` ↔ `.chathistory/session_<N>.jsonl`），会话恢复/切换时自动注入 `<system-reminder>` 提醒模型继续未完成任务。详见下方"踩坑记录 → Todo 与 session 绑定 + 崩溃恢复"。
7. **Skill 加载** —— `skills.py` 按需把 SKILL.md 注入 `tool_result`。
8. **上下文压缩** —— `context_compact.py` 三层策略：micro 清理旧 tool_result / auto LLM 总结 / 阈值触发。
9. **子智能体** —— `subagent.py` 隔离上下文，按 `allowed_tools` 控制权限。
10. **会话管理** —— `session_manage.py` 支持新建 / 切换 / 清空 / 持久化 jsonl。
11. **队友协作** —— `message_bus.py` JSONL 邮箱 + `teammate_manager.py` 持久队友 + idle 循环。
12. **Reasoning 模型兼容** —— `llm_manage.py` 包装原生 `OpenAI` 客户端，兼容 reasoning 模型，保留 `reasoning_content` 多轮回传。
13. **记忆系统** —— s09 教程拆分为 Tool 驱动模式：`write_memory`/`forget_memory` 工具由模型自主调用，MEMORY.md 索引常驻 system prompt，零额外 LLM 开销。详见 [`s09_code_cc.py`](anthropic_v2/s09_memory/s09_code_cc.py)。
14. **System Prompt 组装** —— s10 把硬编码 `SYSTEM` 拆成 section（工具规范 / 记忆索引 / 技能描述 / 工作目录），运行时按状态拼接，并用 `STATIC_BOUNDARY` 标记静态/动态边界以命中 prompt cache。见 [`system_prompt.py`](agents/system_prompt.py)。
15. **错误恢复** —— s11 状态机封装：429/503 内层指数退避重试、连续 503 切 `FALLBACK_MODEL`、`max_tokens` 截断两阶段恢复（升级到 64K → 续写 prompt）、prompt 超长触发 reactive compact、不可恢复错误 abort。见 [`error_recovery.py`](agents/error_recovery.py)。
16. **Hooks 系统** —— s04 四类事件（`UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop`），`PreToolUse` 回调返回非 None 视为阻断信号。默认 hooks 已注册；权限闸门 `check_permission.py` 接入后将在此拦截危险工具调用。见 [`hooks.py`](agents/hooks.py)。
17. **Cron 定时调度** —— s14 三层解耦架构（调度线程 → 任务队列 → 队列处理器），通过 `schedule_cron` / `list_crons` / `cancel_cron` 工具由大模型对话创建定时任务。与教程的关键差异见下方 [s14 与教程的差异](#s14-与教程的差异)。见 [`cron_scheduler.py`](agents/cron_scheduler.py)。
18. **MCP 插件（真实客户端）** —— s19 从 mock 升级为真实 MCP：`mcp_manager.py` 基于官方 `mcp` SDK，`MCPServerSession` 每服务器一个大后台线程跑独立 asyncio 环 + 常驻 `ClientSession`，`MCPManager` 统一管理（配置发现 → 连接 → 动态工具池 → 委托调用 → 热加载）。工具池以 `mcp__{server}__{tool}` 命名空间动态追加、省 token；企业级加固包括 `${VAR}` 密钥插值（不落盘）、断线重连自愈、Resources 只读工具、工具标注（readOnly/destructive/openWorld）与破坏性审批门控、远程 stdio/streamable-http/sse 传输鉴权。名称规范化修复了连字符问题（统一折叠为 `_`）。见 [`mcp_manager.py`](agents/mcp_manager.py)。

**接下来要做的**：

- 跟 v2 教程收尾：**s20 综合**（把 s01–s19 的机制合回一个完整 harness）
- s19 补齐主流 MCP 能力：Server 市场 + 一键安装、Prompts 读取、Sampling、流式输出、工具冲突消解
- 把权限闸门 `check_permission.py` 真正接进主循环（目前 `agent_full_v2.py` 顶部导入被注释，三闸门尚未在 `agent_loop` 里启用）
- 把 subagent / teammate 的事件接进 **Hooks**（PreToolUse / PostToolUse 插桩），便于做轨迹采集
- 把任务系统迁移到 **State Graph 编排**，验证"图编排"和"while 循环"两种范式都能覆盖同一套机制

---

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
cp .env.example .env   # 配置 OPENAI_MODEL_ID / OPENAI_API_KEY / OPENAI_BASE_URL

# 2. ⭐ 跑我自己用 OpenAI SDK 重写的 v2 智能体（主入口）
python agents/agent_cli.py

# 3. 看教程代码（只读，对照参考）
#    v1（已学完，12 节课）
python anthropic/s01_agent_loop.py
python anthropic/s_full.py            # v1 完整版

#    v2（进行中，20 节课）
python anthropic_v2/s01_agent_loop/code.py
python anthropic_v2/s20_comprehensive/code.py   # v2 教程终点

# 4. v2 自带的 Web 学习平台
cd anthropic_v2/web && npm install && npm run dev
# → http://localhost:3000
```

**REPL 命令**（`agent_cli.py` 内置）：

| 命令 | 作用 |
|------|------|
| 直接输入 | 跟 agent 对话 |
| `/tasks` | 列出任务看板 |
| `/compact` | 手动压缩上下文 |
| `/newsession` | 开新会话 |
| `/switchsession <id>` | 切到历史会话 |
| `/clearsession` | 清空当前会话 |
| `/q` / `/exit` | 退出 |

---

## 我的学习笔记

- **v1 笔记**：[`anthropic/docs/zh/`](./anthropic/docs/zh)
- **v2 笔记**：跟代码走，每节的 `sXX_xxx/README.md` 就是当节的中文讲解（s01–s19 已学完，见 [`anthropic_v2/README.md`](anthropic_v2/README.md)）
- **自己的 OpenAI SDK 版**：[`agents/agent_full_v2.py`](./agents/agent_full_v2.py) 及其同级模块 —— 教程思路的 OpenAI SDK 重新实现（最初基于 langchain，后全部剥离）
- **MCP 真实实现笔记**：[`agents/mcp_manager.py`](./agents/mcp_manager.py) —— s19 的 mock → 真实客户端升级（连接池 / 动态工具池 / 热加载 / 鉴权 / 破坏性门控）
- **早期版本归档**：[`history/`](./history) —— v1 / v2 旧实现留档
- **实验留档**：[`WorkSpace/`](./WorkSpace) —— 用 agent 跑过的实际任务

## 踩坑记录

> 实际跑起来遇到的 bug 与解法，挑值得记的写这里。

### 会话孤儿消息（Orphan Tool Calls）

- **症状**：加载历史会话后回传 OpenAI，触发 `BadRequestError: An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'`。
- **根因**：进程在 `AIMessage` 落盘之后、`ToolMessage` 落盘之前被中断（崩溃 / Ctrl+C），导致 jsonl 里出现了"带 `tool_calls` 但没人接话"的孤立消息。
- **解法**：[`agents/session_manage.py::_sanitize_orphan_tool_calls`](./agents/session_manage.py#L187) 在加载时扫描，对每个带 `tool_calls` 的 `AIMessage` 校验紧随其后的 `ToolMessage` 是否覆盖了全部 `tool_call_id`，缺失则把该 `AIMessage` 以及后续错位的 `ToolMessage` 一起丢弃。
- **为什么删而不是补**：被中断的 tool 实际执行结果未知，编造 `ToolMessage` content 等于喂给模型假数据，反而污染后续推理；删除是唯一安全选择。
- **教训**：上策是从源头消灭——在 `_save_message` 层调整落盘顺序（先 `fsync` tool_result 再 commit ai_message，或 `os.replace` 原子写），让孤儿消息根本不产生。

### Todo 与 session 绑定 + 崩溃恢复

相比 v1 教程的 `TodoManager`（`anthropic/s03_todo_write.py` 里的纯内存版），v2 我做了**两处鲁棒性增强**：

- todo **落盘** —— 写到 `WorkSpace/task1/.todo/session_<N>.todo.json`，`TodoManager.__init__` 立刻 `load()`，进程崩溃后内存里的 todo 仍是上次状态
- todo **与 session 绑定** —— 不再是全局单文件，而是和 `.chathistory/session_<N>.jsonl` 用同一个 N 串起来，切换 session 时 `set_todo_manager(N)` 重新指向对应文件，不同会话的 todo 完全隔离
- 启动恢复 **reminder** —— 仅落盘还不够，system prompt 不会渲染 todo，模型不主动调工具就察觉不到。所以 `agent_full_v2.py::_inject_todo_reminder` 在 `init_session` 和 `/switchsession N` 之后，若 `has_open_items()` 为真就注入一条 `role: user` 的 `<system-reminder>` 消息，把当前 todo 列表贴进去，模型下一轮必看到

**关键坑位**（修复前）：

- `TODO_FILE = TODO_DIR / "todo.json"` 是全局路径，session 切换时 todo 不跟着切，session_1 写的 todo 在 session_2 也能看到
- 进程崩溃后 todo 确实能 reload 进内存（`TodoManager.__init__` → `self.load()`），但模型**意识不到**有未完成项——因为 system prompt 只写"如何使用 todo 工具"的规范，不展示当前 todo 列表，模型如果不主动调 `todo` 或 `/tasks`，完全感受不到
- `/clearsession` 只清 chat history，**忘了**清 todo，会话清空后旧 todo 还在

**修改落点**：

- [`agents/tool_base.py`](agents/tool_base.py) —— 删 `TODO_FILE` 常量，加 `todo_file_for_session(session_num)` 工厂 + `TODO_DIR.mkdir`
- [`agents/tools.py`](agents/tools.py) —— 删全局 `TODO_MANAGER`，加 `_TODO_MANAGER_HOLDER` + `set_todo_manager` / `get_todo_manager`；`TOOL_HANDLERS["todo"]` 改为 `get_todo_manager().update(...)`
- [`agents/agent_full_v2.py`](agents/agent_full_v2.py) —— 新增 `_inject_todo_reminder`；在 `init_session` / `/switchsession N` 后调用 `set_todo_manager` + reminder；`/clearsession` 同步 `update([], fresh_start=False)` 重置 todo；`agent_loop` 里 `TODO_MANAGER.xxx` 改 `get_todo_manager().xxx`

**reminder 注入示例**：

```text
<system-reminder>本次会话检测到上次有未完成的待办事项：
Todos (2/5 completed):
  [x] #1: 从19篇论文中提取DRG成本管控相关指标
  [x] #2: 设计指标体系框架（维度-二级指标-三级指标）
  [>] #3: 撰写指标体系研究文档（含权重方法与评分标准）
  [ ] #4: 校验数据
  [ ] #5: 检查交付
请在继续之前确认是否继续执行；如果任务已不再相关，请用 todo 工具把对应项标记为 completed，
或开启新计划（fresh_start=true 整体替换）。</system-reminder>
```

**为什么 reminder 落盘** —— 注入 `history_messages` 的同时也写进 `session_file`，下次启动 reload 仍可见；否则下次重启模型又"失忆"。

---

## s14 与教程的差异

s14 教程采用「消费者线程抢 `agent_lock` → 注入共享 messages → 主 agent_loop 消费」的模式，cron 任务的执行结果混在主会话中，不产生独立会话记录。

我的实现改为**每触发一次 cron 任务创建独立 Agent 实例**，核心差异：

| 维度 | 教程 s14 | 我的实现 |
|------|---------|---------|
| 队列消费 | 抢 `agent_lock`，注入共享 messages | 创建独立 `Agent(session_prefix="cron_")` 实例 |
| 会话文件 | 无独立会话，混在主会话中 | 每个触发任务一个 `cron_{N}.jsonl`，存储在 `.chathistory/` |
| 与主 REPL 关系 | 共享 `agent_lock`，互斥 | 完全隔离，独立 Agent 实例互不干扰 |
| 会话可见性 | 混在主会话中，难以追溯 | 执行完成后 `cron_{N}.jsonl` 可作为独立会话查看 |
| 持久化 | `.scheduled_tasks.json` | `.scheduler/scheduled_tasks.json`（子目录隔离） |

**架构**（保留教程三层设计）：

```
调度线程（每秒轮询）→ cron_queue → 队列处理器
                                    ↓
                          Agent(session_prefix="cron_")
                          → init_session(resume=False)
                          → run_turn("[Scheduled] {prompt}")
                          → cron_{N}.jsonl 落盘
```

**关键模块**：
- [`cron_scheduler.py`](agents/cron_scheduler.py) — `CronScheduler` 类（调度线程 + 队列处理器 + 工具包装）
- [`tools.py`](agents/tools.py) — `schedule_cron` / `list_crons` / `cancel_cron` 工具定义
- [`agent_cli.py`](agents/agent_cli.py) — 启动时创建 `CronScheduler` 实例并注入
- [`session_manage.py`](agents/session_manage.py) — `session_prefix` 参数支持 `cron_` 前缀独立编号

---

## 致谢

原始仓库与全部内容来自 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)，本仓库仅作为个人学习笔记与代码实验使用。

---

> **Bash is all you need. Real agents are all the universe needs.**
>
> **这不是"抄源码"，是"抓住关键设计，自己造一遍"。**
