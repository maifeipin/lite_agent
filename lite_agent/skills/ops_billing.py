import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.billing import service as billing_service
from lite_agent.features.billing.snapshot import build_snapshot, render_report

from lite_agent.core.config_loader import load_config
from lite_agent.core.cron_engine import CronManager

# 向后兼容常量、配置与函数代理
_cfg = load_config() or {}
_mgr = CronManager()
BILLING_SCRIPT_DIR = billing_service.get_billing_env()["script_dir"]
MAIL_CLIENT_PY = billing_service.get_billing_env()["mail_client_py"]
_run_billing_cmd = billing_service.run_billing_cmd
_billing_health_cron = billing_service.billing_health_cron

def _billing_fetch_cron():
    res = billing_fetch(months=1)
    print(f"[billing_fetch_cron] 自动同步账单结果: {res}")
    return None


@skill(
    name='billing_report',
    description='生成银行账单/月度财务汇总报表 (含境外交易)',
    params={
        'months': {
            'type': 'integer',
            'description': '回溯查看的月数，默认 3',
            'default': 3
        }
    }
)
def billing_report(months: int = 3) -> str:
    return billing_service.get_report(months=months)


@skill(
    name='billing_due_soon',
    description='检查临近还款日的账单，进行还款提醒',
    params={
        'months': {
            'type': 'integer',
            'description': '回溯账单月数，默认 3',
            'default': 3
        },
        'days': {
            'type': 'integer',
            'description': '临期天数阈值（比如7天内），默认 7',
            'default': 7
        }
    }
)
def billing_due_soon(months: int = 3, days: int = 7) -> str:
    return billing_service.get_due_soon(months=months, days=days)


@skill(
    name='billing_unpaid',
    description='查询所有尚未还款的账单',
    params={}
)
def billing_unpaid() -> str:
    return billing_service.get_unpaid()


@skill(
    name='billing_mark_paid',
    description='按明确账单 ID 标记为已还款',
    params={
        'statement_id': {
            'type': 'integer',
            'description': '账单记录的正整数 ID，必须先查询确认具体账单'
        },
        'confirmed': {'type': 'boolean', 'description': '仅当用户明确确认已完成该账单还款时为 true', 'default': False}
    }
)
def billing_mark_paid(statement_id: int, confirmed: bool = False) -> str:
    return billing_service.mark_paid(statement_id=statement_id, confirmed=confirmed)


@skill(
    name='billing_reconcile',
    description='查看账单对账差异报表 (检查应还款和实际交易明细总和是否对得上)',
    params={
        'months': {
            'type': 'integer',
            'description': '回溯查看的月数，默认 3',
            'default': 3
        },
        'tolerance': {
            'type': 'number',
            'description': '允许的对账偏差金额（因为汇率可能有几分钱差别），默认 1.0',
            'default': 1.0
        }
    }
)
def billing_reconcile(months: int = 3, tolerance: float = 1.0) -> str:
    return billing_service.reconcile(months=months, tolerance=tolerance)


@skill(
    name='billing_recent',
    description='查看最近账单记录的列表汇总（精简版）',
    params={
        'months': {
            'type': 'integer',
            'description': '回溯查看的月数，默认 3',
            'default': 3
        }
    }
)
def billing_recent(months: int = 3) -> str:
    return billing_service.get_recent(months=months)


@skill(
    name='billing_snapshot',
    description='外币分析优先使用：读取权威账单快照，含分币种汇总、交易样例、原币金额/币种及来源、币种缺失与用途门禁。无需自行写 SQL；样例不是全量，UNKNOWN 不猜币种，境外位置不等于币种；只读',
    params={
        'months': {'type': 'integer', 'default': 1},
        'detail_limit': {'type': 'integer', 'default': 20}
    },
    side_effect=False
)
def billing_snapshot(months: int = 1, detail_limit: int = 20) -> str:
    return billing_service.get_snapshot(months=months, detail_limit=detail_limit)


@skill(
    name='billing_fetch',
    reuse_within_run=True,
    description='拉取最新账单并入库，默认仅同步配置的163账单邮箱；其他邮箱必须由用户明确指定 account。同一次请求同步一次后分析已有结果，不重复拉取。',
    params={
        'months': {
            'type': 'integer',
            'description': '下载最近几个月的账单邮件，默认 1',
            'default': 1
        },
        'account': {
            'type': 'string',
            'description': '用户明确指定的邮箱地址；空值使用配置的163账单邮箱',
            'default': ''
        }
    }
)
def billing_fetch(months: int = 1, account: str = '') -> str:
    return billing_service.fetch_and_consume(months=months, account=account, cfg=_cfg)


@skill(
    name='billing_txns_over',
    description='查询金额大于某个阈值的大额交易明细',
    params={
        'amount': {
            'type': 'number',
            'description': '筛选金额阈值'
        },
        'months': {
            'type': 'integer',
            'description': '回溯查看的月数，默认 3 (0 表示查询所有历史)',
            'default': 3
        }
    }
)
def billing_txns_over(amount: float, months: int = 3) -> str:
    return billing_service.get_txns_over(amount=amount, months=months)


@skill(
    name="billing_parse_health",
    description="检查近 30 天各银行账单解析健康度（NULL 比率异常即可能正则失效）",
    params={
        'threshold': {'type': 'number', 'description': '报警阈值，比如 0.5', 'default': 0.5},
        'days': {'type': 'integer', 'description': '检查近多少天', 'default': 30}
    }
)
def billing_parse_health(threshold: float = 0.5, days: int = 30) -> str:
    return billing_service.check_parse_health(threshold=threshold, days=days)
