"""
Billing 开支明细与汇总导出单元测试 (dev/tests/features/test_billing_export.py)。
覆盖自定义时间解析 (按年、跨年、近 N 月、起止日期、全量)、
数据质量约束 (母账单关联、failed 排除、用途门禁 available/partial/blocked、币种确权隔离)、
原币确权检查与未确权标记、
收支分类防误计 (未知方向与符号冲突优先隔离、debit退款服务费确认为支出)、
日期真实性/先后顺序校验与精确日筛选 (无月份OR放宽穿透)、
单次 UTF-8 BOM CSV 读写校验、
HedgeDoc 隐私安全控制 (默认 public=False)、
/cmd export 命令年份/月数分流以及领域路由覆盖。
"""
import csv
import json
import os
import re
import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

from lite_agent.features.billing.export_service import (
    parse_date_range,
    _validate_and_normalize_date,
    _clean_decimal,
    classify_transaction,
    check_repayment_consistency,
    confirmed_currency,
    stored_purpose,
    query_expense_data,
    generate_expense_csv_content,
    write_expense_csv_file,
    build_expense_markdown_report,
    export_expense_report,
)
from lite_agent.features.billing import BILLING_DOMAIN_SPEC, _handle_cmd
from lite_agent.features.billing.service import export_expenses


def test_parse_date_range_various_modes():
    ref = date(2026, 11, 10)

    # 1. 单一年份
    s, e, desc = parse_date_range(year=2024, as_of=ref)
    assert s == "2024-01-01"
    assert e == "2024-12-31"
    assert "2024年全量" in desc

    # 2. 跨年份区间
    s, e, desc = parse_date_range(year="2022-2024", as_of=ref)
    assert s == "2022-01-01"
    assert e == "2024-12-31"
    assert "2022年至2024年" in desc

    s, e, desc = parse_date_range(year="2023至2025年", as_of=ref)
    assert s == "2023-01-01"
    assert e == "2025-12-31"

    # 3. 自然语言：今年、去年、全部
    s, e, desc = parse_date_range(year="今年", as_of=ref)
    assert s == "2026-01-01"
    assert e == "2026-12-31"

    s, e, desc = parse_date_range(year="去年", as_of=ref)
    assert s == "2025-01-01"
    assert e == "2025-12-31"

    s, e, desc = parse_date_range(year="全部", as_of=ref)
    assert s is None
    assert e is None
    assert desc == "全部历史"

    # 4. 近 N 年
    s, e, desc = parse_date_range(year="近3年", as_of=ref)
    assert s == "2023-12-01"
    assert e == "2026-11-10"

    # 5. 精确起止年月 (YYYY-MM)
    s, e, desc = parse_date_range(start_date="2024-03", end_date="2024-08", as_of=ref)
    assert s == "2024-03-01"
    assert e == "2024-08-31"

    # 6. 精确起止日 (YYYY-MM-DD)
    s, e, desc = parse_date_range(start_date="2025-05-10", end_date="2025-06-20", as_of=ref)
    assert s == "2025-05-10"
    assert e == "2025-06-20"

    # 7. 相对月数：近半年 (months=6)
    s, e, desc = parse_date_range(months=6, as_of=ref)
    assert s == "2026-06-01"
    assert e == "2026-11-10"
    assert "近半年" in desc

    # 8. months=0 (全部)
    s, e, desc = parse_date_range(months=0, as_of=ref)
    assert s is None
    assert e is None
    assert desc == "全部历史"


def test_date_validation_and_ordering():
    """验证非法日期格式、无效日历以及倒错起止日期的严格校验拦截。"""
    # 无法识别的字符串 -> 抛出 ValueError，绝不静默放行成全部历史
    with pytest.raises(ValueError, match="无法识别的日期格式"):
        parse_date_range(start_date="bad")

    # 无效日历日期 (2月31日)
    with pytest.raises(ValueError, match="无效日历日期"):
        parse_date_range(start_date="2026-02-31")

    # 起始晚于截止
    with pytest.raises(ValueError, match="不能晚于截止日期"):
        parse_date_range(start_date="2026-10-31", end_date="2026-10-10")

    # export_expense_report 入口优雅捕获并返回错误信息
    res = export_expense_report(start_date="bad")
    assert res["success"] is False
    assert "❌ 日期参数错误" in res["summary_text"]


def test_clean_decimal():
    assert _clean_decimal("123.45") == Decimal("123.45")
    assert _clean_decimal("1,234.56") == Decimal("1234.56")
    assert _clean_decimal("-50.00") == Decimal("-50.00")
    assert _clean_decimal(None) is None
    assert _clean_decimal("") is None
    assert _clean_decimal("invalid") is None


def test_classify_transaction_edge_cases():
    """
    针对复核提出的边界用例严格测试：
    1. 未知方向的 +100 -> unknown_direction (隔离，绝不默认消费)
    2. debit/-100 -> direction_conflict (隔离，绝不归为其他贷记)
    3. debit/+100/退款服务费 -> expense (借记消费，绝不因含'退款'而归为退款)
    """
    # 1. 未知方向隔离
    assert classify_transaction(None, Decimal("100.00"), "未知交易流水") == "unknown_direction"
    assert classify_transaction("", Decimal("100.00"), "超市消费") == "unknown_direction"

    # 2. 方向与符号冲突隔离
    assert classify_transaction("debit", Decimal("-100.00"), "借记负金额异常") == "direction_conflict"

    # 3. 借记费用一律归入支出 (含退款服务费、分期还款手续费，绝不反转为退款或还款)
    assert classify_transaction("debit", Decimal("100.00"), "退款服务费") == "expense"
    assert classify_transaction("debit", Decimal("15.00"), "分期还款手续费") == "expense"
    assert classify_transaction("debit", Decimal("20.00"), "还款手续费") == "expense"
    assert classify_transaction("debit", Decimal("199.00"), "沃尔玛超市采购") == "expense"

    # 4. 借记明确冲正/撤销
    assert classify_transaction("debit", Decimal("500.00"), "自动还款撤销") == "repayment_reversal"

    # 4. 正常贷记还款与退款
    assert classify_transaction("credit", Decimal("-5000.00"), "掌上生活自动还款") == "repayment"
    assert classify_transaction("credit", Decimal("5000.00"), "掌上生活自动还款") == "repayment"
    assert classify_transaction("credit", Decimal("-50.00"), "商户退款") == "refund"
    assert classify_transaction("credit", Decimal("-10.00"), "账户利息入账") == "other_credit"


