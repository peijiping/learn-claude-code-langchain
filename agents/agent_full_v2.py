#!/usr/bin/env python3
"""
agent_full_v2.py - 主智能体引擎（Agent 类）

从函数式 REPL 重构为类形式：所有依赖与会话状态收敛为实例属性，
不再使用模块级可变单例（tools.py 的全局 TOOL_REGISTRY 已移除）。

- 每个 Agent 实例拥有独立的 ToolRegistry / todo holder / background holder /
  hook_system / subagent_runner / session 状态，支持多实例隔离。
- 交互入口：`python agents/agent_cli.py`（实例化 Agent 驱动 REPL）。

为 s14 定时任务（每任务独立会话）与未来 TUI 多会话预留的接缝：
  agent = Agent()
  agent.init_session(resume=False)   # 新会话
  agent.run_turn("[Scheduled] ...") # 非交互单轮
"""

import json
import os

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from session_manage import SessionManager
from subagent import SubAgent
from background_manager import BackgroundManager
from teammate_manager import TeammateManager
from paths import WORKDIR, CHAT_HISTORY_DIR, SKILLS_DIR, TEAM_DIR
from tools import ToolRegistry
from skills import SkillLoader
from llm_manage import LLMClient
from system_prompt import SystemPromptBuilder
from error_recovery import ErrorRecovery, RecoveryAction
from hooks import HookSystem
from utils import truncate_chars


# 加载环境变量
load_dotenv(override=True)


