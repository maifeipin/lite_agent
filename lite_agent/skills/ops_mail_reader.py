import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lite_agent.core.skill_engine import skill
from lite_agent.core.command_registry import slash_command
from lite_agent.features.mail_ops import reader_service

# 兼容符号与辅助函数导出
_build_llm_pool_env = reader_service.build_llm_pool_env
_run_mail_reader_cmd = reader_service.run_mail_reader_cmd
_format_headers_table = reader_service.format_headers_table
_format_missed_list = reader_service.format_missed_list
_handle_large_content = reader_service.handle_large_content
_parse_email_datetime = reader_service.parse_email_datetime
_parse_high_importance = reader_service.parse_high_importance

@skill(
    name='mail_fetch_summaries',
    description='批量抓取最近几月的邮件，识别账单自动入库，并将通用邮件利用大模型生成分类和智能摘要。比较耗时，请耐心等待。',
    params={
        'months': {
            'type': 'integer',
            'description': '回溯抓取的月数，默认 1',
            'default': 1
        }
    }
)
def mail_fetch_summaries(months: int = 1) -> str:
    return reader_service.mail_fetch_summaries(months=months)


def mail_fetch_cron(agent=None, months: int = 1) -> str:
    return reader_service.mail_fetch_cron(agent=agent, months=months)


slash_command('/mail_fetch', category='邮件管理',
              description='同步拉取邮件 + LLM评分 + 高优推送',
              show_in_dashboard=True, guest_ok=False)(
    lambda agent, msg, args: mail_fetch_cron(agent=agent))


@skill(
    name='mail_fetch_only',
    description='幂等拉取最近邮件正文入库(不调LLM)，新邮件标 status=pending 留待 enrich。秒级，适合频繁定时同步。',
    params={
        'months': {
            'type': 'integer',
            'description': '回溯抓取的月数，默认 1',
            'default': 1
        }
    }
)
def mail_fetch_only(months: int = 1) -> str:
    return reader_service.mail_fetch_only(months=months)


slash_command('/mail_fetch_only', category='邮件管理',
              description='幂等拉取最近邮件入库 (不调 LLM)',
              show_in_dashboard=True, guest_ok=False)(
    lambda agent, msg, args: mail_fetch_only(int(args[0]) if args else 1))


@skill(
    name='mail_llm_enrich',
    description='对 status=pending 的邮件做 LLM 摘要提炼(限量+429退避+端点池轮换)，高优邮件秒级推送。',
    params={
        'limit': {
            'type': 'integer',
            'description': '单次处理上限，默认 30',
            'default': 30
        }
    }
)
def mail_llm_enrich(limit: int = 30, agent=None) -> str:
    return reader_service.mail_llm_enrich(limit=limit, agent=agent)


slash_command('/mail_enrich', category='邮件管理',
              description='对未处理邮件进行 AI 摘要、分类与提取',
              show_in_dashboard=True, guest_ok=False)(
    lambda agent, msg, args: mail_llm_enrich(limit=int(args[0]) if args else 30, agent=agent))


def mail_feedback_ok(summary_id: int) -> str:
    return reader_service.mail_feedback_ok(summary_id=summary_id)


slash_command('/mail_ok', category='邮件管理',
              description='标记邮件已处理 (processed)',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: mail_feedback_ok(int(args[0]) if args else 0))


def mail_feedback_noise(summary_id: int) -> str:
    return reader_service.mail_feedback_noise(summary_id=summary_id)


slash_command('/mail_noise', category='邮件管理',
              description='将邮件标记为噪声并学习降噪规则',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: mail_feedback_noise(int(args[0]) if args else 0))


def mail_delete_noise_rule(arg_val: str) -> str:
    return reader_service.mail_delete_noise_rule(arg_val=arg_val)


slash_command('/mail_unnoise', category='邮件管理',
              description='删除降噪规则 (按 ID 反查或直接指定)',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: mail_delete_noise_rule(args[0]) if args else "❌ 用法：/mail_unnoise <ID或规则值>")


def mail_list_noise_rules() -> str:
    return reader_service.mail_list_noise_rules()


slash_command('/mail_noiselist', category='邮件管理',
              description='查看当前自定义降噪规则列表',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: mail_list_noise_rules())


@skill(
    name='mail_headers',
    description='查询最近的邮件标题列表',
    params={
        'limit': {
            'type': 'integer',
            'description': '显示最近的邮件封数，默认 15',
            'default': 15
        }
    }
)
def mail_headers(limit: int = 15) -> str:
    return reader_service.mail_headers(limit=limit)


slash_command('/mail_headers', category='邮件管理',
              description='查看最近邮件标题列表',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: mail_headers(limit=int(args[0]) if args else 15))


@skill(
    name='mail_missed',
    description='查询最近 7 天可能错过的非高优邮件列表',
    params={
        'limit': {
            'type': 'integer',
            'description': '最大显示封数，默认 15',
            'default': 15
        }
    }
)
def mail_missed(limit: int = 15) -> str:
    return reader_service.mail_missed(limit=limit)


slash_command('/mail_missed', category='邮件管理',
              description='查看最近7天可能错过的非高优邮件',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: mail_missed(limit=int(args[0]) if args else 15))


@skill(
    name='mail_view_original',
    description='查看某封邮件的原文并自动生成 HedgeDoc 链接',
    params={
        'account': {
            'type': 'string',
            'description': '邮箱账号'
        },
        'uid': {
            'type': 'string',
            'description': '邮件 UID'
        }
    }
)
def mail_view_original(account: str, uid: str) -> str:
    return reader_service.mail_view_original(account=account, uid=uid)


slash_command('/mail_view', category='邮件管理',
              description='查看邮件原文 (HedgeDoc)',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: mail_view_original(account=args[0], uid=args[1]) if len(args) >= 2 else "❌ 用法：/mail_view <账户> <UID>")


@skill(
    name='backfill_bodies',
    description='回填缺失的早期账单原文正文 (修复搜索查不到历史数据的问题)。'
)
def backfill_bodies() -> str:
    return reader_service.backfill_bodies()


slash_command('/mail_backfill', category='邮件管理',
              description='回填缺失的早期账单原文 (修复搜索查不到的问题)',
              show_in_dashboard=True, guest_ok=False)(
    lambda agent, msg, args: backfill_bodies())
