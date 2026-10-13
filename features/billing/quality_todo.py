"""Billing quality event to Todo orchestration service.
Pulls unacked quality events from MailCLI, idempotently records them to TodoService,
and confirms (acks) with monotonic revisions.
"""

import logging
import sys
import time
from typing import Any, Dict, List, Optional

from core.utils.masker import mask_secrets
from features.todo.service import TodoService
from integrations.mail.client import MailCLI

logger = logging.getLogger(__name__)


def upsert_source_todo(
    source_key: str,
    title: str,
    description: Optional[str] = None,
    created_via: str = 'mail_quality',
    source_message_id: Optional[str] = None,
    source_metadata: Optional[Dict[str, Any]] = None,
    due_at: Optional[str] = None,
    kind: str = 'code',
    project: Optional[str] = 'mail-statement-parser',
    db_path: Optional[str] = None
) -> Dict[str, Any]:
    return TodoService(db_path).upsert_source(
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
) -> Dict[str, Any]:
    return TodoService(db_path).resolve_source(
        source_key=source_key,
        resolution_note=resolution_note,
        cycle_id=cycle_id,
        revision=revision
    )


def _format_quality_todo_content(event: Dict[str, Any]) -> tuple:
    """根据质量事件生成规范的标题与 Markdown 格式详细描述"""
    bank_code = event.get("bank_code") or "未知银行"
    template_version = event.get("template_version") or "未声明"
    occurrence_count = event.get("occurrence_count", 1)
    evidence_refs = event.get("evidence_refs") or []
    affected_count = len(evidence_refs) if evidence_refs else occurrence_count
    case_key = event.get("case_key") or ""
    first_seen = event.get("first_seen_at") or ""
    last_seen = event.get("last_seen_at") or ""
    diagnostics = event.get("diagnostics") or []

    diag_codes = [d.get("code") for d in diagnostics if isinstance(d, dict) and d.get("code")]
    labels = {'FORMAT_CHANGED': '账单模板变化', 'FORMAT_BASELINE_MISSING': '模板基线待确认',
              'SOURCE_FIELD_MISSING': '原文缺少关键字段', 'EXTRACTION_FAILED': '关键字段提取失败',
              'FIELD_SOURCE_UNKNOWN': '关键字段来源待核验', 'CURRENCY_UNKNOWN': '币种证据不足',
              'TRANSACTION_ROW_INVALID': '交易行解析异常', 'TRANSACTION_COVERAGE_GAP': '交易明细不完整'}
    code_summary = ", ".join(labels.get(code, code) for code in list(dict.fromkeys(diag_codes))[:3]) if diag_codes else "解析异常"

    title = f"[账单质量] {bank_code} 发现 {len(diagnostics)} 项解析问题: {code_summary} (影响 {affected_count} 封邮件)"

    lines = [
        "### 📬 邮件账单解析质量问题诊断",
        f"- **银行代码**: `{bank_code}`",
        f"- **模板版本**: `{template_version}`",
        f"- **受影响邮件数**: {affected_count} 封 (已去重)",
        f"- **累计检出次数**: {occurrence_count} 次",
        f"- **发现时间**: 首次 `{first_seen}` | 最近 `{last_seen}`",
        f"- **质量特征标识**: `{case_key}`",
        "",
        "#### 🔍 诊断代码与原因明细:"
    ]

    for i, d in enumerate(diagnostics, 1):
        if not isinstance(d, dict):
            continue
        code = d.get("code", "UNKNOWN")
        sev = d.get("severity", "warning").upper()
        field = d.get("field") or "整体结构"
        reason = d.get("reason", "")
        purposes = d.get("affected_purposes") or []
        purposes_str = ", ".join(purposes) if purposes else "全用途"

        lines.append(f"{i}. **[{sev}] `{code}`** (字段: `{field}`)")
        lines.append(f"   - 原因: {reason}")
        lines.append(f"   - 受影响用途: `{purposes_str}`")

    lines.extend([
        "",
        "#### 🛠️ 建议排查与修复步骤:",
        f"1. 检查 `rules/{bank_code}_TEMPLATE_*.json` 模板规则中的字段正则、表头别名及门禁配置；",
        "2. 确认银行是否变更了邮件排版布局或改动了关键数据列命名；",
        "3. 在开发环境使用离线重解析命令进行脱机回归验证，确认无误后再发布更新。"
    ])

    return title, "\n".join(lines)


