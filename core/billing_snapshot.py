"""Read-only, currency-preserving billing evidence and complete Markdown report."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import re
import sqlite3


def decimal_amount(value):
    text = str(value if value is not None else '').strip().replace(',', '')
    if not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
        return None
    try:
        amount = Decimal(text)
        return amount if amount.is_finite() else None
    except InvalidOperation:
        return None


def month_start(today, months):
    index = today.year * 12 + today.month - 1 - (months - 1)
    return f'{index // 12:04d}-{index % 12 + 1:02d}'


def _purpose(raw, purpose):
    # Persisted parser contract only: banking rules remain in the mail project.
    try:
        gate=json.loads(raw or '{}').get('quality',{}).get('purpose_availability',{}).get(purpose,{})
        if gate.get('status') in ('available','partial','blocked'):
            return {'status':gate['status'],'reasons':gate.get('reasons') or []}
    except (ValueError,TypeError,AttributeError):
        pass
    return {'status':'blocked' if purpose=='repayment_reminder' else 'partial',
            'reasons':[{'code':'QUALITY_UNVERIFIED','reason':'用途证据未核验，需重解析'}]}


def build_snapshot(db_path, months=1, detail_limit=20, today=None):
    today = today or date.today()
    months = max(1, min(24, int(months)))
    detail_limit = max(0, min(100, int(detail_limit)))
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        # Latest month per mailbox/bank, including older data as an explicit
        # fallback. A missing current month is a freshness gap, not zero spend.
        database_rows = [dict(row) for row in conn.execute('SELECT * FROM statements ORDER BY bank_code, statement_month DESC, id DESC')]
        failed_rows = [row for row in database_rows if row.get('parse_status') == 'failed']
        all_rows = [row for row in database_rows if row.get('parse_status') != 'failed']
        cutoff = month_start(today, months)
        latest = {}
        for row in all_rows:
            key = (row['account_name'], row['bank_code'])
            latest[key] = max(latest.get(key, ''), row['statement_month'] or '')
        selected = [row for row in all_rows if (row['statement_month'] or '') >= cutoff or
                    row['statement_month'] == latest[(row['account_name'], row['bank_code'])]]
        availability={r['id']:{purpose:_purpose(r.get('raw_fields_json'),purpose)
            for purpose in ('financial_summary','transaction_trends','fx_analysis','repayment_reminder')} for r in selected}
        balances={}
        balance_table=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='statement_balances'").fetchone()
        for row in selected:
            entries=[dict(b) for b in conn.execute('SELECT currency,total_due,minimum_due,currency_source FROM statement_balances WHERE statement_id=?',(row['id'],))] if balance_table else []
            if not entries:
                entries=[{'currency':'UNKNOWN','currency_source':None,'total_due':row['total_due'],'minimum_due':row['minimum_due']}]
            for b in entries:
                b['currency_confirmed']=b.get('currency_source') in ('extracted','rule_declared') and b.get('currency') in ('CNY','USD','JPY','EUR','HKD','GBP','AUD','CAD','SGD','CHF','KRW')
                if not b['currency_confirmed']:
                    b['reported_currency']=b['currency']
                    b['currency']='UNKNOWN'
            balances[row['id']]=entries
        txns = []
        anchored = 'statement_id' in {row[1] for row in conn.execute('PRAGMA table_info(statement_transactions)')}
        for row in selected:
            # The anchor is authoritative after schema migration. Legacy UID
            # association is used only for databases without the anchor column.
            query = ('SELECT * FROM statement_transactions WHERE statement_id=? ORDER BY txn_date, id'
                     if anchored else '''SELECT * FROM statement_transactions
                     WHERE bank_code=? AND uid=? AND account_name=? ORDER BY txn_date, id''')
            params = (row['id'],) if anchored else (row['bank_code'], row['uid'], row['account_name'])
            for txn in conn.execute(query, params):
                item=dict(txn)
                item['purpose_availability']=availability[row['id']]
                txns.append(item)
        txns = list({row['id']: row for row in txns}.values())
        anomalies = [f"账单 #{row['id']} 解析失败，已排除出账期选择、金额汇总及交易明细，需离线重解析。"
                     for row in failed_rows]
        groups = {}
        for row in selected:
            groups.setdefault((row['account_name'], row['bank_code'], row['statement_month']), []).append(row['id'])
            amount = decimal_amount(row['total_due'])
            if amount is None:
                anomalies.append(f"账单 #{row['id']} 应还金额缺失或无法无歧义解析：{row['total_due']!r}。")
            if row['statement_month'] and row['statement_month'] > today.strftime('%Y-%m'):
                anomalies.append(f"账单 #{row['id']} 账期晚于当前月份，需核对解析。")
            if not row['is_paid']:
                due = str(row.get('due_date') or '')
                try:
                    days = (date.fromisoformat(due[:10]) - today).days
                    if days < 0:
                        anomalies.append(f"账单 #{row['id']} 数据库仍标为未还款，记录的到期日 {due} 已过；需核实实际还款状态。")
                    elif days <= 7:
                        anomalies.append(f"账单 #{row['id']} 数据库标为未还款，{days} 天内到期（{due}）。")
                except ValueError:
                    anomalies.append(f"账单 #{row['id']} 未还款记录的到期日缺失或格式异常：{due!r}。")
        for key, ids in groups.items():
            if len(ids) > 1:
                anomalies.append(f"{key[1]} / {key[2]} / {key[0]} 有 {len(ids)} 条账单（ID {ids}）；可能包含多卡或重复记录，未经核实不合并计费。")
        for (account, bank), month in latest.items():
            if month < today.strftime('%Y-%m'):
                anomalies.append(f'{bank} / {account} 最新账期为 {month}，当前月账单尚未入库；本报告包含该最新历史账期。')
        sums = {}
        unknown_txns = []
        gated_txns = []
        for txn in txns:
            amount = decimal_amount(txn['amount'])
            if amount is None:
                anomalies.append(f"交易 #{txn['id']} 金额缺失或无法解析。")
                continue
            c_src = txn.get('currency_source')
            is_confirmed = c_src in ('rule_declared','extracted') and txn.get('currency') in ('CNY','USD','JPY','EUR','HKD','GBP','AUD','CAD','SGD','CHF','KRW')
            gate=txn['purpose_availability']['transaction_trends']
            if gate['status']=='blocked':
                gated_txns.append(txn)
            if is_confirmed and gate['status']!='blocked':
                key = (txn.get('currency'), txn.get('direction') or '未标方向')
                sums[key] = sums.get(key, Decimal('0')) + amount
            elif not is_confirmed:
                unknown_txns.append(txn)
        if unknown_txns:
            anomalies.append(f"存在 {len(unknown_txns)} 笔未确认币种的交易，未纳入确认合计，需核实。")
        for row in selected:
            for purpose,gate in availability[row['id']].items():
                if gate['status']!='available':
                    anomalies.append(f"账单 #{row['id']} 用途 {purpose}: {gate['status']}；原因 {gate['reasons']}。")
        if not selected:
            anomalies.append('数据库无账单，无法生成账单分析。')
        compact_statements = [{k: row.get(k) for k in ('id', 'account_name', 'bank_code', 'statement_month',
                    'due_date', 'total_due', 'minimum_due', 'is_paid', 'created_at')} for row in selected]
        for row in compact_statements:
            row['purpose_availability']=availability[row['id']]
            row['balances']=balances[row['id']]
        return {
            'generated_at': datetime.now(timezone.utc).isoformat(), 'as_of': today.isoformat(),
            'scope': f'账期 {cutoff} 起，另含各邮箱/银行最新历史账期',
            'latest_months': [{'account': key[0], 'bank': key[1], 'month': value} for key, value in latest.items()],
            'database_statement_count': len(database_rows), 'failed_statement_count': len(failed_rows),
            'statement_count': len(selected),
            'transaction_count': len(txns), 'statements': compact_statements,
            'transaction_totals': [{'currency': k[0], 'direction': k[1], 'amount': str(v)} for k, v in sums.items()],
            'unknown_currency_transactions': unknown_txns,
            'gated_transactions': gated_txns,
            'transaction_totals_complete': not unknown_txns and not gated_txns and all(g['transaction_trends']['status']=='available' for g in availability.values()),
            'transactions': txns, 'anomalies': anomalies,
            'limitations': ['账单应还金额没有独立币种字段，不跨账单直接求和。',
                '交易按原币种和原方向分别汇总，不把还款、退款或外币直接当作消费。',
                '未还款状态来自数据库标记，不能据此确认现实中的逾期。',
                '余额、上期结转和利息等口径未核实，交易合计与应还金额不直接作为对账差异。'],
            'model_detail_limit': detail_limit,
        }
    finally:
        conn.close()


def model_evidence(snapshot):
    result={k:v for k,v in snapshot.items() if k not in ('transactions','unknown_currency_transactions','gated_transactions')}
    limit=snapshot['model_detail_limit']
    fields=('id','bank_code','txn_date','description','amount','currency','currency_source',
            'direction','original_amount','original_currency','original_currency_source')
    def sample(rows):
        return [{k:row.get(k) for k in fields} for row in rows[:limit]]
    result['transaction_examples']=sample(snapshot['transactions'])
    for key in ('unknown_currency_transactions','gated_transactions'):
        rows=snapshot.get(key,[])
        result[key]=sample(rows)
        result[key+'_count']=len(rows)
    result['details_truncated']=snapshot['transaction_count']>limit
    return result


def _cell(value):
    return str(value if value is not None else '').replace('|', '\\|').replace('\n', ' ')


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
                     ['| ' + ' | '.join(_cell(v) for v in row) + ' |' for row in rows])


def render_report(snapshot, analysis='', fetch_log='', usage=None):
    parts = [f"# 银行账单明细与异常报告\n\n截止日期：{snapshot['as_of']}\n\n范围：{snapshot['scope']}",
             f"数据库共 {snapshot['database_statement_count']} 条账单；本报告 {snapshot['statement_count']} 条账单、{snapshot['transaction_count']} 条交易。",
             '## 分析\n\n' + (analysis or '未生成模型分析；以下为确定性数据与异常检查。'),
             '## 异常与数据缺口\n\n' + '\n'.join('- ' + item for item in (snapshot['anomalies'] or ['未发现规则检测范围内的异常。'])),
             '## 口径说明\n\n' + '\n'.join('- ' + item for item in snapshot['limitations']),
             '## 账单明细\n\n' + table(['ID', '邮箱', '银行', '账期', '到期日', '应还', '最低还款', '数据库已还标记'],
                [[r.get(k) for k in ('id', 'account_name', 'bank_code', 'statement_month', 'due_date', 'total_due', 'minimum_due', 'is_paid')] for r in snapshot['statements']]),
             ('## 交易分组金额（不等同消费净额）\n\n' + ('覆盖不完整，仅为已确认部分。\n\n' if not snapshot.get('transaction_totals_complete') else '')) + table(['币种', '方向', '金额'],
                [[r['currency'], r['direction'], r['amount']] for r in snapshot['transaction_totals']])]
    parts.append('## 账单余额币种及证据\n\n'+table(['账单ID','币种','应还','最低还款','币种来源'],
        [[s['id'],b.get('currency'),b.get('total_due'),b.get('minimum_due'),b.get('currency_source') or '未知']
         for s in snapshot['statements'] for b in s.get('balances',[])]))
    if snapshot.get('unknown_currency_transactions'):
        parts.append('## 未确认币种交易（已隔离未计入总额）\n\n' + table(
            ['ID', '银行', '交易日期', '描述', '金额', '币种', '来源'],
            [[r.get('id'), r.get('bank_code'), r.get('txn_date'), r.get('description'),
              r.get('amount'), r.get('currency'), r.get('currency_source') or '未标来源']
             for r in snapshot['unknown_currency_transactions']]))
    parts.append('## 完整交易明细\n\n' + table(['ID', '邮箱', '银行', '交易日期', '描述', '金额', '入账币种', '币种证据', '方向', '原始金额', '原币币种', '原币证据'],
                [[r.get(k) for k in ('id', 'account_name', 'bank_code', 'txn_date', 'description', 'amount', 'currency', 'currency_source', 'direction', 'original_amount', 'original_currency', 'original_currency_source')] for r in snapshot['transactions']]))
    if fetch_log:
        parts.append('## 本次拉取记录\n\n```text\n' + fetch_log.replace('```', "'''") + '\n```')
    if usage:
        parts.append('## 执行用量\n\n```json\n' + json.dumps(usage, ensure_ascii=False) + '\n```')
    return '\n\n'.join(parts)
