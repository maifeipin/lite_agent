"""Execution state is independent from generated prose and delivery state."""
from dataclasses import dataclass, field


@dataclass
class TaskResult:
    status: str
    content: str
    failed_nodes: list = field(default_factory=list)
    skipped_nodes: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    delivery: dict = field(default_factory=dict)
    output_policy: dict = field(default_factory=dict)

    def status_text(self):
        labels = {'succeeded': '任务已完成', 'partial': '任务部分完成', 'failed': '任务失败'}
        text = labels.get(self.status, '任务状态未知')
        if self.failed_nodes:
            text += '；失败：' + '、'.join(self.failed_nodes)
        if self.skipped_nodes:
            text += '；未执行：' + '、'.join(self.skipped_nodes)
        return text
