"""
Billing 开支明细与汇总导出服务 (features.billing.export_service)。
支持自定义时间范围（按年、跨年、近 N 月、精确起止日期或全量历史）查询开支明细与汇总，
严格继承数据质量约束（母账单有效性、用途门禁、币种确权、收支方向隔离），
借记方向费用（含分期还款手续费、退款服务费）归入支出，还款与还款撤销仅在贷记或明确冲正下识别，
导出单次 UTF-8 BOM 的标准 CSV，组织私有/公开可控的 Markdown 报表上传至 HedgeDoc，
并提供上期应还与后续还款一致性辅助验证。
"""
import csv
import io
import json
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from lite_agent.core import config_loader
from lite_agent.core.constants import PROJECT_ROOT
from lite_agent.core.utils.hedgedoc import upload_to_hedgedoc
from lite_agent.integrations.mail.paths import connect_readonly_db, get_statement_db_path


# 权威币种全称映射与有效币种集合 (与 statement_evidence 契约对齐)
KNOWN_CURRENCIES = {
    'CNY', 'USD', 'JPY', 'EUR', 'HKD', 'GBP', 'AUD', 'CAD', 'SGD', 'CHF', 'KRW'
}
CONFIRMED_SOURCES = {'extracted', 'rule_declared', 'cell_symbol'}


def confirmed_currency(currency: Optional[str], source: Optional[str]) -> bool:
    """与外部 Parser statement_evidence 契约完全对齐的币种确权判定。"""
    if not currency or not source:
        return False
    curr_upper = currency.strip().upper()
    return (source.strip().lower() in CONFIRMED_SOURCES) and (curr_upper in KNOWN_CURRENCIES)


def stored_purpose(raw_fields_json: Optional[str], purpose: str) -> Dict[str, Any]:
    """与 statement_evidence.stored_purpose 对齐的用途门禁状态解析。"""
    try:
        data = json.loads(raw_fields_json or '{}')
        gate = data.get('quality', {}).get('purpose_availability', {}).get(purpose, {})
        if gate.get('status') in ('available', 'partial', 'blocked'):
            return {'status': gate['status'], 'reasons': gate.get('reasons') or []}
    except (ValueError, TypeError, AttributeError):
        pass
    return {
        'status': 'blocked' if purpose == 'repayment_reminder' else 'partial',
        'reasons': [{'code': 'QUALITY_UNVERIFIED', 'reason': 'No verified purpose evidence; reparse required'}]
    }


def _validate_and_normalize_date(date_str: str, is_end: bool = False) -> str:
    """
    严格校验并规范化日期字符串为 YYYY-MM-DD。
    若格式或真实日历非法抛出 ValueError，绝不静默放行或默认为全量。
    """
    s = date_str.strip()
    if re.fullmatch(r"\d{4}", s):
        y = int(s)
        return f"{y:04d}-12-31" if is_end else f"{y:04d}-01-01"

    if re.fullmatch(r"\d{4}-\d{2}", s):
        y, m = int(s[:4]), int(s[5:7])
        if not (1 <= m <= 12):
            raise ValueError(f"月份必须在 01 至 12 之间: {date_str}")
        if is_end:
            last_day = (date(y, m + 1, 1) - timedelta(days=1)).day if m < 12 else 31
            return f"{y:04d}-{m:02d}-{last_day:02d}"
        else:
            return f"{y:04d}-{m:02d}-01"

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
            return d.strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"无效日历日期: {date_str}") from exc

    raise ValueError(f"无法识别的日期格式 (必须为 YYYY, YYYY-MM 或 YYYY-MM-DD): {date_str}")


def parse_date_range(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    months: Optional[int] = None,
    year: Optional[Any] = None,
    as_of: Optional[date] = None,
) -> Tuple[Optional[str], Optional[str], str]:
    """
    解析并标准化账单/开支的时间查询区间。
    严格执行日期真实性与起止先后顺序校验，异常输入抛出 ValueError。
    """
    ref_date = as_of or date.today()

    # 1. 优先解析自然语言年份或指定年份
    if year is not None:
        year_str = str(year).strip()
        if year_str in ("全部", "所有", "all", "ALL", "*"):
            return None, None, "全部历史"

        if year_str == "今年":
            y = ref_date.year
            return f"{y:04d}-01-01", f"{y:04d}-12-31", f"{y}年 (今年)"

        if year_str == "去年":
            y = ref_date.year - 1
            return f"{y:04d}-01-01", f"{y:04d}-12-31", f"{y}年 (去年)"

        # 匹配单一年份如 2024
        m_single = re.fullmatch(r"(\d{4})年?", year_str)
        if m_single:
            y = int(m_single.group(1))
            return f"{y:04d}-01-01", f"{y:04d}-12-31", f"{y}年全量"

        # 匹配跨年份区间如 2022-2024, 2022~2024, 2022至2024
        m_range = re.fullmatch(r"(\d{4})\s*(?:[-~至到]|--)\s*(\d{4})年?", year_str)
        if m_range:
            y1 = int(m_range.group(1))
            y2 = int(m_range.group(2))
            start_y, end_y = min(y1, y2), max(y1, y2)
            return f"{start_y:04d}-01-01", f"{end_y:04d}-12-31", f"{start_y}年至{end_y}年"

        # 匹配近 N 年 / 近 N 月
        m_recent_year = re.fullmatch(r"近(\d+)年", year_str)
        if m_recent_year:
            num_years = int(m_recent_year.group(1))
            months = num_years * 12

        m_recent_month = re.fullmatch(r"近(\d+)个?月", year_str)
        if m_recent_month:
            months = int(m_recent_month.group(1))

    # 2. 解析精确起止日期并严格校验日历与格式
    if start_date or end_date:
        s_res: Optional[str] = None
        e_res: Optional[str] = None

        if start_date:
            s_res = _validate_and_normalize_date(start_date, is_end=False)

        if end_date:
            e_res = _validate_and_normalize_date(end_date, is_end=True)

        if s_res and not e_res:
            e_res = ref_date.strftime("%Y-%m-%d")
        elif e_res and not s_res:
            if months and months > 0:
                e_dt = datetime.strptime(e_res, "%Y-%m-%d").date()
                idx = e_dt.year * 12 + e_dt.month - 1 - (months - 1)
                s_res = f"{idx // 12:04d}-{idx % 12 + 1:02d}-01"
            else:
                s_res = None

        # 校验先后顺序
        if s_res and e_res and s_res > e_res:
            raise ValueError(f"起始日期 {s_res} 不能晚于截止日期 {e_res}")

        desc = f"{s_res or '起初'} 至 {e_res or '至今'}"
        return s_res, e_res, desc

    # 3. 解析 months 相对区间
    if months is not None:
        if months <= 0:
            return None, None, "全部历史"
        idx = ref_date.year * 12 + ref_date.month - 1 - (months - 1)
        s_date = f"{idx // 12:04d}-{idx % 12 + 1:02d}-01"
        e_date = ref_date.strftime("%Y-%m-%d")
        if months == 6:
            desc = f"近半年 ({s_date} 至 {e_date})"
        elif months == 12:
            desc = f"近一年 ({s_date} 至 {e_date})"
        elif months % 12 == 0:
            desc = f"近 {months // 12} 年 ({s_date} 至 {e_date})"
        else:
            desc = f"近 {months} 个月 ({s_date} 至 {e_date})"
        return s_date, e_date, desc

    # 默认值: 近 6 个月 (半年)
    idx = ref_date.year * 12 + ref_date.month - 1 - 5
    s_date = f"{idx // 12:04d}-{idx % 12 + 1:02d}-01"
    e_date = ref_date.strftime("%Y-%m-%d")
    return s_date, e_date, f"近半年 ({s_date} 至 {e_date})"


