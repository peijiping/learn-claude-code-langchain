#!/usr/bin/env python3
"""
V2版本的通用智能体学习
"""

import json
import os

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from session_manage import SessionManager
from subagent import SubAgent
from background_manager import BackgroundManager
from tools import (
    BASE_TOOL,
    MAIN_AGENT_TOOLS,
    TOOL_HANDLERS,
    WORKDIR,
    # BACKGROUND_MANAGER,
    CHAT_HISTORY_DIR,
    SKILLS_DIR,
    set_todo_manager,
    get_todo_manager,
    set_background_manager,
)
from skills import SkillLoader
from llm_manage import LLMClient
from system_prompt import SystemPromptBuilder
from error_recovery import ErrorRecovery, RecoveryAction
# from check_permission import check_permission
from hooks import HookSystem
from utils import truncate_chars

try:
    import readline  # 导入 GNU readline 库，用于增强命令行输入功能
    # 关闭终端特殊字符绑定，避免干扰输入
    readline.parse_and_bind('set bind-tty-special-chars off')
    # 启用输入元字符（支持 UTF-8/中文等非 ASCII 字符的输入）
    readline.parse_and_bind('set input-meta on')
    # 启用输出元字符（支持 UTF-8/中文等非 ASCII 字符的输出显示）
    readline.parse_and_bind('set output-meta on')
    # 关闭元字符转换，防止中文字符被转义
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass  # 如果 readline 不可用（如 Windows 环境），则跳过配置



# 加载环境变量
load_dotenv(override=True)

MODEL = os.environ.get("OPENAI_MODEL_ID", "")
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL_ID", "")



# 系统prompt
SYSTEM = SystemPromptBuilder()
# 创建绑定了工具的 LLM 实例
llm_client = LLMClient().llm

# 钩子实例：主循环的 hook_system 在模块级只实例化一次
hook_system = HookSystem()
hook_system.register_default_hooks()
# 加载技能
Skills = SkillLoader(SKILLS_DIR)
# 子智能体单实例：复用工具集/处理器/hooks，避免每次 sub_agent 调用都重新实例化
subagent_runner = SubAgent(BASE_TOOL, TOOL_HANDLERS, hook_system)
# 后台任务管理器
background_manager = BackgroundManager()
# 挂到 tools 模块的 holder 上，让 check_background 工具的 handler 能拿到同一实例。
# 必须在 TOOL_HANDLERS 实际被使用前完成（模块级赋值即生效）。
set_background_manager(background_manager)




# S11 错误恢复控制器：429/503 重试、max_tokens 截断恢复、prompt 超长压缩
recovery = ErrorRecovery(primary_model=MODEL, fallback_model=FALLBACK_MODEL)

# 最大智能体循环迭代次数，防止无限循环导致程序卡死
MAX_AGENT_ITERATIONS = 100




def _make_executor(tool_name: str, tool_args: dict):
    """
    把"执行一个工具调用"包成无参闭包，供 background_manager 在后台线程调用。

    tool_name / tool_args 是 _make_executor 的形参（独立作用域、每次调用绑一次），
    所以 lambda 直接闭包捕获即可，无须 def 嵌套，也不会出现 for 循环闭包共享
    变量导致所有闭包都引用最后一次迭代值的经典坑。
    """
    if tool_name == "sub_agent":
        return lambda: subagent_runner.spawn_subagent(
            tool_args.get("prompt", ""),
            allowed_tools=tool_args.get("allowed_tools"),
        )
    elif tool_name in TOOL_HANDLERS:
        return lambda: TOOL_HANDLERS[tool_name](**tool_args)
    else:
        return lambda: f"Error: Unknown tool {tool_name}"


