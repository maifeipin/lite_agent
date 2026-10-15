"""
执行结果契约。
Execution state is independent from generated prose and delivery state.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class TaskResult:
    status: str
    content: str
    failed_nodes: List[str] = field(default_factory=list)
    skipped_nodes: List[str] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    delivery: Dict[str, Any] = field(default_factory=dict)
    output_policy: Dict[str, Any] = field(default_factory=dict)

    def status_text(self) -> str:
        labels = {'succeeded': '任务已完成', 'partial': '任务部分完成', 'failed': '任务失败'}
        text = labels.get(self.status, '任务状态未知')
        if self.failed_nodes:
            text += '；失败：' + '、'.join(self.failed_nodes)
        if self.skipped_nodes:
            text += '；未执行：' + '、'.join(self.skipped_nodes)
        return text
