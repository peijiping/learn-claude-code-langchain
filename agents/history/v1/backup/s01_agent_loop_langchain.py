#!/usr/bin/env python3
"""
s01_agent_loop_langchain.py - 使用 LangChain 实现的 Agent Loop

支持两种模式：
1. Anthropic 官方模型（使用 langchain-anthropic）
2. OpenAI 兼容 API 的模型（如 MiniMax-M2.5，使用 langchain-openai）

配置方式：
- 使用 .env 文件配置，参考 .env.example
"""

import json
import os
import subprocess

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_function

from dotenv import load_dotenv

# 加载环境变量
load_dotenv(override=True)

BASE_URL = os.environ.get("OPENAI_BASE_URL","")
API_KEY = os.environ.get("OPENAI_API_KEY","")
MODEL = os.environ.get("OPENAI_MODEL_ID", "")

# 系统prompt
SYSTEM = content=os.environ.get("SYSTEM_MESSAGE", "")

# 检查是否配置了模型 ID
if not MODEL or not SYSTEM or not API_KEY or not BASE_URL:
    raise ValueError("请配置 OPENAI_MODEL_ID、SYSTEM_MESSAGE、OPENAI_API_KEY、OPENAI_BASE_URL 环境变量")
#初始化大模型
llm = ChatOpenAI(
    model=MODEL,
    api_key=API_KEY,
    base_url=BASE_URL,
    temperature=0.0,
    max_tokens=8000,
)



# ============================================================
# 工具定义区域
# ============================================================

# 第1步：定义工具的 JSON Schema（描述工具的名称、参数等）
# 这里定义了一个名为 "bash" 的工具
TOOLS = [{
    "name": "bash",                                    # 工具名称，大模型会返回这个名称
    "description": "Run a shell command.",            # 工具描述，让大模型知道什么场景用这个工具
    "input_schema": {                                 # 工具参数的 JSON Schema
        "type": "object",
        "properties": {
            "command": {"type": "string"}             # 参数 "command"，类型为字符串
        },
        "required": ["command"],                      # 必须提供的参数
    },
}]

# 辅助函数：将多个工具转换为 LangChain 格式
def convert_to_langchain_tools(tools: list):
    """将 tools 转换为 LangChain 格式"""
    return [convert_to_openai_function(tool) for tool in tools]

# 第2步：将工具 JSON Schema 转换为 LangChain 内部格式
# convert_to_openai_function 是 LangChain 提供的工具，将我们的工具定义转为标准格式
tool_schemas = convert_to_langchain_tools(TOOLS)

# 第3步：将工具绑定到大模型
# bind_tools() 是 LangChain 的核心方法，它会让大模型：
#   - 知道有哪些工具可用
#   - 学会在合适的场景下调用工具
#   - 返回工具调用请求而不是普通文本
llm_with_tools = llm.bind_tools(tool_schemas)


# ============================================================
# 工具函数实现区域
# ============================================================

# 第4步：实现具体的工具函数
# 这个函数会在收到大模型的工具调用请求后执行
def run_bash(command: str) -> str:
    """执行 shell 命令并返回结果"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                          capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            return f"Error: command failed with return code {r.returncode}\n{out}"
        return out[:50000] if out else "(command executed successfully, no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


# 第5步：建立工具名称到函数的映射
# 当大模型返回工具调用请求时，我们需要根据工具名找到对应的函数来执行
TOOL_HANDLERS = {
    "bash": run_bash,   # "bash" -> run_bash 函数
}




#执行主体
def agent_loop(history_messages: list):
    while True:
        
        llm_response = llm_with_tools.invoke(history_messages)
        # 加入大模型回复到历史消息中
        history_messages.append(llm_response)

        if not hasattr(llm_response, "tool_calls") or not llm_response.tool_calls:
            return

        #此处用循环是因为大模型可能一次调用多个工具，每个工具都需要单独执行，每个工具的执行结果都需要加入到results中
        tool_call_results = []
        for tool_call in llm_response.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_id = tool_call["id"]
            print("-" * 20)
            print(tool_call)
            print("----------")
            if tool_name in TOOL_HANDLERS:
                # 调用对应的工具函数来执行工具
                tool_output = TOOL_HANDLERS[tool_name](**tool_args)
            else:
                tool_output = f"Error: Unknown tool {tool_name}"
            
            print(f"工具 {tool_name} 执行结果: {tool_output}")
            print("-" * 20)
            tool_call_results.append({
                "type": "tool_result",
                "tool_name": tool_name,
                "tool_args": tool_args,
                "tool_id": tool_id,
                "tool_output": tool_output,
            })

        # 加入工具执行结果到历史消息中
        history_messages.append(HumanMessage(content=json.dumps(tool_call_results)))


def main():
    # 初始化历史消息列表，加入system prompt
    history_messages = []
    history_messages.append(SystemMessage(content=SYSTEM))
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history_messages.append(HumanMessage(content=query))
        # 执行主agent循环，直到大模型不再请求工具调用
        agent_loop(history_messages)
        response_content = history_messages[-1].content
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        else:
            print(response_content)
        print()


if __name__ == "__main__":
    main()
