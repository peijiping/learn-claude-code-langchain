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
| [`agents/anthropic/`](./agents/anthropic) | ✅ 已学完的 **v1 教程**（原样保留） | 原生 Anthropic SDK | 学习材料，只读不改 |
| [`agents/anthropic_v2/`](./agents/anthropic_v2) | 🚧 进行中的 **v2 教程**（原样保留） | 原生 Anthropic SDK | 学习材料，只读不改 |
| [`agents/agent_full_v2.py`](./agents/agent_full_v2.py) + `agents/*.py` | 🛠️ **我自己用 langchain 重写的 v2 智能体** | langchain-core + langchain-openai | 自己造的，**这是主入口** |

**原教程的代码全是原生 Anthropic SDK 写的**，没碰 langchain。我自己的那一份是按 v2 设计、**用 langchain 重新翻译/实现**了一遍，验证"用 LangChain 这套抽象也跑得通"。

`agents/` 根目录下的模块就是我的 langchain 版实现：

- `agent_full_v2.py` —— **v2 智能体主入口**（REPL）
- `llm_manage.py` —— 兼容 reasoning 模型的 `ChatOpenAI` 封装
- `session_manage.py` —— 会话管理（新建 / 切换 / 清空 / 持久化）
- `subagent.py` —— 子智能体（隔离上下文的探索者）
- `tools.py` / `tools_base.py` —— 工具注册表 & 父级工具集
- `skills.py` —— skill loader（按需加载知识）
- `todo_manager.py` —— TodoWrite（短清单）
- `task_manager.py` —— 文件式 Task System（`blockedBy` / `blocks` 依赖图）
- `background_manager.py` —— 后台任务（线程池 + 通知队列）
- `compact.py` —— 三层上下文压缩（micro / auto / 阈值触发）
- `message_bus.py` —— 队友间 JSONL 邮箱
- `teammate_manager.py` —— 持久队友 + idle 自循环
- `history/v1`、`history/v2` —— 之前写过的 v1 / v2 早期版本归档

> 教程代码归教程，自己写的归自己写。两边不混用，便于回看官方实现 vs 自己实现。

## 核心模式：Agent Loop

不管 v1 还是 v2，整个 agent 都建立在一个最朴素的循环之上：

```python
def agent_loop(messages):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM,
            messages=messages, tools=TOOLS,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = TOOL_HANDLERS[block.name](**block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
        messages.append({"role": "user", "content": results})
```

每一节、每一个机制，都是在这个 loop 外面**加一层**。loop 本身永远不变。

---

## 学习路径

### ✅ v1（已完成） —— 12 节课，一条朴素的主线

代码在 [`agents/anthropic/`](./agents/anthropic) 目录。

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

中文学习笔记放在 `agents/anthropic/docs/zh/`。

### 🚧 v2（s01–s08 已学完，s09+ 待学） —— 20 节课，更完整的 Harness

代码在 [`agents/anthropic_v2/`](./agents/anthropic_v2) 目录。

v2 把 v1 的 12 节课扩展到 20 节，引入了 v1 没单拆出来的关键能力 —— **权限系统、Hooks、记忆子系统、错误恢复、Cron 调度、MCP 插件**，并按"动手 → 复杂任务 → 记忆恢复 → 长任务 → 协作 → 扩展装配"的链路重排了顺序，更贴近真实工程。

| 阶段 | 课程 | 新增能力 |
|------|------|----------|
| **Stage 1 · 让 Agent 动手** | s01 Agent Loop / s02 Tool Use / **s03 Permission** / **s04 Hooks** | 工具 + 权限 + 扩展点 |
| **Stage 2 · 处理复杂任务** | s05 TodoWrite / s06 Subagent / s08 Context Compact | 计划 + 子任务 + 上下文压缩 |
| **Stage 3 · 记忆与恢复** | **s09 Memory** / **s10 System Prompt** / **s11 Error Recovery** | 记忆 + 提示词装配 + 错误恢复 |

