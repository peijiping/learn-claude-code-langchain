#!/usr/bin/env python3
"""
session_manage.py - 会话管理模块

提供对话历史的持久化存储和管理功能：
- 会话文件的创建、加载、切换
- 消息的序列化和反序列化
- 支持多个独立会话

使用方式：
    from session_manage import SessionManager
    
    manager = SessionManager(chat_history_dir, system_prompt)
    session_num, session_file, messages = manager.init_session()
"""

import json
from pathlib import Path
from typing import Optional

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage


class SessionManager:
    """会话管理器，负责对话历史的持久化存储和管理"""
    
    # 上下文窗口最大 token 数 (192K = 196608 tokens，与 API 限制一致)
    MAX_CONTEXT_TOKENS = 196608
    # 安全阈值：保留 5% 的缓冲空间，避免精确触顶
    SAFE_TOKEN_THRESHOLD = int(MAX_CONTEXT_TOKENS * 0.95)  # 约 186777 tokens
    
    # 工具消息压缩配置
    # 工具消息长度阈值，超过此值会被压缩（字符数）
    TOOL_MESSAGE_THRESHOLD = 2000
    # 保留最近的对话轮数，这些消息不会被压缩
    PRESERVE_RECENT_ROUNDS = 5
    
    def __init__(self, chat_history_dir: Path, system_prompt: str):
        """
        初始化会话管理器
        
        Args:
            chat_history_dir: 会话历史存储目录
            system_prompt: 系统提示词
        """
        self.chat_history_dir = chat_history_dir
        self.system_prompt = system_prompt
        self.chat_history_dir.mkdir(parents=True, exist_ok=True)
    
    def estimate_tokens(self, messages: list) -> int:
        """
        估算消息列表的 token 数量
        
        使用简单的估算方法：
        - 中文：每个字符约 1.5 tokens
        - 英文：每个单词约 1.3 tokens
        - 加上消息格式的开销（每条消息约 4 tokens）
        
        Args:
            messages: 消息列表
            
        Returns:
            估算的 token 数量
        """
        total_tokens = 0
        
        for msg in messages:
            # 消息格式开销
            total_tokens += 4
            
            content = ""
            if hasattr(msg, "content"):
                content = msg.content
                if isinstance(content, list):
                    # 处理多模态内容
                    content = json.dumps(content)
            elif isinstance(msg, dict):
                content = msg.get("content", "")
            
            if isinstance(content, str):
                # 简单估算：先统计中文字符和英文单词
                import re
                chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', content))
                english_words = len(re.findall(r'[a-zA-Z]+', content))
                other_chars = len(content) - chinese_chars - english_words
                
                # 中文约 1.5 tokens/字，英文约 1.3 tokens/词，其他字符约 0.5 tokens
                total_tokens += int(chinese_chars * 1.5 + english_words * 1.3 + other_chars * 0.5)
            else:
                # 非字符串内容按 JSON 长度估算
                total_tokens += len(json.dumps(content)) // 4
        
        return total_tokens
    
    def get_token_usage_percent(self, messages: list) -> float:
        """
        计算已使用 token 占上下文窗口的百分比
        
        Args:
            messages: 消息列表
            
        Returns:
            已使用百分比 (0-100)
        """
        tokens = self.estimate_tokens(messages)
        return min(100.0, (tokens / self.MAX_CONTEXT_TOKENS) * 100)
    
    def get_remaining_token_percent(self, messages: list) -> float:
        """
        计算剩余 token 占上下文窗口的百分比
        
        Args:
            messages: 消息列表
            
        Returns:
            剩余百分比 (0-100)，超过限制时返回 0
        """
        remaining = 100.0 - self.get_token_usage_percent(messages)
        return max(0.0, remaining)
    
    def trim_messages_to_limit(self, messages: list) -> list:
        """
        截断消息列表以适应上下文限制
        
        策略：
        1. 始终保留 SystemMessage（第一条）
        2. 从 oldest 开始删除，直到总 tokens 低于安全阈值
        3. 保留最近的消息（用户问题和 AI 回复）
        
        Args:
            messages: 原始消息列表
            
        Returns:
            截断后的消息列表
        """
        if not messages:
            return messages
        
        # 估算当前 tokens
        current_tokens = self.estimate_tokens(messages)
        
        # 如果低于安全阈值，无需截断
        if current_tokens <= self.SAFE_TOKEN_THRESHOLD:
            return messages
        
        print(f"\033[33m[上下文截断] 当前 {current_tokens} tokens，超过安全阈值 {self.SAFE_TOKEN_THRESHOLD}\033[0m")
        
        # 保留第一条（SystemMessage）和最后几条消息
        trimmed = [messages[0]]  # 保留系统提示
        
        # 从后往前保留消息，直到接近安全阈值
        # 策略：保留最近的用户输入和 AI 回复
        for msg in reversed(messages[1:]):
            temp_list = [trimmed[0], msg] + trimmed[1:]
            if self.estimate_tokens(temp_list) <= self.SAFE_TOKEN_THRESHOLD:
                trimmed.insert(1, msg)
            else:
                break
        
        final_tokens = self.estimate_tokens(trimmed)
        removed_count = len(messages) - len(trimmed)
        print(f"\033[33m[上下文截断] 已移除 {removed_count} 条旧消息，剩余 {len(trimmed)} 条，约 {final_tokens} tokens\033[0m")
        
        return trimmed
    
    def trim_messages_with_tool_compression(
        self,
        messages: list
    ) -> list:
        """
        截断消息列表以适应上下文限制，支持工具消息压缩
        
        策略：
        1. 始终保留 SystemMessage（第一条）
        2. 保留最新的 N 轮对话（不受压缩影响）
        3. 对滑动窗口内最早的、超过阈值的工具消息进行占位符替换
        4. 如果压缩后仍超过限制，则使用滑动窗口截断
        
        配置参数（类常量）：
        - TOOL_MESSAGE_THRESHOLD: 工具消息长度阈值，超过此值会被压缩
        - PRESERVE_RECENT_ROUNDS: 保留最近的对话轮数（不受压缩影响）
        
        Args:
            messages: 原始消息列表
            
        Returns:
            处理后的消息列表
        """
        if not messages:
            return messages
        
        # 估算当前 tokens
        current_tokens = self.estimate_tokens(messages)
        
        # 如果低于安全阈值，无需处理
        if current_tokens <= self.SAFE_TOKEN_THRESHOLD:
            return messages
        
        print(f"\033[33m[上下文压缩] 当前 {current_tokens} tokens，超过安全阈值 {self.SAFE_TOKEN_THRESHOLD}\033[0m")
        
        # 保留系统消息
        result = [messages[0]]
        remaining_messages = messages[1:].copy()
        
        # 识别对话轮次（一轮 = HumanMessage + AIMessage + 可选的 ToolMessages）
        # 从后往前计算轮次，确定需要保留的最近 N 轮
        rounds = []
        current_round = []
        
        for msg in reversed(remaining_messages):
            current_round.insert(0, msg)
            if isinstance(msg, HumanMessage):
                rounds.insert(0, current_round)
                current_round = []
        
        if current_round:
            rounds.insert(0, current_round)
        
        # 确定需要保留的最近 N 轮（这些不会被压缩）
        preserved_count = min(self.PRESERVE_RECENT_ROUNDS, len(rounds))
        preserved_messages = []
        for round_msgs in rounds[-preserved_count:]:
            preserved_messages.extend(round_msgs)
        
        # 可以被压缩的旧消息
        compressible_messages = []
        for round_msgs in rounds[:-preserved_count] if preserved_count > 0 else rounds:
            compressible_messages.extend(round_msgs)
        
        # 统计压缩信息
        compressed_count = 0
        saved_tokens = 0
        
        # 对可压缩区域内的工具消息进行占位符替换
        processed_compressible = []
        for msg in compressible_messages:
            if isinstance(msg, ToolMessage) and len(msg.content) > self.TOOL_MESSAGE_THRESHOLD:
                # 获取工具名称（从 tool_call_id 中提取或使用默认值）
                tool_name = msg.tool_call_id if msg.tool_call_id else "tool"
                placeholder = f"[{tool_name} 执行结果已压缩]"
                saved_tokens += self.estimate_tokens([msg]) - self.estimate_tokens([ToolMessage(content=placeholder, tool_call_id=msg.tool_call_id)])
                compressed_count += 1
                processed_compressible.append(ToolMessage(
                    content=placeholder,
                    tool_call_id=msg.tool_call_id
                ))
            else:
                processed_compressible.append(msg)
        
        # 合并处理后的消息：系统消息 + 压缩后的旧消息 + 保留的最近消息
        result = [messages[0]] + processed_compressible + preserved_messages
        
        compressed_tokens = self.estimate_tokens(result)
        
        if compressed_count > 0:
            print(f"\033[33m[上下文压缩] 已压缩 {compressed_count} 条工具消息，节省约 {saved_tokens} tokens\033[0m")
            print(f"\033[33m[上下文压缩] 压缩后共 {len(result)} 条消息，约 {compressed_tokens} tokens\033[0m")
        
        # 如果压缩后仍超过安全阈值，使用滑动窗口截断
        if compressed_tokens > self.SAFE_TOKEN_THRESHOLD:
            print(f"\033[33m[上下文压缩] 压缩后仍超过阈值，启用滑动窗口截断\033[0m")
            return self.trim_messages_to_limit(result)
        
        return result
    
    def get_latest_session(self) -> tuple[int, Optional[Path]]:
        """
        获取最新的会话编号和文件路径
        
        Returns:
            (会话编号, 会话文件路径) 如果没有会话文件则返回 (0, None)
        """
        session_files = list(self.chat_history_dir.glob("session_*.jsonl"))
        if not session_files:
            return 0, None
        
        max_num = 0
        for f in session_files:
            try:
                num = int(f.stem.replace("session_", ""))
                if num > max_num:
                    max_num = num
            except ValueError:
                continue
        
        if max_num == 0:
            return 0, None
        
        return max_num, self.chat_history_dir / f"session_{max_num}.jsonl"
    
    def get_session_file(self, session_num: int) -> Path:
        """
        根据会话编号获取会话文件路径
        
        Args:
            session_num: 会话编号
            
        Returns:
            会话文件路径
        """
        return self.chat_history_dir / f"session_{session_num}.jsonl"
    
    def load_session_history(self, session_file: Path) -> list:
        """
        从jsonl文件加载对话历史
        
        Args:
            session_file: 会话文件路径
            
        Returns:
            消息列表
        """
        messages = []
        if not session_file.exists():
            return messages
        
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    msg_data = json.loads(line)
                    msg_type = msg_data.get("type")
                    content = msg_data.get("content", "")
                    
                    if msg_type == "system":
                        messages.append(SystemMessage(content=content))
                    elif msg_type == "human":
                        messages.append(HumanMessage(content=content))
                    elif msg_type == "ai":
                        ai_msg = AIMessage(content=content)
                        if "tool_calls" in msg_data:
                            ai_msg.tool_calls = msg_data["tool_calls"]
                        messages.append(ai_msg)
                    elif msg_type == "tool":
                        messages.append(ToolMessage(
                            content=content,
                            tool_call_id=msg_data.get("tool_call_id", "")
                        ))
        except Exception as e:
            print(f"加载会话历史失败: {e}")
        
        return messages
    
    def append_message_to_session(self, session_file: Path, message) -> None:
        """
        向会话文件追加一条消息
        
        Args:
            session_file: 会话文件路径
            message: 消息对象 (SystemMessage/HumanMessage/AIMessage/ToolMessage)
        """
        msg_data = {}
        
        if isinstance(message, SystemMessage):
            msg_data["type"] = "system"
            msg_data["content"] = message.content
        elif isinstance(message, HumanMessage):
            msg_data["type"] = "human"
            msg_data["content"] = message.content
        elif isinstance(message, AIMessage):
            msg_data["type"] = "ai"
            msg_data["content"] = message.content
            if hasattr(message, "tool_calls") and message.tool_calls:
                msg_data["tool_calls"] = message.tool_calls
        elif isinstance(message, ToolMessage):
            msg_data["type"] = "tool"
            msg_data["content"] = message.content
            msg_data["tool_call_id"] = message.tool_call_id
        else:
            msg_data["type"] = "unknown"
            msg_data["content"] = str(message)
        
        try:
            with open(session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg_data, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"写入会话历史失败: {e}")
    
    def create_new_session(self) -> tuple[int, Path]:
        """
        创建新会话
        
        Returns:
            (新会话编号, 新会话文件路径)
        """
        max_num, _ = self.get_latest_session()
        new_num = max_num + 1
        new_file = self.get_session_file(new_num)
        new_file.touch()
        return new_num, new_file
    
    def init_session(self) -> tuple[int, Path, list]:
        """
        初始化会话：加载最后一次对话或创建新对话
        
        Returns:
            (会话编号, 会话文件路径, 消息列表)
        """
        max_num, session_file = self.get_latest_session()
        
        if session_file and session_file.exists():
            messages = self.load_session_history(session_file)
            if messages:
                print(f"已加载会话: session_{max_num}.jsonl ({len(messages)} 条消息)")
                return max_num, session_file, messages
        
        new_num, new_file = self.create_new_session()
        messages = [SystemMessage(content=self.system_prompt)]
        self.append_message_to_session(new_file, messages[0])
        print(f"已创建新会话: session_{new_num}.jsonl")
        return new_num, new_file, messages
    
    def switch_session(self, target_num: int) -> tuple[int, Path, list]:
        """
        切换到指定会话
        
        Args:
            target_num: 目标会话编号
            
        Returns:
            (会话编号, 会话文件路径, 消息列表)
            
        Raises:
            FileNotFoundError: 会话文件不存在
        """
        target_file = self.get_session_file(target_num)
        if not target_file.exists():
            raise FileNotFoundError(f"会话 session_{target_num}.jsonl 不存在")
        
        messages = self.load_session_history(target_file)
        return target_num, target_file, messages
    
    def list_sessions(self) -> list[tuple[int, Path, int]]:
        """
        列出所有会话
        
        Returns:
            [(会话编号, 会话文件路径, 消息数量), ...]
        """
        sessions = []
        session_files = list(self.chat_history_dir.glob("session_*.jsonl"))
        
        for f in session_files:
            try:
                num = int(f.stem.replace("session_", ""))
                with open(f, "r", encoding="utf-8") as file:
                    msg_count = sum(1 for line in file if line.strip())
                sessions.append((num, f, msg_count))
            except (ValueError, IOError):
                continue
        
        return sorted(sessions, key=lambda x: x[0])
    
    def clear_session(self, session_file: Path) -> int:
        """
        清空指定会话的历史消息
        
        清空会话文件内容，只保留系统提示词
        
        Args:
            session_file: 会话文件路径
            
        Returns:
            被删除的消息数量
        """
        if not session_file.exists():
            return 0
        
        # 加载当前会话，获取系统提示词
        messages = self.load_session_history(session_file)
        deleted_count = len(messages)
        
        # 清空文件并重新写入系统提示词
        try:
            with open(session_file, "w", encoding="utf-8") as f:
                # 写入系统提示词
                system_msg = SystemMessage(content=self.system_prompt)
                msg_data = {
                    "type": "system",
                    "content": system_msg.content
                }
                f.write(json.dumps(msg_data, ensure_ascii=False) + "\n")
            
            return deleted_count - 1  # 减去保留的系统提示词
        except Exception as e:
            print(f"清空会话失败: {e}")
            return 0