def _execute_tool_call(tool_call) -> dict:
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
    if background_manager.should_run_background(tool_name, tool_args):
        executor = _make_executor(tool_name, tool_args)
        bg_id = background_manager.start_background_task(tool_name, tool_args, tool_id, executor)
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
        print(f">> {tool_name} 后台分发: {bg_id}")
    else:
        # 同步路径：直接走原逻辑
        executor = _make_executor(tool_name, tool_args)
        # if tool_name == "sub_agent":
        #     print(f">> sub_agent ({tool_args.get('description', '')}): {tool_args.get('prompt', '')[:80]}")
        # else:
        #     print(f">> 工具 {tool_name}({tool_args})")
        tool_output = executor()
        # if tool_name == "sub_agent":
        #     print(f">> sub_agent 执行结果: {str(tool_output)[:200]}...")
        # else:
        #     print(f">> 工具 {tool_name} 执行结果: {tool_output}")
    

    return {
        "role": "tool",
        "tool_name": tool_name,
        "tool_args": tool_args,
        "tool_call_id": tool_id,
        "content": str(tool_output),
    }


def _inject_todo_reminder(history_messages: list, session_file: Path, session_manager: SessionManager) -> None:
    """
    会话恢复/切换时，若当前 session 有未完成的 todo，注入一条 reminder
    让模型意识到"上次有活没干完"。

    reminder 写在 user query 之前、system / 旧 history 之后，
    模型下一轮必能直接看到。reminder 同时落盘 session_file，
    保证下次启动 reload 仍可见。
    """
    mgr = get_todo_manager()
    if not mgr.has_open_items():
        return
    reminder = (
        "<system-reminder>本次会话检测到上次有未完成的待办事项：\n"
        f"{mgr.render()}\n"
        "请在继续之前确认是否继续执行；如果任务已不再相关，请用 todo 工具把对应项标记为 completed，"
        "或开启新计划（fresh_start=true 整体替换）。</system-reminder>"
    )
    history_messages.append({"role": "user", "content": reminder})
    session_manager.append_message_to_session(session_file, history_messages[-1])


