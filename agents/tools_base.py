#!/usr/bin/env python3

"""
tools_base.py - 基础工具函数模块

本模块提供基础的文件操作和系统调用工具函数，被 tools.py 和 teammate_manager.py 等模块共用。
设计理念：避免代码重复，集中管理基础工具函数。

主要功能：
1. 路径安全验证 - 防止路径遍历攻击，确保所有操作都在工作目录内
2. Bash命令执行 - 安全地执行shell命令，支持超时控制
3. 文件读取 - 带限制的行数读取功能
4. 文件写入 - 自动创建父目录，支持覆盖写入
5. 文件编辑 - 支持文本替换（仅替换第一处匹配）
6. 团队协作协议处理 - 管理关闭请求和计划审批等团队交互
"""

import os
import subprocess
from pathlib import Path


# =============================================================================
# 全局常量和工作目录配置
# =============================================================================

# 工作目录：所有文件操作都限制在此目录内，防止误操作系统关键文件
# Path.cwd() 获取当前工作目录，/ "WorkSpace" 拼接子目录
WORKDIR = Path.cwd() / "WorkSpace"





# =============================================================================
# 路径安全验证函数
# =============================================================================

def safe_path(p: str) -> Path:
    """
    验证路径是否在工作目录内，防止路径遍历攻击

    安全机制：
    - 将相对路径与工作目录拼接后转换为绝对路径
    - 检查最终路径是否仍然在 WORKDIR 内
    - 如果路径逃逸到 WORKDIR 之外，抛出 ValueError

    参数：
        p: 相对路径字符串

    返回：
        验证通过后的绝对路径(Path对象)

    异常：
        ValueError: 当路径试图逃逸到工作目录之外时抛出
                     例如：p = "../../etc/passwd" 会被拒绝
    """
    # 拼接工作目录和输入路径，并解析为绝对路径
    # .resolve() 会解析符号链接并返回绝对路径
    path = (WORKDIR / p).resolve()

    # is_relative_to() 检查 path 是否在 WORKDIR 的子目录中
    # 如果 path 是 "/etc/passwd" 或 "../other_dir" 等外部路径，则拒绝
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")

    return path


# =============================================================================
# Bash命令执行函数
# =============================================================================

def run_bash(command: str) -> str:
    """
    执行shell命令并返回结果

    安全特性：
    - 危险命令黑名单检查：禁止 rm -rf /, sudo, shutdown, reboot 等高危操作
    - 超时保护：命令执行超过120秒会自动终止
    - 输出截断：结果最多返回50000字符，防止内存溢出

    参数：
        command: 要执行的shell命令字符串

    返回：
        命令成功：返回标准输出+标准错误的合并内容（最多50000字符）
        命令失败：返回格式 "Error: command failed with return code X\\n错误信息"
        超时：返回 "Error: Timeout (120s)"
        危险命令：返回 "Error: Dangerous command blocked"
    """
    # 危险命令黑名单：这些命令可能被误用造成系统损坏
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

    # 执行黑名单检查，防止危险操作
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        # 使用 subprocess.run 执行命令
        # shell=True: 使用shell解释器执行，适合复杂命令
        # capture_output=True: 捕获stdout和stderr
        # text=True: 返回字符串而非字节
        # timeout=120: 120秒超时保护
        r = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),  # 在当前工作目录执行
            capture_output=True,
            text=True,
            timeout=120
        )

        # 合并stdout和stderr输出
        out = (r.stdout + r.stderr).strip()

        # 检查返回码，非0表示命令执行失败
        if r.returncode != 0:
            return f"Error: command failed with return code {r.returncode}\n{out}"

        # 成功执行：返回输出内容，截断至50000字符
        # 如果没有输出，返回成功提示
        return out[:50000] if out else "(command executed successfully, no output)"

    except subprocess.TimeoutExpired:
        # 命令执行超时（超过120秒）
        return "Error: Timeout (120s)"


# =============================================================================
# 文件读取函数
# =============================================================================

def run_read(path: str, limit: int = None) -> str:
    """
    读取文件内容

    功能特性：
    - 使用 safe_path 进行安全路径验证
    - 支持行数限制：只读取前limit行，避免大文件撑爆内存
    - 当文件被截断时，显示剩余行数提示
    - 自动截断超长内容至50000字符

    参数：
        path: 要读取的文件路径（相对路径）
        limit: 可选，限制读取的行数。默认None表示读取全部

    返回：
        成功：文件内容字符串（可能被截断）
        失败：格式 "Error: {异常信息}"
    """
    try:
        # 使用安全路径读取文件
        text = safe_path(path).read_text()

        # 按行分割文本
        lines = text.splitlines()

        # 如果设置了limit且文件行数超过limit
        if limit and limit < len(lines):
            # 截取前limit行，并添加提示信息
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]

        # 合并行，截断至50000字符
        return "\n".join(lines)[:50000]

    except Exception as e:
        # 捕获所有异常（文件不存在、权限不足等）
        return f"Error: {e}"


# =============================================================================
# 文件写入函数
# =============================================================================

def run_write(path: str, content: str) -> str:
    """
    写入内容到文件

    功能特性：
    - 使用 safe_path 进行安全路径验证
    - 自动创建父目录：如果父目录不存在会递归创建
    - 覆盖写入：目标文件已存在会被覆盖
    - 返回写入字节数，便于验证

    参数：
        path: 要写入的文件路径（相对路径）
        content: 要写入的内容字符串

    返回：
        成功：格式 "Wrote {字节数} bytes to {路径}"
        失败：格式 "Error: {异常信息}"
    """
    try:
        fp = safe_path(path)

        # 自动创建父目录
        # parents=True: 递归创建所有不存在的父目录
        # exist_ok=True: 如果目录已存在不报错
        fp.parent.mkdir(parents=True, exist_ok=True)

        # 写入内容（覆盖模式）
        fp.write_text(content)

        return f"Wrote {len(content)} bytes to {path}"

    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# 文件编辑函数
# =============================================================================



def run_edit(path: str, old_text: str, new_text: str) -> str:
    """
    替换文件中的指定文本

    功能特性：
    - 使用 safe_path 进行安全路径验证
    - 精确替换：只替换第一处匹配（使用 count=1）
    - 先检查再写入：验证old_text存在后才执行替换
    - 原子性保证：读取和写入之间可能存在竞态条件

    参数：
        path: 要编辑的文件路径（相对路径）
        old_text: 要被替换的原文本（必须是完整的连续字符串）
        new_text: 替换后的新文本

    返回：
        成功：格式 "Edited {路径}"
        失败（文本未找到）：格式 "Error: Text not found in {路径}"
        失败（其他）：格式 "Error: {异常信息}"
    """
    try:
        fp = safe_path(path)

        # 读取文件全部内容
        content = fp.read_text()

        # 检查要替换的文本是否存在于文件中
        if old_text not in content:
            return f"Error: Text not found in {path}"

        # 执行替换：只替换第一处匹配
        # replace(old_text, new_text, 1) 中的 1 表示只替换一次
        fp.write_text(content.replace(old_text, new_text, 1))

        return f"Edited {path}"

    except Exception as e:
        return f"Error: {e}"


