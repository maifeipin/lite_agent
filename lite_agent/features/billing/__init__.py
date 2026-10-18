"""
Features Billing 业务模块 (features.billing)。
提供账单确定性工作流、报表生成、临期与核对逻辑。
实现受控插件 register(context) 契约。
"""

from typing import Any
from lite_agent.features.billing.workflow import BillingWorkflow
from lite_agent.contracts.message import AgentResponse

BILLING_DOMAIN_SPEC = {
    "pattern": r"([账帐]单|外币|原币|汇率|bill|invoice|还款|信用卡|交易|流水|对账|大额)",
    "default_tools": [
        "billing_report", "billing_due_soon", "billing_unpaid",
        "billing_reconcile", "billing_recent",
        "billing_snapshot",
        "billing_txns_over", "billing_parse_health",
    ],
    "explicit_intent_tools": [
        "billing_mark_paid", "billing_fetch",
    ],
    "explicit_patterns": r"(标记.*已还|拉取|下载[账帐]单|同步[账帐]单|更新[账帐]单)",
}


def _billing_matcher(cfg: dict, text: str) -> bool:
    try:
        return BillingWorkflow(cfg, None, None).matches(text)
    except Exception:
        return False


def _handle_cmd(agent: Any, msg: Any, args: list) -> AgentResponse:
    from lite_agent.features.billing import service as billing_service
    if not args:
        return AgentResponse(
            "请提供具体账单指令，例如：`/cmd report 3`\n可用命令: report, due_soon_bills, reconcile, recent, fetch (结合了exec和validate)",
            title="提示",
            color="grey",
        )
    try:
        if args[0] == "fetch":
            months = int(args[1]) if len(args) > 1 else 1
            result = billing_service.fetch_and_consume(months)
        elif args[0] in ("fetch_summaries", "fs"):
            from lite_agent.features.mail_ops import reader_service
            months = int(args[1]) if len(args) > 1 else 1
            result = reader_service.mail_fetch_summaries(months=months)
        else:
            result = billing_service.run_billing_cmd(args)
        return AgentResponse(result, title=f"执行结果: {args[0]}", color="blue")
    except Exception as e:
        return AgentResponse(f"执行失败: {e}", title="错误", color="red")


def register(context: Any) -> None:
    """注册 Billing 插件到宿主上下文。"""
    from lite_agent.features.billing import service as billing_service

    # 1. 注册确定性工作流
    context.register_workflow(
        "billing",
        factory=lambda cfg, router, ledger: BillingWorkflow(cfg, router, ledger),
        matcher=_billing_matcher,
        description="账单抓取、解析与对账确定性工作流",
    )

    # 2. 注册 /cmd 运维指令
    context.register_command(
        "/cmd",
        _handle_cmd,
        category="账单",
        description="账单与抓取指令通道",
        admin_only=True,
    )

    # 3. 注册账单技能
    from lite_agent.skills import ops_billing
    from lite_agent.core.skill_engine import _skill_registry

    billing_tools = [
        "billing_report", "billing_due_soon", "billing_unpaid", "billing_mark_paid",
        "billing_reconcile", "billing_recent", "billing_snapshot", "billing_fetch",
        "billing_txns_over", "billing_parse_health"
    ]
    for t_name in billing_tools:
        if t_name in _skill_registry:
            context.register_skill(t_name, _skill_registry[t_name])

    # 4. 注册异步任务流路由
    def _handle_task_stream_route(http_handler: Any, parsed_url: Any):
        http_handler._handle_task_stream(parsed_url.query)

    context.register_route("GET", "/api/v1/task/stream", _handle_task_stream_route)

    # 5. 注册账单定时作业
    context.register_cron("billing_parse_health_tick", "09:00", billing_service.billing_health_cron)
    context.register_cron("billing_fetch_tick", "03:00", billing_service.billing_fetch_cron)

    # 6. 注册账单 RequestSelector 领域路由
    if hasattr(context, "register_domain"):
        context.register_domain("billing", BILLING_DOMAIN_SPEC)
