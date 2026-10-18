"""
高考志愿填报数据查询业务服务 (features.content_ops.gaokao_service)。
从 skills/ops_gaokao.py 迁入 SQLite 只读连接、位次折算、录取线、专业录取与冲稳保推荐算法。
"""
import os
import sqlite3
from typing import Optional, List, Dict, Any

DB_PATH = "/opt/volunteer-web/docker-data/gaokao.db"


def get_db():
    """获取只读数据库连接"""
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"数据库不存在: {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


_NEW_GAOKAO_YEAR = {
    '安徽': 2024, '江西': 2024, '广西': 2024, '贵州': 2024, '甘肃': 2024,
    '黑龙江': 2024, '吉林': 2024,
    '河北': 2021, '辽宁': 2021, '江苏': 2021, '福建': 2021,
    '湖北': 2021, '湖南': 2021, '广东': 2021, '重庆': 2021,
    '北京': 2020, '天津': 2020, '山东': 2020, '海南': 2020,
    '浙江': 2017, '上海': 2017,
}

_COMPREHENSIVE = {'北京', '天津', '上海', '浙江', '山东', '海南'}


def resolve_subject_type(province: str, year: int, subject_type: str) -> list:
    reform_year = _NEW_GAOKAO_YEAR.get(province)
    if province in _COMPREHENSIVE:
        return ['综合']
    if not reform_year:
        if subject_type in ('物理类', '理科'):
            return ['理科']
        return ['文科']

    if year >= reform_year:
        if subject_type in ('物理类', '理科'):
            return ['物理类']
        return ['历史类']
    else:
        if subject_type in ('物理类', '理科'):
            return ['理科']
        return ['文科']


def fmt_rows(rows, max_rows=30) -> str:
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


def gaokao_score_rank(score: int, province: str = "安徽", year: int = 2024, subject_type: str = "物理类") -> str:
    conn = get_db()
    try:
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
            nearby = conn.execute("""
                SELECT score, count_at, rank_cumul
                FROM score_rank
                WHERE province = ? AND year = ? AND subject_type = ?
                  AND score BETWEEN ? AND ?
                ORDER BY ABS(score - ?) LIMIT 5
            """, (province, year, subject_type, score - 10, score + 10, score)).fetchall()
            if nearby:
                result = f"⚠️ 未找到 {score} 分的精确数据，附近分数段：\n\n" + fmt_rows(nearby)
            else:
                avail = conn.execute("""
                    SELECT DISTINCT year, subject_type
                    FROM score_rank WHERE province = ?
                    ORDER BY year DESC, subject_type
                """, (province,)).fetchall()
                result = f"❌ 无 {province} {year}年 {subject_type} 的一分一段数据。\n\n可用数据：\n" + fmt_rows(avail)
        return result
    finally:
        conn.close()


def gaokao_school_admission(school_name: str, from_province: str = "安徽", subject_type: str = "物理类", year: int = 0) -> str:
    conn = get_db()
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
            suggest = conn.execute("""
                SELECT school_name, province, level
                FROM schools WHERE school_name LIKE ? LIMIT 10
            """, (f"%{school_name}%",)).fetchall()
            if suggest:
                return f"未找到「{school_name}」在 {from_province} {subject_type} 的录取数据。\n\n匹配到的院校：\n" + fmt_rows(suggest)
            return f"❌ 未找到包含「{school_name}」的院校"

        header = f"🏫 **{rows[0]['school_name']}** — {from_province} {subject_type} 录取线\n\n"
        return header + fmt_rows(rows)
    finally:
        conn.close()


def gaokao_major_admission(school_name: str, major_name: str = "", from_province: str = "安徽", subject_type: str = "物理类", year: int = 0) -> str:
    conn = get_db()
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
        return header + fmt_rows(rows)
    finally:
        conn.close()


def gaokao_recommend(score: int, province: str = "安徽", year: int = 2024, subject_type: str = "物理类", target_province: str = "") -> str:
    conn = get_db()
    try:
        rank_row = conn.execute("""
            SELECT rank_cumul FROM score_rank
            WHERE province = ? AND year = ? AND subject_type = ? AND score = ?
        """, (province, year, subject_type, score)).fetchone()

        if not rank_row:
            return f"❌ 未找到 {province} {year}年 {subject_type} {score}分 的位次数据，无法推荐。"

        student_rank = rank_row['rank_cumul']

        ctrl = conn.execute("""
            SELECT score FROM score_control_lines
            WHERE province = ? AND year = ? AND subject_type = ? AND batch = '本科批'
        """, (province, year, subject_type)).fetchone()
        ctrl_score = ctrl['score'] if ctrl else None

        ref_year = year - 1
        ref_subjects = resolve_subject_type(province, ref_year, subject_type)
        subj_placeholders = ','.join(['?'] * len(ref_subjects))

        prov_clause = ""
        prov_params = []
        if target_province:
            prov_clause = "AND school_province = ?"
            prov_params = [target_province]

        rush_rank_lo = max(1, int(student_rank * 0.5))
        rush_rank_hi = int(student_rank * 0.85)
        stable_rank_lo = int(student_rank * 0.85)
        stable_rank_hi = int(student_rank * 1.2)
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
                s += f"| {r['school_name']} | {r['min_score']} | {r['min_rank']} | {'✓' if r['is_985'] else ''} | {'✓' if r['is_211'] else ''} | {'✓' if r['is_dyl'] else ''} |\n"
            return s

        lines.append(fmt_tier("冲一冲（有风险但有机会）", "🚀", rush))
        lines.append(fmt_tier("稳一稳（录取概率较高）", "✅", stable))
        lines.append(fmt_tier("保一保（基本稳妥）", "🛡️", safe))
        lines.append("\n> ⚠️ 以上推荐基于历年数据，仅供参考。实际录取受当年报考人数、招生计划变化等因素影响。")

        return "\n".join(lines)
    finally:
        conn.close()


def gaokao_school_info(keyword: str) -> str:
    conn = get_db()
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


def gaokao_sql(sql: str) -> str:
    normalized = sql.strip().upper()
    if not normalized.startswith("SELECT"):
        return "❌ 仅允许 SELECT 查询"

    dangerous = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                 "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX"]
    for kw in dangerous:
        if kw in normalized:
            return f"❌ 不允许使用 {kw} 语句"

    conn = get_db()
    try:
        rows = conn.execute(sql).fetchall()
        if not rows:
            return "查询结果为空"
        return fmt_rows(rows, max_rows=50)
    except Exception as e:
        return f"❌ SQL 执行错误: {e}"
    finally:
        conn.close()


def gaokao_stats() -> str:
    conn = get_db()
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
