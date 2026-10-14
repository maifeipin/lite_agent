"""Manual repayment reminders; never infer payment or execute a payment."""
from datetime import datetime, timezone, timedelta
import time
from decimal import Decimal, InvalidOperation

from features.todo.service import TodoService
from integrations.mail.client import MailCLI


def _key(statement_id):
    if not isinstance(statement_id, int) or isinstance(statement_id, bool) or statement_id < 1:
        raise ValueError('必须提供已确认的正整数账单 ID')
    return f'billing_repayment:{statement_id}'


def reconcile_repayment_todos(client, db_path=None, remind_days=3):
    if not isinstance(remind_days,int) or isinstance(remind_days,bool) or not 0 <= remind_days <= 14:
        raise ValueError('remind_days must be 0..14')
    envelope=client.run('billing_snapshot',['--months','24','--detail-limit','0'])
    if not isinstance(envelope,dict) or envelope.get('status')!='ok':
        raise RuntimeError('还款待办快照读取失败')
    service=TodoService(db_path)
    stats={'created':0,'updated':0,'resolved':0,'verification':0,'notices':0}
    records=(envelope.get('data') or {}).get('statements',[])
    latest={}
    for bill in records:
        account_bank=(bill.get('account_name'),bill.get('bank_code'))
        latest[account_bank]=max(latest.get(account_bank,''),bill.get('statement_month') or '')
    for bill in records:
        if not bill.get('account_name') or bill['account_name'].startswith('__'):
            continue
        key=_key(bill['id'])
        stamp=bill.get('updated_at') or bill.get('created_at')
        try:
            date=datetime.fromisoformat(stamp)
            revision=int(date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()*1_000_000)*2
        except (TypeError,ValueError):
            revision=2
        paid=bill.get('is_paid') in (1,True)
        if paid:
            result=service.resolve_source(key,'账单已明确标记为已还款',revision=revision+1)
            stats['resolved']+=int(result.get('resolved') is True)
            continue
        if bill.get('statement_month') != latest[(bill['account_name'],bill['bank_code'])]:
            continue  # Initial enrollment covers the newest bill, not an old backlog.
        try:
            amount=Decimal(str(bill.get('total_due')))
            positive=amount.is_finite() and amount > 0
        except InvalidOperation:
            positive=False
        amount_verified=bill.get('total_due_source')=='extracted'
        if amount_verified and not positive:
            continue
        if positive and amount_verified:
            notice=service.upsert_source(f"billing_statement_notice:{bill['id']}",
                f"新账单：{bill['bank_code']} {bill['statement_month']}（账单 #{bill['id']}）",
                f"本期应还金额：{bill['total_due']}。请核验账单币种与明细；金额不表示已经还款。",
                created_via='billing_notice',kind='notice',project='billing',
                due_at=datetime.now(timezone.utc).isoformat(),
                source_metadata={'revision':revision,'cycle_id':1,'statement_id':bill['id']})
            stats['notices']+=int(notice.get('created') is True)
        due_at=None
        if positive and amount_verified and bill.get('due_date_source')=='extracted':
            try:
                day=datetime.strptime(bill.get('due_date') or '', '%Y-%m-%d')
                due_at=(day-timedelta(days=remind_days)).replace(hour=12,tzinfo=timezone(timedelta(hours=8))).isoformat()
            except ValueError:
                pass
        gates=bill.get('purpose_availability') or {}
        ready=bool(due_at and gates.get('repayment_reminder',{}).get('status')=='available')
        title=('处理还款' if ready else '核验账单及还款')+f"：{bill['bank_code']} {bill['statement_month']}（账单 #{bill['id']}）"
        description=(f"账户：{bill['account_name']}；账单 ID：{bill['id']}；到期日：{bill.get('due_date') or '未确认'}。\n"
                     '请手动核对账单、完成还款，并明确确认该账单已还款；到期不会自动标已还；手动关闭待办只停止提醒，不改变账单还款状态。\n'
                     f"余额证据：{bill.get('balances') or '未确认'}；用途门禁：{gates.get('repayment_reminder')}。")
        result=service.upsert_source(key,title,description,created_via='billing',kind='reminder' if due_at else 'task',
            due_at=due_at,project='billing',source_message_id=bill.get('message_id'),
            source_metadata={'revision':revision,'cycle_id':1,'statement_id':bill['id'],
                             'event_type':'repayment_detected','due_date':bill.get('due_date'),'verification_required':not ready},
            remind_before_mins=0,remind_interval_mins=0,recur_cron='12:00' if due_at else None)
        stats['created']+=int(result.get('created') is True)
        stats['updated']+=int(result.get('status')=='updated')
        stats['verification']+=int(not ready)
    return stats


def confirm_repayment(statement_id, confirmed=False, client=None, db_path=None):
    key=_key(statement_id)
    if confirmed is not True:
        raise ValueError('需用户明确确认该账单已经还款')
    client=client or MailCLI()
    receipt=client.mark_paid(statement_id=statement_id)
    if receipt.get('updated') is not True:
        envelope=client.run('billing_snapshot',['--months','24','--detail-limit','0'])
        records=(envelope.get('data') or {}).get('statements',[]) if isinstance(envelope,dict) and envelope.get('status')=='ok' else []
        if not any(b.get('id')==statement_id and b.get('is_paid') in (1,True) for b in records):
            return receipt
        receipt=dict(receipt,already_paid=True)
    try:
        result=TodoService(db_path).resolve_source(key,'用户明确确认已还款',revision=time.time_ns()//1000*2+1)
        receipt=dict(receipt,todo_sync='done' if result.get('resolved') or result.get('reason') in ('resolution_recorded','stale_event') else result.get('status'))
    except Exception:
        receipt=dict(receipt,todo_sync='retry_required')
    return receipt
