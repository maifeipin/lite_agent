"""Thread-safe task budget shared by Planner, Workers, and Aggregator."""

from dataclasses import dataclass
import threading
import json
import math
import uuid


class BudgetAdmissionError(RuntimeError):
    """A request cannot fit the uncommitted task allowance."""


def estimate_input_tokens(messages, tools=None, margin=1.25):
    # UTF-8 bytes are a conservative fallback across languages/tokenizers.
    # Includes call arguments, tool schemas and protocol overhead, not just text.
    wire = json.dumps([messages, tools or []], ensure_ascii=False, default=str)
    return max(1, math.ceil((len(wire.encode('utf-8')) + 128) * margin))


@dataclass(frozen=True)
class BudgetSnapshot:
    max_steps: int
    max_tokens: int
    used_steps: int
    used_tokens: int
    reserved_steps: int = 0
    reserved_tokens: int = 0
    estimated_tokens: int = 0

    @property
    def remaining_steps(self) -> int:
        return max(0, self.max_steps - self.used_steps - self.reserved_steps)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.used_tokens - self.reserved_tokens)


class ExecutionBudget:
    """Accumulate real usage without coupling callers to Ledger storage."""

    def __init__(self, max_steps: int, max_tokens: int):
        self.max_steps = max(1, int(max_steps))
        self.max_tokens = max(1, int(max_tokens))
        self._used_steps = 0
        self._used_tokens = 0
        self._by_role: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()
        self._reservations = {}
        self._estimated_tokens = 0

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(
                self.max_steps, self.max_tokens,
                self._used_steps, self._used_tokens,
                sum(r[1] for r in self._reservations.values()),
                sum(r[2] for r in self._reservations.values()),
                self._estimated_tokens,
            )

    def reserve(self, role, estimated_tokens, steps=1):
        steps, tokens = max(0, int(steps)), max(0, int(estimated_tokens))
        with self._lock:
            held_steps = sum(r[1] for r in self._reservations.values())
            held_tokens = sum(r[2] for r in self._reservations.values())
            if (self._used_steps + held_steps + steps > self.max_steps or
                    self._used_tokens + held_tokens + tokens > self.max_tokens):
                raise BudgetAdmissionError('请求预计用量超过剩余预算，未调用模型')
            key = uuid.uuid4().hex
            self._reservations[key] = (role, steps, tokens)
            return key

    def settle(self, reservation, actual_usage=None):
        # Unknown usage (network error / missing provider usage) is charged at
        # the reservation estimate rather than silently treated as free.
        with self._lock:
            role, steps, estimate = self._reservations.pop(reservation)
            tokens = estimate if actual_usage is None else max(0, int(actual_usage))
            if actual_usage is None:
                self._estimated_tokens += tokens
            self._used_steps += steps
            self._used_tokens += tokens
            usage = self._by_role.setdefault(role, {'steps': 0, 'tokens': 0})
            usage['steps'] += steps
            usage['tokens'] += tokens
        return self.snapshot()

    def release(self, reservation):
        with self._lock:
            self._reservations.pop(reservation, None)

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
                     reserve_steps: int = 1, pending_workers: int = None,
                     reserve_tokens: int = 0) -> tuple[int, int]:
        current = self.snapshot()
        count = max(1, int(pending_workers or batch_size))
        steps = max(0, current.remaining_steps - max(0, reserve_steps)) // count
        tokens = max(0, current.remaining_tokens - reserve_tokens) // count
        return steps, tokens

    def usage_by_role(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {name: dict(value) for name, value in self._by_role.items()}
