class LoopDetector:
    """死循环检测器 — 通过连续重复调用指纹检测无限循环。

    Agent 和 WorkerAgent 共用，消除重复实现。
    """

    def __init__(self, threshold: int = 3):
        self._threshold = threshold
        self._last = None
        self._streak = 0

    def check(self, tool_name: str, args_str: str) -> bool:
        """检查本次调用是否触发死循环熔断。返回 True 表示应终止。"""
        fingerprint = f"{tool_name}:{args_str}"
        if fingerprint == self._last:
            self._streak += 1
        else:
            self._streak = 1
        self._last = fingerprint
        return self._streak >= self._threshold

    @property
    def streak(self) -> int:
        return self._streak

    def reset(self):
        self._last = None
        self._streak = 0

    def msg(self, tool_name: str, worker_name: str = "") -> str:
        """生成死循环终止消息。"""
        if worker_name:
            print(f"  🔄 [{worker_name}] 死循环: {tool_name} x{self._streak}")
        return (
            f"死循环终止: {tool_name} "
            f"连续重复 {self._streak} 次"
        )

    def warning(self, tool_name: str) -> str:
        """生成用户可见的警告消息。"""
        return f"🔄 检测到工具 `{tool_name}` 连续重复调用 {self._streak} 次，已自动终止以防止死循环"