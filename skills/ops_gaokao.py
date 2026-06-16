"""
高考志愿填报数据查询技能 (ops_gaokao)

直接读取 volunteer-web 容器挂载的 SQLite 数据库，提供：
  - 分数→位次 查询
  - 院校录取分数线 查询
  - 专业录取数据 查询
  - 志愿推荐（冲/稳/保）分析
  - 院校信息 查询

数据库路径: /opt/volunteer-web/docker-data/gaokao.db (宿主机)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill
import sqlite3
import json

DB_PATH = "/opt/volunteer-web/docker-data/gaokao.db"


def _get_db():
    """获取只读数据库连接"""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"数据库不存在: {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# 新高考改革年份映射：省份 -> 首次使用物理类/历史类的年份
# 之前用理科/文科
_NEW_GAOKAO_YEAR = {
    '安徽': 2024, '江西': 2024, '广西': 2024, '贵州': 2024, '甘肃': 2024,
    '黑龙江': 2024, '吉林': 2024,
    '河北': 2021, '辽宁': 2021, '江苏': 2021, '福建': 2021,
    '湖北': 2021, '湖南': 2021, '广东': 2021, '重庆': 2021,
    '北京': 2020, '天津': 2020, '山东': 2020, '海南': 2020,
    '浙江': 2017, '上海': 2017,
}

# 综合改革省份（不分文理物历，只有「综合」）
_COMPREHENSIVE = {'北京', '天津', '上海', '浙江', '山东', '海南'}


def _resolve_subject_type(province: str, year: int, subject_type: str) -> list:
    """
    根据省份和年份，返回应查询的科类列表。
    例如：安徽 2024 物理类 → ['物理类']，但查 2023 年数据时 → ['理科']
    """
    reform_year = _NEW_GAOKAO_YEAR.get(province)
    if province in _COMPREHENSIVE:
        return ['综合']
    if not reform_year:
        # 未改革省份，理科/文科
        if subject_type in ('物理类', '理科'):
            return ['理科']
        return ['文科']

    if year >= reform_year:
        # 改革后
        if subject_type in ('物理类', '理科'):
            return ['物理类']
        return ['历史类']
    else:
        # 改革前
        if subject_type in ('物理类', '理科'):
            return ['理科']
        return ['文科']


def _fmt_rows(rows, max_rows=30):
    """将 sqlite3.Row 列表格式化为 Markdown 表格"""
    if not rows:
        return "（无数据）"
    cols = rows[0].keys()
    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join(["---"] * len(cols)) + " |"]
    for r in rows[:max_rows]:
        lines.append("| " + " | ".join(str(r[c]) if r[c] is not None else "" for c in cols) + " |")
    if len(rows) > max_rows:
        lines.append(f"\n…共 {len(rows)} 条，仅显示前 {max_rows} 条")
    return "\n".join(lines)


# ── 1. 分数位次查询 ──────────────────────────────────────────

@skill(
    name="gaokao_score_rank",
    description="高考分数→全省位次 查询。输入省份、年份、科类和分数，返回该分数对应的全省排名位次。",
    guest_ok=True,
    params={
        "province": {
            "type": "string",
            "description": "省份，如 安徽、河南、青海",
            "default": "安徽"
        },
        "year": {
            "type": "integer",
            "description": "高考年份，如 2024",
            "default": 2024
        },
        "subject_type": {
            "type": "string",
            "description": "科类：物理类/历史类（新高考）或 理科/文科（旧高考）",
            "default": "物理类"
        },
        "score": {
            "type": "integer",
            "description": "高考分数"
        }
    },
    tags=["gaokao", "data_analysis"]
)
def gaokao_score_rank(score: int, province: str = "安徽",
                      year: int = 2024, subject_type: str = "物理类") -> str:
    conn = _get_db()
    try:
        # 精确查
        row = conn.execute("""
            SELECT score, count_at, rank_cumul
            FROM score_rank
            WHERE province = ? AND year = ? AND subject_type = ? AND score = ?
        """, (province, year, subject_type, score)).fetchone()

        if row:
            result = (f"📊 **{province} {year}年 {subject_type}**\n"
                      f"- 分数: **{row['score']}**\n"
                      f"- 同分人数: {row['count_at']}\n"
                      f"- 累计位次: **{row['rank_cumul']}**")
        else:
            # 找最近的分数
            nearby = conn.execute("""
                SELECT score, count_at, rank_cumul
                FROM score_rank
                WHERE province = ? AND year = ? AND subject_type = ?
                  AND score BETWEEN ? AND ?
                ORDER BY ABS(score - ?) LIMIT 5
            """, (province, year, subject_type, score - 10, score + 10, score)).fetchall()
            if nearby:
                result = f"⚠️ 未找到 {score} 分的精确数据，附近分数段：\n\n"
                result += _fmt_rows(nearby)
            else:
                # 列出可用年份和科类
                avail = conn.execute("""
                    SELECT DISTINCT year, subject_type
                    FROM score_rank WHERE province = ?
                    ORDER BY year DESC, subject_type
                """, (province,)).fetchall()
                result = f"❌ 无 {province} {year}年 {subject_type} 的一分一段数据。\n\n可用数据：\n"
                result += _fmt_rows(avail)
        return result
    finally:
        conn.close()


# ── 2. 院校录取分数线查询 ────────────────────────────────────

@skill(
    name="gaokao_school_admission",
    description="查询某院校的历年录取分数线（最低分、最低位次）。支持模糊搜索校名。",
    guest_ok=True,
    params={
        "school_name": {
            "type": "string",
            "description": "院校名称（支持模糊匹配），如 清华、合肥工业"
        },
        "from_province": {
            "type": "string",
            "description": "考生所在省份",
            "default": "安徽"
        },
        "subject_type": {
            "type": "string",
            "description": "科类，如 物理类、历史类、理科、文科",
            "default": "物理类"
        },
        "year": {
            "type": "integer",
            "description": "指定年份，不传则返回所有年份",
            "default": 0
        }
    },
    tags=["gaokao", "data_analysis"]
)
def gaokao_school_admission(school_name: str, from_province: str = "安徽",
                            subject_type: str = "物理类", year: int = 0) -> str:
    conn = _get_db()
    try:
        params = [f"%{school_name}%", from_province, subject_type]
        year_clause = ""
        if year > 0:
            year_clause = "AND a.year = ?"
            params.append(year)

        rows = conn.execute(f"""
            SELECT a.year, a.school_name, a.batch, a.major_group,
                   a.subject_req, a.min_score, a.min_rank, a.control_line
            FROM admission_lines a
            WHERE a.school_name LIKE ?
              AND a.from_province = ?
              AND a.subject_type = ?
              {year_clause}
            ORDER BY a.school_name, a.year DESC, a.batch, a.min_score DESC
            LIMIT 50
        """, params).fetchall()

        if not rows:
            # 尝试查 schools 表给建议
            suggest = conn.execute("""
                SELECT school_name, province, level
                FROM schools WHERE school_name LIKE ? LIMIT 10
            """, (f"%{school_name}%",)).fetchall()
            if suggest:
                return f"未找到「{school_name}」在 {from_province} {subject_type} 的录取数据。\n\n匹配到的院校：\n" + _fmt_rows(suggest)
            return f"❌ 未找到包含「{school_name}」的院校"

        header = f"🏫 **{rows[0]['school_name']}** — {from_province} {subject_type} 录取线\n\n"
        return header + _fmt_rows(rows)
    finally:
        conn.close()


# ── 3. 专业录取数据查询 ──────────────────────────────────────

@skill(
    name="gaokao_major_admission",
    description="查询某院校某专业的历年录取分数和位次。支持模糊搜索院校名和专业名。",
    guest_ok=True,
    params={
        "school_name": {
            "type": "string",
            "description": "院校名称（模糊匹配）"
        },
        "major_name": {
            "type": "string",
            "description": "专业名称（模糊匹配），如 计算机、电气工程",
            "default": ""
        },
        "from_province": {
            "type": "string",
            "description": "考生省份",
            "default": "安徽"
        },
        "subject_type": {
            "type": "string",
            "description": "科类",
            "default": "物理类"
        },
        "year": {
            "type": "integer",
            "description": "指定年份，0 表示所有年份",
            "default": 0
        }
    },
    tags=["gaokao", "data_analysis"]
)
def gaokao_major_admission(school_name: str, major_name: str = "",
                           from_province: str = "安徽",
                           subject_type: str = "物理类",
                           year: int = 0) -> str:
    conn = _get_db()
    try:
        params = [f"%{school_name}%", from_province, subject_type]
        clauses = ["school_name LIKE ?", "from_province = ?", "subject_type = ?"]

        if major_name:
            clauses.append("major_name LIKE ?")
            params.append(f"%{major_name}%")
        if year > 0:
            clauses.append("year = ?")
            params.append(year)

        where = " AND ".join(clauses)
        rows = conn.execute(f"""
            SELECT year, school_name, major_name, major_code,
                   subject_req, quota, min_score, min_rank, batch
            FROM major_admission
            WHERE {where}
            ORDER BY school_name, major_name, year DESC
            LIMIT 60
        """, params).fetchall()

        if not rows:
            return f"❌ 未找到「{school_name}」{'专业「' + major_name + '」' if major_name else ''} 在 {from_province} {subject_type} 的专业录取数据。"

        header = f"📋 **{rows[0]['school_name']}** 专业录取 — {from_province} {subject_type}\n\n"
        return header + _fmt_rows(rows)
    finally:
        conn.close()


# ── 4. 志愿推荐（冲/稳/保） ──────────────────────────────────

@skill(
    name="gaokao_recommend",
    description="根据分数和位次推荐志愿方案（冲/稳/保三档院校）。自动换算位次并匹配历年录取数据。",
    guest_ok=True,
    params={
        "score": {
            "type": "integer",
            "description": "高考分数"
        },
        "province": {
            "type": "string",
            "description": "考生省份",
            "default": "安徽"
        },
        "year": {
            "type": "integer",
            "description": "高考年份",
            "default": 2024
        },
        "subject_type": {
            "type": "string",
            "description": "科类",
            "default": "物理类"
        },
        "target_province": {
            "type": "string",
            "description": "目标院校所在省份（留空不限）",
            "default": ""
        }
    },
    tags=["gaokao", "data_analysis"]
)
def gaokao_recommend(score: int, province: str = "安徽",
                     year: int = 2024, subject_type: str = "物理类",
                     target_province: str = "") -> str:
    conn = _get_db()
    try:
        # 1) 换算位次
        rank_row = conn.execute("""
            SELECT rank_cumul FROM score_rank
            WHERE province = ? AND year = ? AND subject_type = ? AND score = ?
        """, (province, year, subject_type, score)).fetchone()

        if not rank_row:
            return f"❌ 未找到 {province} {year}年 {subject_type} {score}分 的位次数据，无法推荐。"

        student_rank = rank_row['rank_cumul']

        # 2) 省控线
        ctrl = conn.execute("""
            SELECT score FROM score_control_lines
            WHERE province = ? AND year = ? AND subject_type = ? AND batch = '本科批'
        """, (province, year, subject_type)).fetchone()
        ctrl_score = ctrl['score'] if ctrl else None

        # 3) 查最近年份的录取数据
        ref_year = year - 1  # 用上一年数据做参考

        # 自动映射科类（如2024物理类 → 2023理科）
        ref_subjects = _resolve_subject_type(province, ref_year, subject_type)
        subj_placeholders = ','.join(['?'] * len(ref_subjects))

        prov_clause = ""
        prov_params = []
        if target_province:
            prov_clause = "AND school_province = ?"
            prov_params = [target_province]

        # 冲: 位次 * 0.5 ~ 位次 * 0.85 (高于自己能力的)
        rush_rank_lo = max(1, int(student_rank * 0.5))
        rush_rank_hi = int(student_rank * 0.85)

        # 稳: 位次 * 0.85 ~ 位次 * 1.2
        stable_rank_lo = int(student_rank * 0.85)
        stable_rank_hi = int(student_rank * 1.2)

        # 保: 位次 * 1.2 ~ 位次 * 2.0
        safe_rank_lo = int(student_rank * 1.2)
        safe_rank_hi = int(student_rank * 2.0)

        def query_tier(rank_lo, rank_hi, limit=10):
            return conn.execute(f"""
                SELECT school_name, batch, min_score, min_rank,
                       is_985, is_211, is_dyl
                FROM admission_lines
                WHERE from_province = ? AND year = ?
                  AND subject_type IN ({subj_placeholders})
                  AND min_rank BETWEEN ? AND ?
                  AND min_rank > 0
                  {prov_clause}
                GROUP BY school_name
                HAVING min_rank = MIN(min_rank)
                ORDER BY min_rank ASC
                LIMIT ?
            """, [province, ref_year] + ref_subjects + [rank_lo, rank_hi] + prov_params + [limit]).fetchall()

        rush = query_tier(rush_rank_lo, rush_rank_hi)
        stable = query_tier(stable_rank_lo, stable_rank_hi)
        safe = query_tier(safe_rank_lo, safe_rank_hi)

        lines = [
            f"# 🎯 志愿推荐方案",
            f"**{province} {year}年 {subject_type}** | 分数 **{score}** | 位次 **{student_rank}**"
        ]
        if ctrl_score:
            diff = score - ctrl_score
            lines.append(f"本科线 {ctrl_score} | {'超线 ' + str(diff) + ' 分' if diff >= 0 else '低于线 ' + str(-diff) + ' 分'}")

        lines.append(f"\n参考数据年份: {ref_year}\n")

        def fmt_tier(name, emoji, rows):
            if not rows:
                return f"### {emoji} {name}\n（暂无匹配院校）\n"
            s = f"### {emoji} {name}\n\n"
            s += "| 院校 | 最低分 | 最低位次 | 985 | 211 | 双一流 |\n"
            s += "| --- | --- | --- | --- | --- | --- |\n"
            for r in rows:
                tags = []
                if r['is_985']: tags.append("985")
                if r['is_211']: tags.append("211")
                if r['is_dyl']: tags.append("双一流")
                s += f"| {r['school_name']} | {r['min_score']} | {r['min_rank']} | {'✓' if r['is_985'] else ''} | {'✓' if r['is_211'] else ''} | {'✓' if r['is_dyl'] else ''} |\n"
            return s

        lines.append(fmt_tier("冲一冲（有风险但有机会）", "🚀", rush))
        lines.append(fmt_tier("稳一稳（录取概率较高）", "✅", stable))
        lines.append(fmt_tier("保一保（基本稳妥）", "🛡️", safe))

        lines.append("\n> ⚠️ 以上推荐基于历年数据，仅供参考。实际录取受当年报考人数、招生计划变化等因素影响。")

        return "\n".join(lines)
    finally:
        conn.close()


# ── 5. 院校信息查询 ──────────────────────────────────────────

@skill(
    name="gaokao_school_info",
    description="查询院校基本信息（985/211/双一流、所在省份、办学性质等）。支持模糊搜索。",
    guest_ok=True,
    params={
        "keyword": {
            "type": "string",
            "description": "院校名称关键词（模糊匹配），如 清华、合工大"
        }
    },
    tags=["gaokao", "data_analysis"]
)
def gaokao_school_info(keyword: str) -> str:
    conn = _get_db()
    try:
        rows = conn.execute("""
            SELECT school_name, province, authority, level, nature,
                   is_985, is_211, is_dyl
            FROM schools
            WHERE school_name LIKE ?
            ORDER BY is_985 DESC, is_211 DESC, is_dyl DESC, school_name
            LIMIT 20
        """, (f"%{keyword}%",)).fetchall()

        if not rows:
            return f"❌ 未找到包含「{keyword}」的院校"

        lines = [f"🏫 搜索「{keyword}」匹配到 {len(rows)} 所院校：\n"]
        for r in rows:
            tags = []
            if r['is_985']: tags.append("🏆985")
            if r['is_211']: tags.append("⭐211")
            if r['is_dyl']: tags.append("🌟双一流")
            tag_str = " ".join(tags) if tags else ""
            lines.append(
                f"- **{r['school_name']}** ({r['province']}) "
                f"| {r['nature'] or '公办'} | {r['level'] or ''} "
                f"| {r['authority'] or ''} {tag_str}"
            )
        return "\n".join(lines)
    finally:
        conn.close()


# ── 6. SQL 自由查询（高级） ──────────────────────────────────

@skill(
    name="gaokao_sql",
    description="直接执行 SQL 查询高考数据库（只读）。表: admission_lines, score_rank, score_control_lines, major_admission, major_plans, schools。用于复杂的自定义查询。",
    guest_ok=True,
    params={
        "sql": {
            "type": "string",
            "description": "SQL SELECT 语句（仅允许 SELECT）"
        }
    },
    tags=["gaokao", "data_analysis"]
)
def gaokao_sql(sql: str) -> str:
    # 安全检查
    normalized = sql.strip().upper()
    if not normalized.startswith("SELECT"):
        return "❌ 仅允许 SELECT 查询"

    dangerous = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                 "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX"]
    for kw in dangerous:
        if kw in normalized:
            return f"❌ 不允许使用 {kw} 语句"

    conn = _get_db()
    try:
        rows = conn.execute(sql).fetchall()
        if not rows:
            return "查询结果为空"
        return _fmt_rows(rows, max_rows=50)
    except Exception as e:
        return f"❌ SQL 执行错误: {e}"
    finally:
        conn.close()


# ── 7. 数据概况 ──────────────────────────────────────────────

@skill(
    name="gaokao_stats",
    description="查看高考数据库的数据概况：各表记录数、覆盖省份和年份。",
    guest_ok=True,
    params={},
    tags=["gaokao", "data_analysis"]
)
def gaokao_stats() -> str:
    conn = _get_db()
    try:
        counts = {}
        for table in ["admission_lines", "score_rank", "score_control_lines",
                      "major_admission", "major_plans", "schools"]:
            row = conn.execute(f"SELECT count(*) as c FROM {table}").fetchone()
            counts[table] = row['c']

        provinces = conn.execute("""
            SELECT DISTINCT from_province FROM admission_lines ORDER BY from_province
        """).fetchall()
        prov_list = [r['from_province'] for r in provinces]

        years = conn.execute("""
            SELECT DISTINCT year FROM admission_lines ORDER BY year DESC
        """).fetchall()
        year_list = [str(r['year']) for r in years]

        rank_years = conn.execute("""
            SELECT DISTINCT province, year, subject_type
            FROM score_rank ORDER BY province, year DESC
        """).fetchall()

        lines = [
            "# 📊 高考数据库概况\n",
            "## 数据量",
            f"| 表 | 记录数 |",
            f"| --- | --- |",
        ]
        for t, c in counts.items():
            lines.append(f"| {t} | {c:,} |")

        lines.append(f"\n## 录取数据覆盖")
        lines.append(f"- **省份**: {', '.join(prov_list)}")
        lines.append(f"- **年份**: {', '.join(year_list)}")

        lines.append(f"\n## 一分一段数据覆盖")
        for r in rank_years[:20]:
            lines.append(f"- {r['province']} {r['year']} {r['subject_type']}")
        if len(rank_years) > 20:
            lines.append(f"- …共 {len(rank_years)} 个组合")

        return "\n".join(lines)
    finally:
        conn.close()
