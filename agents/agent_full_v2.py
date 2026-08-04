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
from tools import (
    BASE_TOOL,
    MAIN_AGENT_TOOLS,
    TOOL_HANDLERS,
    WORKDIR,
    BACKGROUND_MANAGER,
    CHAT_HISTORY_DIR,
    SKILLS_DIR,
    TODO_MANAGER,
)
from skills import SkillLoader
from llm_manage import LLMClient
from system_prompt import SystemPromptBuilder
from error_recovery import ErrorRecovery, RecoveryAction
# from check_permission import check_permission
from hooks import HookSystem

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
SKILLS = SkillLoader(SKILLS_DIR)
# 子智能体单实例：复用工具集/处理器/hooks，避免每次 sub_agent 调用都重新实例化
SUB_AGENT_RUNNER = SubAgent(BASE_TOOL, TOOL_HANDLERS, hook_system)

# S11 错误恢复控制器：429/503 重试、max_tokens 截断恢复、prompt 超长压缩
recovery = ErrorRecovery(primary_model=MODEL, fallback_model=FALLBACK_MODEL)

# 最大智能体循环迭代次数，防止无限循环导致程序卡死
MAX_AGENT_ITERATIONS = 100




def _execute_tool_call(tool_call: dict) -> dict:
    """执行单个工具调用（sub_agent 或普通工具），返回结果字典"""
    tool_name = tool_call.function.name
    # OpenAI SDK 返回的 function.arguments 是 JSON 字符串,需解析为 dict 才能 ** 解包
    raw_args = tool_call.function.arguments
    tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    tool_id = tool_call.id


    if tool_name == "sub_agent":
        allowed_tools = tool_args.get("allowed_tools")
        print(f">> sub_agent ({tool_args.get('description', '')}): {tool_args['prompt'][:80]}")
        tool_output = SUB_AGENT_RUNNER.spawn_subagent(
            tool_args["prompt"], allowed_tools=allowed_tools
        )
        print(f">> sub_agent 执行结果: {tool_output[:200]}...")
    elif tool_name in TOOL_HANDLERS:
        print(f">> 工具 {tool_name}({tool_args})")
        tool_output = TOOL_HANDLERS[tool_name](**tool_args)
        print(f">> 工具 {tool_name} 执行结果: {tool_output}")
    else:
        tool_output = f"Error: Unknown tool {tool_name}"
        print(f">> 工具 {tool_name} 执行结果: {tool_output}")
    print("-" * 20)

    return {
        "role": "tool",
        "tool_name": tool_name,
        "tool_args": tool_args,
        "tool_call_id": tool_id,
        "content": tool_output,
    }


#执行主体
def agent_loop(history_messages: list, session_file: Path, session_manager: SessionManager):

    iteration = 0  # 循环迭代计数
    rounds_since_todo = 0  # 记录距离上次调用 todo 工具的轮数，用于 nag reminder

    # 这里可以增加s10课程中更新systemprompt的逻辑，同时更新内存message和会话记录的jsonl文件。
    # 这样可以保证记忆、工具、skill的实时更新，但会影响缓存未命中率。

    while True:
        iteration += 1
        if iteration > MAX_AGENT_ITERATIONS:
            print(f"\033[31m[警告] 智能体循环达到最大迭代次数 ({MAX_AGENT_ITERATIONS})，强制结束\033[0m")
            break

        # 在 LLM 调用之前，排空后台通知并注入为系统消息
        notifs = BACKGROUND_MANAGER.drain_notifications()
        if notifs and history_messages:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            history_messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
            history_messages.append({"role": "assistant", "content": "Noted background results."})

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
            print(f"[本轮思考] {response_msg.reasoning_content}")
            print(f"[本轮回复] {response_msg.content}")


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

        print(f"》》》》》》》》[本轮 tool_calls 数量] {len(response_tool_calls)}")
        print(response_tool_calls)
        print("*********")
        # 所有工具调用都根据 parallel 参数分组，并行组用线程池执行，串行组按顺序执行
        tool_call_results = []
        used_todo = False
        # 目前先在循环中串行执行所有工具调用，大模型返回工具根据任务需要会一次返回多个或一个工具
        for tool_call in response_tool_calls:
            if tool_call.function.name == "todo":
                used_todo = True
            
            # s04 change: hook，用钩子增加工具权限校验和日志记录，若不符合权限则阻塞执行
            blocked = hook_system.trigger("PreToolUse", tool_call)
            if blocked:
                #往消息中记录工具调用被阻塞的原因
                tool_call_results.append({"role": "tool", "tool_call_id": tool_call.id,
                                "content": str(blocked)})
                continue
            
            # 执行工具调用或 sub_agent 调用
            tool_call_result = _execute_tool_call(tool_call)
            tool_call_results.append(tool_call_result)

            # s04: post hook
            hook_system.trigger("PostToolUse", tool_call, tool_call_result)  

        # 并行执行 parallel=true 的工具,
        # 目前先注释掉，不支持并行执行，因为当前的并行并未考虑到当同时返回多个工具时，多个工具有并行和顺序执行的执行顺序情况，目前仅为同时并行或同时串行。

        # 串行执行下, tool_call_results 与 response_tool_calls 顺序一一对应, 直接 zip 组装 tool message
        # 后续扩展: 并行执行 parallel=true 的工具, 需要根据工具的 parallel 参数, 分组执行, 并在每个组内按顺序执行, 保持与大模型回复的 tool_calls 顺序一致
        for tc, result in zip(response_tool_calls, tool_call_results):
            # result.content 约定是字符串; 兜底处理 handler 偷懒返回 dict 的情况,
            # 避免二次 json.dumps 导致 session jsonl 出现双层嵌套
            content = result.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            tool_msg = {"role": "tool", "content": content, "tool_call_id": tc.id}
            history_messages.append(tool_msg)
            session_manager.append_message_to_session(session_file, tool_msg)

        # todo 更新追踪: 本轮用了 todo 就清零, 否则累加;
        # 连续 3 轮未更新且仍有 open items 时, 注入提醒作为本轮最后一条消息, 并清零避免重复打扰
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        if rounds_since_todo >= 3 and TODO_MANAGER.has_open_items():
            reminder_msg = {"role": "user", "content": "<reminder>Update your tasks.</reminder>"}
            history_messages.append(reminder_msg)
            session_manager.append_message_to_session(session_file, reminder_msg)
            rounds_since_todo = 0


def main():
    #这里和教程不同的地方是systemprompt没有放到agentloop中去实时更新，而是在初始化时就构建好，
    # 这样可以保证高缓存命中率，但不能保证记忆、工具、skill的实时更新。
    session_manager = SessionManager(CHAT_HISTORY_DIR, SYSTEM.build_system_prompt())
    session_num, session_file, history_messages = session_manager.init_session()
    
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
            print(f"\033[33m已创建新会话: session_{session_num}.jsonl\033[0m")
            continue
        
        if query.strip().lower().startswith("/switchsession "):
            try:
                target_num = int(query.strip().split()[1])
                session_num, session_file, history_messages = session_manager.switch_session(target_num)
                print(f"\033[33m已切换到会话: session_{session_num}.jsonl ({len(history_messages)} 条消息)\033[0m")
            except (ValueError, IndexError):
                print("\033[31m用法: /switchsession <数字>\033[0m")
            except FileNotFoundError as e:
                print(f"\033[31m{e}\033[0m")
            continue
        
        if query.strip().lower() == "/clearsession":
            deleted_count = session_manager.clear_session(session_file)
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
