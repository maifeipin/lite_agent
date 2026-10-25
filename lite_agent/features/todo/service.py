"""Todo business service: coordinates todo lifecycles, source events, and periodic tasks.
Zero database connections, file writes, or cron registrations occur upon module import.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Set

from lite_agent.integrations.notifications import send_im_alert
from lite_agent.features.todo.rendering import render_markdown
from lite_agent.features.todo.repository import TodoRepository, generate_todo_id

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_until(until: str) -> str:
    if until.startswith('+'):
        m = re.match(r'^\+(\d+)([mhd])$', until)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            delta = {'m': timedelta(minutes=n), 'h': timedelta(hours=n), 'd': timedelta(days=n)}[unit]
            return (datetime.now(timezone.utc) + delta).isoformat()
    try:
        dt = datetime.fromisoformat(until.replace('Z', '+00:00'))
        return dt.isoformat()
    except ValueError:
        raise ValueError(f"无法解析时间: {until!r}（支持 ISO8601 或 +Nm/+Nh/+Nd）")



_CRON_DOW_TO_PY = {
    0: 6, 7: 6,  # Sunday
    1: 0,        # Monday
    2: 1,        # Tuesday
    3: 2,        # Wednesday
    4: 3,        # Thursday
    5: 4,        # Friday
    6: 5,        # Saturday
}
_DOW_NAME_MAP = {
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0
}


def _parse_cron_field(field_str: str, min_val: int, max_val: int, name_map: Optional[Dict[str, int]] = None) -> Optional[Set[int]]:
    """解析单个 cron 字段（支持 *、数字、连字符区间如 1-5 或 mon-fri、步长如 */15 或 10-30/10、逗号列表）。"""
    s = field_str.strip().lower()
    if not s:
        return None
    if s == "*":
        return set(range(min_val, max_val + 1))

    allowed: Set[int] = set()
    for item in s.split(','):
        item = item.strip()
        if not item:
            continue
        step = 1
        range_part = item
        if '/' in item:
            parts = item.split('/', 1)
            range_part = parts[0].strip()
            step_str = parts[1].strip()
            if not step_str.isdigit() or int(step_str) <= 0:
                return None
            step = int(step_str)

        if range_part == "" or range_part == "*":
            start_val, end_val = min_val, max_val
        elif '-' in range_part:
            r_parts = range_part.split('-', 1)
            s_str, e_str = r_parts[0].strip(), r_parts[1].strip()
            start_val = name_map[s_str] if (name_map and s_str in name_map) else (int(s_str) if s_str.isdigit() else None)
            end_val = name_map[e_str] if (name_map and e_str in name_map) else (int(e_str) if e_str.isdigit() else None)
            if start_val is None or end_val is None:
                return None
        else:
            v = name_map[range_part] if (name_map and range_part in name_map) else (int(range_part) if range_part.isdigit() else None)
            if v is None:
                return None
            start_val = v
            end_val = max_val if step > 1 else v

        if start_val < min_val or start_val > max_val or end_val < min_val or end_val > max_val:
            return None

        if start_val <= end_val:
            for val in range(start_val, end_val + 1, step):
                allowed.add(val)
        else:
            full_span = list(range(start_val, max_val + 1)) + list(range(min_val, end_val + 1))
            for val in full_span[::step]:
                allowed.add(val)

    return allowed if allowed else None


def _matches_cron_schedule(local_dt: datetime, recur_cron: Optional[str]) -> bool:
    """校验指定日期时间是否满足 cron 或周期别名排程。"""
    if not recur_cron:
        return True
    s = recur_cron.strip().lower()
    if s in ("daily", "every_day", "everyday"):
        return True
    if s in ("workday", "workdays", "weekday", "weekdays"):
        return local_dt.weekday() in range(0, 5)
    if s in ("weekly", "every_monday"):
        return local_dt.weekday() == 0
    if s in ("monthly",):
        return local_dt.day == 1

    if re.match(r'^\d{1,2}:\d{2}$', s):
        return True

    parts = s.split()
    if len(parts) == 5:
        m_str, h_str, dom_str, mon_str, dow_str = parts
        # 0. Minute (m)
        if m_str != '*':
            allowed_mins = _parse_cron_field(m_str, 0, 59)
            if allowed_mins is not None and local_dt.minute not in allowed_mins:
                return False
        # 1. Hour (h)
        if h_str != '*':
            allowed_hrs = _parse_cron_field(h_str, 0, 23)
            if allowed_hrs is not None and local_dt.hour not in allowed_hrs:
                return False
        # 2. Day of month (dom)
        if dom_str != '*':
            allowed_doms = _parse_cron_field(dom_str, 1, 31)
            if allowed_doms is not None and local_dt.day not in allowed_doms:
                return False
        # 3. Month (mon)
        if mon_str != '*':
            allowed_mons = _parse_cron_field(mon_str, 1, 12)
            if allowed_mons is not None and local_dt.month not in allowed_mons:
                return False
        # 4. Day of week (dow)
        if dow_str != '*':
            raw_dows = _parse_cron_field(dow_str, 0, 7, _DOW_NAME_MAP)
            if raw_dows is not None:
                allowed_py_dows = {_CRON_DOW_TO_PY[d] for d in raw_dows if d in _CRON_DOW_TO_PY}
                if local_dt.weekday() not in allowed_py_dows:
                    return False
        return True
    return True


def _parse_target_hm(recur_cron: Optional[str]) -> str:
    """提取执行时分 HH:MM，默认 09:00。"""
    if not recur_cron:
        return "09:00"
    s = recur_cron.strip().lower()
    if s in ("daily", "every_day", "everyday", "workday", "workdays", "weekday", "weekdays", "weekly", "every_monday", "monthly"):
        return "09:00"
    m_hm = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if m_hm:
        return f"{int(m_hm.group(1)):02d}:{int(m_hm.group(2)):02d}"
    parts = s.split()
    if len(parts) == 5:
        m_str, h_str = parts[0], parts[1]
        m_val = int(m_str) if m_str.isdigit() else 0
        h_val = int(h_str) if h_str.isdigit() else 9
        return f"{h_val:02d}:{m_val:02d}"
    return "09:00"


def _parse_target_hm_and_dow(recur_cron: str) -> Tuple[str, Optional[int]]:
    """向后兼容辅助函数。"""
    if not recur_cron:
        return "09:00", None
    hm = _parse_target_hm(recur_cron)
    s = recur_cron.strip().lower()
    if s in ("weekly", "every_monday"):
        return hm, 0
    parts = s.split()
    if len(parts) == 5:
        dow_str = parts[4]
        if dow_str != '*':
            raw_dows = _parse_cron_field(dow_str, 0, 7, _DOW_NAME_MAP)
            if raw_dows:
                py_dows = [_CRON_DOW_TO_PY[d] for d in raw_dows if d in _CRON_DOW_TO_PY]
                if len(py_dows) == 1:
                    return hm, py_dows[0]
    return hm, None


def _should_spawn_recurring(recur_cron: str, last_spawned_at: Optional[str], now_iso_str: str) -> bool:
    try:
        local_now = datetime.fromisoformat(now_iso_str).astimezone()
    except Exception:
        local_now = datetime.now().astimezone()

    today_prefix = local_now.strftime("%Y-%m-%d")
    if last_spawned_at:
        try:
            last_dt = datetime.fromisoformat(last_spawned_at).astimezone()
            last_date_str = last_dt.strftime("%Y-%m-%d")
        except Exception:
            last_date_str = last_spawned_at[:10]
        if today_prefix == last_date_str:
            return False

    if not _matches_cron_schedule(local_now, recur_cron):
        return False

    target_hm = _parse_target_hm(recur_cron)
    current_hm = local_now.strftime("%H:%M")
    return current_hm >= target_hm


class TodoService:
    """Core domain service for Todo lifecycle management."""

    def __init__(self, db_path: Any = None):
        if isinstance(db_path, TodoRepository):
            self.repo = db_path
        else:
            self.repo = TodoRepository(db_path)

    @property
    def db_path(self) -> str:
        return self.repo.db_path

    def add(
        self,
        title: str,
        kind: str = "misc",
        project: Optional[str] = None,
        description: Optional[str] = None,
        due_at: Optional[str] = None,
        recur_cron: Optional[str] = None,
        remind_interval_mins: int = 0,
        remind_before_mins: int = 30,
        notify_enabled: bool = True,
        priority: int = 1,
        dispatch_target: Optional[str] = None
    ) -> str:
        self._validate_reminders(notify_enabled, remind_before_mins, remind_interval_mins)
        if not isinstance(notify_enabled, bool):
            raise ValueError('notify_enabled must be boolean')
        if kind == "recurring" and not recur_cron:
            recur_cron = "daily"

        tid = generate_todo_id(title, self.db_path)
        self.repo.add_todo_atomic(
            tid=tid,
            title=title,
            description=description,
            kind=kind,
            project=project,
            due_at=due_at,
            recur_cron=recur_cron,
            remind_interval_mins=remind_interval_mins,
            remind_before_mins=remind_before_mins,
            notify_enabled=notify_enabled,
            priority=priority,
            dispatch_target=dispatch_target
        )
        render_markdown(self.db_path)
        if kind == "recurring":
            return f"✅ 成功添加周期任务模板: {title} (ID: {tid}, 周期: {recur_cron})"
        return f"✅ 成功添加待办事项: {title} (ID: {tid})"

    def list(
        self,
        status: Optional[str] = "pending,active",
        project: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 20
    ) -> str:
        rows = self.repo.list_todos(status=status, project=project, kind=kind, limit=limit)
        if not rows:
            return "没有找到符合条件的待办事项。"

        result = []
        for r in rows:
            tags = [r['kind']]
            if r.get('project'):
                tags.append(f"proj:{r['project']}")
            tag_str = f"[{','.join(tags)}]"
            due_str = f" (到期: {r['due_at']})" if r.get('due_at') else ""
            result.append(f"- [{r['status']}] {r['id']} {tag_str} {r['title']}{due_str}")
        return "\n".join(result)

    def get_json(self, status: Optional[str] = "pending,active", limit: int = 50) -> List[Dict[str, Any]]:
        return self.repo.list_todos(status=status, limit=limit)

    def get(self, tid: str) -> str:
        t = self.repo.get_todo(tid)
        if not t:
            return f"❌ 未找到任务 {tid}"

        res = [f"【任务详情】 ID: {t['id']}"]
        for k, v in t.items():
            if v is not None:
                res.append(f"{k}: {v}")
        return "\n".join(res)

    def start(self, tid: str) -> str:
        rc = self.repo.update_todo_status_atomic(tid, "active", {
            "started_at": _now_iso(),
            "completed_at": None,
            "notified_due_at": None
        })
        if rc > 0:
            render_markdown(self.db_path)
            return f"✅ 任务 {tid} 状态已更新为 active"
        return f"❌ 未找到任务 {tid}"

    def done(self, tid: str) -> str:
        rc = self.repo.update_todo_status_atomic(tid, "done", {"completed_at": _now_iso()})
        if rc > 0:
            render_markdown(self.db_path)
            return f"✅ 任务 {tid} 状态已更新为 done"
        return f"❌ 未找到任务 {tid}"

    def drop(self, tid: str, reason: Optional[str] = None) -> str:
        rc = self.repo.update_todo_status_atomic(tid, "dropped", {
            "dropped_at": _now_iso(),
            "dropped_reason": reason
        })
        if rc > 0:
            render_markdown(self.db_path)
            return f"✅ 任务 {tid} 状态已更新为 dropped"
        return f"❌ 未找到任务 {tid}"

    def snooze(self, tid: str, until: str) -> str:
        try:
            dt = _parse_until(until)
        except ValueError as e:
            return f"❌ {e}"
        rc = self.repo.update_todo_status_atomic(tid, "snoozed", {
            "snoozed_until": dt,
            "completed_at": None,
            "notified_due_at": None
        })
        if rc > 0:
            render_markdown(self.db_path)
            return f"✅ 任务 {tid} 状态已更新为 snoozed"
        return f"❌ 未找到任务 {tid}"

    def shelve(self, tid: str, reason: Optional[str] = None) -> str:
        rc = self.repo.update_todo_status_atomic(tid, "shelved", {
            "shelved_at": _now_iso(),
            "shelved_reason": reason
        })
        if rc > 0:
            render_markdown(self.db_path)
            return f"✅ 任务 {tid} 状态已更新为 shelved"
        return f"❌ 未找到任务 {tid}"

    def resume(self, tid: str) -> str:
        rc = self.repo.update_todo_status_atomic(tid, "pending", {
            "snoozed_until": None,
            "shelved_at": None,
            "shelved_reason": None,
            "notified_due_at": None,
            "completed_at": None
        })
        if rc > 0:
            render_markdown(self.db_path)
            return f"✅ 任务 {tid} 状态已更新为 pending"
        return f"❌ 未找到任务 {tid}"

    def update(
        self,
        tid: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        due_at: Optional[str] = None,
        project: Optional[str] = None,
        recur_cron: Optional[str] = None,
        kind: Optional[str] = None,
        remind_interval_mins: Optional[int] = None,
        remind_before_mins: Optional[int] = None,
        notify_enabled: Optional[bool] = None,
        priority: Optional[int] = None,
        dispatch_target: Optional[str] = None
    ) -> str:
        self._validate_reminders(notify_enabled, remind_before_mins, remind_interval_mins)
        fields: Dict[str, Any] = {}
        if title is not None: fields["title"] = title
        if description is not None: fields["description"] = description
        if due_at is not None:
            fields["due_at"] = due_at
            fields["notified_due_at"] = None
        if project is not None: fields["project"] = project
        if recur_cron is not None: fields["recur_cron"] = recur_cron
        if kind is not None: fields["kind"] = kind
        if remind_interval_mins is not None: fields["remind_interval_mins"] = remind_interval_mins
        if remind_before_mins is not None: fields["remind_before_mins"] = remind_before_mins
        if notify_enabled is not None: fields['notify_enabled'] = notify_enabled
        if priority is not None: fields['priority'] = priority
        if dispatch_target is not None: fields['dispatch_target'] = dispatch_target

        if not fields:
            return f"❌ 未提供任何修改项"

        rc = self.repo.update_todo_fields_atomic(tid, fields)
        if rc > 0:
            render_markdown(self.db_path)
            return f"✅ 任务 {tid} 已更新"
        return f"❌ 未找到任务 {tid}"

    def delete(self, tid: str) -> bool:
        rc = self.repo.delete_todo_atomic(tid)
        if rc > 0:
            render_markdown(self.db_path)
            return True
        return False

    def dispatch(self, tid: str, target: str = "manual") -> str:
        t = self.repo.get_todo(tid)
        if not t:
            return f"❌ 未找到任务 {tid}"

        self.repo.update_todo_fields_atomic(tid, {"dispatch_last_run_at": _now_iso()})
        if target == "manual":
            proj = t.get('project') or "未知项目"
            prompt = f"""请在本地 IDE 执行以下任务：
