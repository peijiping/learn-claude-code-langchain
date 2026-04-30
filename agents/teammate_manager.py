#!/usr/bin/env python3
"""
teammate_manager.py - 团队成员管理模块

本模块实现基于文件的 JSONL 收件箱的团队成员管理系统。提供两个核心类：

1. MessageBus（消息总线）：负责团队成员之间的消息传递
   - 每位团队成员拥有独立的 JSONL 收件箱文件
   - 支持点对点消息、广播消息、以及特殊消息类型

2. TeammateManager（团队成员管理器）：负责管理团队成员的生命周期
   - 持久化团队配置到 config.json
   - 通过独立线程运行每位团队成员的代理循环
   - 支持动态创建、状态跟踪和优雅关闭

关键概念：
- 团队成员（Teammate）：持久化的命名代理，拥有独立线程和收件箱
- 收件箱（Inbox）：基于 JSONL 文件的消息队列，仅追加写入，读取后清空
- 消息类型：支持 5 种预定义消息类型，用于不同通信场景
"""

import json
import os
import subprocess
import time
import threading
from pathlib import Path
from typing import Optional
from llm_manage import create_llm_with_tools
from tools_base import safe_path, run_bash, run_read, run_write, run_edit

WORKDIR = Path.cwd() / "WorkSpace"

# 预定义的有效消息类型集合
# 用于验证消息总线中传输的消息类型是否合法
VALID_MSG_TYPES = {
    "message",               # 普通文本消息，点对点发送
    "broadcast",             # 广播消息，发送给所有团队成员
    "shutdown_request",     # 关闭请求，请求目标团队成员优雅关闭
    "shutdown_response",     # 关闭响应，目标对关闭请求的批准/拒绝回复
    "plan_approval_response", # 计划审批响应，对计划请求的批准/拒绝回复
}