def consume_quality_events(
    client: Optional[MailCLI] = None,
    db_path: Optional[str] = None,
    limit: int = 100,
    stop_on_error: bool = False,
    deadline: Optional[float] = None,
    upsert_fn: Optional[Any] = None,
    resolve_fn: Optional[Any] = None,
    time_fn: Optional[Any] = None
) -> Dict[str, Any]:
    """拉取未确认的质量事件，幂等写入 Todo 数据库，并在成功后执行 ack。"""
    _time_mod = time_fn or time
    _upsert_fn = upsert_fn or upsert_source_todo
    _resolve_fn = resolve_fn or resolve_source_todo

    cli = client or MailCLI()
    stats = {
        "total": 0,
        "created": 0,
        "updated": 0,
        "protected": 0,
        "resolved": 0,
        "skipped": 0,
        "acked": 0,
        "failed": 0,
        "failed_acks": 0,
        "errors": []
    }

    def remaining():
        if deadline is None:
            return {}
        seconds = deadline - _time_mod.monotonic()
        if seconds <= 0:
            raise TimeoutError('质量待办消费预算耗尽，未确认事件保留待重试')
        return {'timeout': seconds}

    try:
        events = cli.quality_events(limit=limit, **remaining())
    except Exception as exc:
        err_msg = mask_secrets(f"拉取质量事件失败: {exc}")
        logger.error(err_msg)
        stats["errors"].append(err_msg)
        stats["failed"] += 1
        return stats

    stats["total"] = len(events)

    for event in events:
        if stop_on_error and (stats['failed'] or stats['failed_acks']):
            break
        if deadline is not None and _time_mod.monotonic() >= deadline:
            stats['failed'] += 1
            stats['errors'].append('质量待办消费预算耗尽，未确认事件保留待重试')
            break
        if not isinstance(event, dict):
            stats['failed'] += 1
            stats['errors'].append('Invalid quality event object')
            continue
        event_id = event.get("event_id")
        case_key = event.get("case_key")
        if not isinstance(event_id, str) or not event_id.strip() or not isinstance(case_key, str) or not case_key.strip():
            stats["failed"] += 1
            stats["errors"].append("事件缺少必要字段 (event_id/case_key)")
            continue

        event_type = event.get("event_type")
        revision = event.get("revision")
        cycle_id = event.get("cycle_id")

        if event_type == "statement_recorded":
            stats["skipped"] += 1
            continue

        if (event_type not in ('quality_detected', 'quality_updated', 'quality_resolved')
                or any(not isinstance(v, int) or isinstance(v, bool) or v < 1
                       for v in (revision, cycle_id))
                or not isinstance(event.get('diagnostics', []), list)
                or any(not isinstance(d, dict) for d in event.get('diagnostics', []))
                or not isinstance(event.get('evidence_refs', []), list)):
            stats['failed'] += 1
            stats['errors'].append(f'Unsupported or malformed event: {event_id}')
            continue

        # 显式解决事件 (quality_resolved)
        if event_type == "quality_resolved":
            source_key = f"mail_quality:{case_key}"
            try:
                res_todo = _resolve_fn(
                    source_key=source_key,
                    resolution_note=f"上游质量问题已确认解决 (cycle={cycle_id}, rev={revision})",
                    cycle_id=cycle_id,
                    db_path=db_path,
                    revision=revision
                )
                if res_todo.get('resolved') is True:
                    stats['resolved'] += 1
                else:
                    stats['protected'] += 1
                todo_id = res_todo.get("id") or f"resolved:{case_key}"
            except Exception as exc:
                err_msg = mask_secrets(f"解决事件 {event_id} 处理 Todo 异常: {exc}")
                logger.error(err_msg)
                stats["failed"] += 1
                stats["errors"].append(err_msg)
                continue

            try:
                ack_res = cli.quality_ack(event_id=event_id, todo_id=todo_id, revision=revision, **remaining())
                if ack_res.get("acked") is True:
                    stats["acked"] += 1
                else:
                    stats["failed_acks"] += 1
                    err_msg = mask_secrets(f"解决事件 {event_id} ack 未确认: {ack_res.get('reason')}")
                    logger.warning(err_msg)
                    stats["errors"].append(err_msg)
            except Exception as exc:
                stats["failed_acks"] += 1
                err_msg = mask_secrets(f"解决事件 {event_id} ack 失败: {exc}")
                logger.warning(err_msg)
                stats["errors"].append(err_msg)
            continue

        # 质量异常诊断事件 (quality_detected 等)
        source_key = f"mail_quality:{case_key}"
        occurrence_count = event.get("occurrence_count", 1)
        evidence_refs = event.get("evidence_refs") or []
        affected_count = len(evidence_refs) if evidence_refs else occurrence_count

        metadata = {
            "evidence_refs": evidence_refs,
            "diagnostics": event.get("diagnostics") or [],
            "event_id": event_id,
            "case_key": case_key,
            "bank_code": event.get("bank_code"),
            "account_name": event.get("account_name"),
            "template_version": event.get("template_version"),
            "occurrence_count": occurrence_count,
            "affected_count": affected_count,
            "revision": revision,
            "cycle_id": cycle_id,
            "event_type": event_type,
            "first_seen_at": event.get("first_seen_at"),
            "last_seen_at": event.get("last_seen_at")
        }

        try:
            title, description = _format_quality_todo_content(event)
            todo_res = _upsert_fn(
                source_key=source_key,
                title=title,
                description=description,
                created_via="mail_quality",
                source_message_id=None,
                source_metadata=metadata,
                due_at=None,
                kind="code",
                project="mail-statement-parser",
                db_path=db_path
            )
        except Exception as exc:
            err_msg = mask_secrets(f"事件 {event_id} 写入 Todo 失败: {exc}")
            logger.error(err_msg)
            stats["failed"] += 1
            stats["errors"].append(err_msg)
            continue

        todo_id = todo_res.get("id") or f"receipt:{case_key}"
        status = todo_res.get("status")

        if status == "created":
            stats["created"] += 1
        elif status == "updated":
            stats["updated"] += 1
        elif status == "protected":
            stats["protected"] += 1

        try:
            ack_res = cli.quality_ack(event_id=event_id, todo_id=todo_id, revision=revision, **remaining())
            if ack_res.get("acked") is True:
                stats["acked"] += 1
            else:
                stats["failed_acks"] += 1
                err_msg = mask_secrets(f"事件 {event_id} (Todo {todo_id}) ack 拒绝/未确认: {ack_res.get('reason')}")
                logger.warning(err_msg)
                stats["errors"].append(err_msg)
        except Exception as exc:
            stats["failed_acks"] += 1
            err_msg = mask_secrets(f"事件 {event_id} (Todo {todo_id}) ack 异常: {exc}")
            logger.warning(err_msg)
            stats["errors"].append(err_msg)

    return stats
