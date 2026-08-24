# 团队智能体改为「/teams 门控进入」方案

## Summary

将 S17 团队智能体从「默认对主智能体暴露」改为「门控进入」：对话默认走**子智能体（sub_agent）分发**模式；只有当用户输入 `/teams` 时才进入**粘性团队模式**，此时才把团队工具（spawn/send_message/check_inbox/request_shutdown/request_plan/review_plan）喂给 LLM。团队模式下用 `/subagent` 退出，回到默认子智能体模式。

这样做的收益：
- **省 token**：默认模式不再把 6 个团队工具 schema 喂给每个 API 调用，显著减负。
- **满足语义**：只有显式 `/teams` 才让模型有机会拉起代价高昂的队友线程，避免模型自行误触发团队协作。
- **效率可控**：团队编排需跨多轮（spawn→发消息→查收件箱），粘性模式保证流程不被打断。

## Current State Analysis（现状）

- [tools.py](file:///Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main/agents/tools.py)：`ToolRegistry.tools`（L561）内联包含 6 个团队工具定义（L713-777，`spawn_teammate/send_message/check_inbox/request_shutdown/request_plan/review_plan`）；`main_agent_tools`（L781）= `tools` + `sub_agent`（大段描述，L789）。
- [agent_full_v2.py](file:///Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main/agents/agent_full_v2.py)：主循环每次调用 LLM 时固定喂 `tools=self.tools.main_agent_tools`（L368），即**每轮都暴露团队工具**。
- [agent_cli.py](file:///Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main/agents/agent_cli.py)：REPL 入口，`run_turn(query)` 直接跑；当前无 `/teams` 命令。
- [system_prompt.py](file:///Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main/agents/system_prompt.py)：`_get_tools()` 用 `main_agent_tools` 枚举"可用工具"（L90），含团队工具，与默认模式不一致。
- [teammate_manager.py](file:///Users/peijiping/Documents/Codes/AiCodes/learn-claude-code-main/agents/teammate_manager.py)：团队逻辑已收敛为类，`Agent.__init__` 中始终实例化。**保持不变**。

## Proposed Changes

### 1. `agents/tools.py` — 拆分工具定义，新增默认工具集

**目标**：提供「默认（无团队工具）+ sub_agent」与「全部工具 + sub_agent」两套定义。

- 新增模块级集合：
  ```python
  TEAM_TOOL_NAMES = {
      "spawn_teammate", "send_message", "check_inbox",
      "request_shutdown", "request_plan", "review_plan",
  }
  ```
- 新增私有方法 `_team_tool_defs()`：把 L713-777 的 6 个团队工具 dict 文字量搬进来，返回 `list`。`tools` 属性末尾改为 `*self._team_tool_defs()`（对外行为不变）。
- 新增私有方法 `_sub_agent_tool_def()`：把 L787-804 的 `sub_agent` dict 文字量搬进来，返回单个 dict。
- 新增属性 `default_agent_tools`（加 `_default_agent_tools_cache` 缓存）：
  ```python
  @property
  def default_agent_tools(self):
      """默认工具集 = 全部工具剔除团队工具 + sub_agent（sub_agent 分发模式）。"""
      if self._default_agent_tools_cache is None:
          non_team = [t for t in self.tools
                      if t["function"]["name"] not in TEAM_TOOL_NAMES]
          self._default_agent_tools_cache = [*non_team, self._sub_agent_tool_def()]
      return self._default_agent_tools_cache
  ```
- `main_agent_tools` 保持 `tools` + `self._sub_agent_tool_def()`（团队模式用，供 `/teams` 时喂给 LLM）。

### 2. `agents/agent_full_v2.py` — 按模式选择工具集

- `__init__` 新增 `self.team_mode = False`（粘性标志，默认子智能体模式）。
- `agent_loop`（L368）`tools=` 改为：
  ```python
  tools=self.tools.main_agent_tools if self.team_mode else self.tools.default_agent_tools,
  ```

### 3. `agents/agent_cli.py` — `/teams` / `/subagent` 命令

- `/help` 文案加上两者。
- `/teams`：
  - 置 `agent.team_mode = True`。
  - 若携带指令（`query[7:].strip()` 非空）→ 用剩余文本作为本轮 `run_turn` 输入。
  - 若不带指令 → 仅切换模式，打印提示语（如"已进入团队模式，可用 spawn_teammate 等工具编排队友"），continue 不跑本轮。
- `/subagent`：置 `agent.team_mode = False`，打印提示语，continue。
- 提示符（L54）当 `agent.team_mode` 为真时追加模式标记，例如 `[session_1 (标签|teams)] >>`。

### 4. `agents/system_prompt.py` — 同步可用工具枚举（小改）

- `_get_tools()` 中 `tool_lines` 由 `main_agent_tools` 改为 `default_agent_tools`（默认模式下不再列出团队工具名）。
- 在 section 末尾追加一段静态说明（模式无关）：团队工具仅在 `/teams` 进入团队模式后可用，默认用 `sub_agent` 分发子任务。
  - 说明：system prompt 在 `init_session` 时缓存，粘性模式下跨轮切换会出现缓存提示与实际工具集的时差；故采用**模式无关的静态提示**（不枚举团队工具），团队工具 schema 在团队模式时由 API 的 `tools` 参数直接下发，模型可自然发现，无需写进 prompt。最小改动、避免缓存失效问题。

## Assumptions & Decisions

- **粘性模式**（用户已确认）：`/teams` 进入后持续到 `/subagent` 才退出，保证跨轮编排不中断。
- **退出命令**：`/subagent`（用户已确认）。
- **团队模式下仍保留 `sub_agent` 工具**：`main_agent_tools` 含 sub_agent，队友可继续并行/后台分发普通子任务。
- **不改**：`agents/teammate_manager.py`、`message_bus.py`、tools.py 的 handlers 映射（handlers 在不在注册列表不影响门控，门控只在喂给 LLM 的定义层面生效）。
- **不加新依赖**、不加新文件，全部改动落在既有 4 个文件。

## Verification

1. 语法自检：`python -m py_compile agents/tools.py agents/agent_full_v2.py agents/agent_cli.py agents/system_prompt.py`。
2. 手动启动 `python agents/agent_cli.py`：
   - 启动后提示符**无** `teams` 标记；在对话中让模型用 `sub_agent` 分发（确认默认模式工具集可用）。
   - 输入 `/teams`（不带指令），提示符出现 `teams` 标记，`/skills` 类查询正常。
   - 输入 `/teams 让队友 A 负责 X...`，确认本轮 LLM 收到的工具含 `spawn_teammate/send_message/...`（观察打印的工具调用或仅看行为）。
   - 输入 `/subagent`，提示符 `teams` 标记消失，回到默认模式。
3. 团队多轮流程冒烟：`/teams` 进入 → `spawn_teammate` → 隔轮 `check_inbox` → `/subagent` 退出，确认流程连贯。