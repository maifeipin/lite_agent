"""
Billing 核心领域服务 (features.billing.service)。
从 skills/ops_billing.py 迁入业务逻辑、CLI 调用与 SQLite 健康巡检。
"""
import os
import subprocess
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from lite_agent.core.config_loader import load_config
from lite_agent.core.utils.masker import mask_secrets


def get_billing_env(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if cfg is None:
        cfg = load_config() or {}
    default_dir = os.environ.get("MAIL_STATEMENT_PARSER_DIR") or os.path.expanduser("~/mail-statement-parser")
    script_dir = cfg.get("billing", {}).get("script_dir", default_dir)
    mail_client_py = os.path.join(script_dir, "mail_client.py")
    stmt_db = cfg.get("billing", {}).get("statement_db_path") or os.path.join(script_dir, "statements.db")
    todo_db = cfg.get("billing", {}).get("todo_db_path")
    return {
        "config": cfg,
        "script_dir": script_dir,
        "mail_client_py": mail_client_py,
        "stmt_db": stmt_db,
        "todo_db": todo_db,
    }


def run_billing_cmd(cmd_args: List[str], timeout: int = 60) -> str:
    """执行账单解析脚本"""
    env = get_billing_env()
    mail_client_py = env["mail_client_py"]
    script_dir = env["script_dir"]

    if not os.path.exists(mail_client_py):
        return f"❌ 找不到账单脚本: {mail_client_py}，请确认账单解析程序是否在该目录。"

    cmd = ["python3", mail_client_py] + cmd_args
    try:
        r = subprocess.run(cmd, cwd=script_dir, capture_output=True, text=True, timeout=timeout)
        output = r.stdout.strip()
        if r.returncode != 0:
            return f"⚠️ 账单脚本执行出错 (代码 {r.returncode}):\n{r.stderr.strip()}"
        return output or "✅ 账单脚本执行成功，无额外输出。"
    except subprocess.TimeoutExpired:
        return f"❌ 账单脚本执行超时 (> {timeout}秒)"
    except Exception as e:
        return f"❌ 账单脚本调用失败: {e}"


def get_report(months: int = 3) -> str:
    """生成银行账单/月度财务汇总报表"""
    return run_billing_cmd(["report", str(months)])


def get_due_soon(months: int = 3, days: int = 7) -> str:
    """检查临近还款日的账单，进行还款提醒"""
    return run_billing_cmd(["due_soon_bills", str(months), str(days)])


def get_unpaid() -> str:
    """查询所有尚未还款的账单"""
    return run_billing_cmd(["unpaid"])


def mark_paid(statement_id: int, confirmed: bool = False) -> str:
    """按明确账单 ID 标记为已还款"""
    if not isinstance(statement_id, int) or isinstance(statement_id, bool) or statement_id <= 0:
        return '❌ 必须提供已确认账单的正整数 ID'
    if confirmed is not True:
        return '❌ 请明确确认该账单已经还款；不会自动标记已还。'
    try:
        from lite_agent.integrations.mail.client import MailCLI
        from lite_agent.features.billing.repayment_todo import confirm_repayment
        env = get_billing_env()
        result = confirm_repayment(
            statement_id,
            confirmed=True,
            client=MailCLI(),
            db_path=env["todo_db"],
        )
        if result.get('updated') is True or result.get('already_paid') is True:
            return (
                f'✅ 账单 #{statement_id} 已标记为已还款'
                + ('；还款提醒同步待重试，下一次账单同步将补齐。' if result.get('todo_sync') == 'retry_required' else '；对应还款待办已处理。')
            )
        return f'❌ 未找到可更新的账单 #{statement_id}'
    except Exception as exc:
        return '❌ 标记还款失败：' + mask_secrets(str(exc))


def reconcile(months: int = 3, tolerance: float = 1.0) -> str:
    """查看账单对账差异报表"""
    return run_billing_cmd(["reconcile", str(months), str(tolerance)])


def get_recent(months: int = 3) -> str:
    """查看最近账单记录的列表汇总"""
    return run_billing_cmd(["recent", str(months)])


def get_snapshot(months: int = 1, detail_limit: int = 20) -> str:
    """外币分析优先使用：读取权威账单快照"""
    import json
    from lite_agent.features.billing.snapshot import build_snapshot, model_evidence
    env = get_billing_env()
    snapshot_data = build_snapshot(env["stmt_db"], months, detail_limit)
    return json.dumps(model_evidence(snapshot_data), ensure_ascii=False)


def fetch_and_consume(months: int = 1, account: str = '', cfg: Optional[Dict[str, Any]] = None) -> str:
    """拉取最新账单并入库，单次遍历流水线：下载归档 + 解析入库，并消费质量事件"""
    from lite_agent.integrations.mail.sync_service import sync_and_consume
    env = get_billing_env(cfg)
    res = sync_and_consume(
        script_dir=env["script_dir"],
        statement_db_path=env["stmt_db"],
        todo_db_path=env["todo_db"],
        months=months,
        account=account or None,
    )

    lines = []
    summary = res.get("summary")
    fetch_errors = res.get("fetch_errors", [])
    if summary and not fetch_errors:
        accounts = summary.get("accounts", [])
        lines.append(f"✅ 账单邮箱同步完成 ({len(accounts)} 个账户)")
        for a in accounts:
            name = a.get("account", "?")
            lines.append(f"  - 账户 {name}: 状态 {a.get('status', 'ok')}, 新增 {a.get('new_bills', a.get('inserted', 0))} 封")
    elif fetch_errors:
        lines.append("⚠️ 账单邮箱同步存在异常:")
        for err in fetch_errors:
            lines.append(f"  - {err}")
    else:
        lines.append("✅ 账单邮箱同步执行完毕。")

    q = res.get("quality_stats", {})
    q_errors = q.get("errors", [])
    if q.get("total", 0) > 0:
        lines.append(
            f"\n📋 质量事件与待办处理: 共扫描 {q.get('total')} 个事件 "
            f"(新增待办 {q.get('created')}, 更新 {q.get('updated')}, 解决 {q.get('resolved')}, "
            f"跳过出账 {q.get('skipped')}, 成功确认 {q.get('acked')})"
        )
    elif q.get("failed", 0) or q.get("failed_acks", 0) or q_errors:
        lines.append("\n⚠️ 质量事件读取/确认失败，无法判断是否有待处理解析异常。")
    else:
        lines.append("\n📋 质量事件: 本轮未发现待处理事件（既有未完成 Todo 仍需处理）。")

    if q.get("backlog_hit"):
        lines.append("⚠️ 提示: 达到批次消费上限，仍有未确认质量事件积压，将在下次同步时继续处理。")
    if q.get("failed", 0) > 0 or q.get("failed_acks", 0) > 0 or (q_errors and not q.get("backlog_hit")):
        lines.append(f"⚠️ 待办处理异常: 写入失败 {q.get('failed', 0)} 笔, 确认失败 {q.get('failed_acks', 0)} 笔")
        for q_err in q_errors[:3]:
            lines.append(f"  - {q_err}")

    return mask_secrets("\n".join(lines))


def get_txns_over(amount: float, months: int = 3) -> str:
    """查询金额大于某个阈值的大额交易明细"""
    args = ["txns_over", str(amount)]
    if months > 0:
        args.append(str(months))
    return run_billing_cmd(args)


def check_parse_health(threshold: float = 0.5, days: int = 30) -> str:
    """检查近 30 天各银行账单解析健康度（NULL 比率异常即可能正则失效）"""
    months = max(1, (days + 29) // 30)
    try:
        from lite_agent.features.billing.snapshot import build_snapshot
        snapshot = build_snapshot(months=months, detail_limit=0)
        statements = snapshot.get("statements") or []
    except Exception as exc:
        try:
            from lite_agent.integrations.mail.paths import connect_readonly_db
            conn = connect_readonly_db()
            if not conn:
                return f"❌ 找不到账单数据库或服务不可用: {exc}"
            cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT bank_code, COUNT(*) as total, SUM(CASE WHEN total_due IS NULL THEN 1 ELSE 0 END) as null_count
                FROM statements
                WHERE created_at > ?
                GROUP BY bank_code
            """, (cutoff_date,))
            rows = cursor.fetchall()
            conn.close()
            if not rows:
                return f"✅ 近 {days} 天无新账单入库，暂无解析异常。"
            lines = [f"📊 **账单解析健康度检查 (近 {days} 天)**\n"]
            has_warning = False
            for row in rows:
                bank, total, null_count = row[0], row[1], row[2] or 0
                ratio = null_count / total if total > 0 else 0
                status = "✅"
                if ratio > threshold:
                    status = "⚠️"
                    has_warning = True
                lines.append(f"{status} **{bank}**: 总计 {total} 笔，NULL {null_count} 笔 (占比 {ratio*100:.1f}%)")
            if has_warning:
                lines.append("\n⚠️ 发现解析异常！某些银行的 NULL 比例过高，可能是邮件模板格式发生变更导致正则失效，建议尽快排查。")
            else:
                lines.append("\n✅ 所有银行解析健康，未超出报警阈值。")
            return "\n".join(lines)
        except Exception:
            return f"❌ 账单解析健康度检查失败: {exc}"

    if not statements:
        return f"✅ 近 {days} 天无新账单入库，暂无解析异常。"

    bank_stats: Dict[str, Dict[str, int]] = {}
    cutoff_dt = datetime.now() - timedelta(days=days)
    for st in statements:
        created_str = st.get("created_at")
        if created_str:
            try:
                st_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00")).replace(tzinfo=None)
                if st_dt < cutoff_dt:
                    continue
            except Exception:
                pass
        bank = st.get("bank_code") or "UNKNOWN"
        if bank not in bank_stats:
            bank_stats[bank] = {"total": 0, "null": 0}
        bank_stats[bank]["total"] += 1
        if st.get("total_due") is None:
            bank_stats[bank]["null"] += 1

    if not bank_stats:
        return f"✅ 近 {days} 天无新账单入库，暂无解析异常。"

    lines = [f"📊 **账单解析健康度检查 (近 {days} 天)**\n"]
    has_warning = False
    for bank, stats in bank_stats.items():
        total = stats["total"]
        null_count = stats["null"]
        ratio = null_count / total if total > 0 else 0
        status = "✅"
        if ratio > threshold:
            status = "⚠️"
            has_warning = True
        lines.append(f"{status} **{bank}**: 总计 {total} 笔，NULL {null_count} 笔 (占比 {ratio*100:.1f}%)")

    if has_warning:
        lines.append("\n⚠️ 发现解析异常！某些银行的 NULL 比例过高，可能是邮件模板格式发生变更导致正则失效，建议尽快排查。")
    else:
        lines.append("\n✅ 所有银行解析健康，未超出报警阈值。")

    return "\n".join(lines)


def billing_health_cron() -> Optional[str]:
    """定时作业：每周日跑账单解析健康度检查，仅在告警时推送"""
    if datetime.now().weekday() != 6:
        return None
    res = check_parse_health(threshold=0.5, days=30)
    if "⚠️" in res:
        return res
    return None


def billing_fetch_cron() -> None:
    """定时作业：每日自动抓取，确保数据库账单状态最新"""
    res = fetch_and_consume(months=1)
    print(f"[billing_fetch_cron] 自动同步账单结果: {res}")
    return None
