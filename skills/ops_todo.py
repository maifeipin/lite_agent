"""Ops Todo Skill: LLM tools and CLI adapters for personal and source todos.
Delegates domain logic, database operations, and rendering to features.todo.
Maintains full backward-compatible interfaces and tool signatures.
"""

import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.skill_engine import skill
from core.cron_engine import CronManager
from features.todo.repository import (
    DEFAULT_DB_PATH,
    _ensure_schema as _repo_ensure_schema,
    generate_todo_id,
    get_connection,
    resolve_db_path,
)
from features.todo.rendering import (
    DEFAULT_MD_PATH,
    render_markdown,
    resolve_md_path,
)
from features.todo.service import (
    TodoService,
    send_im_alert,
    _now_iso,
    _parse_until,
)

# Compatibility exports
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
DB_PATH = DEFAULT_DB_PATH
MD_PATH = DEFAULT_MD_PATH

_send_im_alert = send_im_alert


def _ensure_schema(db_path: Optional[str] = None):
    target = db_path or DB_PATH
    _repo_ensure_schema(target)


def _conn(db_path: Optional[str] = None):
    target = db_path or DB_PATH
    return get_connection(target)


def _gen_id(title: str, db_path: Optional[str] = None) -> str:
    target = db_path or DB_PATH
    return generate_todo_id(title, target)


def _render_markdown(db_path: Optional[str] = None):
    target_db = db_path or DB_PATH
    target_md = MD_PATH if target_db == DB_PATH else None
    render_markdown(target_db, target_md)


def get_service(db_path: Optional[str] = None) -> TodoService:
    target = db_path or DB_PATH
    return TodoService(target)


def upsert_source_todo(
    source_key: str,
    title: str,
    description: Optional[str] = None,
    created_via: str = 'mail_quality',
    source_message_id: Optional[str] = None,
    source_metadata: Optional[dict] = None,
    due_at: Optional[str] = None,
    kind: str = 'code',
    project: Optional[str] = 'mail-statement-parser',
    db_path: Optional[str] = None
) -> dict:
    svc = get_service(db_path)
    return svc.upsert_source(
        source_key=source_key,
        title=title,
        description=description,
        created_via=created_via,
        source_message_id=source_message_id,
        source_metadata=source_metadata,
        due_at=due_at,
        kind=kind,
        project=project
    )


def resolve_source_todo(
    source_key: str,
    resolution_note: str = '上游问题已解决',
    cycle_id: Optional[int] = None,
    db_path: Optional[str] = None,
    revision: Optional[int] = None
) -> dict:
    svc = get_service(db_path)
    return svc.resolve_source(
        source_key=source_key,
        resolution_note=resolution_note,
        cycle_id=cycle_id,
        revision=revision
    )


def get_todos_json(status: str = "pending,active", limit: int = 50) -> list:
    return get_service().get_json(status=status, limit=limit)


def _update_todo_status(tid: str, status: str, extra_fields: dict = None):
    svc = get_service()
    if status == 'active':
        return svc.start(tid)
    elif status == 'done':
        return svc.done(tid)
    elif status == 'dropped':
        reason = extra_fields.get("dropped_reason") if extra_fields else None
        return svc.drop(tid, reason)
    elif status == 'shelved':
        reason = extra_fields.get("shelved_reason") if extra_fields else None
        return svc.shelve(tid, reason)
    elif status == 'pending':
        return svc.resume(tid)
    return f"❌ 不支持的状态: {status}"


# Skill Tool Definitions
@skill(
    name="todo_add",
    description="添加一条新的待办事项",
    params={
        "title": {"type": "string", "description": "任务标题"},
        "kind": {"type": "string", "description": "分类: misc, code, 或 recurring (循环任务)", "default": "misc"},
        "project": {"type": "string", "description": "关联项目名称，可选"},
        "description": {"type": "string", "description": "详细描述，可选"},
        "due_at": {"type": "string", "description": "到期时间必须是 ISO8601 且含时区，例如 2026-06-20T12:00:00+08:00，可选"},
        "recur_cron": {"type": "string", "description": "循环周期配置，如 daily, weekly, 09:00, 可选"},
        "remind_interval_mins": {"type": "integer", "description": "高频重复提醒间隔(分钟)，如 10 表示每10分钟提醒一次，0表示仅提醒一次", "default": 0},
        "remind_before_mins": {"type": "integer", "description": "提前多少分钟开始首推提醒，0为到点，默认30", "default": 30},
        "notify_enabled": {"type": "boolean", "description": "是否发送逐条提醒；false保留待办和排期，仅关闭通知", "default": True}
    }
)
def todo_add(
    title: str,
    kind: str = "misc",
    project: str = None,
    description: str = None,
    due_at: str = None,
    recur_cron: str = None,
    remind_interval_mins: int = 0,
    remind_before_mins: int = 30,
    notify_enabled: bool = True,
    db_path: Optional[str] = None
) -> str:
    return get_service(db_path).add(
        title=title,
        kind=kind,
        project=project,
        description=description,
        due_at=due_at,
        recur_cron=recur_cron,
        remind_interval_mins=remind_interval_mins,
        remind_before_mins=remind_before_mins,
        notify_enabled=notify_enabled
    )