# -- MessageBus: JSONL inbox per teammate --
class MessageBus:
    """
    消息总线类，负责团队成员之间的消息传递

    设计理念：
    - 每个团队成员拥有独立的 JSONL 收件箱文件（.team/inbox/{name}.jsonl）
    - 消息以追加模式写入（append-only），保证消息不丢失
    - 读取收件箱后自动清空文件，实现"消费"语义

    消息格式（JSON对象）：
    {
        "type": str,         # 消息类型，取值自 VALID_MSG_TYPES
        "from": str,         # 发送者名称
        "content": str,      # 消息内容
        "timestamp": float,  # 时间戳（从 epoch 开始的秒数）
        ...extra             # 可选的扩展字段
    }
    """

    def __init__(self, inbox_dir: Path):
        """
        初始化消息总线

        参数:
            inbox_dir: 收件箱目录路径，所有成员的收件箱文件将存放于此
        """
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)  # 确保目录存在

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        """
        向指定团队成员发送消息

        参数:
            sender: 发送者名称
            to: 接收者名称（目标团队成员）
            content: 消息内容
            msg_type: 消息类型，默认为 "message"（普通文本消息）
            extra: 可选的扩展字段字典，会合并到消息对象中

        返回:
            str: 操作结果字符串，"Sent {msg_type} to {to}" 或错误信息

        注意:
            - 消息类型必须为 VALID_MSG_TYPES 中的有效值
            - 消息以 JSONL 格式追加到接收者的收件箱文件
        """
        # 验证消息类型是否合法
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"

        # 构造消息对象
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),  # 记录发送时间戳
        }
        # 合并可选的扩展字段
        if extra:
            msg.update(extra)

        # 将消息追加写入接收者的收件箱文件
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")

        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        """
        读取并清空指定团队成员的收件箱

        参数:
            name: 团队成员名称

        返回:
            list: 消息对象列表，如果收件箱为空或不存在则返回空列表

        注意:
            - 读取后收件箱文件会被清空，实现"消费"语义
            - 这是一种"排他性读取"，消息只会被一个消费者处理
        """
        inbox_path = self.dir / f"{name}.jsonl"

        # 如果收件箱文件不存在，返回空列表
        if not inbox_path.exists():
            return []

        # 读取所有消息行并解析为 JSON 对象
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))

        # 清空收件箱文件
        inbox_path.write_text("")

        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        """
        向所有团队成员广播消息

        参数:
            sender: 发送者名称
            content: 消息内容
            teammates: 团队成员名称列表

        返回:
            str: 广播结果字符串，格式为 "Broadcast to {count} teammates"

        注意:
            - 广播消息使用 "broadcast" 类型
            - 发送者不会收到自己发送的广播消息
        """
        count = 0
        for name in teammates:
            # 排除发送者自己
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# -- TeammateManager: persistent named agents with config.json --
class TeammateManager:
    """
    团队成员管理器，负责管理团队成员的生命周期和团队配置

    核心职责：
    - 持久化存储团队配置（config.json），包含团队名称和所有成员信息
    - 管理团队成员的线程，实现真正的并发执行
    - 追踪成员状态：working（工作中）、idle（空闲）、shutdown（已关闭）
    - 提供成员Spawn机制，为每位成员创建独立的代理循环线程

    成员状态机：
    - idle -> working: 当收到新任务并开始执行时
    - working -> idle: 当任务完成或线程达到最大轮数时
    - working -> shutdown: 当收到关闭请求并批准时
    - idle -> shutdown: 当收到关闭请求并批准时
    """

    def __init__(self, team_dir: Path):
        """
        初始化团队成员管理器

        参数:
            team_dir: 团队目录路径，用于存放 config.json 和收件箱目录
        """
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)  # 确保目录存在
        self.config_path = self.dir / "config.json"  # 团队配置文件路径
        self.config = self._load_config()  # 加载团队配置
        self.threads = {}  # 存储团队成员对应的线程对象 {name: Thread}


    def _load_config(self) -> dict:
        """
        从文件加载团队配置

        返回:
            dict: 团队配置对象，格式为：
            {
                "team_name": str,    # 团队名称
                "members": list      # 成员列表
            }
            如果配置文件不存在，返回默认配置
        """
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        """
        将当前团队配置保存到文件

        注意:
            - 配置以格式化 JSON 形式保存（带缩进）
            - 每次成员状态变更或新增成员时都会保存配置
        """
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        """
        根据名称查找团队成员

        参数:
            name: 成员名称

        返回:
            dict: 成员对象，如果未找到则返回 None
        """
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """
        创建（Spawn）一个新的团队成员

        参数:
            name: 成员名称，用于标识和通信
            role: 成员角色，描述其职责或专业领域
            prompt: 初始任务描述，将作为该成员的首条消息

        返回:
            str: 操作结果字符串

        逻辑说明：
        1. 如果成员已存在且状态为 idle 或 shutdown，则重新激活
        2. 如果成员已存在且状态为 working，则返回错误（成员正忙）
        3. 如果成员不存在，则创建新成员记录
        4. 创建并启动新线程运行该成员的代理循环
        """
        member = self._find_member(name)

        if member:
            # 成员已存在，检查其当前状态
            if member["status"] not in ("idle", "shutdown"):
                # 成员正忙，无法重新创建
                return f"Error: '{name}' is currently {member['status']}"
            # 重新激活成员：更新角色和状态
            member["status"] = "working"
            member["role"] = role
        else:
            # 新成员：创建成员记录
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)

        self._save_config()  # 保存更新后的配置

        # 创建并启动新线程运行成员代理循环
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,  # 设置为守护线程，主程序退出时自动终止
        )
        self.threads[name] = thread
        thread.start()

        return f"Spawned '{name}' (role: {role})"

    def _teammate_loop(self, name: str, role: str, prompt: str):
        """
        团队成员的代理循环，在独立线程中运行

        参数:
            name: 成员名称
            role: 成员角色
            prompt: 初始任务描述

        循环逻辑：
        1. 首先检查收件箱，读取所有待处理消息
        2. 调用 LLM 处理对话和工具调用
        3. 执行工具调用，更新消息历史
        4. 重复直到对话结束或达到最大轮数（50轮）

        线程安全：
        - 每个成员拥有独立的线程和消息历史
        - 通过 MessageBus 进行线程间通信（文件级别的 JSONL）
        """
        # 构建成员的系统提示词
        sys_prompt = (
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            f"Use send_message to communicate. Complete your task."
        )

        # 初始化消息历史，以初始任务描述作为首条用户消息
        messages = [{"role": "system", "content": sys_prompt}]
        messages.append({"role": "user", "content": prompt})

        # 获取该成员可用的工具列表
        tools = self._teammate_tools()

        llm_with_tools = create_llm_with_tools(tools)

        # 代理循环，最多执行 50 轮
        for _ in range(50):
            # 步骤1：检查收件箱，获取所有待处理消息
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                # 将每条消息作为用户消息添加到历史
                messages.append({"role": "user", "content": json.dumps(msg)})

            try:
                # 步骤2：调用 LLM 进行推理
                response = llm_with_tools.invoke(messages)
            except Exception:
                # LLM 调用失败，退出循环
                break

            # 将 LLM 响应添加到消息历史
            messages.append({"role": "assistant", "content": response.content})

            # 步骤3：如果 LLM 停止原因是工具调用，则执行工具
            if not hasattr(response, "tool_calls") or not response.tool_calls:
                # LLM 选择不调用工具，对话结束
                break
            # 步骤4：执行所有工具调用
            results = []
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_id = tool_call["id"]
                output = self._exec(name, tool_name, tool_args)
                print(f"  [{name}] {tool_name}: {str(output)[:120]}")
                results.append({
                    "type": "tool_result",
                    "tool_call_id": tool_id,
                    "content": str(output),
                })

            # 将工具执行结果作为用户消息添加回对话
            messages.append({"role": "user", "content": json.dumps(results)})

        # 循环结束，更新成员状态为 idle（除非已关闭）
        member = self._find_member(name)
        if member and member["status"] != "shutdown":
            member["status"] = "idle"
            self._save_config()

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        """
        执行工具调用的分发器

        参数:
            sender: 调用者的名称（用于 send_message 等需要标识发送者的工具）
            tool_name: 工具名称
            args: 工具参数字典

        返回:
            str: 工具执行结果的字符串表示

        支持的工具：
        - bash: 执行 Shell 命令
        - read_file: 读取文件内容
        - write_file: 写入文件内容
        - edit_file: 编辑文件（替换指定文本）
        - send_message: 发送消息给团队成员
        - read_inbox: 读取并清空自己的收件箱
        """
        # bash: 执行 Shell 命令
        if tool_name == "bash":
            return run_bash(args["command"])

        # read_file: 读取文件内容
        if tool_name == "read_file":
            return run_read(args["path"])

        # write_file: 写入文件内容
        if tool_name == "write_file":
            return run_write(args["path"], args["content"])

        # edit_file: 编辑文件（替换精确匹配的文本）
        if tool_name == "edit_file":
            return run_edit(args["path"], args["old_text"], args["new_text"])

        # send_message: 发送消息给团队成员
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))

        # read_inbox: 读取并清空自己的收件箱
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2)

        # 未知工具
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        """
        获取团队成员可用的工具列表

        返回:
            list: Anthropic 格式的工具定义列表

        说明:
            团队成员拥有受限的工具集，比主智能体权限更小。
            工具集包括基础的文件操作和团队通信工具。

        工具列表：
        - bash: 执行 Shell 命令
        - read_file: 读取文件内容
        - write_file: 写入文件内容
        - edit_file: 编辑文件（替换指定文本）
        - send_message: 发送消息给团队成员
        - read_inbox: 读取并清空自己的收件箱
        """
        return [
            {
                "name": "bash",
                "description": "Run a shell command.",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"]
                }
            },
            {
                "name": "read_file",
                "description": "Read file contents.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]
                }
            },
            {
                "name": "write_file",
                "description": "Write content to file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"}
                    },
                    "required": ["path", "content"]
                }
            },
            {
                "name": "edit_file",
                "description": "Replace exact text in file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"}
                    },
                    "required": ["path", "old_text", "new_text"]
                }
            },
            {
                "name": "send_message",
                "description": "Send message to a teammate.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "content": {"type": "string"},
                        "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}
                    },
                    "required": ["to", "content"]
                }
            },
            {
                "name": "read_inbox",
                "description": "Read and drain your inbox.",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
        ]

    def list_all(self) -> str:
        """
        列出所有团队成员及其状态

        返回:
            str: 格式化的成员列表字符串
        """
        if not self.config["members"]:
            return "No teammates."

        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        """
        获取所有团队成员的名称列表

        返回:
            list: 成员名称字符串列表
        """
        return [m["name"] for m in self.config["members"]]


TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / ".inbox"
BUS = MessageBus(INBOX_DIR)
TEAM = TeammateManager(TEAM_DIR)
