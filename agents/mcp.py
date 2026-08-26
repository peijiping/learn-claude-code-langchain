#!/usr/bin/env python3
"""
mcp.py - MCPManager（MCP 插件接入）

整合自 s19 课程（MCP plugin / dynamic tool pool），并以「类」方式重构进自有代码库。

核心概念（与 s19 一致）：
- MCPClient：单个 MCP 服务器的客户端（持有工具定义 + 处理函数），按名委托调用。
- MCPManager：管理「已连接服务器集合」，负责 connect（发现工具）、组装 OpenAI 格式
  工具池（mcp__{server}__{tool} 命名空间前缀，避免与内置工具重名）、以及委托调用。

与 s19 的三处关键差异：
1. **类化**：s19 用模块级 `mcp_clients` 全局 dict + 模块级 `connect_mcp` / `assemble_tool_pool`；
   这里收敛为 `MCPManager` 实例（多 Agent 实例各持一份，与 s17/s18 holder 注入一致）。
2. **OpenAI 格式转换**：项目用 `ChatOpenAI.bind_tools()`，工具定义是
   `type:"function"` + `function.parameters`；把课程里的 Anthropic `inputSchema` 转成 `parameters`。
3. **启动自动加载**：与 s19「先用 connect_mcp 惰性发现」不同，这里在 Agent 启动时用
   `connect_all()` 把 `_registry` 里**所有已配置服务器**自动连接，工具从一开始就出现在
   工具池里（LLM 首轮即可直接用，无需手动 connect）。`assemble_tools/handlers` 仍每轮
   现场组装（非缓存），保证连接状态变化后立即反映——兼容 s19「动态工具池」的免缓存思想，
   只是把"连接时机"从「模型按需 connect」调整成「启动即连」。
   `connect_mcp` / `list_mcp` 保留作运行时「追加连接 / 查询目录」的补充入口。

MCP 是 Lead（主智能体）级动态能力：子智能体不暴露 connect_mcp 与 mcp__* 工具。
"""

import re


# 名称规范化正则：仅保留 [a-zA-Z0-9_-]，其余替换为下划线
_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


def normalize_mcp_name(name: str) -> str:
    """把名称中所有非 [a-zA-Z0-9_-] 字符替换为下划线，保证工具名合法唯一。"""
    return _DISALLOWED_CHARS.sub('_', name)


class MCPClient:
    """单个 MCP 服务器的客户端：注册工具定义与处理函数，按名委托调用。

    真实系统中 MCPClient 通常走 MCP 协议与外部进程通信；这里是教学用 mock 实现，
    工具定义与处理函数在 `register` 时一次性注册。
    """

    def __init__(self, name: str):
        self.name = name                    # MCP 服务器名称（如 "docs"/"deploy"）
        self.tools: list[dict] = []         # 该服务器暴露的工具定义列表（含 inputSchema）
        self._handlers: dict[str, callable] = {}  # 工具名 → 实际处理函数

    def register(self, tool_defs: list[dict], handlers: dict[str, callable]) -> None:
        """注册该服务器的工具定义与对应的处理函数。"""
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        """按工具名调用处理函数；异常统一兜底，返回错误字符串。"""
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(**args)
        except Exception as e:
            return f"MCP error: {e}"


# ── 可连接的 mock MCP 服务器（教学用，与 s19 保持一致）─────────────

