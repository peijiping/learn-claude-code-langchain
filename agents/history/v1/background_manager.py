import subprocess
import threading
import uuid


# -- BackgroundManager: 线程执行 + 通知队列 --
class BackgroundManager:
    """
    后台任务管理器，支持在独立线程中执行命令，
    并通过通知队列在任务完成时传递结果。
    """

    def __init__(self):
        """
        初始化后台任务管理器。
        - tasks: 存储所有任务的状态信息，key 为 task_id
        - _notification_queue: 存放已完成任务的通知队列，供主线程获取
        - _lock: 线程锁，保护共享数据结构
        """
        self.tasks = {}  # task_id -> {status, result, command}
        self._notification_queue = []  # completed task results
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        """
        启动一个后台线程来执行命令，立即返回 task_id。

        Args:
            command: 要执行的 shell 命令

        Returns:
            包含 task_id 和命令信息的字符串
        """
        # 生成一个短 UUID 作为任务 ID
        task_id = str(uuid.uuid4())[:8]
        # 初始化任务状态为 running
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        # 创建守护线程，在后台执行 _execute 方法
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return f"Background task {task_id} started: {command[:80]}"

    def _execute(self, task_id: str, command: str):
        """
        线程执行目标函数：运行子进程，捕获输出，结果放入通知队列。

        Args:
            task_id: 任务唯一标识符
            command: 要执行的 shell 命令
        """
        try:
            # 执行命令，设置工作目录、超时时间和输出捕获
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            # 合并标准输出和标准错误，最多保留 50000 字符
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            # 命令执行超时（300秒）
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as e:
            # 其他执行异常
            output = f"Error: {e}"
            status = "error"

        # 更新任务状态和结果
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"

        # 将完成通知放入队列（带线程锁保护）
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "(no output)")[:500],
            })

    def check(self, task_id: str = None) -> str:
        """
        查询任务状态。

        Args:
            task_id: 要查询的任务 ID，如果为 None 则列出所有任务

        Returns:
            任务状态信息字符串
        """
        if task_id:
            # 查询指定任务
            t = self.tasks.get(task_id)
            if not t:
                return f"Error: Unknown task {task_id}"
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"
        else:
            # 列出所有任务
            lines = []
            for tid, t in self.tasks.items():
                lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
            return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self) -> list:
        """
        取出并清空所有待处理的通知。

        Returns:
            所有已完成任务的通知列表

        Note:
            此方法在每次 LLM 调用前被调用，以确保将后台任务的结果
            注入到 agent 的上下文中，实现"即发即忘"模式。
        """
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs
