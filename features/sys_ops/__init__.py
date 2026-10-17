"""
Features Sys Ops 系统运维与健康巡检模块 (features.sys_ops)。
提供系统体检、备份、日志与安全巡检能力。
实现受控插件 register(context) 契约。
"""

from typing import Any
from contracts.message import AgentResponse

SECURITY_DOMAIN_SPEC = {
    "pattern": r"(巡检|安全扫描|入侵|爆破|基线|漂移|审计)",
    "default_tools": [
        "sentinel_scan", "sentinel_findings", "ops_security_audit",
    ],
    "explicit_intent_tools": [
        "sentinel_baseline",
    ],
    "explicit_patterns": r"(刷新基线|接受基线)",
}

BACKUP_DOMAIN_SPEC = {
    "pattern": r"(备份|网盘|百度网盘|bypy)",
    "default_tools": [
        "bypy_info",
    ],
    "explicit_intent_tools": [
        "bypy_mkdir", "bypy_syncup", "bypy_syncdown",
        "ops_backup_data", "ops_backup_cloud",
    ],
    "explicit_patterns": r"(同步|备份|创建目录|上传|下载)",
}

WORKSPACE_DOMAIN_SPEC = {
    "pattern": r"(工作区|沙箱|执行代码|运行python)",
    "default_tools": [],
    "explicit_intent_tools": [
        "ops_workspace_run",
    ],
    "explicit_patterns": r"(执行|运行|写入)",
}



def _handle_check(agent: Any, msg: Any, args: list) -> AgentResponse:
    try:
        from features.sys_ops.health_service import get_health_report
        return AgentResponse(get_health_report(), title="🏥 健康检查", color="green")
    except Exception as e:
        return AgentResponse(f"自检失败: {e}", title="⚠️", color="red")


def register(context: Any) -> None:
    """注册 Sys Ops 插件到宿主上下文。"""
    # 1. 注册运维 Slash 指令
    context.register_command(
        "/check",
        _handle_check,
        category="系统",
        description="运行系统全面健康自检",
        admin_only=True,
    )
    from features.sys_ops.llm_ops_service import check_deepseek_balance
    context.register_command(
        "/balance",
        lambda agent, msg, args: check_deepseek_balance(),
        category="系统",
        description="查询 DeepSeek 账户余额",
        show_in_dashboard=False,
        guest_ok=False,
    )

    # 2. 注册运维与安全技能
    from skills import (
        ops_backup, ops_bypy, ops_crontab, ops_logs, ops_sys,
        ops_security, ops_self_check, ops_sentinel, ops_workspace,
        ops_llm
    )
    from core.skill_engine import _skill_registry

    sys_tools = [
        "ops_backup_data", "ops_backup_cloud", "bypy_info", "bypy_mkdir",
        "bypy_syncup", "bypy_syncdown", "ops_list_crontab", "ops_run_script",
        "ops_read_logs", "ops_read_journal", "ops_sys_status", "ops_security_audit",
        "ops_self_check", "sentinel_scan", "sentinel_baseline", "sentinel_findings",
        "ops_workspace_run", "llm_check_balance", "llm_usage_report"
    ]
    for t_name in sys_tools:
        if t_name in _skill_registry:
            context.register_skill(t_name, _skill_registry[t_name])


    # 3. 注册定时作业 (备份与云同步)
    from features.sys_ops import backup_service
    context.register_cron("ops_backup_cloud", "03:00", backup_service.do_backup_and_sync)


    # 4. 注册专用作业处理器
    from features.sys_ops import health_service
    from features.sys_ops.security_service import sentinel_scan

    def _daily_health_handler(**kwargs):
        try:
            report_text = health_service.get_health_report()
            response = AgentResponse(report_text, title="🌙 每日系统体检报告", color="wathet")
            if context.send_owner_response(response):
                return "巡检广播已推送"
            return "巡检广播失败：无可用通道或未取得发送回执"
        except Exception as e:
            return f"巡检广播失败: {e}"

    if hasattr(context, "register_job_handler"):
        context.register_job_handler("ops_self_check::daily_health", _daily_health_handler)
        def _sentinel_scan_handler(**kwargs):
            text = sentinel_scan(**kwargs)
            if "⚠️" in text or "[HIGH]" in text or "High:" in text:
                context.send_owner_response(AgentResponse(
                    text, title="🛡️ Sentinel 安全告警", color="red"
                ))
            return text

        context.register_job_handler("ops_sentinel::sentinel_scan", _sentinel_scan_handler)

    # 5. 注册运维与安全 RequestSelector 领域路由
    if hasattr(context, "register_domain"):
        from core.request_selector import CORE_DOMAIN_MAP
        for name, spec in CORE_DOMAIN_MAP.items():
            context.register_domain(name, spec)
        context.register_domain("security", SECURITY_DOMAIN_SPEC)
        context.register_domain("backup", BACKUP_DOMAIN_SPEC)
        context.register_domain("workspace", WORKSPACE_DOMAIN_SPEC)


__all__ = ["register", "SECURITY_DOMAIN_SPEC", "BACKUP_DOMAIN_SPEC", "WORKSPACE_DOMAIN_SPEC"]