class Agent:
    """
    主智能体引擎：持有全部依赖与会话状态，支持多实例隔离。

    每个实例拥有独立的：
    - tools（ToolRegistry：基础工具方法 / definitions / handlers / execute）
    - skills / memory / hook_system / background_manager / subagent_runner / recovery
    - session 状态（session_num / session_file / history_messages / todo holder）

    交互入口 agent_cli.py 实例化本类并驱动 REPL；
    未来 cron（每任务独立会话）与 TUI（每会话一实例）直接复用。
    """

    MAX_AGENT_ITERATIONS = 100

    def __init__(
        self,
        *,
        skills: SkillLoader | None = None,
        memory=None,
        tools: ToolRegistry | None = None,
        session_prefix: str = "session_",
        cron_scheduler=None,
        silent: bool = False,
    ):
        # ── 模型参数（从 .env 读取） ──
        self.model = os.environ.get("OPENAI_MODEL_ID", "")
        self.fallback_model = os.environ.get("FALLBACK_MODEL_ID", "")

        # ── 会话文件名前缀：默认 "session_"；cron 调度器传入 "cron_" ──
        self.session_prefix = session_prefix

        # ── silent 模式：抑制所有打印输出（cron 定时任务用） ──
        self.silent = silent

        # ── 依赖（默认惰性构造；允许外部注入，多实例可共享/自定义） ──
        self.skills = skills if skills is not None else SkillLoader(SKILLS_DIR)
        self.tools = tools if tools is not None else ToolRegistry(
            skills=self.skills, cron_scheduler=cron_scheduler,
        )
        self.memory = memory if memory is not None else self.tools.memory

        # 钩子实例：每实例独立，主循环与子智能体共用
        self.hook_system = HookSystem(silent=self.silent)
        self.hook_system.register_default_hooks()

        # 后台任务管理器：挂到本实例 tools 的 holder 上（实例级，非全局）
        self.background_manager = BackgroundManager()
        self.tools.set_background_manager(self.background_manager)

        # 团队成员管理器（s17）：挂到本实例 tools 的 holder 上（注入本实例 tools，实例级）
        self.teammate_manager = TeammateManager(TEAM_DIR, tools=self.tools)
        self.tools.set_teammate_manager(self.teammate_manager)

        # 团队模式标志（粘性）：默认 False（子智能体分发模式）；
        # 由 CLI 的 /teams 置 True、/subagent 置 False，决定 agent_loop 喂哪套工具集。
        self.team_mode = False

        # 子智能体：复用本实例的工具集/处理器/hooks
        self.subagent_runner = SubAgent(
            self.tools.base_tools, self.tools.handlers, self.hook_system
        )

        # 系统 prompt：注入本实例的 skills / memory / tools
        self.system_prompt = SystemPromptBuilder(
            workdir=WORKDIR,
            skills=self.skills,
            memory=self.memory,
            tools=self.tools,
            chat_history_dir=CHAT_HISTORY_DIR,
        )

        # LLM 客户端 + S11 错误恢复控制器
        self.llm_client = LLMClient().llm
        self.recovery = ErrorRecovery(
            primary_model=self.model, fallback_model=self.fallback_model
        )

        # ── 会话状态（由 init_session / new_session / switch_session 填充） ──
        self.session_manager: SessionManager | None = None
        self.session_num: int | None = None
        self.session_file: Path | None = None
        self.history_messages: list = []

    # ── silent 打印辅助 ──────────────────────────────────────────
    def _print(self, *args, **kwargs):
        """silent 模式下抑制所有 print 输出（cron 定时任务用）。"""
        if not self.silent:
            print(*args, **kwargs)

    # ═══════════════════════════════════════════════════════════
    #  会话生命周期（CLI / cron / TUI 共用接缝）
    # ═══════════════════════════════════════════════════════════

    def init_session(self, resume: bool = True) -> int:
        """
        创建/恢复会话：构建 SessionManager → 初始化 → 绑定 todo → 注入 reminder。

        resume=True：加载最近一次会话；resume=False：新建独立会话（cron 用）。
        """
        if self.session_manager is None:
            self.session_manager = SessionManager(
                CHAT_HISTORY_DIR, self.system_prompt.build_system_prompt(),
                session_prefix=self.session_prefix,
            )
        if resume:
            self.session_num, self.session_file, self.history_messages = \
                self.session_manager.init_session()
        else:
            self.session_num, self.session_file, self.history_messages = \
                self.session_manager.create_initialized_session()
        # todo 与 session 绑定：每次切会话都要重新指向对应的 todo 文件
        self.tools.set_todo_manager(self.session_num)
        # task 与 session 绑定：任务板限定在本会话作用域（"session_N"/"cron_N"）
        self.tools.task_manager.set_scope(f"{self.session_prefix}{self.session_num}")
        self._inject_todo_reminder()
        return self.session_num

    def run_turn(self, user_query: str) -> str:
        """
        跑一轮非交互对话（CLI / cron / TUI 共用）。返回最终回复文本。

        agent_loop 内部仍会打印 thinking / 本轮回复（保持现状 UX）；
        本方法额外返回历史最后一条消息的文本，供调用方打印。
        """
        self.hook_system.trigger("UserPromptSubmit", user_query)
        self.history_messages.append({"role": "user", "content": user_query})
        self.session_manager.append_message_to_session(
            self.session_file, self.history_messages[-1]
        )
        self.session_manager.maybe_compact_context(
            self.history_messages, self.session_file
        )
        self.agent_loop()
        last = self.history_messages[-1].get("content", "")
        if isinstance(last, list):
            return "".join(b.get("text", "") for b in last if isinstance(b, dict))
        return str(last)

    def new_session(self) -> tuple[int, str]:
        """创建新会话并绑定 todo，返回 (新会话编号, 提示语)。"""
        self.session_num, self.session_file, self.history_messages = \
            self.session_manager.create_initialized_session()
        # 新会话的 todo 文件尚不存在，set_todo_manager 会建出空列表；reminder 不会注入
        self.tools.set_todo_manager(self.session_num)
        self.tools.task_manager.set_scope(f"{self.session_prefix}{self.session_num}")
        return self.session_num, f"已创建新会话: session_{self.session_num}.jsonl"

    def switch_session(self, target_num: int) -> tuple[int, int]:
        """
        切换到指定会话，绑定对应 todo 并注入 reminder。
        返回 (会话编号, 消息数)；会话不存在时抛 FileNotFoundError。
        """
        self.session_num, self.session_file, self.history_messages = \
            self.session_manager.switch_session(target_num)
        self.tools.set_todo_manager(self.session_num)
        self.tools.task_manager.set_scope(f"{self.session_prefix}{self.session_num}")
        self._inject_todo_reminder()
        return self.session_num, len(self.history_messages)

    def clear_session(self) -> int:
        """清空当前会话（todo 同步重置），返回被删除的消息数。"""
        deleted_count = self.session_manager.clear_session(self.session_file)
        # todo 与 chat history 同生共死：清空 chat 的同时把当前 session 的 todo 也重置为空
        self.tools.get_todo_manager().update([], fresh_start=False)
        self.history_messages = self.session_manager.load_session_history(
            self.session_file
        )
        return deleted_count

    def show_tasks(self) -> str:
        """返回当前会话待办看板文本。"""
        return self.tools.get_todo_manager().render()

    def compact(self) -> None:
        """手动触发上下文压缩（/compact）。"""
        self.session_manager.maybe_compact_context(
            self.history_messages, self.session_file, manual=True
        )

    def show_skills(self) -> str:
        """返回可用技能列表文本。"""
        return self.skills.list_skills()

    def context_label(self) -> str:
        """格式化当前上下文窗口显示信息（用于 REPL 提示符）。"""
        return self.session_manager.format_context_label(self.history_messages)

    # ═══════════════════════════════════════════════════════════
    #  工具执行辅助
    # ═══════════════════════════════════════════════════════════

    def _inject_todo_reminder(self) -> None:
        """
        会话恢复/切换时，若当前 session 有未完成的 todo，注入一条 reminder
        让模型意识到"上次有活没干完"。

        reminder 写在 user query 之前、system / 旧 history 之后，
        模型下一轮必能直接看到。reminder 同时落盘 session_file，
        保证下次启动 reload 仍可见。
        """
        mgr = self.tools.get_todo_manager()
        if not mgr.has_open_items():
            return
        reminder = (
            "<system-reminder>本次会话检测到上次有未完成的待办事项：\n"
            f"{mgr.render()}\n"
            "请在继续之前确认是否继续执行；如果任务已不再相关，请用 todo 工具把对应项标记为 completed，"
            "或开启新计划（fresh_start=true 整体替换）。</system-reminder>"
        )
        self.history_messages.append({"role": "user", "content": reminder})
        self.session_manager.append_message_to_session(
            self.session_file, self.history_messages[-1]
        )

    def _make_executor(self, tool_name: str, tool_args: dict):
        """
        把"执行一个工具调用"包成无参闭包，供 background_manager 在后台线程调用。

        tool_name / tool_args 是 _make_executor 的形参（独立作用域、每次调用绑一次），
        所以 lambda 直接闭包捕获即可，无须 def 嵌套，也不会出现 for 循环闭包共享
        变量导致所有闭包都引用最后一次迭代值的经典坑。
        """
        if tool_name == "sub_agent":
            return lambda: self.subagent_runner.spawn_subagent(
                tool_args.get("prompt", ""),
                allowed_tools=tool_args.get("allowed_tools"),
            )
        elif tool_name in self.tools.handlers:
            return lambda: self.tools.execute(tool_name, **tool_args)
        else:
            return lambda: f"Error: Unknown tool {tool_name}"

    def _execute_tool_call(self, tool_call) -> dict:
        """
        执行单个工具调用（sub_agent 或普通工具），同步或后台均可。

        同步路径：直接调用 executor()，返回真实输出。
        后台路径：分发给 background_manager 守护线程，立即返回占位
        "[Background task bg_xxxx started] Command: ..." 字符串作为
        本轮的 tool_result。占位 result 必须立即写进 history，
        否则下一轮 LLM 会因 tool_call_id 缺失而报错。
        """
        tool_name = tool_call.function.name
        # OpenAI SDK 返回的 function.arguments 是 JSON 字符串,需解析为 dict 才能 ** 解包
        raw_args = tool_call.function.arguments
        tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        tool_id = tool_call.id

        # 判定是否走后台：模型显式 run_in_background=True 优先，否则启发式
        if self.background_manager.should_run_background(tool_name, tool_args):
            executor = self._make_executor(tool_name, tool_args)
            bg_id = self.background_manager.start_background_task(
                tool_name, tool_args, tool_id, executor
            )
            cmd_text = (
                tool_args.get("command")
                or (tool_args.get("prompt", "")[:80] if tool_args.get("prompt") else "")
                or tool_name
            )
            tool_output = (
                f"[Background task {bg_id} started] "
                f"Command: {cmd_text}. "
                f"Result will be available when complete."
            )
            self._print(f">> {tool_name} 后台分发: {bg_id}")
        else:
            # 同步路径：直接走原逻辑
            executor = self._make_executor(tool_name, tool_args)
            tool_output = executor()

        return {
            "role": "tool",
            "tool_name": tool_name,
            "tool_args": tool_args,
            "tool_call_id": tool_id,
            "content": str(tool_output),
        }

    # ═══════════════════════════════════════════════════════════
    #  智能体主循环
    # ═══════════════════════════════════════════════════════════

    def agent_loop(self) -> None:
        """一轮对话的核心循环：LLM 调用 → 工具执行 → 结果回放，直到无 tool_call。"""

        # ── 后台任务通知预热（turn 起点，while 之外，只跑一次）────────────
        # 上一 turn 退出时，turn 内最后那一轮迭代才会触发 collect_background_results()；
        # 如果上一 turn 在 LLM 不再返回 tool_call 时自然结束，那一帧可能没机会把
        # "已完成的 bg" 喂进来。这段预热专门处理"新 turn 进来时，把之前已经完成、
        # 还没被消费过的后台任务结果先注入上下文"，避免模型在新 turn 第一轮就误判
        # "任务还在跑"而另起一个新任务重复劳动。
        #
        # 注意：必须放在 while 之外，只在 turn 起点跑一次；while 内部的
        # 迭代间反馈仍由 collect_background_results() 负责。
        pre_notifs = self.background_manager.collect_background_results()
        if pre_notifs:
            pre_msg = {"role": "user", "content": "\n".join(pre_notifs)}
            self.history_messages.append(pre_msg)
            self.session_manager.append_message_to_session(self.session_file, pre_msg)
            self._print(
                f"  \033[32m[inject pre-loop] {len(pre_notifs)} background notification(s)\033[0m"
            )

        iteration = 0  # 循环迭代计数
        rounds_since_todo = 0  # 记录距离上次调用 todo 工具的轮数，用于 nag reminder

        # 这里可以增加s10课程中更新systemprompt的逻辑，同时更新内存message和会话记录的jsonl文件。
        # 这样可以保证记忆、工具、skill的实时更新，但会影响缓存未命中率。

        while True:
            iteration += 1
            if iteration > self.MAX_AGENT_ITERATIONS:
                self._print(
                    f"\033[31m[警告] 智能体循环达到最大迭代次数 ({self.MAX_AGENT_ITERATIONS})，强制结束\033[0m"
                )
                break

            # 在调用 LLM 前检查上下文，达到阈值时阻塞执行压缩并同步会话文件。
            self.session_manager.maybe_compact_context(
                self.history_messages, self.session_file
            )

            # S11 Error Recovery — 错误不是结束，是重试的开始（详见 agents/error_recovery.py）
            try:
                # 调用大模型执行当前轮次的回复
                # lambda 通过闭包把当前轮次的 max_tokens / model 锁住，控制器在循环里
                # 可能会通过 ESCALATE / FALLBACK 改变这些值，但本轮 lambda 已固定
                llm_response = self.recovery.with_retry(
                    lambda mt=self.recovery.current_max_tokens, mdl=self.recovery.current_model:
                    self.llm_client.chat.completions.create(
                        model=mdl,
                        messages=self.history_messages,
                        max_tokens=mt,
                        tools=self.tools.main_agent_tools if self.team_mode else self.tools.default_agent_tools,
                        tool_choice="auto",  # 工具选择，值域 none、auto、required，默认 auto
                        parallel_tool_calls=True,  # 是否并行执行工具调用，默认 False
                        stream=False,  # 是否流式输出，默认 False
                        temperature=0.5,
                        reasoning_effort="high",  # 思考强度，DeepSeek只有 high、max 两个选项
                        extra_body={"thinking": {"type": "enabled"}},  # 思考模式开关
                    )
                )
                # 从大模型回复中提取消息
                response_msg = llm_response.choices[0].message
                # 提取大模型回复中的工具调用
                response_tool_calls = response_msg.tool_calls or []
                # 打印大模型的思考和回复内容
                # ANSI: \033[2m=暗(细体)，\033[90m=灰色，\033[0m=重置
                self._print(
                    f"\033[2;90m[thinking]\n{truncate_chars(response_msg.reasoning_content, 300)}\n[/thinking]\033[0m"
                )
                self._print(f"[本轮回复]\n{response_msg.content}")

            except Exception as e:
                # 外层异常处理：内层 with_retry 主动 raise 出来的"非临时错误"会到这一层。
                # 控制器根据错误类型决定：继续重试（CONTINUE）或退出（ABORT）。
                if self.recovery.handle_exception(
                    e, self.history_messages, self.session_manager, self.session_file
                ) == RecoveryAction.ABORT:
                    return
                continue

            # Path 1：max_tokens 截断恢复
            # 注意：max_tokens 不是异常，是 API 正常返回的 finish_reason 之一
            # DeepSeek 走 OpenAI 兼容协议，没有 Anthropic 的 stop_reason 字段；
            # 截断的判定在 choices[0].finish_reason == "length"（OpenAI 官方语义）
            if llm_response.choices[0].finish_reason == "length":
                if self.recovery.handle_truncation(
                    response_msg, self.history_messages, self.session_manager, self.session_file
                ) == RecoveryAction.ABORT:
                    return
                continue

            # ── 正常完成：把 assistant 的回复追加到对话历史 ──
            # 注意：这里的 response.content 是模型完整返回的内容
            # （如果是 max_tokens 截断，就已经在上面 append 过了，不会走到这里）
            # 将 Pydantic 模型转换为 dict，保留 role/content/reasoning_content/tool_calls 等字段
            response_msg_dict = response_msg.model_dump()
            # 加入大模型回复到历史消息中,role 为 assistant，包含思考过程和回答内容
            self.history_messages.append(response_msg_dict)
            self.session_manager.append_message_to_session(
                self.session_file, response_msg_dict
            )

            if len(response_tool_calls) == 0:
                # 增加一个hook，用于在大模型回复中检查是否需要强制结束当前轮次
                force = self.hook_system.trigger("Stop", self.history_messages)
                if force:
                    # 往消息中记录强制结束的原因
                    self.history_messages.append({"role": "user", "content": force})
                    continue
                return

            # ANSI: \033[2m=暗(细体)，\033[93m=浅黄，\033[0m=重置（与上方灰色 [thinking] 区分）
            self._print(f"\033[2;93m[本轮大模型调用工具数量] {len(response_tool_calls)}\033[0m")
            for tc in response_tool_calls:
                # 单行打印超 200 字符截断，避免大参数（如大段代码/长路径）刷屏
                self._print(
                    f"\033[2;93m{truncate_chars(f"  - {tc.function.name}({tc.function.arguments})  #id={tc.id}\n ")}\033[0m"
                )
            self._print(f"\033[2;93m[本轮大模型工具调用结束,等待执行结果]\033[0m")
            # 三阶段执行：后台 → 并行 → 串行, 互斥分桶。
            #   后台桶: args.run_in_background=true, 立即分发给 background_manager 守护线程
            #   并行桶: args.parallel=true (且非后台), 线程池并发, 全部完成才走下一步
            #   串行桶: args.parallel=false 或缺省 (且非后台), 按声明顺序逐个执行
            # 结果用 {tool_call_id: result} 收集, 最后按 LLM 原始声明顺序回放到 history,
            # 保证 tool 消息顺序与 tool_calls 顺序一致(OpenAI 协议硬约束)。
            tool_call_results: dict[str, dict] = {}
            used_todo = False
            background_calls, parallel_calls, serial_calls = [], [], []
            for tool_call in response_tool_calls:
                if tool_call.function.name == "todo":
                    used_todo = True
                # 解析一次参数, 后面复用, 避免每阶段都重复 json.loads
                raw_args = tool_call.function.arguments
                tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                tool_call._args_cache = tool_args
                if tool_args.get("run_in_background"):
                    background_calls.append(tool_call)
                elif tool_args.get("parallel"):
                    parallel_calls.append(tool_call)
                else:
                    serial_calls.append(tool_call)

            # 阶段 1: 后台分发——立即拿到 bg_id 占位 result, 不阻塞当前 turn
            for tool_call in background_calls:
                # s04: PreToolUse 钩子, 主线程触发(hook 大多是同步观察者, 不该跨线程)
                blocked = self.hook_system.trigger("PreToolUse", tool_call)
                if blocked:
                    tool_call_results[tool_call.id] = {
                        "role": "tool", "tool_call_id": tool_call.id, "content": str(blocked)
                    }
                    continue
                tool_call_result = self._execute_tool_call(tool_call)
                self._print(
                    f"\033[2;93m [工具执行结果(后台)]\n {truncate_chars(str(tool_call_result.get("content", "")))}\n [/工具执行结果]\033[0m"
                )
                tool_call_results[tool_call.id] = tool_call_result
                self.hook_system.trigger("PostToolUse", tool_call, tool_call_result)

            # 阶段 2: 并行执行——parallel=true 且非后台, 线程池并发
            if parallel_calls:
                with ThreadPoolExecutor(max_workers=len(parallel_calls)) as executor:
                    # PreToolUse 在主线程顺序触发, 避免 hook 跨线程
                    futures: dict = {}
                    for tool_call in parallel_calls:
                        blocked = self.hook_system.trigger("PreToolUse", tool_call)
                        if blocked:
                            tool_call_results[tool_call.id] = {
                                "role": "tool", "tool_call_id": tool_call.id, "content": str(blocked)
                            }
                            continue
                        fut = executor.submit(self._execute_tool_call, tool_call)
                        futures[fut] = tool_call
                    # 收集结果, as_completed 谁先完谁先回填; 异常兜底避免 tool_call_id 缺失
                    for fut in as_completed(futures):
                        tc = futures[fut]
                        try:
                            tool_call_result = fut.result()
                        except Exception as e:
                            tool_call_result = {
                                "role": "tool", "tool_call_id": tc.id,
                                "content": f"Error: {type(e).__name__}: {e}",
                            }
                        self._print(
                            f"\033[2;93m [工具执行结果(并行)]\n {truncate_chars(str(tool_call_result.get("content", "")))}\n [/工具执行结果]\033[0m"
                        )
                        tool_call_results[tc.id] = tool_call_result
                        self.hook_system.trigger("PostToolUse", tc, tool_call_result)

            # 阶段 3: 串行执行——按声明顺序, 一个一个来
            for tool_call in serial_calls:
                blocked = self.hook_system.trigger("PreToolUse", tool_call)
                if blocked:
                    tool_call_results[tool_call.id] = {
                        "role": "tool", "tool_call_id": tool_call.id, "content": str(blocked)
                    }
                    continue
                tool_call_result = self._execute_tool_call(tool_call)
                self._print(
                    f"\033[2;93m [工具执行结果(串行)]\n {truncate_chars(str(tool_call_result.get("content", "")))}\n [/工具执行结果]\033[0m"
                )
                tool_call_results[tool_call.id] = tool_call_result
                self.hook_system.trigger("PostToolUse", tool_call, tool_call_result)

            # 按 LLM 声明顺序回放 tool 消息(三桶结果合并, 严格保序)
            for tc in response_tool_calls:
                result = tool_call_results.get(tc.id)
                if result is None:
                    # 极端兜底: hook 拦截或异常分支下, 万一没填, 写一条占位
                    result = {"role": "tool", "tool_call_id": tc.id,
                              "content": f"Error: no result for {tc.id}"}
                content = result.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                tool_msg = {"role": "tool", "content": content, "tool_call_id": tc.id}
                self.history_messages.append(tool_msg)
                self.session_manager.append_message_to_session(
                    self.session_file, tool_msg
                )

            # 后台任务通知注入：本轮（或更早轮次）已完成的后台任务，
            # 把它们的输出整理成 <task_notification> 文本块作为 user 消息追加。
            # 与 s13 教程的"每轮都收集"语义一致：
            # - daemon 线程可能在任何时刻完成 task，调用方随时可以拿到通知；
            # - 同一结果只通知一次（collect_background_results 内部 pop）。
            # 不要把通知合并进 tool message——tool 消息必须严格对应
            # assistant.tool_calls 里的 tool_call_id，否则 LLM 会报参数错误。
            bg_notifications = self.background_manager.collect_background_results()
            if bg_notifications:
                notification_msg = {
                    "role": "user",
                    "content": "\n".join(bg_notifications),
                }
                self.history_messages.append(notification_msg)
                self.session_manager.append_message_to_session(
                    self.session_file, notification_msg
                )
                self._print(
                    f"  \033[32m[inject] {len(bg_notifications)} background notification(s)\033[0m"
                )

            # todo 更新追踪: 本轮用了 todo 就清零, 否则累加;
            # 连续 3 轮未更新且仍有 open items 时, 注入提醒作为本轮最后一条消息, 并清零避免重复打扰
            rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
            if rounds_since_todo >= 3 and self.tools.get_todo_manager().has_open_items():
                reminder_msg = {"role": "user", "content": "<reminder>Update your tasks.</reminder>"}
                self.history_messages.append(reminder_msg)
                self.session_manager.append_message_to_session(
                    self.session_file, reminder_msg
                )
                rounds_since_todo = 0

        self._print("\033[2;93m[****一个turn循环结束****]\n \033[0m\n")