> **注意**：s09 教程代码是"事后分析"模式（每轮结束额外调 LLM 抽取记忆），我自己的实现改成了 **Tool 驱动模式** — 模型通过 `write_memory`/`forget_memory` 工具即时写入，更贴合真实 CC 的行为。详见 [`s09_code_cc.py`](agents/anthropic_v2/s09_memory/s09_code_cc.py)。
| **Stage 4 · 跑长任务** | s12 Task System / s13 Background Tasks / **s14 Cron Scheduler** | 任务系统 + 后台 + 定时 |
| **Stage 5 · 多人协作** | s15 Agent Teams / s16 Team Protocols / s17 Autonomous Agents / s18 Worktree Isolation | 团队 + 协议 + 自组织 + 隔离 |
| **Stage 6 · 扩展装配** | s07 Skill Loading / **s19 MCP Plugin** / **s20 Comprehensive** | 技能 + MCP + 集成 |

v2 的特点是每节都是独立文件夹：`README.md`（中文）+ `README.en.md`（英文）+ `code.py`（可运行）+ `images/`（SVG 图）。

---

## 目录结构

```
learn-claude-code-main/
├── agents/
│   │
│   │  # === 🛠️ 我自己用 langchain 重写的 v2 智能体（主入口在这里）===
│   ├── agent_full_v2.py          # ⭐ v2 智能体主入口（REPL）
│   ├── llm_manage.py             # ChatOpenAI 封装（兼容 reasoning 模型）
│   ├── session_manage.py         # 会话管理
│   ├── subagent.py               # 子智能体
│   ├── tools.py / tools_base.py  # 工具注册表
│   ├── skills.py                 # skill loader
│   ├── todo_manager.py           # TodoWrite
│   ├── task_manager.py           # 文件式任务系统
│   ├── background_manager.py     # 后台任务 + 通知
│   ├── compact.py                # 上下文压缩
│   ├── message_bus.py            # 队友邮箱
│   ├── teammate_manager.py       # 队友 + idle 循环
│   ├── history/                  # 早期版本归档（v1 / v2）
│   │
│   │  # === 📚 教程原样保留（只读不动）===
│   ├── anthropic/                # ✅ v1 教程代码：12 节课 + s_full Capstone
│   │   ├── s01_agent_loop.py
│   │   ├── s02_tool_use.py
│   │   ├── ...
│   │   ├── s12_worktree_task_isolation.py
│   │   ├── s_full.py              # v1 完整版
│   │   └── docs/zh/               # v1 中文笔记
│   │
│   └── anthropic_v2/             # 🚧 v2 教程代码：20 节课 + Web 平台
│       ├── s01_agent_loop/        # 每节一个文件夹
│       ├── s02_tool_use/
│       ├── ...
│       ├── s20_comprehensive/     # v2 终点
│       ├── web/                   # Next.js 学习平台
│       ├── tests/                 # smoke tests
│       └── README.md              # v2 教程总入口
│
├── skills/                        # v1 s05 用的 skill 文件
├── tests/                         # v1 模块单元测试
├── WorkSpace/                     # 用 agent 跑过的实际任务留档
│   ├── task1/                     # DRG 论文综述生成
│   └── task2/                     # 病历结构化提取
├── pyproject.toml                 # Python 3.13 + langchain / langgraph
├── uv.lock
└── README.md                      # 你正在读这个
```

---

## 学习目标 & 已完成项

**目标**：跟着 v1 → v2 教程，用 **langchain 重写**一套完整可跑的 Coding Agent，验证教程里的所有设计模式在 langchain 这套抽象下同样能跑得通。

**已落地的核心机制**（`agents/` 根目录）：

1. **核心循环** —— `agent_full_v2.py::agent_loop`，基于 `ChatOpenAI.bind_tools(...)`，多轮 `invoke` 直至无 `tool_calls` 为止。
2. **工具集** —— `tools.py` 注册 bash / read / write / edit / read_pdf / 任务看板 / 后台 / skill / sub_agent 等。
3. **并发** —— 同一轮内 `parallel=true` 的工具用 `ThreadPoolExecutor` 并行跑，串行的按顺序。
4. **后台任务** —— `background_manager.py` 起线程跑长命令，结果通过通知队列在下轮注入。
5. **任务看板** —— `task_manager.py` 文件式 + 依赖图（`blockedBy` / `blocks`）。
6. **TodoWrite** —— `todo_manager.py` 短清单 + nag 提醒。
7. **Skill 加载** —— `skills.py` 按需把 SKILL.md 注入 `tool_result`。
8. **上下文压缩** —— `compact.py` 三层策略：micro 清理旧 tool_result / auto LLM 总结 / 阈值触发。
9. **子智能体** —— `subagent.py` 隔离上下文，按 `allowed_tools` 控制权限。
10. **会话管理** —— `session_manage.py` 支持新建 / 切换 / 清空 / 持久化 jsonl。
11. **队友协作** —— `message_bus.py` JSONL 邮箱 + `teammate_manager.py` 持久队友 + idle 循环。
12. **Reasoning 模型兼容** —— `llm_manage.py` 包装 `ChatOpenAI`，保留 `reasoning_content` 多轮回传。
13. **记忆系统** —— s09 教程拆分为 Tool 驱动模式：`write_memory`/`forget_memory` 工具由模型自主调用，MEMORY.md 索引常驻 system prompt，零额外 LLM 开销。详见 [`s09_code_cc.py`](agents/anthropic_v2/s09_memory/s09_code_cc.py)。