def _mock_server_docs():
    """构造名为 'docs' 的 mock MCP 服务器（只读型文档工具）。"""
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {"name": "search", "description": "Search documentation. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "get_version", "description": "Get API version. (readOnly)",
             "inputSchema": {"type": "object", "properties": {},
                             "required": []}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return client


def _mock_server_deploy():
    """构造名为 'deploy' 的 mock MCP 服务器（含破坏性工具 trigger，真实系统中需二次审批）。"""
    client = MCPClient("deploy")
    client.register(
        tool_defs=[
            {"name": "trigger",
             "description": "Trigger a deployment. (destructive — requires approval in real CC)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
            {"name": "status", "description": "Check deployment status. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        })
    return client


# 默认 MCP 服务器注册表（服务器名 → 工厂函数）；允许通过构造参数注入自定义注册表。
_MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


class MCPManager:
    """管理已连接的 MCP 服务器集合：连接发现、组装工具池、委托调用。

    职责（纯 MCP，不感知任务/agent）：
    - connect：连接一个服务器并发现其工具（已连接/未知名/server unknown 均给出提示）
    - assemble_tools：把所有已连接服务器工具转成 OpenAI 格式（mcp__{server}__{tool}）
    - assemble_handlers：`mcp__{server}__{tool}` → 委托 client.call_tool 的闭包

    assemble_tools / assemble_handlers 每轮现场组装（不缓存），保证 connect 后立即生效。
    MCP 工具命名带命名空间前缀 mcp__{server}__{tool}，避免与内置工具重名。
    """

    def __init__(self, servers: dict[str, callable] | None = None):
        # _clients 与 _registry 的区别：
        # - _registry：可连接清单（“菜单”），name → 工厂函数，调用后才产出 MCPClient 实例，
        #   描述“潜在可连接能力”（available_servers / Unknown 提示都依赖它）。
        # - _clients：已连接会话（“已点的菜”），name → 已实例化的 MCPClient 客户端对象，
        #   描述“当前活动状态”（assembled tools 按它遍历）。是否 connect 过由运行时决定。
        self._clients: dict[str, MCPClient] = {}   # 已连接：name → 实例（当前已连，易变）
        self._registry = servers if servers is not None else _MOCK_SERVERS  # 可连接：name → 工厂函数（固定）

    # ── 查询 ───────────────────────────────────────────────

    def available_servers(self) -> list[str]:
        """返回可连接的服务器名称列表（读 _registry，含尚未 connect 的）。"""
        return list(self._registry.keys())

    def connected_names(self) -> list[str]:
        """返回当前已连接的服务器名称列表（读 _clients，仅是已 connect 的）。"""
        return list(self._clients.keys())

    def catalog(self) -> dict[str, list[str]]:
        """返回「服务器 → 工具名列表」的可连接目录（读 _registry，不持久化连接）。

        通过临时实例化各工厂函数读取其暴露的工具名，仅用于给 LLM 展示可连接能力
        菜单（list_mcp / connect_mcp 的目录文本）。不写入 _clients、不改变连接状态。
        """
        catalog_: dict[str, list[str]] = {}
        for name, factory in self._registry.items():
            try:
                tool_names = [t["name"] for t in factory().tools]
            except Exception:
                tool_names = []
            catalog_[name] = tool_names
        return catalog_

    def catalog_text(self) -> str:
        """把可连接目录格式化为人类/LLM 可读文本（每行一个服务器 + 其工具名）。

        例如：\\n  - docs: search, get_version\\n  - deploy: trigger, status
        """
        lines = []
        for name, tool_names in self.catalog().items():
            names = ", ".join(tool_names) if tool_names else "(no tools)"
            lines.append(f"  - {name}: {names}")
        return "\n".join(lines)

    # ── 连接与发现 ─────────────────────────────────────────

    def connect(self, name: str) -> str:
        """连接一个 MCP 服务器，发现其工具并登记到已连接集合。

        返回提示信息：已连接 / 未知名服务器（列出可选）/ 连接成功（列出工具名）。
        """
        # 1) 幂等保护：如果这个名字已经在「已连接的集合」里，说明之前连过了，
        #    直接提示"已连接"并返回，避免重复实例化同一个服务器客户端。
        if name in self._clients:
            return f"MCP server '{name}' already connected"

        # 2) 查「可连接清单」：_registry 记录了"我能连哪些服务器"以及每个服务器
        #    对应的"工厂函数"（factory 就是用来创建该服务器客户端的构造函数）。
        #    这里用 name 去清单里找工厂函数；找不到说明这是个未注册的名字。
        factory = self._registry.get(name)
        if not factory:
            # 名字不合法：把当前所有可选的服务器名拼成字符串，提示给上层/模型，
            #    方便它拿到正确名字后重试。
            available = ", ".join(self._registry.keys())
            return f"Unknown server '{name}'. Available: {available}"

        # 3) 实例化并登记：调用工厂函数真正"连上"这个服务器——产出该服务器的
        #    客户端对象 mcp_client，其中已带好它暴露的工具定义和调用函数。
        mcp_client = factory()
        # 把连上的客户端存进 _clients（name -> 客户端实例），表示"已连接"。
        # 后面 assemble_tools / assemble_handlers 都遍历这个 dict 来出工具池。
        self._clients[name] = mcp_client

        # 4) 汇总信息：从客户端里取出它暴露的所有工具名，方便展示本次发现了哪些工具。
        tool_names = [t["name"] for t in mcp_client.tools]
        # 终端打一条红色日志（装饰用），仅做视觉提示、帮助开发者肉眼确认"连上了"。
        print(f"  \033[31m[mcp] connected: {name} → {tool_names}\033[0m")
        # 5) 返回给大模型一段自然语言结果，它会看到连接成功并列出发现的具体工具名，
        #    下一轮就能用 mcp__{server}__{tool} 调用它们了。
        return (f"Connected to MCP server '{name}'. "
                f"Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}")

    def connect_all(self) -> int:
        """启动自动加载：把 `_registry` 里所有已配置服务器一次性连接起来。

        Agent 启动时调用（见 agent_full_v2.py），让已配置的 MCP 工具从一开始就
        出现在工具池里（免去模型手动 connect）。`_clients` 为空或个别服务器未知时
        自动跳过，返回成功连接的数量。
        """
        count = 0
        for name in list(self._registry.keys()):
            result = self.connect(name)
            if not result.startswith("Unknown server"):
                count += 1
        return count

    # ── 工具池组装（每轮现场组装，非缓存）──────────────────────

    def assemble_tools(self) -> list[dict]:
        """把所有已连接服务器的工具定义转成 OpenAI 格式工具池。

        每个工具命名为 `mcp__{server}__{tool}`（两端均过 normalize_mcp_name），
        返回 `{"type":"function","function":{name,description,parameters}}` 结构，
        `parameters` 由课程里的 Anthropic `inputSchema` 直接复用。
        """
        tools: list[dict] = []
        for server_name, mcp_client in self._clients.items():
            safe_server = normalize_mcp_name(server_name)
            for tool_def in mcp_client.tools:
                safe_tool = normalize_mcp_name(tool_def["name"])
                prefixed = f"mcp__{safe_server}__{safe_tool}"
                tools.append({
                    "type": "function",
                    "function": {
                        "name": prefixed,
                        "description": tool_def.get("description", ""),
                        "parameters": tool_def.get("inputSchema", {}),
                    },
                })
        return tools

    def assemble_handlers(self) -> dict[str, callable]:
        """返回 `mcp__{server}__{tool}` → 闭包的映射，委托给对应客户端的 call_tool。"""
        handlers: dict[str, callable] = {}
        for server_name, mcp_client in self._clients.items():
            safe_server = normalize_mcp_name(server_name)
            for tool_def in mcp_client.tools:
                safe_tool = normalize_mcp_name(tool_def["name"])
                prefixed = f"mcp__{safe_server}__{safe_tool}"
                # 用闭包绑定当前客户端与工具名，委托给该客户端的 call_tool
                handlers[prefixed] = (
                    lambda *, c=mcp_client, t=tool_def["name"], **kw: c.call_tool(t, kw))
        return handlers