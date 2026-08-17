#!/usr/bin/env python3
"""agent_cli.py - 主智能体命令行交互入口

实例化 Agent 并驱动 REPL：input 循环 + 斜杠命令 + readline 中文输入配置。

- 新推荐入口：python agent_cli.py
- 向后兼容：python agent_full_v2.py 内部延迟调用本模块的 main()

为 s14 定时任务（每任务独立会话）与未来 TUI 多会话预留的接缝在 Agent 类上：
  agent = Agent()
  agent.init_session(resume=False)   # 新会话
  agent.run_turn("[Scheduled] ...") # 非交互单轮
"""
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


def main() -> None:
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
            try:
                target_num = int(cmd.split()[1])
                num, msg_count = agent.switch_session(target_num)
                print(f"\033[33m已切换到会话: session_{num}.jsonl ({msg_count} 条消息)\033[0m")
            except (ValueError, IndexError):
                print("\033[31m用法: /switchsession <数字>\033[0m")
            except FileNotFoundError as e:
                print(f"\033[31m{e}\033[0m")
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