def _clean_decimal(value: Any) -> Optional[Decimal]:
    """严格解析金额为 Decimal，空/非法/NaN/Infinity 返回 None。"""
    if value is None:
        return None
    val_str = str(value).strip().replace(",", "")
    if not val_str:
        return None
    try:
        dec = Decimal(val_str)
        return dec if dec.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def classify_transaction(
    direction: Optional[str],
    amount: Optional[Decimal],
    description: str
) -> str:
    """
    确定性分类交易方向与属性。
    必须先隔离未知方向与方向符号冲突，绝不盲目默认消费。
    借记方向费用（含分期还款手续费、退款服务费）归入支出，还款与还款撤销仅在贷记或明确冲正下识别。
    - 'expense': 正常借记消费/支出/手续费 (debit 且 amount >= 0，包括“分期还款手续费”、“退款服务费”等所有借记支出)
    - 'repayment_reversal': 还款撤销/冲正 (如“还款撤销”、“自动还款撤销”)
    - 'refund': 贷记退款/冲正 (credit，且匹配退款特征)
    - 'repayment': 贷记还款入账 (credit，且匹配还款特征)
    - 'other_credit': 其他未分类贷记 (credit，非还款非退款)
    - 'direction_conflict': 方向与符号冲突 (如 debit 且 amount < 0) -> 隔离
    - 'unknown_direction': 未标方向或方向无法识别 -> 隔离
    """
    desc = (description or "").strip()
    dir_str = (direction or "").strip().lower()

    # 1. 严格检查方向有效性：未标方向或非标准方向一律隔离为 unknown_direction
    if dir_str not in ("debit", "credit"):
        return "unknown_direction"

    # 2. 借记方向 (debit)
    if dir_str == "debit":
        # 方向与符号冲突：借记交易金额为负数
        if amount is not None and amount < Decimal("0"):
            return "direction_conflict"
        # 借记还款撤销/冲正（原还款撤销冲账）
        if re.search(r"(还款撤销|撤销还款|还款冲正|还款退回)", desc):
            return "repayment_reversal"
        # 借记方向全部归入费用/消费支出（如“分期还款手续费”、“还款手续费”、“退款服务费”等，均为向持卡人借记收取的费用，绝非还款）
        return "expense"

    # 3. 贷记方向 (credit)
    if dir_str == "credit":
        if re.search(r"(还款撤销|撤销还款|还款冲正)", desc):
            return "repayment_reversal"
        if re.search(r"(还款|转账存入|存入|银联跨行还款|快捷还款|自动还款|还款缴款|转入)", desc):
            return "repayment"
        if re.search(r"(退款|退货|退税|退回|冲正|返还|返现|冲减)", desc):
            return "refund"
        return "other_credit"

    return "unknown_direction"


CATEGORY_LABELS = {
    "expense": "消费支出",
    "refund": "退款冲抵",
    "repayment": "还款入账",
    "repayment_reversal": "还款撤销",
    "other_credit": "其他贷记",
    "direction_conflict": "方向与符号冲突",
    "unknown_direction": "未标方向",
}