**接下来要做的**：

- v2 教程里还剩 **s19 MCP 插件** 没接入自己的实现
- 把 subagent / teammate 的事件接进 **Hooks**（PreToolUse / PostToolUse 插桩），便于做轨迹采集
- 把任务系统迁移到 **LangGraph state graph**，验证"图编排"和"while 循环"两种范式都能覆盖同一套机制

---

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
cp .env.example .env   # 配置 OPENAI_MODEL_ID / OPENAI_API_KEY / OPENAI_BASE_URL

# 2. ⭐ 跑我自己用 langchain 重写的 v2 智能体（主入口）
python agents/agent_full_v2.py

# 3. 看教程代码（只读，对照参考）
#    v1（已学完，12 节课）
python agents/anthropic/s01_agent_loop.py
python agents/anthropic/s_full.py            # v1 完整版

#    v2（进行中，20 节课）
python agents/anthropic_v2/s01_agent_loop/code.py
python agents/anthropic_v2/s20_comprehensive/code.py   # v2 教程终点

# 4. v2 自带的 Web 学习平台
cd agents/anthropic_v2/web && npm install && npm run dev
# → http://localhost:3000
```

**REPL 命令**（`agent_full_v2.py` 内置）：

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

- **v1 笔记**：[`agents/anthropic/docs/zh/`](./agents/anthropic/docs/zh)
- **v2 笔记**：跟代码走，每节的 `sXX_xxx/README.md` 就是当节的中文讲解
- **自己的 langchain 版**：[`agents/agent_full_v2.py`](./agents/agent_full_v2.py) 及其同级模块 —— 教程的 langchain 翻译实现
- **早期版本归档**：[`agents/history/`](./agents/history) —— v1 / v2 旧实现留档
- **实验留档**：[`WorkSpace/`](./WorkSpace) —— 用 agent 跑过的实际任务

## 踩坑记录

> 实际跑起来遇到的 bug 与解法，挑值得记的写这里。

### 会话孤儿消息（Orphan Tool Calls）

- **症状**：加载历史会话后回传 OpenAI，触发 `BadRequestError: An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'`。
- **根因**：进程在 `AIMessage` 落盘之后、`ToolMessage` 落盘之前被中断（崩溃 / Ctrl+C），导致 jsonl 里出现了"带 `tool_calls` 但没人接话"的孤立消息。
- **解法**：[`agents/session_manage.py::_sanitize_orphan_tool_calls`](./agents/session_manage.py#L187) 在加载时扫描，对每个带 `tool_calls` 的 `AIMessage` 校验紧随其后的 `ToolMessage` 是否覆盖了全部 `tool_call_id`，缺失则把该 `AIMessage` 以及后续错位的 `ToolMessage` 一起丢弃。
- **为什么删而不是补**：被中断的 tool 实际执行结果未知，编造 `ToolMessage` content 等于喂给模型假数据，反而污染后续推理；删除是唯一安全选择。
- **教训**：上策是从源头消灭——在 `_save_message` 层调整落盘顺序（先 `fsync` tool_result 再 commit ai_message，或 `os.replace` 原子写），让孤儿消息根本不产生。

---

## 致谢

原始仓库与全部内容来自 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)，本仓库仅作为个人学习笔记与代码实验使用。

---

> **Bash is all you need. Real agents are all the universe needs.**
>
> **这不是"抄源码"，是"抓住关键设计，自己造一遍"。**
