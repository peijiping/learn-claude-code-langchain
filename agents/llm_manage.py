#!/usr/bin/env python3
"""
llm_manage.py - 大模型管理模块

统一管理大模型的初始化和配置，供其他模块引用。
"""

import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

# 加载环境变量
load_dotenv(override=True)

BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("OPENAI_MODEL_ID", "")

# 检查是否配置了模型参数
if not MODEL or not API_KEY or not BASE_URL:
    raise ValueError("请配置 OPENAI_MODEL_ID、OPENAI_API_KEY、OPENAI_BASE_URL 环境变量")


def create_llm(
    model: str = MODEL,
    api_key: str = API_KEY,
    base_url: str = BASE_URL,
    temperature: float = 0.0,
    max_tokens: int = 8000,
) -> ChatOpenAI:
    """
    创建并返回一个 ChatOpenAI 实例。

    Args:
        model: 模型ID，默认从环境变量读取
        api_key: API密钥，默认从环境变量读取
        base_url: API基础URL，默认从环境变量读取
        temperature: 温度参数，控制输出的随机性
        max_tokens: 最大生成token数

    Returns:
        ChatOpenAI 实例
    """
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def create_llm_with_tools(tools, **kwargs) -> ChatOpenAI:
    """
    创建一个绑定了工具的 ChatOpenAI 实例。

    bind_tools() 是 LangChain 的核心方法，它会让大模型：
      - 知道有哪些工具可用
      - 学会在合适的场景下调用工具
      - 返回工具调用请求而不是普通文本

    Args:
        tools: 工具列表
        **kwargs: 传递给 create_llm 的其他参数

    Returns:
        绑定了工具的 ChatOpenAI 实例
    """
    llm = create_llm(**kwargs)
    return llm.bind_tools(tools)


# 默认的 LLM 实例（向后兼容）
llm = create_llm()
