"""Thread-safe task budget shared by Planner, Workers, and Aggregator."""

from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class BudgetSnapshot:
    max_steps: int
    max_tokens: int
    used_steps: int
    used_tokens: int

    @property
    def remaining_steps(self) -> int:
        return max(0, self.max_steps - self.used_steps)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.used_tokens)


class ExecutionBudget:
    """Accumulate real usage without coupling callers to Ledger storage."""

    def __init__(self, max_steps: int, max_tokens: int):
        self.max_steps = max(1, int(max_steps))
        self.max_tokens = max(1, int(max_tokens))
        self._used_steps = 0
        self._used_tokens = 0
        self._by_role: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(
                self.max_steps, self.max_tokens,
                self._used_steps, self._used_tokens,
            )

    def consume(self, role: str, *, steps: int = 0, tokens: int = 0):
        steps = max(0, int(steps or 0))
        tokens = max(0, int(tokens or 0))
        with self._lock:
            self._used_steps += steps
            self._used_tokens += tokens
            usage = self._by_role.setdefault(role, {"steps": 0, "tokens": 0})
            usage["steps"] += steps
            usage["tokens"] += tokens
        return self.snapshot()

    def can_start(self, *, steps: int = 1, tokens: int = 1) -> bool:
        current = self.snapshot()
        return (
            current.remaining_steps >= max(0, steps)
            and current.remaining_tokens >= max(0, tokens)
        )

    def worker_share(self, batch_size: int,
                     reserve_steps: int = 1) -> tuple[int, int]:
        current = self.snapshot()
        count = max(1, int(batch_size))
        steps = max(0, current.remaining_steps - max(0, reserve_steps)) // count
        tokens = current.remaining_tokens // count
        return steps, tokens

    def usage_by_role(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {name: dict(value) for name, value in self._by_role.items()}
