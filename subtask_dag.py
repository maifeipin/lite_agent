from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SubtaskType(Enum):
    TEXT = "text"
    CODE = "code"
    MULTIMODAL = "multimodal"
    COMPLEX_REASONING = "complex_reasoning"
    DATA_ANALYSIS = "data_analysis"


class SubtaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Subtask:
    id: str
    name: str
    type: SubtaskType = SubtaskType.TEXT
    prompt: str = ""
    depends_on: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    assigned_model: str = ""
    status: SubtaskStatus = SubtaskStatus.PENDING
    result: str = ""
    error: str = ""
    token_usage: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0


class SubtaskDAG:

    def __init__(self, subtasks: list[Subtask]):
        self.subtasks: dict[str, Subtask] = {s.id: s for s in subtasks}
        self._validate_no_cycle()

    def _validate_no_cycle(self):
        visited = set()
        path = set()

        def dfs(node_id):
            if node_id in path:
                raise ValueError(f"循环依赖: {node_id}")
            if node_id in visited:
                return
            path.add(node_id)
            node = self.subtasks.get(node_id)
            if node:
                for dep in node.depends_on:
                    dfs(dep)
            path.discard(node_id)
            visited.add(node_id)

        for sid in self.subtasks:
            dfs(sid)

    def get_ready(self) -> list[Subtask]:
        ready = []
        for s in self.subtasks.values():
            if s.status != SubtaskStatus.PENDING:
                continue
            if all(
                self.subtasks[dep].status in (SubtaskStatus.DONE, SubtaskStatus.SKIPPED)
                for dep in s.depends_on
            ):
                ready.append(s)
        return ready

    def is_all_done(self) -> bool:
        return all(
            s.status in (SubtaskStatus.DONE, SubtaskStatus.SKIPPED, SubtaskStatus.FAILED)
            for s in self.subtasks.values()
        )

    def has_failure(self) -> bool:
        return any(s.status == SubtaskStatus.FAILED for s in self.subtasks.values())

    def mark_downstream_skipped(self, failed_id: str):
        import collections
        queue = collections.deque([failed_id])
        while queue:
            current_id = queue.popleft()
            for s in self.subtasks.values():
                if current_id in s.depends_on and s.status == SubtaskStatus.PENDING:
                    s.status = SubtaskStatus.SKIPPED
                    s.error = f"上游关联任务失败，跳过"
                    queue.append(s.id)

    def progress(self) -> dict:
        done = sum(1 for s in self.subtasks.values() if s.status == SubtaskStatus.DONE)
        failed = sum(1 for s in self.subtasks.values() if s.status == SubtaskStatus.FAILED)
        running = sum(1 for s in self.subtasks.values() if s.status == SubtaskStatus.RUNNING)
        pending = sum(1 for s in self.subtasks.values() if s.status == SubtaskStatus.PENDING)
        skipped = sum(1 for s in self.subtasks.values() if s.status == SubtaskStatus.SKIPPED)
        total = len(self.subtasks)
        return {
            "done": done,
            "failed": failed,
            "running": running,
            "pending": pending,
            "skipped": skipped,
            "total": total,
        }

    def to_dict(self) -> list[dict]:
        result = []
        for s in self.subtasks.values():
            result.append({
                "id": s.id,
                "name": s.name,
                "type": s.type.value,
                "prompt": s.prompt,
                "depends_on": s.depends_on,
                "tools": s.tools,
                "assigned_model": s.assigned_model,
                "status": s.status.value,
                "result": s.result[:1000] if s.result else "",
                "error": s.error,
                "token_usage": s.token_usage,
            })
        return result

    @classmethod
    def from_dict(cls, data: list[dict]) -> "SubtaskDAG":
        subtasks = []
        for item in data:
            subtasks.append(Subtask(
                id=item["id"],
                name=item["name"],
                type=SubtaskType(item.get("type", "text")),
                prompt=item.get("prompt", ""),
                depends_on=item.get("depends_on", []),
                tools=item.get("tools", []),
                assigned_model=item.get("assigned_model", ""),
                status=SubtaskStatus(item.get("status", "pending")),
                result=item.get("result", ""),
                error=item.get("error", ""),
                token_usage=item.get("token_usage", 0),
            ))
        return cls(subtasks)
