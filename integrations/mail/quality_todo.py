import json
import logging
from typing import Optional, Dict, Any, List

from integrations.mail.client import MailCLI
from skills.ops_todo import upsert_source_todo, resolve_source_todo

logger = logging.getLogger(__name__)


def _format_quality_todo_content(event: Dict[str, Any]) -> tuple:
    """
    根据质量事件生成规范的标题与 Markdown 格式详细描述
    """
    bank_code = event.get("bank_code") or "未知银行"
    template_version = event.get("template_version") or "未声明"
    occurrence_count = event.get("occurrence_count", 1)
    evidence_refs = event.get("evidence_refs") or []
    affected_count = len(evidence_refs) if evidence_refs else occurrence_count
    case_key = event.get("case_key") or ""
    first_seen = event.get("first_seen_at") or ""
    last_seen = event.get("last_seen_at") or ""
    diagnostics = event.get("diagnostics") or []

    # 提取核心异常诊断代码列表
    diag_codes = [d.get("code") for d in diagnostics if isinstance(d, dict) and d.get("code")]
    code_summary = ", ".join(list(dict.fromkeys(diag_codes))[:3]) if diag_codes else "解析异常"

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
    limit: int = 100
) -> Dict[str, Any]:
    """
    拉取未确认的质量事件，幂等写入 Todo 数据库，并在成功后执行 ack（重构契约 §1.3 / R4）。
    严格保证:
    1. 事件类型严格分流：
       - `statement_recorded`: 出账单记录事件，专属账单/还款适配器，质量消费者严禁消费与 ack（跳过）；
       - `quality_resolved`: 上游解决事件，调用 resolve_source_todo 关闭对应 Todo，不建异常任务，带 revision ack；
       - `quality_detected`: 异常事件，根据 case_key 幂等写入 Todo，带 revision ack；
    2. dropped 状态的任务绝不复活 (protected)；
    3. snoozed/shelved 状态的任务保留人类偏好；
    4. 仅在 Todo 写入成功后才调用 quality_ack；写入失败时不 ack；
    5. 严格核验 quality_ack 返回值，未确认成功时不得虚报 acked 计数。
    """
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

    try:
        events = cli.quality_events(limit=limit)
    except Exception as exc:
        err_msg = f"拉取质量事件失败: {exc}"
        logger.error(err_msg)
        stats["errors"].append(err_msg)
        stats["failed"] += 1
        return stats

    stats["total"] = len(events)

    for event in events:
        if not isinstance(event, dict):
            stats['failed'] += 1
            stats['errors'].append('Invalid quality event object')
            continue
        event_id = event.get("event_id")
        case_key = event.get("case_key")
        if not isinstance(event_id, str) or not event_id.strip() or not isinstance(case_key, str) or not case_key.strip():
            stats["failed"] += 1
            stats["errors"].append(f"事件缺少必要字段 (event_id/case_key): {event}")
            continue

        event_type = event.get("event_type")
        revision = event.get("revision")
        cycle_id = event.get("cycle_id")

        # 1. 新账单记录事件 (statement_recorded) 专属还款适配器，质量消费者严禁消费与 ack
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

        # 2. 显式解决事件 (quality_resolved)
        if event_type == "quality_resolved":
            source_key = f"mail_quality:{case_key}"
            try:
                res_todo = resolve_source_todo(
                    source_key=source_key,
                    resolution_note=f"上游质量问题已确认解决 (cycle={cycle_id}, rev={revision})",
                    cycle_id=cycle_id,
                    revision=revision,
                    db_path=db_path
                )
                if res_todo.get('resolved') is True:
                    stats['resolved'] += 1
                else:
                    stats['protected'] += 1
                todo_id = res_todo.get("id") or f"resolved:{case_key}"
            except Exception as exc:
                err_msg = f"解决事件 {event_id} 处理 Todo 异常: {exc}"
                logger.error(err_msg)
                stats["failed"] += 1
                stats["errors"].append(err_msg)
                continue

            # 确认解决事件（带 revision）
            try:
                ack_res = cli.quality_ack(event_id=event_id, todo_id=todo_id, revision=revision)
                if ack_res.get("acked") is True:
                    stats["acked"] += 1
                else:
                    stats["failed_acks"] += 1
                    err_msg = f"解决事件 {event_id} ack 未确认: {ack_res.get('reason')}"
                    logger.warning(err_msg)
                    stats["errors"].append(err_msg)
            except Exception as exc:
                stats["failed_acks"] += 1
                err_msg = f"解决事件 {event_id} ack 失败: {exc}"
                logger.warning(err_msg)
                stats["errors"].append(err_msg)
            continue

        # 3. 质量异常诊断事件 (quality_detected 等)
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
            todo_res = upsert_source_todo(
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
            err_msg = f"事件 {event_id} 写入 Todo 失败: {exc}"
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

        # Todo 写入成功，调用 quality_ack 进行幂等条件确认（带 revision）
        try:
            ack_res = cli.quality_ack(event_id=event_id, todo_id=todo_id, revision=revision)
            if ack_res.get("acked") is True:
                stats["acked"] += 1
            else:
                stats["failed_acks"] += 1
                err_msg = f"事件 {event_id} (Todo {todo_id}) ack 拒绝/未确认: {ack_res.get('reason')}"
                logger.warning(err_msg)
                stats["errors"].append(err_msg)
        except Exception as exc:
            stats["failed_acks"] += 1
            err_msg = f"事件 {event_id} (Todo {todo_id}) ack 异常: {exc}"
            logger.warning(err_msg)
            stats["errors"].append(err_msg)

    return stats