def test_repayment_consistency_checks(tmp_path):
    """
    验证上期应还与后续还款一致性核验 (check_repayment_consistency)：
    1. 单次全额还款 -> 金额吻合 (|Δ| <= 0.01)
    2. 多次还款累加吻合 (600 + 400 = 1000)
    3. 到期日未到且还款不足 -> 待观察
    4. 到期日已过且还款不足 -> 存在差额
    5. [P1] 严格校验账户归属 (不同账户同银行绝不串配)
    6. [P1] 一笔还款唯一归属，绝不能同时匹配两期账单
    7. [P1] statement_balances 逐币种确权 (支持 USD/CNY，未确权判无法判断)
    8. [P2] 真实同日多笔相同还款保留 (不丢失合法交易)
    9. [P2] 数据库流水尚未覆盖窗口时返回待观察 (防范范围截断误判)
    10. 借记分期还款手续费 (expense) 不混入还款 R
    11. 还款撤销扣减 (1000 - 200 = 800)
    12. 同月多卡多账单歧义 -> 无法判断
    """
    ref_date = date(2026, 11, 10)

    # 1. 单次全额还款 -> 金额吻合
    stmts_1 = [{
        "id": 1, "account_name": "acct1", "bank_code": "CMB", "statement_month": "2026-10",
        "statement_date": "2026-10-10", "due_date": "2026-10-28",
        "balances": [{"currency": "CNY", "total_due": "1000.00", "currency_source": "extracted"}]
    }]
    txns_1 = [{
        "id": 1, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-20",
        "amount": "-1000.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "自动还款"
    }]
    res_1 = check_repayment_consistency(stmts_1, txns_1, as_of=ref_date)
    assert len(res_1) == 1
    assert res_1[0]["status"] == "金额吻合"
    assert res_1[0]["repayment_amount"] == Decimal("1000.00")
    assert res_1[0]["delta"] == Decimal("0.00")

    # 2. 分两次还款 (600 + 400) -> 金额吻合
    txns_2 = [
        {"id": 2, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-15",
         "amount": "-600.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "银联还款"},
        {"id": 3, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-25",
         "amount": "-400.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "微信还款"},
    ]
    res_2 = check_repayment_consistency(stmts_1, txns_2, as_of=ref_date)
    assert res_2[0]["status"] == "金额吻合"
    assert res_2[0]["repayment_amount"] == Decimal("1000.00")

    # 3. 待观察 (到期日 2026-11-25 晚于 ref_date 2026-11-10，当前仅还 100)
    stmts_future = [{
        "id": 2, "account_name": "acct1", "bank_code": "CMB", "statement_month": "2026-11",
        "statement_date": "2026-11-05", "due_date": "2026-11-25",
        "balances": [{"currency": "CNY", "total_due": "1000.00", "currency_source": "extracted"}]
    }]
    txns_partial = [{
        "id": 4, "statement_id": 2, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-11-08",
        "amount": "-100.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "部分还款"
    }]
    res_future = check_repayment_consistency(stmts_future, txns_partial, as_of=ref_date)
    assert res_future[0]["status"] == "待观察"
    assert "尚未到期" in res_future[0]["reason"]

    # 4. 存在差额 (到期日已过 2026-10-28 < 2026-11-10，仅还 100)
    txns_diff = [{
        "id": 5, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-25",
        "amount": "-100.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "部分还款"
    }]
    res_diff = check_repayment_consistency(stmts_1, txns_diff, as_of=ref_date)
    assert res_diff[0]["status"] == "存在差额"
    assert res_diff[0]["delta"] == Decimal("-900.00")

    # 5. [P1] 严格校验账户归属：A 账户应还 1000，B 账户同银行还款 1000，绝不串配
    txns_wrong_acct = [{
        "id": 6, "statement_id": 1, "account_name": "acct_B", "bank_code": "CMB", "txn_date": "2026-10-20",
        "amount": "-1000.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "B账户还款"
    }]
    res_wrong_acct = check_repayment_consistency(stmts_1, txns_wrong_acct, as_of=ref_date)
    # A 账户未匹配到还款，R=0，差额 -1000.00，绝不能判断为金额吻合！
    assert res_wrong_acct[0]["status"] == "存在差额"
    assert res_wrong_acct[0]["repayment_amount"] == Decimal("0.00")
    assert res_wrong_acct[0]["delta"] == Decimal("-1000.00")

    # 6. [P1] 一笔还款唯一归属，绝不能同时匹配两期账单
    # 账单 1: 10-10 出账，还款日 10-28
    # 账单 2: 11-10 出账，还款日 11-28
    # 还款: 11-05 发生一笔 1000 元还款。它属于账单 1 的还款窗口 [10-10, 11-10)，唯一分配给账单 1，账单 2 绝不能重复吃进！
    stmts_two_periods = [
        {"id": 10, "account_name": "acct1", "bank_code": "CMB", "statement_month": "2026-10",
         "statement_date": "2026-10-10", "due_date": "2026-10-28",
         "balances": [{"currency": "CNY", "total_due": "1000.00", "currency_source": "extracted"}]},
        {"id": 11, "account_name": "acct1", "bank_code": "CMB", "statement_month": "2026-11",
         "statement_date": "2026-11-10", "due_date": "2026-11-28",
         "balances": [{"currency": "CNY", "total_due": "1000.00", "currency_source": "extracted"}]},
    ]
    txns_single_nov5 = [{
        "id": 7, "statement_id": 10, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-11-05",
        "amount": "-1000.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "自动还款"
    }]
    res_two_periods = check_repayment_consistency(stmts_two_periods, txns_single_nov5, as_of=date(2026, 12, 1))
    res_by_sid = {r["statement_id"]: r for r in res_two_periods}
    # 账单 10 获得该笔还款 -> 金额吻合
    assert res_by_sid[10]["status"] == "金额吻合"
    assert res_by_sid[10]["repayment_amount"] == Decimal("1000.00")
    # 账单 11 绝不能再次匹配 11-05 的还款！其还款为 0，状态为存在差额 (12-01 已过 11-28 到期日)
    assert res_by_sid[11]["repayment_amount"] == Decimal("0.00")
    assert res_by_sid[11]["status"] == "存在差额"

    # 7. [P1] statement_balances 逐币种确权 (支持 USD/CNY，未确权判无法判断)
    # 7.1 USD 确权应还与 USD 还款 -> 金额吻合，币种为 USD
    stmts_usd = [{
        "id": 20, "account_name": "acct1", "bank_code": "CMB", "statement_month": "2026-10",
        "statement_date": "2026-10-10", "due_date": "2026-10-28",
        "balances": [{"currency": "USD", "total_due": "50.00", "currency_source": "extracted"}]
    }]
    txns_usd = [{
        "id": 8, "statement_id": 20, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-20",
        "amount": "-50.00", "currency": "USD", "currency_source": "extracted", "category": "repayment", "description": "USD购汇还款"
    }]
    res_usd = check_repayment_consistency(stmts_usd, txns_usd, as_of=ref_date)
    assert res_usd[0]["status"] == "金额吻合"
    assert res_usd[0]["currency"] == "USD"
    assert res_usd[0]["repayment_amount"] == Decimal("50.00")

    # 7.2 balances 缺失确权来源 (currency_source 为空) -> 无法判断
    stmts_unconfirmed = [{
        "id": 21, "account_name": "acct1", "bank_code": "CMB", "statement_month": "2026-10",
        "statement_date": "2026-10-10", "due_date": "2026-10-28",
        "balances": [{"currency": "CNY", "total_due": "100.00", "currency_source": None}]
    }]
    res_unconf = check_repayment_consistency(stmts_unconfirmed, txns_1, as_of=ref_date)
    assert res_unconf[0]["status"] == "无法判断"
    assert "缺少确权币种应还金额证据" in res_unconf[0]["reason"]

    # 8. [P2] 真实同日多笔相同还款保留 (不丢失合法交易)
    # 同一日分两次各还 500 元，商户描述相同，但在流水表中有独立 id
    txns_same_day = [
        {"id": 31, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-20",
         "amount": "-500.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "快捷还款"},
        {"id": 32, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-20",
         "amount": "-500.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "快捷还款"},
    ]
    res_same_day = check_repayment_consistency(stmts_1, txns_same_day, as_of=ref_date)
    assert res_same_day[0]["status"] == "金额吻合"
    assert res_same_day[0]["repayment_amount"] == Decimal("1000.00")

    # 9. [P2] 数据库流水尚未覆盖完整窗口时返回待观察 (防范导出范围截断误判)
    # 构造独立 SQLite 库验证
    db_file = tmp_path / "coverage_test.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("CREATE TABLE statements (id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT, bank_code TEXT, statement_month TEXT, statement_date TEXT, due_date TEXT, total_due TEXT, parse_status TEXT, raw_fields_json TEXT)")
    cur.execute("CREATE TABLE statement_balances (statement_id INTEGER, currency TEXT, total_due TEXT, minimum_due TEXT, currency_source TEXT)")
    cur.execute("CREATE TABLE statement_transactions (id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT, bank_code TEXT, statement_id INTEGER, txn_date TEXT, post_date TEXT, description TEXT, amount TEXT, currency TEXT, currency_source TEXT, direction TEXT)")
    # 账单出账 2026-10-20，还款日 2026-11-08，应还 1000
    cur.execute("INSERT INTO statements VALUES (100, 'acct1', 'u1', 'CMB', '2026-10', '2026-10-20', '2026-11-08', '1000.00', 'ok', '{}')")
    cur.execute("INSERT INTO statement_balances VALUES (100, 'CNY', '1000.00', '100.00', 'extracted')")
    # 数据库最新流水仅到 2026-10-31，还没有导入 11 月的数据，且当前还款 0
    cur.execute("INSERT INTO statement_transactions VALUES (1001, 'acct1', 'u1', 'CMB', 100, '2026-10-25', '2026-10-25', '消费', '100.00', 'CNY', 'extracted', 'debit')")
    conn.commit()
    conn.close()

    stmt_cov = [{"id": 100, "account_name": "acct1", "bank_code": "CMB", "statement_month": "2026-10", "statement_date": "2026-10-20", "due_date": "2026-11-08"}]
    # 设定 ref_date 晚于还款日 (2026-11-15)，但数据库流水 max_date 仅 2026-10-25，未覆盖到 2026-11-08 的还款窗口！
    res_cov = check_repayment_consistency(stmt_cov, db_path=str(db_file), as_of=date(2026, 11, 15))
    assert res_cov[0]["status"] == "待观察"
    assert "尚未覆盖完整还款窗口" in res_cov[0]["reason"]

    # 10. 借记分期还款手续费 (expense) 不混入还款 R
    txns_with_fee = [
        {"id": 41, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-18",
         "amount": "15.00", "currency": "CNY", "currency_source": "extracted", "category": "expense", "description": "分期还款手续费"},
        {"id": 42, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-20",
         "amount": "-1000.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "自动还款"},
    ]
    res_fee = check_repayment_consistency(stmts_1, txns_with_fee, as_of=ref_date)
    assert res_fee[0]["status"] == "金额吻合"
    assert res_fee[0]["repayment_amount"] == Decimal("1000.00")

    # 11. 还款撤销扣减 (1000 - 200 = 800)
    txns_reversal = [
        {"id": 51, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-20",
         "amount": "-1000.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment", "description": "自动还款"},
        {"id": 52, "statement_id": 1, "account_name": "acct1", "bank_code": "CMB", "txn_date": "2026-10-21",
         "amount": "200.00", "currency": "CNY", "currency_source": "extracted", "category": "repayment_reversal", "description": "还款撤销"},
    ]
    res_rev = check_repayment_consistency(stmts_1, txns_reversal, as_of=ref_date)
    assert res_rev[0]["status"] == "存在差额"
    assert res_rev[0]["repayment_amount"] == Decimal("800.00")
    assert res_rev[0]["delta"] == Decimal("-200.00")

    # 12. 多卡歧义 -> 无法判断
    stmts_multi_card = [
        {"id": 1, "account_name": "acct1", "bank_code": "CMB", "statement_month": "2026-10", "statement_date": "2026-10-10", "due_date": "2026-10-28", "balances": [{"currency": "CNY", "total_due": "1000.00", "currency_source": "extracted"}]},
        {"id": 3, "account_name": "acct1", "bank_code": "CMB", "statement_month": "2026-10", "statement_date": "2026-10-12", "due_date": "2026-10-28", "balances": [{"currency": "CNY", "total_due": "500.00", "currency_source": "extracted"}]},
    ]
    res_multi = check_repayment_consistency(stmts_multi_card, txns_1, as_of=ref_date)
    assert len(res_multi) == 2
    assert all(r["status"] == "无法判断" for r in res_multi)
    assert "多卡归属不明" in res_multi[0]["reason"]


def test_repayment_attribution_invariant_to_export_scope(tmp_path):
    """
    [P1 回归验证 1] 验证还款归属不随导出范围改变：
    数据库中存在 10月账单(10-10出账)与 11月账单(11-01出账)，11-05发生一笔还款。
    无论同时传入两期账单，还是仅传入 10月账单，11-05的还款都客观归属于 11月出账后的区间，
    10月账单的还款入账均保持为 0.00，绝不因导出范围截断而改变还款归属！
    """
    db_file = tmp_path / "scope_invariant.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("CREATE TABLE statements (id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT, bank_code TEXT, statement_month TEXT, statement_date TEXT, due_date TEXT, total_due TEXT, parse_status TEXT, raw_fields_json TEXT)")
    cur.execute("CREATE TABLE statement_balances (statement_id INTEGER, currency TEXT, total_due TEXT, minimum_due TEXT, currency_source TEXT)")
    cur.execute("CREATE TABLE statement_transactions (id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT, bank_code TEXT, statement_id INTEGER, txn_date TEXT, post_date TEXT, description TEXT, amount TEXT, currency TEXT, currency_source TEXT, direction TEXT)")

    # 插入 10月与 11月两张账单
    cur.execute("INSERT INTO statements VALUES (1, 'user1', 'u1', 'CMB', '2026-10', '2026-10-10', '2026-10-28', '1000.00', 'ok', '{}')")
    cur.execute("INSERT INTO statement_balances VALUES (1, 'CNY', '1000.00', '100.00', 'extracted')")

    cur.execute("INSERT INTO statements VALUES (2, 'user1', 'u2', 'CMB', '2026-11', '2026-11-01', '2026-11-20', '1000.00', 'ok', '{}')")
    cur.execute("INSERT INTO statement_balances VALUES (2, 'CNY', '1000.00', '100.00', 'extracted')")

    # 11-05 还款 1000 元 (挂在 statement_id=2 下，但在时间上属于 11-01 之后发生的还款)
    cur.execute("INSERT INTO statement_transactions VALUES (101, 'user1', 'u2', 'CMB', 2, '2026-11-05', '2026-11-05', '自动还款', '-1000.00', 'CNY', 'extracted', 'credit')")
    conn.commit()
    conn.close()

    stmt_10 = [{"id": 1, "account_name": "user1", "bank_code": "CMB", "statement_month": "2026-10", "statement_date": "2026-10-10", "due_date": "2026-10-28"}]
    stmt_11 = [{"id": 2, "account_name": "user1", "bank_code": "CMB", "statement_month": "2026-11", "statement_date": "2026-11-01", "due_date": "2026-11-20"}]

    ref_d = date(2026, 11, 25)

    # 场景 A: 导出范围同时传入两期账单
    res_both = check_repayment_consistency([stmt_10[0], stmt_11[0]], db_path=str(db_file), as_of=ref_d)
    both_by_id = {r["statement_id"]: r for r in res_both}
    # 10月账单还款为 0
    assert both_by_id[1]["repayment_amount"] == Decimal("0.00")
    # 11月账单匹配到该还款
    assert both_by_id[2]["repayment_amount"] == Decimal("1000.00")

    # 场景 B: 导出范围仅传入 10月账单
    res_single = check_repayment_consistency([stmt_10[0]], db_path=str(db_file), as_of=ref_d)
    # 10月账单还款依然严格为 0，绝对不被改变归属或误显示金额吻合！
    assert res_single[0]["repayment_amount"] == Decimal("0.00")
    assert res_single[0]["status"] == "存在差额"


def test_unconfirmed_and_blocked_txns_excluded_from_repayment(tmp_path):
    """
    [P1 回归验证 2] 验证未确权币种来源 (如 legacy_assumed) 或母账单 blocked 的流水绝不参与对账：
    即使金额刚好匹配，未确权或受阻流水也不能被计入还款净入账！
    """
    db_file = tmp_path / "quality_filter_test.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("CREATE TABLE statements (id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT, bank_code TEXT, statement_month TEXT, statement_date TEXT, due_date TEXT, total_due TEXT, parse_status TEXT, raw_fields_json TEXT)")
    cur.execute("CREATE TABLE statement_balances (statement_id INTEGER, currency TEXT, total_due TEXT, minimum_due TEXT, currency_source TEXT)")
    cur.execute("CREATE TABLE statement_transactions (id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT, bank_code TEXT, statement_id INTEGER, txn_date TEXT, post_date TEXT, description TEXT, amount TEXT, currency TEXT, currency_source TEXT, direction TEXT)")

    # 正常母账单 1
    cur.execute("INSERT INTO statements VALUES (1, 'u1', 'uid1', 'CMB', '2026-10', '2026-10-10', '2026-10-28', '1000.00', 'ok', '{}')")
    cur.execute("INSERT INTO statement_balances VALUES (1, 'CNY', '1000.00', '100.00', 'extracted')")

    # 用途被 blocked 的母账单 2
    cur.execute("INSERT INTO statements VALUES (2, 'u1', 'uid2', 'CMB', '2026-10', '2026-10-10', '2026-10-28', '1000.00', 'ok', '{\"quality\":{\"purpose_availability\":{\"transaction_trends\":{\"status\":\"blocked\"}}}}')")

    # 流水 A: 币种来源为 legacy_assumed (未确权) -> 必须被排除
    cur.execute("INSERT INTO statement_transactions VALUES (11, 'u1', 'uid1', 'CMB', 1, '2026-10-20', '2026-10-20', '自动还款', '-1000.00', 'CNY', 'legacy_assumed', 'credit')")

    # 流水 B: 母账单为 blocked -> 必须被排除
    cur.execute("INSERT INTO statement_transactions VALUES (12, 'u1', 'uid2', 'CMB', 2, '2026-10-22', '2026-10-22', '自动还款', '-1000.00', 'CNY', 'extracted', 'credit')")
    conn.commit()
    conn.close()

    stmt = [{"id": 1, "account_name": "u1", "bank_code": "CMB", "statement_month": "2026-10", "statement_date": "2026-10-10", "due_date": "2026-10-28"}]
    res = check_repayment_consistency(stmt, db_path=str(db_file), as_of=date(2026, 11, 15))
    # 两笔流水均被排除，还款入账为 0，不能显示“金额吻合”
    assert res[0]["repayment_amount"] == Decimal("0.00")
    assert res[0]["status"] != "金额吻合"


def test_other_account_txns_do_not_affect_target_coverage(tmp_path):
    """
    [P2 回归验证 3] 验证其他账户的新交易不改变目标账户的覆盖状态：
    A 账户数据库最新流水只到 10-25，还款日 11-08，还款窗口 window_end 为 11-23。
    即便 B 账户插入了 12-01 的晚期流水 (全库 MAX >= 11-23)，A 账户的覆盖状态判定仍按自身流水 (10-25 < 11-23)，
    在还款不足时坚决保持“待观察”，杜绝误判为“存在差额”！
    """
    db_file = tmp_path / "acct_isolation_cov.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("CREATE TABLE statements (id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT, bank_code TEXT, statement_month TEXT, statement_date TEXT, due_date TEXT, total_due TEXT, parse_status TEXT, raw_fields_json TEXT)")
    cur.execute("CREATE TABLE statement_balances (statement_id INTEGER, currency TEXT, total_due TEXT, minimum_due TEXT, currency_source TEXT)")
    cur.execute("CREATE TABLE statement_transactions (id INTEGER PRIMARY KEY, account_name TEXT, uid TEXT, bank_code TEXT, statement_id INTEGER, txn_date TEXT, post_date TEXT, description TEXT, amount TEXT, currency TEXT, currency_source TEXT, direction TEXT)")

    # A 账户账单：出账 10-20，还款日 11-08，应还 1000
    cur.execute("INSERT INTO statements VALUES (1, 'acct_A', 'uA', 'CMB', '2026-10', '2026-10-20', '2026-11-08', '1000.00', 'ok', '{}')")
    cur.execute("INSERT INTO statement_balances VALUES (1, 'CNY', '1000.00', '100.00', 'extracted')")
    # A 账户流水最新仅到 10-25
    cur.execute("INSERT INTO statement_transactions VALUES (1, 'acct_A', 'uA', 'CMB', 1, '2026-10-25', '2026-10-25', '消费', '100.00', 'CNY', 'extracted', 'debit')")

    # B 账户账单与流水：B 账户有 12-01 的晚期消费 (显著晚于 A 的还款截止窗口 11-23)！
    cur.execute("INSERT INTO statements VALUES (2, 'acct_B', 'uB', 'CMB', '2026-11', '2026-11-01', '2026-11-20', '500.00', 'ok', '{}')")
    cur.execute("INSERT INTO statement_balances VALUES (2, 'CNY', '500.00', '50.00', 'extracted')")
    cur.execute("INSERT INTO statement_transactions VALUES (2, 'acct_B', 'uB', 'CMB', 2, '2026-12-01', '2026-12-01', '消费', '200.00', 'CNY', 'extracted', 'debit')")
    conn.commit()
    conn.close()

    stmt_A = [{"id": 1, "account_name": "acct_A", "bank_code": "CMB", "statement_month": "2026-10", "statement_date": "2026-10-20", "due_date": "2026-11-08"}]
    # 基准日设为 2026-11-25 (晚于 A 的窗口截止 11-23)，但 A 自身的数据只到 10-25
    res_A = check_repayment_consistency(stmt_A, db_path=str(db_file), as_of=date(2026, 11, 25))
    # 状态必须保持“待观察”，绝不能因为 B 账户的 12-01 流水而误变成“存在差额”！
    assert res_A[0]["status"] == "待观察"
    assert "尚未覆盖完整还款窗口" in res_A[0]["reason"]


@pytest.fixture
def sample_stmt_db(tmp_path):
    """创建包含交易明细、母账单关联、各种质量边界与原币的受控测试库"""
    db_file = tmp_path / "test_statements.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL DEFAULT '__orphan__',
            uid TEXT NOT NULL,
            message_id TEXT,
            bank_code TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            email_date TEXT,
            statement_month TEXT,
            statement_date TEXT,
            due_date TEXT,
            total_due TEXT,
            minimum_due TEXT,
            raw_fields_json TEXT,
            is_paid INTEGER NOT NULL DEFAULT 0,
            paid_at TEXT,
            parse_status TEXT NOT NULL DEFAULT 'ok',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE statement_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL DEFAULT '__orphan__',
            uid TEXT NOT NULL,
            bank_code TEXT NOT NULL,
            statement_id INTEGER,
            txn_date TEXT,
            post_date TEXT,
            description TEXT,
            amount TEXT,
            currency TEXT,
            txn_location_code TEXT,
            original_amount TEXT,
            direction TEXT,
            raw_row_json TEXT,
            created_at TEXT NOT NULL,
            original_currency TEXT,
            currency_source TEXT,
            original_currency_source TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE statement_balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            total_due TEXT,
            minimum_due TEXT,
            currency_source TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(statement_id, currency)
        )
    """)

    # 1. 正常完全核验账单 (id=1: CMB 2026-10-10, available)
    cur.execute("""
        INSERT INTO statements (id, account_name, uid, bank_code, rule_id, statement_month, statement_date, due_date, total_due, parse_status, raw_fields_json, created_at, updated_at)
        VALUES
        (1, 'test_user', 'u1', 'CMB', 'r1', '2026-10', '2026-10-10', '2026-10-28', '3500.00', 'ok',
         '{"quality":{"purpose_availability":{"transaction_trends":{"status":"available","reasons":[]}}}}',
         '2026-10-10', '2026-10-10')
    """)
    cur.execute("""
        INSERT INTO statement_balances (statement_id, currency, total_due, minimum_due, currency_source, created_at, updated_at)
        VALUES
        (1, 'CNY', '3500.00', '350.00', 'extracted', '2026-10-10', '2026-10-10')
    """)

    # 2. 部分核验账单 (id=2: CITIC 2026-09-15, partial - 缺少部分证据)
    cur.execute("""
        INSERT INTO statements (id, account_name, uid, bank_code, rule_id, statement_month, statement_date, due_date, total_due, parse_status, raw_fields_json, created_at, updated_at)
        VALUES
        (2, 'test_user', 'u2', 'CITIC', 'r2', '2026-09', '2026-09-15', '2026-10-05', '1200.50', 'ok',
         '{"quality":{"purpose_availability":{"transaction_trends":{"status":"partial","reasons":[{"code":"QUALITY_UNVERIFIED","reason":"缺少质量证据"}]}}}}',
         '2026-09-15', '2026-09-15')
    """)
    cur.execute("""
        INSERT INTO statement_balances (statement_id, currency, total_due, minimum_due, currency_source, created_at, updated_at)
        VALUES
        (2, 'CNY', '1200.50', '120.00', 'extracted', '2026-09-15', '2026-09-15')
    """)

    # 3. 解析失败账单 (id=3: HX failed)
    cur.execute("""
        INSERT INTO statements (id, account_name, uid, bank_code, rule_id, statement_month, statement_date, total_due, parse_status, raw_fields_json, created_at, updated_at)
        VALUES
        (3, 'test_user', 'u3', 'HX', 'r3', '2026-08', '2026-08-10', '500.00', 'failed',
         '{}', '2026-08-10', '2026-08-10')
    """)

    # 4. 用途受阻账单 (id=4: ICBC blocked)
    cur.execute("""
        INSERT INTO statements (id, account_name, uid, bank_code, rule_id, statement_month, statement_date, total_due, parse_status, raw_fields_json, created_at, updated_at)
        VALUES
        (4, 'test_user', 'u4', 'ICBC', 'r4', '2026-07', '2026-07-10', '800.00', 'ok',
         '{"quality":{"purpose_availability":{"transaction_trends":{"status":"blocked","reasons":[{"code":"FIELD_UNCONFIRMED"}]}}}}',
         '2026-07-10', '2026-07-10')
    """)

    # 5. 插入各类明细流水
    cur.execute("""
        INSERT INTO statement_transactions
        (id, account_name, statement_id, uid, bank_code, txn_date, post_date, description, amount, currency, currency_source, original_amount, original_currency, original_currency_source, direction, created_at)
        VALUES
        -- 1. 正常消费 (CNY, extracted) -> 299.00
        (1, 'test_user', 1, 'u1', 'CMB', '2026-10-05', '2026-10-06', '京东商城*配件采购', '299.00', 'CNY', 'extracted', NULL, NULL, NULL, 'debit', '2026-10-06'),
        -- 2. 正常外币消费 (USD, extracted) + 原币已确权 (USD, extracted) -> 20.00
        (2, 'test_user', 1, 'u1', 'CMB', '2026-10-08', '2026-10-08', 'GITHUB*SPONSOR', '20.00', 'USD', 'extracted', '20.00', 'USD', 'extracted', 'debit', '2026-10-08'),
        -- 3. 还款入账 -> 5000.00 (credit)
        (3, 'test_user', 1, 'u1', 'CMB', '2026-10-09', '2026-10-09', '掌上生活自动还款', '-5000.00', 'CNY', 'extracted', NULL, NULL, NULL, 'credit', '2026-10-09'),
        -- 4. 正常消费含有未确权原币 (EUR, 无 original_currency_source) -> 78.00
        (4, 'test_user', 1, 'u1', 'CMB', '2026-10-07', '2026-10-07', '境外电子书', '78.00', 'CNY', 'extracted', '10.00', 'EUR', NULL, 'debit', '2026-10-07'),
        -- 5. debit 交易含“退款服务费”字样 -> 属于消费支出，不是退款！
        (5, 'test_user', 1, 'u1', 'CMB', '2026-10-06', '2026-10-06', '退款服务费', '15.00', 'CNY', 'extracted', NULL, NULL, NULL, 'debit', '2026-10-06'),
        -- 6. 未知方向流水 (direction=None) -> 必须被隔离，不计入消费
        (6, 'test_user', 1, 'u1', 'CMB', '2026-10-04', '2026-10-04', '未知方向流水', '100.00', 'CNY', 'extracted', NULL, NULL, NULL, NULL, '2026-10-04'),
        -- 7. 方向与符号冲突 (debit 且 amount < 0) -> 必须被隔离
        (7, 'test_user', 1, 'u1', 'CMB', '2026-10-03', '2026-10-03', '借记负金额流水', '-60.00', 'CNY', 'extracted', NULL, NULL, NULL, 'debit', '2026-10-03'),
        -- 8. 所属母账单为 partial (id=2: 退款 50.00)
        (8, 'test_user', 2, 'u2', 'CITIC', '2026-09-10', '2026-09-11', '商户退款', '-50.00', 'CNY', 'extracted', NULL, NULL, NULL, 'credit', '2026-09-11'),
        -- 9. 所属母账单为 partial (id=2: 消费 150.00)
        (9, 'test_user', 2, 'u2', 'CITIC', '2026-09-12', '2026-09-13', '餐饮消费', '150.00', 'CNY', 'extracted', NULL, NULL, NULL, 'debit', '2026-09-13'),
        -- 10. 缺失/未确权币种流水 (currency=None) -> 必须隔离为 UNKNOWN
        (10, 'test_user', 1, 'u1', 'CMB', '2026-10-02', '2026-10-02', '未确权币种测试', '120.00', NULL, NULL, NULL, NULL, NULL, 'debit', '2026-10-02'),
        -- 11. 属于 failed 母账单 -> 必须被排除
        (11, 'test_user', 3, 'u3', 'HX', '2026-08-05', '2026-08-06', '失败账单交易', '888.00', 'CNY', 'extracted', NULL, NULL, NULL, 'debit', '2026-08-06'),
        -- 12. 属于 blocked 用途母账单 -> 必须被隔离
        (12, 'test_user', 4, 'u4', 'ICBC', '2026-07-05', '2026-07-06', '受阻用途交易', '999.00', 'CNY', 'extracted', NULL, NULL, NULL, 'debit', '2026-07-06'),
        -- 13. 孤儿流水 -> 必须被排除
        (13, 'test_user', 9999, 'orphan', 'CMB', '2026-10-01', '2026-10-01', '孤儿流水', '666.00', 'CNY', 'extracted', NULL, NULL, NULL, 'debit', '2026-10-01')
    """)
    conn.commit()
    conn.close()
    return str(db_file)


def test_query_expense_data_complete_quality_and_classification(sample_stmt_db):
    res = query_expense_data(db_path=sample_stmt_db, start_date="2026-01-01", end_date="2026-12-31")
    assert res["success"] is True

    # 1. 验证各种异常流水隔离
    anom = res["anomalies"]
    assert anom["orphan_count"] == 1             # id=13 孤儿流水
    assert anom["failed_statement_count"] == 1    # id=11 failed 账单流水
    assert anom["gated_count"] == 1               # id=12 blocked 账单流水
    assert anom["unknown_currency_count"] == 1    # id=10 币种未确权
    assert anom["unknown_direction_count"] == 1   # id=6 未知方向
    assert anom["direction_conflict_count"] == 1  # id=7 方向符号冲突
    assert anom["partial_count"] == 2             # id=8, id=9 属于 partial 账单
    assert anom["is_complete"] is False           # 含有 partial 与隔离流水，非 complete

    # 2. 验证消费支出分类与金额计算
    totals = res["totals_by_currency"]
    cny = totals["CNY"]
    # CNY 正常消费: id=1(299.00) + id=4(78.00) + id=5(退款服务费 15.00) + id=9(150.00) = 542.00
    assert cny["gross_expense"] == Decimal("542.00")
    # CNY 退款: id=8(50.00)
    assert cny["refund"] == Decimal("50.00")
    # CNY 净支出: 542.00 - 50.00 = 492.00
    assert cny["net_expense"] == Decimal("492.00")
    # CNY 还款: id=3(5000.00) (独立列示，绝不混入消费)
    assert cny["repayment"] == Decimal("5000.00")

    # 3. 验证原币确权检查
    tx_by_id = {tx["id"]: tx for tx in res["transactions"]}
    assert tx_by_id[2]["original_currency_confirmed"] is True
    assert tx_by_id[2]["original_currency_display"] == "USD 20.00"

    assert tx_by_id[4]["original_currency_confirmed"] is False
    assert "未确权原币" in tx_by_id[4]["original_currency_display"]


def test_statement_date_filtering_no_month_leak(sample_stmt_db):
    """
    验证精确账单日期筛选不会因为月份条件 OR 放宽而错误返回其他日期的账单。
    测试库中有一张账单是 2026-10-10。
    """
    # 仅查询 2026-10-31 这单日
    res_single_day = query_expense_data(sample_stmt_db, start_date="2026-10-31", end_date="2026-10-31")
    # 2026-10-10 的账单绝不能被返回！
    assert len(res_single_day["statements"]) == 0

    # 查询包含 2026-10-10 的区间
    res_range = query_expense_data(sample_stmt_db, start_date="2026-10-01", end_date="2026-10-31")
    assert len(res_range["statements"]) == 1
    assert res_range["statements"][0]["statement_date"] == "2026-10-10"


def test_csv_single_bom_and_clean_header(sample_stmt_db, tmp_path):
    """验证 CSV 文件严格只包含单次 UTF-8 BOM，读回首列表头无残留不可见字符。"""
    res = query_expense_data(db_path=sample_stmt_db)
    csv_text = generate_expense_csv_content(res["transactions"])

    assert not csv_text.startswith("\ufeff")

    out_file = str(tmp_path / "sub" / "output.csv")
    written_path = write_expense_csv_file(csv_text, out_file)

    # 1. 验证二进制字节：前 3 字节为 EF BB BF，第 4-6 字节绝不能又是 EF BB BF
    with open(written_path, "rb") as f:
        head_bytes = f.read(6)
        assert head_bytes[:3] == b"\xef\xbb\xbf", "必须以 UTF-8 BOM 开头"
        assert head_bytes[3:6] != b"\xef\xbb\xbf", "严禁重复写入双重 BOM (EF BB BF EF BB BF)"

    # 2. 以 utf-8-sig 打开读取，首个表头字段必须干净等于 '序号'
    with open(written_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header_row = next(reader)
        assert header_row[0] == "序号"
        assert "\ufeff" not in header_row[0]
        assert "原币确权状态" in header_row


def test_hedgedoc_privacy_defaults_to_private(sample_stmt_db, tmp_path):
    """验证 HedgeDoc 上传默认 public=False (私有笔记)，仅在明确传参时公开。"""
    with patch("lite_agent.features.billing.export_service.upload_to_hedgedoc") as mock_upload, \
         patch("lite_agent.features.billing.export_service.config_loader.load_config") as mock_cfg, \
         patch("lite_agent.features.billing.export_service.PROJECT_ROOT", str(tmp_path)):

        mock_cfg.return_value = {"hedgedoc": {"enabled": True}}
        mock_upload.return_value = "https://md.maifeipin.com/n/test-private"

        # 1. 默认调用：public 必须为 False
        res = export_expense_report(year=2026, db_path=sample_stmt_db, upload_hedgedoc=True)
        assert res["success"] is True
        mock_upload.assert_called_once()
        _, kwargs = mock_upload.call_args
        assert kwargs.get("public") is False, "默认导出必须为私有笔记 (public=False)"
        assert "私有笔记" in res["summary_text"]

        # 2. 明确声明 public=True
        mock_upload.reset_mock()
        mock_upload.return_value = "https://md.maifeipin.com/n/test-public"
        res_pub = export_expense_report(year=2026, db_path=sample_stmt_db, upload_hedgedoc=True, public=True)
        _, kwargs_pub = mock_upload.call_args
        assert kwargs_pub.get("public") is True
        assert "公开只读" in res_pub["summary_text"]


def test_cmd_export_year_vs_months_dispatch():
    """验证 /cmd export 正确区分 4 位年份与月数参数。"""
    with patch("lite_agent.features.billing.service.export_expenses") as mock_exp:
        mock_exp.return_value = "ok"

        # 1. 纯 4 位年份 -> 传入 year="2024"，绝不能变成 months=2024
        _handle_cmd(None, None, ["export", "2024"])
        mock_exp.assert_called_with(year="2024")

        # 2. 纯月数数字 -> 传入 months=6
        mock_exp.reset_mock()
        _handle_cmd(None, None, ["export", "6"])
        mock_exp.assert_called_with(months=6)

        # 3. 跨年区间 -> 传入 year="2022-2024"
        mock_exp.reset_mock()
        _handle_cmd(None, None, ["export", "2022-2024"])
        mock_exp.assert_called_with(year="2022-2024")

        # 4. 自然语言年份 -> 传入 year="今年"
        mock_exp.reset_mock()
        _handle_cmd(None, None, ["export", "今年"])
        mock_exp.assert_called_with(year="今年")


def test_billing_domain_intent_pattern_coverage():
    """验证扩充后的 BILLING_DOMAIN_SPEC pattern 能成功覆盖用户的自然语言输入"""
    pattern = re.compile(BILLING_DOMAIN_SPEC["pattern"], re.I)

    # 用户的真实典型请求
    assert pattern.search("给我生成半年内的开支明细") is not None
    assert pattern.search("请帮我生成近半年的开支明细并导出csv") is not None
    assert pattern.search("统计一下近两年的支出汇总") is not None
    assert pattern.search("查一下2024年的消费明细") is not None
    assert pattern.search("近三年花费了多少钱") is not None
    assert pattern.search("导出近一年的财务明细") is not None
    assert pattern.search("记账明细导出") is not None

    # 原有账单相关意图仍能稳定命中
    assert pattern.search("拉取最新账单") is not None
    assert pattern.search("招商银行账单") is not None
    assert pattern.search("查看还款提醒") is not None
    assert pattern.search("外币交易明细") is not None
    assert pattern.search("信用卡大额流水") is not None


def test_billing_export_skill_registered_and_invocable(sample_stmt_db, tmp_path):
    """验证 skill_engine 已正确登记 billing_export_expenses 技能，且参数契约可被调用"""
    import importlib
    import lite_agent.skills.ops_billing
    importlib.reload(lite_agent.skills.ops_billing)
    from lite_agent.core.skill_engine import _skill_registry
    from lite_agent.skills.ops_billing import billing_export_expenses

    assert "billing_export_expenses" in _skill_registry
    skill_meta = _skill_registry["billing_export_expenses"]
    properties = skill_meta["schema"]["function"]["parameters"]["properties"]
    assert "months" in properties
    assert "year" in properties
    assert "upload_hedgedoc" in properties
    assert "public" in properties
    assert properties["public"]["default"] is False

    # 模拟调用技能
    with patch("lite_agent.features.billing.service.get_billing_env") as mock_env, \
         patch("lite_agent.features.billing.export_service.PROJECT_ROOT", str(tmp_path)):
        mock_env.return_value = {"stmt_db": sample_stmt_db}
        res_str = billing_export_expenses(months=3, upload_hedgedoc=False)
        assert "📊 **开支明细与汇总报表生成完毕**" in res_str