项目: {proj}
任务标题: {t['title']}
任务ID: {t['id']}
背景详情:
{t.get('description') or '无'}
"""
            return f"✅ 手动派发成功。请将以下内容复制给本地 IDE：\n\n{prompt}"
        return f"❌ 暂不支持的 dispatch target: {target}"

    @staticmethod
    def _validate_reminders(enabled, before, interval):
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError('notify_enabled must be boolean')
        for value in (before, interval):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError('reminder minutes must be nonnegative integers')

    def existing_source_keys(self, prefix: str) -> set:
        return self.repo.get_source_keys(prefix)

    def upsert_source(
        self,
        source_key: str,
        title: str,
        description: Optional[str] = None,
        created_via: str = 'mail_quality',
        source_message_id: Optional[str] = None,
        source_metadata: Optional[Dict[str, Any]] = None,
        due_at: Optional[str] = None,
        kind: str = 'code',
        project: Optional[str] = 'mail-statement-parser',
        remind_before_mins: int = 30,
        remind_interval_mins: int = 0,
        recur_cron: Optional[str] = None,
        create_if_missing: bool = True,
        replace_schedule: bool = False
    ) -> Dict[str, Any]:
        res = self.repo.upsert_source_receipt_atomic(
            source_key=source_key,
            title=title,
            description=description,
            created_via=created_via,
            source_message_id=source_message_id,
            source_metadata=source_metadata,
            due_at=due_at,
            kind=kind,
            project=project,
            remind_before_mins=remind_before_mins,
            remind_interval_mins=remind_interval_mins,
            recur_cron=recur_cron,
            create_if_missing=create_if_missing,
            replace_schedule=replace_schedule
        )
        render_markdown(self.db_path)
        return res

    def resolve_source(
        self,
        source_key: str,
        resolution_note: str = '上游问题已解决',
        cycle_id: Optional[int] = None,
        revision: Optional[int] = None,
        expected_owner: str = 'mail_quality',
        close_user_edited: bool = False
    ) -> Dict[str, Any]:
        res = self.repo.resolve_source_receipt_atomic(
            source_key=source_key,
            resolution_note=resolution_note,
            cycle_id=cycle_id,
            revision=revision,
            expected_owner=expected_owner,
            close_user_edited=close_user_edited
        )
        render_markdown(self.db_path)
        return res

    # 兼容别名
    upsert_source_todo = upsert_source
    resolve_source_todo = resolve_source

    def get_by_id(self, tid: str) -> Optional[Dict[str, Any]]:
        return self.repo.get_todo(tid)

    def _dispatch_alert(
        self,
        title: str,
        text: str,
        color: str = "yellow",
        dedup_key: Optional[str] = None,
        dispatch_target: Optional[str] = None
    ) -> bool:
        """根据分发目标路由通知到内置渠道、邮件或全通道。"""
        target = (dispatch_target or "").lower()
        want_mail = "mail" in target or "email" in target or "邮件" in target
        want_all = "all" in target or "全通道" in target

        if want_all:
            sent_mail = False
            try:
                from lite_agent.integrations.smtp import send_email_delivery
                sent_mail = bool(send_email_delivery(title, text))
            except Exception as e:
                logger.debug(f"[TodoService] 邮件分发异常: {e}")
            sent_im = send_im_alert(title=title, text=text, color=color, dedup_key=dedup_key)
            return bool(sent_im or sent_mail)
        elif want_mail:
            sent_mail = False
            try:
                from lite_agent.integrations.smtp import send_email_delivery
                sent_mail = bool(send_email_delivery(title, text))
            except Exception as e:
                logger.debug(f"[TodoService] 邮件分发异常: {e}")
            if not sent_mail:
                return send_im_alert(title=title, text=text, color=color, dedup_key=dedup_key)
            return True
        else:
            return send_im_alert(title=title, text=text, color=color, dedup_key=dedup_key)

    def tick(self, now_str: Optional[str] = None) -> Dict[str, int]:
        """Periodic tick: wakes snoozed, inspects due tasks for alerts, spawns recurring templates."""
        now = now_str or _now_iso()
        woken_count = self.repo.process_tick_wake_snoozed(now)
        due_soon_count = 0
        spawned_count = 0

        # 2. Due soon tasks check
        due_tasks = self.repo.get_due_reminder_candidates()
        now_dt = datetime.fromisoformat(now)

        for row in due_tasks:
            tid = row['id']
            title = row['title']
            due_at = row['due_at']
            project = row.get('project')
            kind = row.get('kind')
            description = row.get('description')
            interval_mins = row.get('remind_interval_mins')
            before_mins = row.get('remind_before_mins')
            last_notified = row.get('notified_due_at')
            priority_val = row.get('priority', 1)
            dispatch_target = row.get('dispatch_target')
            p_tag = {0: "🚨 [P0 紧急]", 1: "⚡ [P1 重要]", 2: "📌 [P2 次要]", 3: "☕ [P3 稍后]"}.get(priority_val, "⚡ [P1 重要]")

            try:
                due_dt = datetime.fromisoformat(due_at)
            except Exception:
                continue

            if kind == 'reminder':
                schedule_zone = due_dt.tzinfo or timezone.utc
                local_now = now_dt.astimezone(schedule_zone)
                last_day = None
                if last_notified:
                    try:
                        last_day = datetime.fromisoformat(last_notified).astimezone(schedule_zone).date()
                    except ValueError:
                        continue
                if now_dt < due_dt or not _matches_cron_schedule(local_now, row.get('recur_cron')) or local_now.strftime('%H:%M') < _parse_target_hm(row.get('recur_cron') or due_dt.strftime('%H:%M')) or last_day == local_now.date():
                    continue
                if not self.repo.mark_notified_due(tid, now, last_notified, due_at):
                    continue
                sent = self._dispatch_alert(
                    title=f'{p_tag} 🔔 周期提醒: {title}',
                    text=f'{title}\n{description or ""}',
                    color='red' if priority_val == 0 else 'yellow',
                    dedup_key=f'todo_periodic:{tid}:{local_now.date()}',
                    dispatch_target=dispatch_target
                )
                if sent is False:
                    self.repo.release_notification_claim(tid, now, last_notified)
                else:
                    due_soon_count += 1
                continue
            window_mins = before_mins if before_mins is not None else 30
            start_notify_dt = due_dt - timedelta(minutes=window_mins)
            if now_dt < start_notify_dt:
                continue

            should_notify = False
            if not last_notified:
                should_notify = True
            elif interval_mins and interval_mins > 0:
                try:
                    last_dt = datetime.fromisoformat(last_notified)
                    if (now_dt - last_dt).total_seconds() >= (interval_mins * 60 - 5):
                        should_notify = True
                except Exception:
                    pass

            if should_notify:
                if not self.repo.mark_notified_due(tid, now, last_notified, due_at):
                    continue
                due_soon_count += 1
                is_overdue = due_dt < now_dt
                tag = "📬 一次性通知" if kind == "notice" else ("🔴 已超期" if is_overdue else f"⏰ 即将到期 ({window_mins}分钟内)")
                proj_info = f"【{project}】" if project else ""
                msg = f"{tag} {proj_info}\n**{title}**\n\n- 到期时间: {due_at}\n- 任务类型: {kind}\n- 任务 ID: `{tid}`"
                if interval_mins and interval_mins > 0:
                    msg += f"\n- 提醒频次: 每 {interval_mins} 分钟"
                if description:
                    msg += f"\n- 说明: {description[:100]}"

                alert_color = "yellow" if kind == "notice" else ("red" if (is_overdue or priority_val == 0) else ("blue" if priority_val == 3 else "yellow"))
                alert_title = f"📬 {title}" if kind == "notice" else f"{p_tag} 待办提醒: {title}"

                sent = self._dispatch_alert(
                    title=alert_title,
                    text=msg,
                    color=alert_color,
                    dedup_key=f"todo_due:{tid}:{now[:16]}" if (interval_mins and interval_mins > 0) else f"todo_due:{tid}",
                    dispatch_target=dispatch_target
                )
                if sent is False:
                    self.repo.release_notification_claim(tid, now, last_notified)
                    due_soon_count -= 1
                elif kind == 'notice':
                    self.repo.finish_notice(tid, now)

        # 3. Recurring templates
        recurring_templates = self.repo.get_recurring_templates()
        local_now = datetime.now().astimezone()

        for r in recurring_templates:
            pid = r['id']
            ptitle = r['title']
            pdesc = r.get('description')
            pproj = r.get('project')
            rcron = r.get('recur_cron')
            rlast = r.get('recur_last_spawned_at')
            if _should_spawn_recurring(rcron, rlast, now):
                child_id = generate_todo_id(f"{ptitle}_{local_now.strftime('%Y-%m-%d')}", self.db_path)
                target_hm = _parse_target_hm(rcron)
                due_time = local_now.strftime(f"%Y-%m-%dT{target_hm}:00%z")
                if not self.repo.spawn_recurring_child(child_id, ptitle, pdesc, pproj, now, pid, due_time, rlast):
                    continue
                spawned_count += 1
                if not r.get('notify_enabled', True):
                    continue
                send_im_alert(
                    title=f"🔄 周期任务自动派发: {ptitle}",
                    text=f"已自动派发今日周期任务：**{ptitle}** (`{child_id}`)\n- 所属项目: {pproj or '默认'}\n- 周期配置: `{rcron}`",
                    color="green",
                    dedup_key=f"todo_spawn:{child_id}"
                )

        if woken_count > 0 or due_soon_count > 0 or spawned_count > 0:
            render_markdown(self.db_path)

        return {"woken": woken_count, "due_notified": due_soon_count, "spawned": spawned_count}

    def push_brief(self) -> str:
        """Aggregate unfinished and overdue todos, sending daily brief to IM."""
        now = _now_iso()
        today_prefix = now[:10]
        rows = self.repo.get_todos_for_brief()

        if not rows:
            msg = f"📋 **每日待办简报 ({today_prefix})**\n\n🎉 当前没有任何待办事项，保持极佳状态！"
            send_im_alert(title=f"📋 每日待办简报 ({today_prefix})", text=msg, color="green", dedup_key=f"todo_brief:{today_prefix}")
            return "✅ 无待办事项，已发送空简报"

        overdue = []
        due_today = []
        active = []
        pending = []

        for t in rows:
            tid = t['id']
            title = t['title']
            project = t.get('project')
            due_at = t.get('due_at')
            st = t['status']

            item_str = f"- **{title}** (`{tid}`)"
            if project:
                item_str += f" [{project}]"
            if due_at:
                item_str += f" *(到期: {due_at[:16]})*"

            if due_at and due_at < now:
                overdue.append(item_str)
            elif due_at and due_at.startswith(today_prefix):
                due_today.append(item_str)
            elif st == 'active':
                active.append(item_str)
            else:
                pending.append(item_str)

        sections = [f"📋 **每日待办简报 ({today_prefix})**\n"]
        total_count = len(rows)
        MAX_DISPLAY_PER_SECTION = 100
        if overdue:
            sections.append(f"🔴 **已超期任务 ({len(overdue)})**:")
            sections.extend(overdue[:MAX_DISPLAY_PER_SECTION])
            if len(overdue) > MAX_DISPLAY_PER_SECTION:
                sections.append(f"... 还有 {len(overdue) - MAX_DISPLAY_PER_SECTION} 项超期任务")
            sections.append("")
        if due_today:
            sections.append(f"⏰ **今日到期任务 ({len(due_today)})**:")
            sections.extend(due_today[:MAX_DISPLAY_PER_SECTION])
            if len(due_today) > MAX_DISPLAY_PER_SECTION:
                sections.append(f"... 还有 {len(due_today) - MAX_DISPLAY_PER_SECTION} 项今日到期任务")
            sections.append("")
        if active:
            sections.append(f"🚀 **进行中任务 ({len(active)})**:")
            sections.extend(active[:MAX_DISPLAY_PER_SECTION])
            if len(active) > MAX_DISPLAY_PER_SECTION:
                sections.append(f"... 还有 {len(active) - MAX_DISPLAY_PER_SECTION} 项进行中任务")
            sections.append("")
        if pending:
            sections.append(f"📌 **待处理任务 ({len(pending)})**:")
            sections.extend(pending[:MAX_DISPLAY_PER_SECTION])
            if len(pending) > MAX_DISPLAY_PER_SECTION:
                sections.append(f"... 还有 {len(pending) - MAX_DISPLAY_PER_SECTION} 项待处理任务")
            sections.append("")

        text = "\n".join(sections).strip()
        send_im_alert(title=f"📋 每日待办简报 ({today_prefix})", text=text, color="blue", dedup_key=f"todo_brief:{today_prefix}")
        return f"✅ 已成功推送每日待办简报（包含 {len(rows)} 条任务）"
