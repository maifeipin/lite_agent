"""
高考志愿填报数据查询技能 (ops_gaokao) - 薄入口。
业务与 SQLite 算法委托 features.content_ops.gaokao_service。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.features.content_ops import gaokao_service


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
    return gaokao_service.gaokao_score_rank(score=score, province=province, year=year, subject_type=subject_type)


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
    return gaokao_service.gaokao_school_admission(school_name=school_name, from_province=from_province, subject_type=subject_type, year=year)


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
    return gaokao_service.gaokao_major_admission(school_name=school_name, major_name=major_name, from_province=from_province, subject_type=subject_type, year=year)


@skill(
    name="gaokao_recommend",
    description="根据分数和位次推荐志愿方案（冲/稳/保三档院校）。自动换算位次并匹配历年录取数据。",
    guest_ok=True,
    guard_keywords=["高考", "位次", "排位", "录取", "志愿", "分数线", "投档", "冲稳保", "物理类", "历史类", "文科", "理科"],
    guard_prompt="用户当前请求涉及高考录取与志愿推荐数据。你必须调用相应的高考数据库查询工具获取真实数据，绝对禁止仅凭你的预训练记忆编造院校录取分数、位次、招生计划或录取政策。回复中的所有学校和分数必须完全来自于工具返回的真实结果，严禁捏造或添加未查询到的虚假院校或数据。查不到数据时请如实回答“未查询到相关数据”。",
    guard_threshold=2,
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
    return gaokao_service.gaokao_recommend(score=score, province=province, year=year, subject_type=subject_type, target_province=target_province)


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
    return gaokao_service.gaokao_school_info(keyword=keyword)


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
    return gaokao_service.gaokao_sql(sql=sql)


@skill(
    name="gaokao_stats",
    description="查看高考数据库的数据概况：各表记录数、覆盖省份和年份。",
    guest_ok=True,
    params={},
    tags=["gaokao", "data_analysis"]
)
def gaokao_stats() -> str:
    return gaokao_service.gaokao_stats()
