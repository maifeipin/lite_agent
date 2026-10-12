"""Read-only, currency-preserving billing evidence and complete Markdown report.

Delegates authoritative billing facts, date scoping, currency calculations,
and anomaly detection to mail-statement-parser read model.
Agent features/billing performs field adaptation and Markdown rendering only,
without direct database access or duplicated financial calculation.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Dict, List, Optional
from integrations.mail.client import MailCLI


def decimal_amount(value) -> Optional[Decimal]:
    """Helper for numeric formatting parsing (maintained for compatibility)."""
    text = str(value if value is not None else '').strip().replace(',', '')
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
        return None
    try:
        amount = Decimal(text)
        return amount if amount.is_finite() else None
    except InvalidOperation:
        return None


def month_start(today, months: int) -> str:
    """Helper for month cutoff formatting (maintained for compatibility)."""
    index = today.year * 12 + today.month - 1 - (months - 1)
    return f'{index // 12:04d}-{index % 12 + 1:02d}'


def build_snapshot(
    db_path: Optional[str] = None,
    months: int = 1,
    detail_limit: int = 20,
    today: Optional[Any] = None
) -> Dict[str, Any]:
    """获取版本化账单快照（完全委托给外部 Parser 权威读模型）。"""
    cli = MailCLI(db_path=db_path) if db_path else MailCLI()
    args = [
        "--months", str(max(1, min(24, int(months)))),
        "--detail-limit", str(max(0, min(100, int(detail_limit))))
    ]
    today = today or date.today()
    if today is not None:
        as_of_str = today.isoformat() if hasattr(today, 'isoformat') else str(today)
        args.extend(["--as-of", as_of_str])

    res = cli.run("billing_snapshot", args)
    if res.get("status") != "ok":
        errors = "; ".join(res.get("errors") or ["获取账单快照失败"])
        raise RuntimeError(f"Parser billing_snapshot 失败: {errors}")

    data = res.get("data") or {}
    return data


def model_evidence(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Extract evidence and sample transactions for model prompt generation."""
    result = {k: v for k, v in snapshot.items() if k not in ('transactions', 'unknown_currency_transactions', 'gated_transactions')}
    limit = snapshot.get('model_detail_limit', 20)
    fields = (
        'id', 'bank_code', 'txn_date', 'description', 'amount', 'currency', 'currency_source',
        'direction', 'original_amount', 'original_currency', 'original_currency_source'
    )

    def sample(rows):
        return [{k: row.get(k) for k in fields} for row in (rows or [])[:limit]]

    result['transaction_examples'] = sample(snapshot.get('transactions', []))
    for key in ('unknown_currency_transactions', 'gated_transactions'):
        rows = snapshot.get(key, [])
        result[key] = sample(rows)
        result[key + '_count'] = len(rows)
    result['details_truncated'] = snapshot.get('transaction_count', 0) > limit
    return result


def _cell(value: Any) -> str:
    return str(value if value is not None else '').replace('|', '\\|').replace('\n', ' ')


def table(headers: List[str], rows: List[List[Any]]) -> str:
    return '\n'.join(
        ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
        ['| ' + ' | '.join(_cell(v) for v in row) + ' |' for row in rows]
    )


def render_report(snapshot: Dict[str, Any], analysis: str = '', fetch_log: str = '', usage: Optional[Dict[str, Any]] = None) -> str:
    """Render deterministic financial snapshot to Markdown report."""
    parts = [
        f"# 银行账单明细与异常报告\n\n截止日期：{snapshot.get('as_of')}\n\n范围：{snapshot.get('scope')}",
        f"数据库共 {snapshot.get('database_statement_count', 0)} 条账单；本报告 {snapshot.get('statement_count', 0)} 条账单、{snapshot.get('transaction_count', 0)} 条交易。",
        '## 分析\n\n' + (analysis or '未生成模型分析；以下为确定性数据与异常检查。'),
        '## 异常与数据缺口\n\n' + '\n'.join('- ' + item for item in (snapshot.get('anomalies') or ['未发现规则检测范围内的异常。'])),
        '## 口径说明\n\n' + '\n'.join('- ' + item for item in snapshot.get('limitations', [])),
        '## 账单明细\n\n' + table(
            ['ID', '邮箱', '银行', '账期', '到期日', '应还', '最低还款', '数据库已还标记'],
            [[r.get(k) for k in ('id', 'account_name', 'bank_code', 'statement_month', 'due_date', 'total_due', 'minimum_due', 'is_paid')]
             for r in snapshot.get('statements', [])]
        ),
        ('## 交易分组金额（不等同消费净额）\n\n' +
         ('覆盖不完整，仅为已确认部分。\n\n' if not snapshot.get('transaction_totals_complete') else '')) +
        table(
            ['币种', '方向', '金额'],
            [[r['currency'], r['direction'], r['amount']] for r in snapshot.get('transaction_totals', [])]
        )
    ]
    parts.append('## 账单余额币种及证据\n\n' + table(
        ['账单ID', '币种', '应还', '最低还款', '币种来源'],
        [[s.get('id'), b.get('currency'), b.get('total_due'), b.get('minimum_due'), b.get('currency_source') or '未知']
         for s in snapshot.get('statements', []) for b in s.get('balances', [])]
    ))
    if snapshot.get('unknown_currency_transactions'):
        parts.append('## 未确认币种交易（已隔离未计入总额）\n\n' + table(
            ['ID', '银行', '交易日期', '描述', '金额', '币种', '来源'],
            [[r.get('id'), r.get('bank_code'), r.get('txn_date'), r.get('description'),
              r.get('amount'), r.get('currency'), r.get('currency_source') or '未标来源']
             for r in snapshot['unknown_currency_transactions']]
        ))
    parts.append('## 完整交易明细\n\n' + table(
        ['ID', '邮箱', '银行', '交易日期', '描述', '金额', '入账币种', '币种证据', '方向', '原始金额', '原币币种', '原币证据'],
        [[r.get(k) for k in ('id', 'account_name', 'bank_code', 'txn_date', 'description', 'amount', 'currency', 'currency_source', 'direction', 'original_amount', 'original_currency', 'original_currency_source')]
         for r in snapshot.get('transactions', [])]
    ))
    if fetch_log:
        parts.append('## 本次拉取记录\n\n```text\n' + fetch_log.replace('```', "'''") + '\n```')
    if usage:
        parts.append('## 执行用量\n\n```json\n' + json.dumps(usage, ensure_ascii=False) + '\n```')
    return '\n\n'.join(parts)
