import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill
import subprocess

# 设定账单解析程序的绝对路径
BILLING_SCRIPT_DIR = "/root/mail-statement-parser"
MAIL_CLIENT_PY = os.path.join(BILLING_SCRIPT_DIR, "mail_client.py")

def _run_billing_cmd(cmd_args: list, timeout=60) -> str:
    """内部通用函数：执行账单解析脚本"""
    if not os.path.exists(MAIL_CLIENT_PY):
        return f"❌ 找不到账单脚本: {MAIL_CLIENT_PY}，请确认账单解析程序是否在该目录。"
        
    cmd = ["python3", MAIL_CLIENT_PY] + cmd_args
    try:
        r = subprocess.run(cmd, cwd=BILLING_SCRIPT_DIR, capture_output=True, text=True, timeout=timeout)
        output = r.stdout.strip()
        if r.returncode != 0:
            return f"⚠️ 账单脚本执行出错 (代码 {r.returncode}):\n{r.stderr.strip()}"
        return output or "✅ 账单脚本执行成功，无额外输出。"
    except subprocess.TimeoutExpired:
        return f"❌ 账单脚本执行超时 (> {timeout}秒)"
    except Exception as e:
        return f"❌ 账单脚本调用失败: {e}"

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
    return _run_billing_cmd(["report", str(months)])

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
    return _run_billing_cmd(["due_soon_bills", str(months), str(days)])

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
    return _run_billing_cmd(["reconcile", str(months), str(tolerance)])

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
    return _run_billing_cmd(["recent", str(months)])

@skill(
    name='billing_fetch',
    description='批量从邮箱下载最新的账单邮件，解析并入库 (自动同步最新账单数据)。比较耗时，请耐心等待',
    params={
        'months': {
            'type': 'integer',
            'description': '下载最近几个月的账单邮件，默认 1',
            'default': 1
        }
    }
)
def billing_fetch(months: int = 1) -> str:
    # 结合下载并入库：mail_client.py 好像 exec3m (即 download_bank_bills) + validate (validate_bank_bills)
    # 根据原菜单，[2] 是 download_bank_bills, [3] 是 validate_bank_bills
    # 我们可以连续执行两次
    out1 = _run_billing_cmd(["exec3m", str(months)], timeout=180)
    out2 = _run_billing_cmd(["validate3m", str(months)], timeout=180)
    return f"--- 步骤1: 邮件下载 ---\n{out1}\n\n--- 步骤2: 账单解析与入库 ---\n{out2}"

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
    args = ["txns_over", str(amount)]
    if months > 0:
        args.append(str(months))
    return _run_billing_cmd(args)