#执行主体
def agent_loop(history_messages: list, session_file: Path, session_manager: SessionManager):

    # ── 后台任务通知预热（turn 起点，while 之外，只跑一次）────────────
    # 上一 turn 退出时，turn 内最后那一轮迭代才会触发 line ~340 的
    # collect_background_results()；如果上一 turn 在 LLM 不再返回 tool_call
    # 时自然结束，那一帧可能没机会把"已完成的 bg"喂进来。
    # 这段预热专门处理"新 turn 进来时，把之前已经完成、还没被消费过的
    # 后台任务结果先注入上下文"，避免模型在新 turn 第一轮就误判
    # "任务还在跑"而另起一个新任务重复劳动。
    #
    # 注意：必须放在 while 之外，只在 turn 起点跑一次；while 内部的
    # 迭代间反馈仍由 line ~340 的 collect_background_results() 负责。
    pre_notifs = background_manager.collect_background_results()
    if pre_notifs:
        pre_msg = {"role": "user", "content": "\n".join(pre_notifs)}
        history_messages.append(pre_msg)
        session_manager.append_message_to_session(session_file, pre_msg)
        print(f"  \033[32m[inject pre-loop] {len(pre_notifs)} background notification(s)\033[0m")

    iteration = 0  # 循环迭代计数
    rounds_since_todo = 0  # 记录距离上次调用 todo 工具的轮数，用于 nag reminder

    # 这里可以增加s10课程中更新systemprompt的逻辑，同时更新内存message和会话记录的jsonl文件。
    # 这样可以保证记忆、工具、skill的实时更新，但会影响缓存未命中率。

    while True:
        iteration += 1
        if iteration > MAX_AGENT_ITERATIONS:
            print(f"\033[31m[警告] 智能体循环达到最大迭代次数 ({MAX_AGENT_ITERATIONS})，强制结束\033[0m")
            break

        # 在调用 LLM 前检查上下文，达到阈值时阻塞执行压缩并同步会话文件。
        session_manager.maybe_compact_context(history_messages, session_file)

        #S11 Error Recovery — 错误不是结束，是重试的开始（详见 agents/error_recovery.py）
        try:
            #调用大模型执行当前轮次的回复
            # lambda 通过闭包把当前轮次的 max_tokens / model 锁住，控制器在循环里
            # 可能会通过 ESCALATE / FALLBACK 改变这些值，但本轮 lambda 已固定
            llm_response = recovery.with_retry(
                lambda mt=recovery.current_max_tokens, mdl=recovery.current_model:
                llm_client.chat.completions.create(
                    model=mdl,
                    messages=history_messages,
                    max_tokens=mt,
                    tools=MAIN_AGENT_TOOLS,
                    tool_choice="auto", #工具选择，值域 none、auto、required，默认 auto
                    parallel_tool_calls=True, #是否并行执行工具调用，默认 False
                    stream=False, #是否流式输出，默认 False
                    temperature=0.5,
                    reasoning_effort="high", #思考强度，DeepSeek只有 high、max 两个选项
                    extra_body={"thinking": {"type": "enabled"}}, #思考模式开关，值范围 disabled、enabled，默认 enabled
                )
            )
            # 从大模型回复中提取消息
            response_msg = llm_response.choices[0].message
            # 提取大模型回复中的工具调用
            response_tool_calls = response_msg.tool_calls or []
            #打印大模型的思考和回复内容
            # ANSI: \033[2m=暗(细体)，\033[90m=灰色，\033[0m=重置
            print(f"\033[2;90m[thinking]\n{truncate_chars(response_msg.reasoning_content, 300)}\n[/thinking]\033[0m")
            print(f"[本轮回复]\n{response_msg.content}")


        except Exception as e:
            # 外层异常处理：内层 with_retry 主动 raise 出来的"非临时错误"会到这一层。
            # 控制器根据错误类型决定：继续重试（CONTINUE）或退出（ABORT）。
            if recovery.handle_exception(
                e, history_messages, session_manager, session_file
            ) == RecoveryAction.ABORT:
                return
            continue

        # Path 1：max_tokens 截断恢复
        # 注意：max_tokens 不是异常，是 API 正常返回的 finish_reason 之一
        # DeepSeek 走 OpenAI 兼容协议，没有 Anthropic 的 stop_reason 字段；
        # 截断的判定在 choices[0].finish_reason == "length"（OpenAI 官方语义）
        if llm_response.choices[0].finish_reason == "length":
            if recovery.handle_truncation(
                response_msg, history_messages, session_manager, session_file
            ) == RecoveryAction.ABORT:
                return
            continue

        # ── 正常完成：把 assistant 的回复追加到对话历史 ──
        # 注意：这里的 response.content 是模型完整返回的内容
        # （如果是 max_tokens 截断，就已经在上面 append 过了，不会走到这里）
        # 将 Pydantic 模型转换为 dict，保留 role/content/reasoning_content/tool_calls 等字段
        response_msg_dict = response_msg.model_dump()
        # 加入大模型回复到历史消息中,role 为 assistant，包含思考过程和回答内容
        history_messages.append(response_msg_dict)
        session_manager.append_message_to_session(session_file, response_msg_dict)

        if len(response_tool_calls) == 0:
            #增加一个hook，用于在大模型回复中检查是否需要强制结束当前轮次
            force = hook_system.trigger("Stop", history_messages)
            if force:
                #往消息中记录强制结束的原因
                history_messages.append({"role": "user", "content": force})
                continue
            return

        # ANSI: \033[2m=暗(细体)，\033[93m=浅黄，\033[0m=重置（与上方灰色 [thinking] 区分）
        print(f"\033[2;93m[本轮大模型调用工具数量] {len(response_tool_calls)}\033[0m")
        for tc in response_tool_calls:
            # 单行打印超 200 字符截断，避免大参数（如大段代码/长路径）刷屏
            print(f"\033[2;93m{truncate_chars(f"  - {tc.function.name}({tc.function.arguments})  #id={tc.id}\n ")}\033[0m")
        print(f"\033[2;93m[本轮大模型工具调用结束,等待执行结果]\033[0m")
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
            blocked = hook_system.trigger("PreToolUse", tool_call)
            if blocked:
                tool_call_results[tool_call.id] = {
                    "role": "tool", "tool_call_id": tool_call.id, "content": str(blocked)
                }
                continue
            tool_call_result = _execute_tool_call(tool_call)
            print(f"\033[2;93m [工具执行结果(后台)]\n {truncate_chars(str(tool_call_result.get("content", "")))}\n [/工具执行结果]\033[0m")
            tool_call_results[tool_call.id] = tool_call_result
            hook_system.trigger("PostToolUse", tool_call, tool_call_result)

        # 阶段 2: 并行执行——parallel=true 且非后台, 线程池并发
        if parallel_calls:
            with ThreadPoolExecutor(max_workers=len(parallel_calls)) as executor:
                # PreToolUse 在主线程顺序触发, 避免 hook 跨线程
                futures: dict = {}
                for tool_call in parallel_calls:
                    blocked = hook_system.trigger("PreToolUse", tool_call)
                    if blocked:
                        tool_call_results[tool_call.id] = {
                            "role": "tool", "tool_call_id": tool_call.id, "content": str(blocked)
                        }
                        continue
                    fut = executor.submit(_execute_tool_call, tool_call)
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
                    print(f"\033[2;93m [工具执行结果(并行)]\n {truncate_chars(str(tool_call_result.get("content", "")))}\n [/工具执行结果]\033[0m")
                    tool_call_results[tc.id] = tool_call_result
                    hook_system.trigger("PostToolUse", tc, tool_call_result)

        # 阶段 3: 串行执行——按声明顺序, 一个一个来
        for tool_call in serial_calls:
            blocked = hook_system.trigger("PreToolUse", tool_call)
            if blocked:
                tool_call_results[tool_call.id] = {
                    "role": "tool", "tool_call_id": tool_call.id, "content": str(blocked)
                }
                continue
            tool_call_result = _execute_tool_call(tool_call)
            print(f"\033[2;93m [工具执行结果(串行)]\n {truncate_chars(str(tool_call_result.get("content", "")))}\n [/工具执行结果]\033[0m")
            tool_call_results[tool_call.id] = tool_call_result
            hook_system.trigger("PostToolUse", tool_call, tool_call_result)

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
            history_messages.append(tool_msg)
            session_manager.append_message_to_session(session_file, tool_msg)

        # 后台任务通知注入：本轮（或更早轮次）已完成的后台任务，
        # 把它们的输出整理成 <task_notification> 文本块作为 user 消息追加。
        # 与 s13 教程的"每轮都收集"语义一致：
        # - daemon 线程可能在任何时刻完成 task，调用方随时可以拿到通知；
        # - 同一结果只通知一次（collect_background_results 内部 pop）。
        # 不要把通知合并进 tool message——tool 消息必须严格对应
        # assistant.tool_calls 里的 tool_call_id，否则 LLM 会报参数错误。
        bg_notifications = background_manager.collect_background_results()
        if bg_notifications:
            notification_msg = {
                "role": "user",
                "content": "\n".join(bg_notifications),
            }
            history_messages.append(notification_msg)
            session_manager.append_message_to_session(session_file, notification_msg)
            print(f"  \033[32m[inject] {len(bg_notifications)} background notification(s)\033[0m")

        # todo 更新追踪: 本轮用了 todo 就清零, 否则累加;
        # 连续 3 轮未更新且仍有 open items 时, 注入提醒作为本轮最后一条消息, 并清零避免重复打扰
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3 and get_todo_manager().has_open_items():
            reminder_msg = {"role": "user", "content": "<reminder>Update your tasks.</reminder>"}
            history_messages.append(reminder_msg)
            session_manager.append_message_to_session(session_file, reminder_msg)
            rounds_since_todo = 0
    
    print("\033[2;93m[****一个turn循环结束****]\n \033[0m\n")


def main():
    #这里和教程不同的地方是systemprompt没有放到agentloop中去实时更新，而是在初始化时就构建好，
    # 这样可以保证高缓存命中率，但不能保证记忆、工具、skill的实时更新。
    session_manager = SessionManager(CHAT_HISTORY_DIR, SYSTEM.build_system_prompt())
    session_num, session_file, history_messages = session_manager.init_session()
    # todo 与 session 绑定：每次切会话都要重新指向对应的 todo 文件，
    # 并尝试注入未完成项 reminder（坑 ② 修复）
    set_todo_manager(session_num)
    _inject_todo_reminder(history_messages, session_file, session_manager)
    
    while True:
        try:
            context_label = session_manager.format_context_label(history_messages)
            query = input(f"\033[36m[session_{session_num} ({context_label})] >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        #请在此处增加/help命令，用于显示所有可用命令
        if query.strip().lower() == "/help":
            print("可用命令:")
            print("/q: 退出会话")
            print("/newsession: 创建新会话")
            print("/clearsession: 清空当前会话")
            print("/tasks: 显示当前任务")
            print("/compact: 压缩上下文")
            print("/skills: 显示当前技能")
            continue

        if query.strip().lower() in ("/q", "/exit", ""):
            break
        
        if query.strip().lower() == "/newsession":
            session_num, session_file, history_messages = session_manager.create_initialized_session()
            # 新会话的 todo 文件尚不存在，set_todo_manager 会建出空列表；reminder 不会注入
            set_todo_manager(session_num)
            print(f"\033[33m已创建新会话: session_{session_num}.jsonl\033[0m")
            continue

        if query.strip().lower().startswith("/switchsession "):
            try:
                target_num = int(query.strip().split()[1])
                session_num, session_file, history_messages = session_manager.switch_session(target_num)
                # 切到目标会话后，重新指向该 session 的 todo 文件并尝试注入 reminder
                set_todo_manager(session_num)
                _inject_todo_reminder(history_messages, session_file, session_manager)
                print(f"\033[33m已切换到会话: session_{session_num}.jsonl ({len(history_messages)} 条消息)\033[0m")
            except (ValueError, IndexError):
                print("\033[31m用法: /switchsession <数字>\033[0m")
            except FileNotFoundError as e:
                print(f"\033[31m{e}\033[0m")
            continue

        if query.strip().lower() == "/clearsession":
            deleted_count = session_manager.clear_session(session_file)
            # todo 与 chat history 同生共死：清空 chat 的同时把当前 session 的 todo 也重置为空
            # 直接调 update([]) 落空 JSON，保留文件结构便于 TodoManager.load 解析
            get_todo_manager().update([], fresh_start=False)
            history_messages = session_manager.load_session_history(session_file)
            print(f"\033[33m已清空当前会话，删除了 {deleted_count} 条历史消息\033[0m")
            continue
        
        if query.strip() == "/tasks":
            print(get_todo_manager().render())
            continue

        if query.strip() == "/compact":
            session_manager.maybe_compact_context(history_messages, session_file, manual=True)
            continue

        if query.strip() == "/skills":
            skill_list = SKILLS.list_skills()
            print(f"当前可用技能:\n {skill_list}")
            continue

        # s04: UserPromptSubmit 钩子 —— 上下文注入提示。此处仅做"提示/观察",并不真正修改 query，先占位后续可以再此处扩展逻辑
        hook_system.trigger("UserPromptSubmit", query)
        
        history_messages.append({"role": "user", "content": query})
        session_manager.append_message_to_session(session_file, history_messages[-1])
        session_manager.maybe_compact_context(history_messages, session_file)
        # 执行智能体主循环
        agent_loop(history_messages, session_file, session_manager)
        response_content = history_messages[-1].get("content", "")
        if isinstance(response_content, list):
            for block in response_content:
                print(block.get("text", ""))
        else:
            print(response_content)
        print()


if __name__ == "__main__":
    main()