@skill(
    name="todo_list",
    description="查询待办事项列表",
    params={
        "status": {"type": "string", "description": "状态过滤，如 pending, active, done, 或逗号分隔", "default": "pending,active"},
        "project": {"type": "string", "description": "按项目过滤"},
        "kind": {"type": "string", "description": "按分类过滤"},
        "limit": {"type": "integer", "description": "限制返回数量", "default": 20}
    }
)
def todo_list(status: str = "pending,active", project: str = None, kind: str = None, limit: int = 20, db_path: Optional[str] = None) -> str:
    return get_service(db_path).list(status=status, project=project, kind=kind, limit=limit)


@skill(name="todo_get", description="获取待办事项详情", params={"id": {"type": "string", "description": "任务 ID"}})
def todo_get(id: str, db_path: Optional[str] = None) -> str:
    return get_service(db_path).get(id)


@skill(name="todo_start", description="开始处理待办事项", params={"id": {"type": "string"}})
def todo_start(id: str, db_path: Optional[str] = None) -> str:
    return get_service(db_path).start(id)


@skill(name="todo_done", description="将待办事项标记为完成", params={"id": {"type": "string"}})
def todo_done(id: str, db_path: Optional[str] = None) -> str:
    return get_service(db_path).done(id)


@skill(
    name="todo_drop",
    description="放弃待办事项（不再做但保留记录）",
    params={"id": {"type": "string"}, "reason": {"type": "string", "description": "放弃原因，可选"}}
)
def todo_drop(id: str, reason: str = None, db_path: Optional[str] = None) -> str:
    return get_service(db_path).drop(id, reason)


@skill(
    name="todo_snooze",
    description="将任务延期挂起",
    params={"id": {"type": "string"}, "until": {"type": "string", "description": "唤醒时间 ISO8601 格式或 +Nm/+Nh/+Nd 相对时间（如 +2m, +3d）"}}
)
def todo_snooze(id: str, until: str, db_path: Optional[str] = None) -> str:
    return get_service(db_path).snooze(id, until)


@skill(
    name="todo_shelve",
    description="无限期搁置任务（不知何时做）",
    params={"id": {"type": "string"}, "reason": {"type": "string", "description": "搁置原因，可选"}}
)
def todo_shelve(id: str, reason: str = None, db_path: Optional[str] = None) -> str:
    return get_service(db_path).shelve(id, reason)


@skill(name="todo_resume", description="恢复被延期或搁置的任务为待办状态", params={"id": {"type": "string"}})
def todo_resume(id: str, db_path: Optional[str] = None) -> str:
    return get_service(db_path).resume(id)


@skill(
    name="todo_update",
    description="修改任务的基本信息",
    params={
        "id": {"type": "string"},
        "title": {"type": "string", "description": "新标题"},
        "description": {"type": "string", "description": "新详情"},
        "due_at": {"type": "string", "description": "新到期时间 ISO8601"},
        "project": {"type": "string", "description": "新项目"},
        "recur_cron": {"type": "string", "description": "新循环周期"},
        "kind": {"type": "string", "description": "新任务分类"},
        "remind_interval_mins": {"type": "integer", "description": "新重复提醒间隔(分钟)"},
        "remind_before_mins": {"type": "integer", "description": "新提前提醒时间(分钟)，0为到点"},
        "notify_enabled": {"type": "boolean", "description": "关闭/恢复逐条提醒，不改变待办完成状态"}
    }
)
def todo_update(
    id: str,
    title: str = None,
    description: str = None,
    due_at: str = None,
    project: str = None,
    recur_cron: str = None,
    kind: str = None,
    remind_interval_mins: int = None,
    remind_before_mins: int = None,
    notify_enabled: bool = None,
    db_path: Optional[str] = None
) -> str:
    return get_service(db_path).update(
        tid=id,
        title=title,
        description=description,
        due_at=due_at,
        project=project,
        recur_cron=recur_cron,
        kind=kind,
        remind_interval_mins=remind_interval_mins,
        remind_before_mins=remind_before_mins,
        notify_enabled=notify_enabled
    )


@skill(name="todo_dispatch", description="派发代码任务以供本地 IDE 处理", params={"id": {"type": "string"}, "target": {"type": "string", "default": "manual"}})
def todo_dispatch(id: str, target: str = "manual", db_path: Optional[str] = None) -> str:
    return get_service(db_path).dispatch(id, target)


@skill(name="todo_push_brief", description="汇总当前待办事项并推送到 IM 聊天频道", params={})
def todo_push_brief(db_path: Optional[str] = None) -> str:
    return get_service(db_path).push_brief()


def _todo_tick():
    return get_service().tick()


def register_todo_cron(mgr: Optional[CronManager] = None) -> None:
    """Idempotent cron registration entry point."""
    cron = mgr or CronManager()
    if not any(j.name == 'todo_tick' for j in cron.jobs.values()):
        cron.add_job('todo_tick', 'every_minute', _todo_tick)
    if not any(j.name == 'todo_brief' for j in cron.jobs.values()):
        cron.add_job('todo_brief', '08:30', todo_push_brief)