def check_repayment_consistency(
    statements: List[Dict[str, Any]],
    transactions: Optional[List[Dict[str, Any]]] = None,
    db_path: Optional[str] = None,
    conn: Optional[Any] = None,
    as_of: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    上期应还与后续还款的一致性检查（辅助核验证据）：
    比较对象：上期账单的已确权币种应还金额 (B)，与后续对应还款入账金额 (R)。
    匹配维度：account_name + bank_code + currency (严格按确权币种比较)。
    差额：Δ = R - B。
    输出状态：
      - '金额吻合'：身份、币种、窗口均明确，且 |Δ| <= 0.01。
      - '存在差额'：证据完整且窗口已走完但金额不符 (|Δ| > 0.01)。
      - '待观察'：到期日尚未到，或数据库流水尚未覆盖完整还款窗口。
      - '无法判断'：账户未确权、多卡混合、币种未确权、缺少确权应还金额、还款流水存在跨期歧义。
    """
    ref_date = as_of or date.today()
    results: List[Dict[str, Any]] = []

    # 0. 准备或复用 DB 连接 (用于加载确权 balances 与扩充还款窗口)
    managed_conn = False
    active_conn = conn
    if active_conn is None and db_path:
        active_conn = connect_readonly_db(db_path)
        managed_conn = True

    try:
        # 1. 为缺失 balances 的账单从 DB 补齐 statement_balances
        stmt_ids = [st.get("id") for st in statements if st.get("id")]
        if active_conn and stmt_ids:
            cur = active_conn.cursor()
            cur.execute("PRAGMA table_info(statement_balances)")
            sb_cols = {row[1] for row in cur.fetchall()}
            if sb_cols:
                ph = ",".join("?" for _ in stmt_ids)
                cur.execute(
                    f"SELECT statement_id, currency, total_due, minimum_due, currency_source FROM statement_balances WHERE statement_id IN ({ph})",
                    tuple(stmt_ids)
                )
                sb_rows = cur.fetchall()
                sb_by_sid: Dict[int, List[Dict[str, Any]]] = {}
                for row in sb_rows:
                    r_dict = dict(row)
                    sb_by_sid.setdefault(r_dict.get("statement_id"), []).append(r_dict)
                for st in statements:
                    if not st.get("balances"):
                        st["balances"] = sb_by_sid.get(st.get("id"), [])

        # 2. 展开待核验的目标条目 (targets) 并校验账户与确权币种
        targets: List[Dict[str, Any]] = []
        for st in statements:
            sid = st.get("id")
            acct = (st.get("account_name") or "").strip()
            bank = (st.get("bank_code") or "").strip().upper()
            month = st.get("statement_month") or ""
            due_str = st.get("due_date") or ""
            stmt_date = st.get("statement_date") or (f"{month}-01" if month else "")

            # [P1] 严格校验账户归属：缺失或为 __orphan__ 则无法确定归属
            if not acct or acct == "__orphan__":
                results.append({
                    "statement_id": sid, "account_name": acct, "bank_code": bank, "statement_month": month,
                    "due_date": due_str, "currency": "UNKNOWN", "total_due": None,
                    "repayment_amount": Decimal("0.00"), "delta": None,
                    "status": "无法判断", "reason": "账户标识缺失或未确权 (__orphan__)",
                })
                continue

            # [P1] 必须按已确权 balances 逐币种核验
            confirmed_bals = [
                b for b in st.get("balances", [])
                if confirmed_currency(b.get("currency"), b.get("currency_source"))
            ]
            if not confirmed_bals:
                results.append({
                    "statement_id": sid, "account_name": acct, "bank_code": bank, "statement_month": month,
                    "due_date": due_str, "currency": "UNKNOWN", "total_due": None,
                    "repayment_amount": Decimal("0.00"), "delta": None,
                    "status": "无法判断", "reason": "缺少确权币种应还金额证据 (statement_balances 未确权或缺失)",
                })
                continue

            for bal in confirmed_bals:
                curr = bal["currency"].strip().upper()
                b_dec = _clean_decimal(bal.get("total_due"))
                if b_dec is None:
                    results.append({
                        "statement_id": sid, "account_name": acct, "bank_code": bank, "statement_month": month,
                        "due_date": due_str, "currency": curr, "total_due": None,
                        "repayment_amount": Decimal("0.00"), "delta": None,
                        "status": "无法判断", "reason": f"确权币种 {curr} 应还金额缺失或无法解析为合法数字",
                    })
                    continue

                targets.append({
                    "statement_id": sid,
                    "account_name": acct,
                    "bank_code": bank,
                    "statement_month": month,
                    "statement_date": stmt_date,
                    "due_date": due_str,
                    "currency": curr,
                    "total_due": b_dec,
                    "B": b_dec,
                })

        # 3. 检查同一账户同银行同月份同币种多张账单（多卡/重复账单歧义）
        month_curr_counts: Dict[Tuple[str, str, str, str], int] = {}
        for t in targets:
            k = (t["account_name"], t["bank_code"], t["statement_month"], t["currency"])
            month_curr_counts[k] = month_curr_counts.get(k, 0) + 1

        unambiguous_targets: List[Dict[str, Any]] = []
        for t in targets:
            k = (t["account_name"], t["bank_code"], t["statement_month"], t["currency"])
            if month_curr_counts.get(k, 0) > 1:
                results.append({
                    "statement_id": t["statement_id"], "account_name": t["account_name"],
                    "bank_code": t["bank_code"], "statement_month": t["statement_month"],
                    "due_date": t["due_date"], "currency": t["currency"], "total_due": t["total_due"],
                    "repayment_amount": Decimal("0.00"), "delta": None,
                    "status": "无法判断", "reason": "同一账户银行月份存在多张账单（多卡归属不明）",
                })
            else:
                unambiguous_targets.append(t)

        if not unambiguous_targets:
            return results

        # 4. [P1 修复] 从 DB 加载目标账户和银行的完整账单序列，锁定绝对稳定的还款窗口，杜绝随导出范围变化
        full_stmts_by_acct_bank: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        target_acct_banks = {(t["account_name"], t["bank_code"]) for t in unambiguous_targets}

        if active_conn:
            cur = active_conn.cursor()
            for acct, bank in target_acct_banks:
                try:
                    cur.execute("""
                        SELECT id, account_name, bank_code, statement_month, statement_date, due_date, parse_status, raw_fields_json
                        FROM statements
                        WHERE account_name = ? AND UPPER(bank_code) = ?
                          AND parse_status != 'failed'
                        ORDER BY COALESCE(NULLIF(statement_date, ''), statement_month || '-01') ASC, id ASC
                    """, (acct, bank))
                    rows = cur.fetchall()
                    valid_stmt_list = []
                    for r in rows:
                        rd = dict(r)
                        gate = stored_purpose(rd.get("raw_fields_json"), "transaction_trends")
                        if gate.get("status") != "blocked":
                            s_d = rd.get("statement_date") or (f"{rd.get('statement_month')}-01" if rd.get("statement_month") else "")
                            if s_d:
                                rd["norm_stmt_date"] = s_d
                                valid_stmt_list.append(rd)
                    full_stmts_by_acct_bank[(acct, bank)] = valid_stmt_list
                except Exception:
                    pass

        # 内存单测回退：若未连接 DB，使用传入的 statements 按 (acct, bank) 组织有效序列
        for st in statements:
            acct = (st.get("account_name") or "").strip()
            bank = (st.get("bank_code") or "").strip().upper()
            if not acct or acct == "__orphan__":
                continue
            k = (acct, bank)
            if k not in full_stmts_by_acct_bank:
                s_d = st.get("statement_date") or (f"{st.get('statement_month')}-01" if st.get("statement_month") else "")
                if s_d:
                    st_copy = dict(st)
                    st_copy["norm_stmt_date"] = s_d
                    full_stmts_by_acct_bank.setdefault(k, []).append(st_copy)
        for k in full_stmts_by_acct_bank:
            full_stmts_by_acct_bank[k].sort(key=lambda x: (x.get("norm_stmt_date") or "", x.get("id") or 0))

        # 为每个 target 计算绝对客观的归属区间 [stmt_date, window_end)
        for t in unambiguous_targets:
            acct = t["account_name"]
            bank = t["bank_code"]
            stmt_date = t["statement_date"]
            sid = t["statement_id"]

            all_stmts = full_stmts_by_acct_bank.get((acct, bank), [])
            next_stmt = None
            found_current = False
            for s in all_stmts:
                if not found_current:
                    if (sid is not None and s.get("id") == sid) or (s.get("norm_stmt_date") == stmt_date):
                        found_current = True
                        continue
                else:
                    if s.get("norm_stmt_date") > stmt_date:
                        next_stmt = s
                        break

            if next_stmt:
                t["window_end"] = next_stmt["norm_stmt_date"]
                t["has_next_statement"] = True
            else:
                t["has_next_statement"] = False
                due_s = t["due_date"] or ""
                try:
                    d_obj = datetime.strptime(due_s[:10], "%Y-%m-%d").date()
                    t["window_end"] = (d_obj + timedelta(days=15)).strftime("%Y-%m-%d")
                except Exception:
                    try:
                        s_obj = datetime.strptime(stmt_date[:10], "%Y-%m-%d").date()
                        t["window_end"] = (s_obj + timedelta(days=45)).strftime("%Y-%m-%d")
                    except Exception:
                        t["window_end"] = "2099-12-31"

        # [P2 修复] 查询目标账户在目标银行下的最新流水日期 (绝不用全库最大日期污染目标账户覆盖度)
        target_max_txn_dates: Dict[Tuple[str, str], Optional[str]] = {}
        if active_conn:
            cur = active_conn.cursor()
            for acct, bank in target_acct_banks:
                try:
                    cur.execute("""
                        SELECT MAX(COALESCE(NULLIF(t.txn_date, ''), NULLIF(t.post_date, '')))
                        FROM statement_transactions t
                        JOIN statements s ON t.statement_id = s.id
                        WHERE t.account_name = ? AND UPPER(t.bank_code) = ?
                          AND s.parse_status != 'failed'
                    """, (acct, bank))
                    row_max = cur.fetchone()
                    target_max_txn_dates[(acct, bank)] = str(row_max[0])[:10] if (row_max and row_max[0]) else None
                except Exception:
                    target_max_txn_dates[(acct, bank)] = None

        # [P2 修复] 从 DB 独立加载还款候选窗口流水
        candidate_txns_by_id: Dict[Any, Dict[str, Any]] = {}

        if active_conn:
            cur = active_conn.cursor()
            starts = [t["statement_date"] for t in unambiguous_targets if t["statement_date"]]
            min_start = min(starts) if starts else "1970-01-01"
            ends = [t["window_end"] for t in unambiguous_targets if t.get("window_end")]
            max_end = max(ends) if ends else "2099-12-31"

            try:
                cur.execute("""
                    SELECT t.id, t.bank_code, t.account_name, t.uid,
                           t.txn_date, t.post_date, t.description,
                           t.amount, t.currency, t.currency_source,
                           t.direction, t.statement_id, s.raw_fields_json
                    FROM statement_transactions t
                    JOIN statements s ON t.statement_id = s.id
                    WHERE s.parse_status != 'failed'
                      AND SUBSTR(COALESCE(NULLIF(t.txn_date, ''), NULLIF(t.post_date, '')), 1, 10) >= ?
                      AND SUBSTR(COALESCE(NULLIF(t.txn_date, ''), NULLIF(t.post_date, '')), 1, 10) <= ?
                """, (min_start, max_end))
                for row in cur.fetchall():
                    r = dict(row)
                    # [P1 质量门禁 1] 母账单用途门禁检查 (排除 blocked)
                    gate = stored_purpose(r.get("raw_fields_json"), "transaction_trends")
                    if gate.get("status") == "blocked":
                        continue
                    # [P1 质量门禁 2] 币种严格确权 (排除 legacy_assumed 与未确权来源)
                    if not confirmed_currency(r.get("currency"), r.get("currency_source")):
                        continue

                    r["category"] = classify_transaction(r.get("direction"), _clean_decimal(r.get("amount")), r.get("description") or "")
                    if r["category"] in ("repayment", "repayment_reversal"):
                        candidate_txns_by_id[r["id"]] = r
            except Exception:
                pass

        # 合并传入的 transactions (内存传入或补充)，同样严格经过质量过滤与币种确权
        raw_txns = transactions or []
        for idx, t in enumerate(raw_txns):
            t_copy = dict(t)
            t_id = t_copy.get("id") or f"mem_{idx}"
            # [P1 质量门禁 2] 币种严格确权
            if not confirmed_currency(t_copy.get("currency"), t_copy.get("currency_source")):
                continue
            # 母账单用途门禁
            if t_copy.get("is_blocked"):
                continue
            if t_copy.get("raw_fields_json"):
                gate = stored_purpose(t_copy.get("raw_fields_json"), "transaction_trends")
                if gate.get("status") == "blocked":
                    continue

            if "category" not in t_copy:
                t_copy["category"] = classify_transaction(t_copy.get("direction"), _clean_decimal(t_copy.get("amount")), t_copy.get("description") or "")
            if t_copy["category"] in ("repayment", "repayment_reversal"):
                candidate_txns_by_id[t_id] = t_copy

        all_candidate_txns = list(candidate_txns_by_id.values())

        # 5. [P1: 每笔还款唯一归属，杜绝一笔还款匹配两期账单]
        # 按 (account_name, bank_code, currency) 分组处理账单序列
        grouped_targets: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for t in unambiguous_targets:
            k = (t["account_name"], t["bank_code"], t["currency"])
            grouped_targets.setdefault(k, []).append(t)

        ambiguous_statement_ids = set()
        matched_txns_by_target_idx: Dict[int, List[Dict[str, Any]]] = {}
        target_map = {idx: t for idx, t in enumerate(unambiguous_targets)}

        for group_key, grp in grouped_targets.items():
            grp_acct, grp_bank, grp_curr = group_key
            target_intervals = [(tgt, tgt["statement_date"], tgt["window_end"]) for tgt in grp]

            # 筛选严格匹配该组 (account_name, bank_code, currency) 的流水
            grp_txns = [
                tx for tx in all_candidate_txns
                if (tx.get("account_name") or "").strip() == grp_acct
                and (tx.get("bank_code") or "").strip().upper() == grp_bank
                and (tx.get("currency") or "").strip().upper() == grp_curr
            ]

            for tx in grp_txns:
                tx_date = (tx.get("txn_date") or tx.get("post_date") or "")[:10]
                if not tx_date:
                    continue

                # 寻找包含该交易日期的账单区间 [s_d, e_d)
                matched = [tgt for (tgt, s_d, e_d) in target_intervals if s_d <= tx_date < e_d]
                if len(matched) == 1:
                    # 唯一归属
                    tgt = matched[0]
                    t_idx = next(i for i, v in target_map.items() if v is tgt)
                    matched_txns_by_target_idx.setdefault(t_idx, []).append(tx)
                elif len(matched) > 1:
                    # 跨期重叠歧义：绝不能两期都匹配！
                    for tgt in matched:
                        ambiguous_statement_ids.add(tgt["statement_id"])

        # 6. 计算各 target 的还款净入账、差额与状态
        for t_idx, tgt in enumerate(unambiguous_targets):
            sid = tgt["statement_id"]
            if sid in ambiguous_statement_ids:
                results.append({
                    "statement_id": sid, "account_name": tgt["account_name"],
                    "bank_code": tgt["bank_code"], "statement_month": tgt["statement_month"],
                    "due_date": tgt["due_date"], "currency": tgt["currency"], "total_due": tgt["total_due"],
                    "repayment_amount": Decimal("0.00"), "delta": None,
                    "status": "无法判断", "reason": "还款流水归属存在歧义 (跨期候选窗口重叠)",
                })
                continue

            B = tgt["B"]
            matched_list = matched_txns_by_target_idx.get(t_idx, [])
            R = Decimal("0.00")
            for tx in matched_list:
                cat = tx.get("category")
                amt = _clean_decimal(tx.get("amount")) or Decimal("0.00")
                amt_abs = abs(amt)
                if cat == "repayment":
                    R += amt_abs
                elif cat == "repayment_reversal":
                    R -= amt_abs

            delta = R - B
            window_end = tgt.get("window_end") or "9999-12-31"
            due_str = tgt.get("due_date") or ""
            has_next = tgt.get("has_next_statement", False)
            acct = tgt["account_name"]
            bank = tgt["bank_code"]

            # 判断时间窗口是否已走完
            ref_str = ref_date.strftime("%Y-%m-%d")
            time_window_passed = ref_str >= window_end
            if due_str:
                time_window_passed = ref_str > due_str[:10]

            # [P2 修复] 目标账户银行独立数据覆盖度判断
            if has_next:
                # 存在后续有效账单，表明该期还款窗口已客观走完且下期数据已入库
                data_covered = True
            elif active_conn:
                # 在真实数据库环境下，没有后续账单时，必须检查目标账户在该银行的最新流水日期是否覆盖到 window_end
                acct_max_d = target_max_txn_dates.get((acct, bank))
                if acct_max_d is None or acct_max_d < window_end:
                    data_covered = False
                else:
                    data_covered = True
            else:
                # 纯内存单测调用 (无 DB 连接)，以调用者传入的流水集合与时间窗口为准
                data_covered = True

            if abs(delta) <= Decimal("0.01"):
                status = "金额吻合"
                reason = f"还款净入账 {R:,.2f} 与应还 {B:,.2f} 完全吻合"
            elif R < B and (not time_window_passed or not data_covered):
                status = "待观察"
                if not time_window_passed:
                    reason = f"到期日 ({due_str}) 尚未到期，当前还款 {R:,.2f} (应还 {B:,.2f})"
                else:
                    reason = f"账户 ({acct}/{bank}) 数据库流水尚未覆盖完整还款窗口 ({tgt['statement_date']} 至 {window_end})，当前还款 {R:,.2f} vs 应还 {B:,.2f}"
            else:
                status = "存在差额"
                reason = f"差额 Δ = {delta:+,.2f} (实际还款 {R:,.2f} vs 应还 {B:,.2f})"

            results.append({
                "statement_id": sid, "account_name": tgt["account_name"],
                "bank_code": tgt["bank_code"], "statement_month": tgt["statement_month"],
                "due_date": due_str, "currency": tgt["currency"], "total_due": B,
                "repayment_amount": R, "delta": delta,
                "status": status, "reason": reason,
            })

        return results

    finally:
        if managed_conn and active_conn:
            active_conn.close()


def query_expense_data(
    db_path: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    bank_code: Optional[str] = None,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """
    从 SQLite 只读查询指定日期区间的交易明细与月度账单。
    严格执行数据质量约束：
    1. 关联母账单，剔除孤儿交易与 parse_status='failed' 的账单交易；
    2. 检查母账单用途门禁 (purpose_availability.transaction_trends)，区分 available / partial / blocked；
    3. 严格币种确权 (confirmed_currency)，未确认币种隔离为 UNKNOWN，严禁静默视为 CNY；
    4. 严格原币确权检查 (original_currency_source)，未确权原币明确标注；
    5. 严格收支方向隔离 (先隔离 unknown_direction / direction_conflict，再分类 expense / refund / repayment)；
    6. 账单日期严格以 statement_date 为准，仅当其缺失时以月份作为回退；
    7. 计算还款一致性辅助核验检查。
    """
    conn = connect_readonly_db(db_path)
    if conn is None:
        return {
            "success": False,
            "error": "无法连接账单数据库 (statements.db 不存在或路径不可达)",
            "transactions": [],
            "statements": [],
        }

    try:
        cur = conn.cursor()

        cur.execute("PRAGMA table_info(statement_transactions)")
        cols = {row[1] for row in cur.fetchall()}
        has_stmt_id = 'statement_id' in cols

        # 1. 查询交易并关联母账单
        join_clause = (
            "LEFT JOIN statements s ON t.statement_id = s.id"
            if has_stmt_id
            else "LEFT JOIN statements s ON (t.bank_code = s.bank_code AND t.uid = s.uid AND t.account_name = s.account_name)"
        )

        txn_sql = f"""
            SELECT t.id, t.bank_code, t.uid, t.account_name,
                   t.txn_date, t.post_date, t.description,
                   t.amount, t.currency, t.currency_source,
                   t.original_amount, t.original_currency, t.original_currency_source,
                   t.txn_location_code, t.direction,
                   s.id AS parent_statement_id, s.statement_month, s.statement_date,
                   s.parse_status, s.raw_fields_json
            FROM statement_transactions t
            {join_clause}
            WHERE 1=1
        """
        txn_params: List[Any] = []

        if start_date:
            txn_sql += " AND SUBSTR(COALESCE(NULLIF(t.txn_date, ''), NULLIF(t.post_date, '')), 1, 10) >= ?"
            txn_params.append(start_date)
        if end_date:
            txn_sql += " AND SUBSTR(COALESCE(NULLIF(t.txn_date, ''), NULLIF(t.post_date, '')), 1, 10) <= ?"
            txn_params.append(end_date)
        if bank_code:
            txn_sql += " AND UPPER(t.bank_code) = ?"
            txn_params.append(bank_code.strip().upper())

        txn_sql += " ORDER BY COALESCE(t.txn_date, t.post_date) DESC, t.id DESC"

        cur.execute(txn_sql, tuple(txn_params))
        txn_rows = cur.fetchall()

        # 2. 查询宏观月度账单 (statements)
        # 严格过滤：以 statement_date 为第一准绳；仅当 statement_date 为空/NULL 时，才以 statement_month || '-01' 回退
        stmt_sql = """
            SELECT id, account_name, bank_code, statement_month, statement_date, due_date,
                   total_due, minimum_due, is_paid, parse_status, raw_fields_json
            FROM statements
            WHERE 1=1
        """
        stmt_params: List[Any] = []

        if start_date:
            stmt_sql += " AND SUBSTR(COALESCE(NULLIF(statement_date, ''), statement_month || '-01'), 1, 10) >= ?"
            stmt_params.append(start_date)
        if end_date:
            stmt_sql += " AND SUBSTR(COALESCE(NULLIF(statement_date, ''), statement_month || '-01'), 1, 10) <= ?"
            stmt_params.append(end_date)
        if bank_code:
            stmt_sql += " AND UPPER(bank_code) = ?"
            stmt_params.append(bank_code.strip().upper())

        stmt_sql += " ORDER BY COALESCE(statement_date, statement_month) DESC, id DESC"

        cur.execute(stmt_sql, tuple(stmt_params))
        stmt_rows = cur.fetchall()
        statements: List[Dict[str, Any]] = [dict(row) for row in stmt_rows]

        # 预加载已确权的 statement_balances
        stmt_ids = [st["id"] for st in statements if st.get("id")]
        if stmt_ids:
            cur.execute("PRAGMA table_info(statement_balances)")
            sb_cols = {row[1] for row in cur.fetchall()}
            if sb_cols:
                ph = ",".join("?" for _ in stmt_ids)
                cur.execute(
                    f"SELECT statement_id, currency, total_due, minimum_due, currency_source FROM statement_balances WHERE statement_id IN ({ph})",
                    tuple(stmt_ids)
                )
                sb_by_sid: Dict[int, List[Dict[str, Any]]] = {}
                for row in cur.fetchall():
                    r_dict = dict(row)
                    sb_by_sid.setdefault(r_dict.get("statement_id"), []).append(r_dict)
                for st in statements:
                    st["balances"] = sb_by_sid.get(st.get("id"), [])

        # 3. 逐笔校验交易质量与收支聚合
        verified_transactions: List[Dict[str, Any]] = []
        orphan_transactions: List[Dict[str, Any]] = []
        failed_statement_transactions: List[Dict[str, Any]] = []
        gated_transactions: List[Dict[str, Any]] = []
        partial_transactions: List[Dict[str, Any]] = []
        unknown_currency_transactions: List[Dict[str, Any]] = []
        isolated_direction_transactions: List[Dict[str, Any]] = []

        # 聚合结构
        totals_by_currency: Dict[str, Dict[str, Decimal]] = {}
        by_month: Dict[str, Dict[str, Any]] = {}
        by_bank: Dict[str, Dict[str, Any]] = {}

        for row in txn_rows:
            r = dict(row)
            date_val = r.get("txn_date") or r.get("post_date") or ""
            month_key = date_val[:7] if len(date_val) >= 7 else "未知月份"
            bank = (r.get("bank_code") or "未知银行").upper()
            amt_dec = _clean_decimal(r.get("amount"))
            r["amount_decimal"] = amt_dec

            # 质量约束 1: 孤儿交易过滤 (母账单不存在)
            if r.get("parent_statement_id") is None:
                orphan_transactions.append(r)
                continue

            # 质量约束 2: 母账单解析失败过滤
            if (r.get("parse_status") or "").lower() == "failed":
                failed_statement_transactions.append(r)
                continue

            # 质量约束 3: 用途门禁过滤与 partial 状态追踪
            trends_gate = stored_purpose(r.get("raw_fields_json"), "transaction_trends")
            gate_status = trends_gate.get("status", "partial")
            r["purpose_status"] = gate_status
            if gate_status == "blocked":
                gated_transactions.append(r)
                continue
            elif gate_status == "partial":
                partial_transactions.append(r)

            # 质量约束 4: 币种确权检查
            raw_curr = (r.get("currency") or "").strip().upper()
            raw_c_src = r.get("currency_source")
            is_confirmed = confirmed_currency(raw_curr, raw_c_src)

            if not is_confirmed:
                r["currency"] = "UNKNOWN"
                r["currency_confirmed"] = False
                unknown_currency_transactions.append(r)
                # 未确认币种交易已隔离，不计入确认金额合计
                verified_transactions.append(r)
                continue

            r["currency"] = raw_curr
            r["currency_confirmed"] = True

            # 质量约束 5: 原币确权检查
            orig_amt = r.get("original_amount")
            if orig_amt is not None and str(orig_amt).strip():
                raw_orig_curr = (r.get("original_currency") or "").strip().upper()
                raw_orig_src = r.get("original_currency_source")
                orig_confirmed = confirmed_currency(raw_orig_curr, raw_orig_src)
                r["original_currency_confirmed"] = orig_confirmed
                if orig_confirmed:
                    r["original_currency_display"] = f"{raw_orig_curr} {orig_amt}"
                else:
                    r["original_currency_display"] = f"未确权原币({raw_orig_curr or '未知'}) {orig_amt}"
            else:
                r["original_currency_confirmed"] = False
                r["original_currency_display"] = "-"

            # 质量约束 6: 收支方向分类与隔离
            cat = classify_transaction(r.get("direction"), amt_dec, r.get("description") or "")
            r["category"] = cat
            r["category_label"] = CATEGORY_LABELS.get(cat, "未知分类")

            verified_transactions.append(r)

            # 未知方向或符号冲突的交易必须隔离，不计入确认收支总额
            if cat in ("unknown_direction", "direction_conflict"):
                isolated_direction_transactions.append(r)
                continue

            if amt_dec is None:
                continue

            amt_abs = abs(amt_dec)

            # 币种收支结构初始化
            if raw_curr not in totals_by_currency:
                totals_by_currency[raw_curr] = {
                    "gross_expense": Decimal("0.00"),
                    "refund": Decimal("0.00"),
                    "net_expense": Decimal("0.00"),
                    "repayment": Decimal("0.00"),
                    "other_credit": Decimal("0.00"),
                }

            t_map = totals_by_currency[raw_curr]
            if cat == "expense":
                t_map["gross_expense"] += amt_abs
            elif cat == "refund":
                t_map["refund"] += amt_abs
            elif cat == "repayment":
                t_map["repayment"] += amt_abs
            elif cat == "repayment_reversal":
                t_map["repayment"] -= amt_abs
            elif cat == "other_credit":
                t_map["other_credit"] += amt_abs
            t_map["net_expense"] = t_map["gross_expense"] - t_map["refund"]

            # 月度走势汇总
            if month_key not in by_month:
                by_month[month_key] = {"count": 0, "by_currency": {}}
            by_month[month_key]["count"] += 1
            m_curr = by_month[month_key]["by_currency"]
            if raw_curr not in m_curr:
                m_curr[raw_curr] = {"expense": Decimal("0.00"), "refund": Decimal("0.00"), "repayment": Decimal("0.00")}
            if cat == "expense":
                m_curr[raw_curr]["expense"] += amt_abs
            elif cat == "refund":
                m_curr[raw_curr]["refund"] += amt_abs
            elif cat == "repayment":
                m_curr[raw_curr]["repayment"] += amt_abs
            elif cat == "repayment_reversal":
                m_curr[raw_curr]["repayment"] -= amt_abs

            # 银行走势汇总
            if bank not in by_bank:
                by_bank[bank] = {"count": 0, "by_currency": {}}
            by_bank[bank]["count"] += 1
            b_curr = by_bank[bank]["by_currency"]
            if raw_curr not in b_curr:
                b_curr[raw_curr] = {"expense": Decimal("0.00"), "refund": Decimal("0.00"), "repayment": Decimal("0.00")}
            if cat == "expense":
                b_curr[raw_curr]["expense"] += amt_abs
            elif cat == "refund":
                b_curr[raw_curr]["refund"] += amt_abs
            elif cat == "repayment":
                b_curr[raw_curr]["repayment"] += amt_abs
            elif cat == "repayment_reversal":
                b_curr[raw_curr]["repayment"] -= amt_abs

        # 4. 计算大额消费 TOP 10 (按币种分别排行，且仅统计实际消费 expense，排除还款、退款与隔离交易)
        top_expenses_by_currency: Dict[str, List[Dict[str, Any]]] = {}
        for curr in sorted(totals_by_currency.keys()):
            curr_expenses = [
                tx for tx in verified_transactions
                if tx.get("currency") == curr and tx.get("category") == "expense" and tx.get("amount_decimal") is not None
            ]
            sorted_curr = sorted(curr_expenses, key=lambda x: abs(x["amount_decimal"]), reverse=True)[:10]
            top_expenses_by_currency[curr] = sorted_curr

        # 5. 上期应还与后续还款一致性检查 (辅助验证)
        repayment_checks = check_repayment_consistency(
            statements=statements,
            transactions=verified_transactions,
            conn=conn,
            db_path=db_path,
            as_of=as_of,
        )

        anomalies_summary = {
            "orphan_count": len(orphan_transactions),
            "failed_statement_count": len(failed_statement_transactions),
            "gated_count": len(gated_transactions),
            "partial_count": len(partial_transactions),
            "unknown_currency_count": len(unknown_currency_transactions),
            "unknown_direction_count": len([t for t in isolated_direction_transactions if t.get("category") == "unknown_direction"]),
            "direction_conflict_count": len([t for t in isolated_direction_transactions if t.get("category") == "direction_conflict"]),
            "is_complete": (
                len(orphan_transactions) == 0
                and len(failed_statement_transactions) == 0
                and len(gated_transactions) == 0
                and len(partial_transactions) == 0
                and len(unknown_currency_transactions) == 0
                and len(isolated_direction_transactions) == 0
            ),
        }

        return {
            "success": True,
            "error": None,
            "transactions": verified_transactions,
            "statements": statements,
            "totals_by_currency": totals_by_currency,
            "by_month": by_month,
            "by_bank": by_bank,
            "top_expenses_by_currency": top_expenses_by_currency,
            "repayment_checks": repayment_checks,
            "anomalies": anomalies_summary,
            "unknown_currency_transactions": unknown_currency_transactions,
            "gated_transactions": gated_transactions,
            "isolated_direction_transactions": isolated_direction_transactions,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"查询数据库异常: {exc}",
            "transactions": [],
            "statements": [],
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def generate_expense_csv_content(transactions: List[Dict[str, Any]]) -> str:
    """
    生成标准纯文本 RFC 4180 CSV（绝不在此重复包含 BOM 字符，由写文件统一写入）。
    """
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    headers = [
        "序号", "交易日期", "记账日期", "银行",
        "交易说明", "分类", "金额", "币种",
        "原币金额", "原币币种", "原币确权状态", "交易方向", "位置/备注"
    ]
    writer.writerow(headers)

    for idx, tx in enumerate(transactions, start=1):
        orig_confirmed_str = "已确权" if tx.get("original_currency_confirmed") else "未确权"
        writer.writerow([
            idx,
            tx.get("txn_date") or "",
            tx.get("post_date") or "",
            tx.get("bank_code") or "",
            tx.get("description") or "",
            tx.get("category_label") or "未知",
            tx.get("amount") or "",
            (tx.get("currency") or "UNKNOWN").upper(),
            tx.get("original_amount") or "",
            (tx.get("original_currency") or "").upper(),
            orig_confirmed_str,
            tx.get("direction") or "",
            tx.get("txn_location_code") or "",
        ])

    return output.getvalue()


def write_expense_csv_file(csv_content: str, output_path: str) -> str:
    """
    将 CSV 内容安全写入指定文件。
    清理字符串前端残留的 BOM 字符，使用 utf-8-sig 编码，确保磁盘文件严格以单次 EF BB BF 开头。
    """
    clean_content = csv_content.lstrip("\ufeff")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(clean_content)
    return os.path.abspath(output_path)


def build_expense_markdown_report(
    range_desc: str,
    query_result: Dict[str, Any],
    csv_content: str,
) -> str:
    """
    组织丰富、可供在线查阅与二次导出的 Markdown 格式开支报表。
    区分完整核验与部分核验，明确口径，分币种呈现排行，标记未确权原币，展示还款一致性辅助验证。
    """
    txns = query_result.get("transactions") or []
    stmts = query_result.get("statements") or []
    totals = query_result.get("totals_by_currency") or {}
    by_month = query_result.get("by_month") or {}
    by_bank = query_result.get("by_bank") or {}
    top_by_curr = query_result.get("top_expenses_by_currency") or {}
    repayment_checks = query_result.get("repayment_checks") or []
    anomalies = query_result.get("anomalies") or {}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 核验完整度提示
    is_complete = anomalies.get("is_complete", False)
    status_tag = "✅ 完整核验 (全部质量规则与确权通过)" if is_complete else "⚠️ 部分核验 (存在未完全核验或质量受限记录，统计覆盖可能不完整)"

    # 统计核心指标看板
    summary_blocks = []
    for curr, stat in sorted(totals.items()):
        summary_blocks.append(
            f"- **{curr}**: 消费净额 `{stat['net_expense']:,.2f}` "
            f"(消费支出 `{stat['gross_expense']:,.2f}` - 退款 `{stat['refund']:,.2f}`) | "
            f"还款净入账 `{stat['repayment']:,.2f}`"
        )
    summary_str = "\n".join(summary_blocks) if summary_blocks else "- 暂无可统计的确认币种金额"

    lines = [
        f"# 📊 财务开支明细与汇总报表",
        f"",
        f"> **统计周期**: {range_desc}  ",
        f"> **生成时间**: {now_str}  ",
        f"> **核验状态**: {status_tag}  ",
        f"> **交易记录**: 共 {len(txns)} 笔 | **月度出账账单**: {len(stmts)} 封  ",
        f"",
        f"### 💰 收支净额口径汇总",
        summary_str,
        f"",
    ]

    # 异常或隔离信息合规披露
    anom_notes = []
    if anomalies.get("partial_count", 0) > 0:
        anom_notes.append(f"存在 {anomalies['partial_count']} 笔交易所属账单处于 partial 状态 (缺少完整质量证据或存在核验缺口)，统计覆盖可能不完整")
    if anomalies.get("unknown_currency_count", 0) > 0:
        anom_notes.append(f"存在 {anomalies['unknown_currency_count']} 笔未确认币种交易，已单独标记为 UNKNOWN 并在总额中隔离")
    if anomalies.get("unknown_direction_count", 0) > 0:
        anom_notes.append(f"存在 {anomalies['unknown_direction_count']} 笔未标方向交易，已单独隔离，未盲目计入消费支出")
    if anomalies.get("direction_conflict_count", 0) > 0:
        anom_notes.append(f"存在 {anomalies['direction_conflict_count']} 笔方向与符号冲突交易 (如借记金额为负)，已单独隔离")
    if anomalies.get("gated_count", 0) > 0:
        anom_notes.append(f"排除 {anomalies['gated_count']} 笔 transaction_trends 门禁受阻 (blocked) 的账单交易")
    if anomalies.get("failed_statement_count", 0) > 0:
        anom_notes.append(f"排除 {anomalies['failed_statement_count']} 笔属于解析失败 (parse_status=failed) 账单的交易")
    if anomalies.get("orphan_count", 0) > 0:
        anom_notes.append(f"排除 {anomalies['orphan_count']} 笔未关联到母账单的孤儿流水")

    if anom_notes:
        lines.append("### ⚠️ 数据质量与合规检查披露")
        for n in anom_notes:
            lines.append(f"- {n}")
        lines.append("")

    lines.extend([
        f"---",
        f"",
        f"## 🏦 各银行收支汇总",
        f"",
        f"| 银行代码 | 交易笔数 | 消费支出 (CNY/外币) | 退款冲抵 | 还款净入账 |",
        f"| :--- | :--- | :--- | :--- | :--- |",
    ])

    if by_bank:
        for bank, b_data in sorted(by_bank.items()):
            cnt = b_data.get("count", 0)
            b_currs = b_data.get("by_currency", {})
            exp_strs = [f"{c} {v['expense']:,.2f}" for c, v in sorted(b_currs.items()) if v['expense'] > 0]
            ref_strs = [f"{c} {v['refund']:,.2f}" for c, v in sorted(b_currs.items()) if v['refund'] > 0]
            rep_strs = [f"{c} {v['repayment']:,.2f}" for c, v in sorted(b_currs.items()) if v['repayment'] != 0]
            lines.append(
                f"| {bank} | {cnt} 笔 | "
                f"{' / '.join(exp_strs) or '0.00'} | "
                f"{' / '.join(ref_strs) or '0.00'} | "
                f"{' / '.join(rep_strs) or '0.00'} |"
            )
    else:
        lines.append(f"| - | 0 笔 | 0.00 | 0.00 | 0.00 |")

    lines.extend([
        f"",
        f"## 📅 月度走势汇总",
        f"",
        f"| 月份 | 笔数 | 消费支出 | 退款冲抵 | 还款净入账 |",
        f"| :--- | :--- | :--- | :--- | :--- |",
    ])

    if by_month:
        for m_key, m_data in sorted(by_month.items(), reverse=True):
            cnt = m_data.get("count", 0)
            m_currs = m_data.get("by_currency", {})
            exp_strs = [f"{c} {v['expense']:,.2f}" for c, v in sorted(m_currs.items()) if v['expense'] > 0]
            ref_strs = [f"{c} {v['refund']:,.2f}" for c, v in sorted(m_currs.items()) if v['refund'] > 0]
            rep_strs = [f"{c} {v['repayment']:,.2f}" for c, v in sorted(m_currs.items()) if v['repayment'] != 0]
            lines.append(
                f"| {m_key} | {cnt} 笔 | "
                f"{' / '.join(exp_strs) or '0.00'} | "
                f"{' / '.join(ref_strs) or '0.00'} | "
                f"{' / '.join(rep_strs) or '0.00'} |"
            )
    else:
        lines.append(f"| - | 0 笔 | 0.00 | 0.00 | 0.00 |")

    # 大额消费排行 (分币种展示，排除还款与退款)
    if top_by_curr:
        for curr, curr_top in top_by_curr.items():
            if not curr_top:
                continue
            lines.extend([
                f"",
                f"## 💳 大额消费 TOP 10 ({curr}，已排除还款、退款与隔离交易)",
                f"",
                f"| 日期 | 银行 | 交易说明 | 金额 | 币种 | 原币信息 |",
                f"| :--- | :--- | :--- | :--- | :--- | :--- |",
            ])
            for tx in curr_top:
                d = tx.get("txn_date") or tx.get("post_date") or "-"
                b = tx.get("bank_code") or "-"
                desc = (tx.get("description") or "-").replace("|", "/")
                amt = tx.get("amount") or "0.00"
                c = (tx.get("currency") or "CNY").upper()
                orig_disp = tx.get("original_currency_display") or "-"
                lines.append(f"| {d} | {b} | {desc} | {amt} | {c} | {orig_disp} |")

    # 上期应还与后续还款一致性辅助验证表格
    if repayment_checks:
        lines.extend([
            f"",
            f"## 🔍 上期应还与后续还款一致性检查 (辅助验证)",
            f"",
            f"| 银行 | 账期 | 到期日 | 币种 | 上期应还 (B) | 还款净入账 (R) | 差额 (Δ) | 检查状态 | 备注说明 |",
            f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for chk in repayment_checks:
            b_code = chk.get("bank_code") or "-"
            m = chk.get("statement_month") or "-"
            due_d = chk.get("due_date") or "-"
            cur_code = chk.get("currency") or "CNY"
            b_amt = f"{chk['total_due']:,.2f}" if chk.get("total_due") is not None else "-"
            r_amt = f"{chk['repayment_amount']:,.2f}"
            d_amt = f"{chk['delta']:+,.2f}" if chk.get("delta") is not None else "-"
            st = chk.get("status") or "-"
            rsn = (chk.get("reason") or "-").replace("|", "/")
            lines.append(f"| {b_code} | {m} | {due_d} | {cur_code} | {b_amt} | {r_amt} | {d_amt} | {st} | {rsn} |")

    # 交易明细预览 (最近 30 笔)
    preview_limit = 30
    preview_txns = txns[:preview_limit]
    lines.extend([
        f"",
        f"## 📝 交易明细预览 (最近 {len(preview_txns)} 笔)",
        f"",
        f"| 日期 | 银行 | 分类 | 交易说明 | 金额 | 币种 | 原币信息 |",
        f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ])
    for tx in preview_txns:
        d = tx.get("txn_date") or tx.get("post_date") or "-"
        b = tx.get("bank_code") or "-"
        cat_lbl = tx.get("category_label") or "未知"
        desc = (tx.get("description") or "-").replace("|", "/")
        amt = tx.get("amount") or "0.00"
        c = (tx.get("currency") or "CNY").upper()
        orig_disp = tx.get("original_currency_display") or "-"
        lines.append(f"| {d} | {b} | {cat_lbl} | {desc} | {amt} | {c} | {orig_disp} |")

    if len(txns) > preview_limit:
        lines.append(f"\n> 💡 *注：共 {len(txns)} 笔记录，网页仅预览前 {preview_limit} 笔。完整全量数据请复制下方 CSV 数据块。*")

    # 嵌入全量 CSV 数据块（去掉 BOM，方便直接全选复制）
    clean_csv = csv_content.lstrip("\ufeff")
    lines.extend([
        f"",
        f"---",
        f"",
        f"## 📎 完整 CSV 数据 (可一键复制代码块导入 Excel/Numbers)",
        f"",
        f"```csv",
        clean_csv.strip(),
        f"```",
    ])

    return "\n".join(lines)


def export_expense_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    months: Optional[int] = None,
    year: Optional[Any] = None,
    bank_code: Optional[str] = None,
    upload_hedgedoc: bool = True,
    public: bool = False,
    db_path: Optional[str] = None,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """
    执行开支明细与汇总导出全流程：
    1. 解析并规范化时间周期（严格日历校验与先后顺序校验）；
    2. 查询 SQLite 获取明细与账单数据（严格应用母账单与用途约束）；
    3. 写入单次 BOM 的标准 CSV 文件到 workspace/exports/；
    4. 渲染 Markdown 报告（含还款一致性核验）；
    5. 上传至 HedgeDoc（默认私有笔记 public=False，仅当显式声明 public=True 时开放匿名读）；
    6. 返回结构化结果与对用户友好的摘要文字。
    """
    # 1. 解析时间周期并严格捕获日历/格式异常
    try:
        s_date, e_date, range_desc = parse_date_range(
            start_date=start_date,
            end_date=end_date,
            months=months,
            year=year,
            as_of=as_of,
        )
    except ValueError as exc:
        return {
            "success": False,
            "error": str(exc),
            "summary_text": f"❌ 日期参数错误: {exc}",
        }

    # 2. 查询数据
    query_res = query_expense_data(
        db_path=db_path,
        start_date=s_date,
        end_date=e_date,
        bank_code=bank_code,
        as_of=as_of,
    )

    if not query_res.get("success"):
        return {
            "success": False,
            "error": query_res.get("error"),
            "summary_text": f"❌ 开支明细查询失败: {query_res.get('error')}",
        }

    txns = query_res.get("transactions") or []
    stmts = query_res.get("statements") or []
    totals = query_res.get("totals_by_currency") or {}
    anomalies = query_res.get("anomalies") or {}

    # 3. 生成 CSV 内容并落盘 (单次 BOM)
    csv_content = generate_expense_csv_content(txns)

    # 构建安全文件名
    s_tag = s_date.replace("-", "") if s_date else "start"
    e_tag = e_date.replace("-", "") if e_date else "now"
    bank_tag = f"_{bank_code.strip().lower()}" if bank_code else ""
    csv_filename = f"expenses_{s_tag}_{e_tag}{bank_tag}.csv"
    csv_filepath = os.path.join(PROJECT_ROOT, "workspace", "exports", csv_filename)

    saved_csv_path = write_expense_csv_file(csv_content, csv_filepath)

    # 4. 构建 Markdown 报表
    md_report = build_expense_markdown_report(range_desc, query_res, csv_content)

    # 5. 上传 HedgeDoc (默认 public=False，严格保障隐私)
    hedgedoc_url = None
    hedgedoc_msg = ""
    if upload_hedgedoc:
        cfg = config_loader.load_config() or {}
        hc_cfg = cfg.get("hedgedoc", {})
        if hc_cfg.get("enabled"):
            try:
                # 默认 public=False（仅 owner 认证后可见），防范敏感明细泄露
                hedgedoc_url = upload_to_hedgedoc(
                    markdown_text=md_report,
                    hc_config=hc_cfg,
                    public=public,
                )
                if hedgedoc_url:
                    privacy_desc = "公开只读" if public else "私有笔记 (需登录查看)"
                    hedgedoc_msg = f"\n👉 **[点击查看 HedgeDoc 在线报表 ({privacy_desc})]({hedgedoc_url})**"
                else:
                    hedgedoc_msg = "\n*(HedgeDoc 上传未返回链接，请使用本地导出的 CSV 文件)*"
            except Exception as e:
                hedgedoc_msg = f"\n*(HedgeDoc 上传异常: {e}，已保存本地 CSV)*"
        else:
            hedgedoc_msg = "\n*(HedgeDoc 服务未启用，已保存本地 CSV 文件)*"

    # 6. 组织返回文字摘要
    curr_summary_lines = []
    for curr, stat in sorted(totals.items()):
        curr_summary_lines.append(
            f"  - **{curr}**: 消费净额 `{stat['net_expense']:,.2f}` "
            f"(消费支出 `{stat['gross_expense']:,.2f}`, 退款 `{stat['refund']:,.2f}`), "
            f"还款净入账 `{stat['repayment']:,.2f}`"
        )
    curr_summary_text = "\n".join(curr_summary_lines) if curr_summary_lines else "  - 暂无有效确认金额"

    rel_csv_path = os.path.relpath(saved_csv_path, PROJECT_ROOT)
    status_summary = "✅ 完整核验" if anomalies.get("is_complete") else "⚠️ 部分核验 (存在未完全核验或质量受限记录)"

    summary_text = (
        f"📊 **开支明细与汇总报表生成完毕**\n"
        f"- **统计区间**: {range_desc}\n"
        f"- **核验状态**: {status_summary}\n"
        f"- **明细交易**: 共 {len(txns)} 笔 (月度账单 {len(stmts)} 封)\n"
        f"- **收支口径汇总**:\n{curr_summary_text}\n"
        f"- **CSV 导出**: `{rel_csv_path}` (UTF-8 BOM 格式，兼容 Excel / Numbers)\n"
        f"{hedgedoc_msg}"
    )

    return {
        "success": True,
        "range_desc": range_desc,
        "start_date": s_date,
        "end_date": e_date,
        "total_count": len(txns),
        "totals_by_currency": {
            curr: {k: float(v) for k, v in stat.items()}
            for curr, stat in totals.items()
        },
        "csv_path": saved_csv_path,
        "hedgedoc_url": hedgedoc_url,
        "summary_text": summary_text,
    }
