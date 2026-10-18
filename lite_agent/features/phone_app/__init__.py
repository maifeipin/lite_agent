"""
Phone App 功能域插件入口 (features.phone_app).
负责在宿主生命周期注册 REST 路由、技能以及资源清理逻辑。
遵循 PluginContext 显式注册与事务隔离规范，默认关闭，依赖显式配置启用。
"""

import os
import secrets
from typing import Any, Optional, Dict
import re
import logging
from lite_agent.features.phone_app.service import PhoneAppService
from lite_agent.features.phone_app import routes

logger = logging.getLogger("features.phone_app")
_global_service: Optional[PhoneAppService] = None


def get_phone_app_service(
    db_path: Optional[str] = None,
    router: Optional[Any] = None,
    gateway: Optional[Any] = None,
    config: Optional[Dict[str, Any]] = None,
    worker_hub: Optional[Any] = None,
    expected_token: Optional[str] = None,
    default_model: Optional[str] = None
) -> PhoneAppService:
    global _global_service
    if _global_service is None:
        _global_service = PhoneAppService(
            db_path=db_path,
            router=router,
            gateway=gateway,
            config=config,
            worker_hub=worker_hub,
            expected_token=expected_token,
            default_model=default_model
        )
    return _global_service


def register(context: Any) -> None:
    """注册 Phone App 插件到宿主上下文 (C01 & C05: 真实对齐宿主 Agent 属性与模型选择)。"""
    cfg = getattr(context, "config", {}) or {}
    phone_cfg = cfg.get("phone_app", {}) if isinstance(cfg, dict) else {}

    # 遵循规范：功能默认关闭，不修改配置默认不启动
    if not phone_cfg.get("enabled", False):
        logger.info("Phone App 功能已禁用 (config.phone_app.enabled != True)，跳过插件注册")
        return

    db_path = phone_cfg.get("db_path")
    if not db_path:
        db_cfg = cfg.get("database", {}) if isinstance(cfg, dict) else {}
        db_path = db_cfg.get("phone_app_path")

    # 凭证校验：环境变量或配置，绝不默认固定弱口令 (C01)
    worker_token = phone_cfg.get("worker_token") or os.environ.get("PHONE_WORKER_TOKEN")
    agent = getattr(context, "agent", None)
    # 宿主真实属性对齐：_request_model_router 与 llm (C05)
    router = getattr(agent, "_request_model_router", None) or getattr(agent, "router", None)
    gateway = getattr(agent, "llm", None) or getattr(agent, "gateway", None)
    default_model = (
        phone_cfg.get("model") or
        getattr(agent, "model", None) or
        getattr(router, "default_model", None) or
        (cfg.get("llm", {}).get("default") if isinstance(cfg, dict) else None)
    )

    service = get_phone_app_service(
        db_path=db_path,
        router=router,
        gateway=gateway,
        config=cfg,
        expected_token=worker_token,
        default_model=default_model
    )

    # 1. 注册 HTTP 路由到 context (支持事务跟踪与回滚)
    if hasattr(context, "register_route"):
        context.register_route(
            "GET", "/api/v1/phone-app/devices",
            lambda h, u: routes.handle_get_devices(h, u, service),
            description="获取在线受控设备列表"
        )
        context.register_route(
            "GET", "/api/v1/phone-app/apps",
            lambda h, u: routes.handle_get_apps(h, u, service),
            description="获取已验证 App 目录及能力"
        )
        context.register_route(
            "POST", "/api/v1/phone-app/batches",
            lambda h, u: routes.handle_post_batches(h, u, service),
            description="创建固定搜索或脚本批次"
        )
        context.register_route(
            "GET", "/api/v1/phone-app/batches",
            lambda h, u: routes.handle_get_batches(h, u, service),
            description="查询历史批次列表"
        )
        context.register_route(
            "GET", "/api/v1/phone-app/events",
            lambda h, u: routes.handle_get_events(h, u, service),
            description="轮询批次事件流"
        )

        # 动态批次路由分发器 (前缀匹配 /api/v1/phone-app/batches/)
        def _batch_sub_dispatcher(handler: Any, parsed_url: Any):
            path = parsed_url.path
            m = re.match(r"^/agent/api/v1/phone-app/batches/([^/]+)(?:/(generate|draft|approve|execute|cancel|reject))?$", path)
            if not m:
                m = re.match(r"^/api/v1/phone-app/batches/([^/]+)(?:/(generate|draft|approve|execute|cancel|reject))?$", path)
            if not m:
                handler.send_response(404)
                handler.end_headers()
                return

            batch_id = m.group(1)
            action = m.group(2)
            method = handler.command.upper()

            if action is None and method == "GET":
                routes.handle_get_batch_detail(handler, parsed_url, service, batch_id)
            elif action == "generate" and method == "POST":
                routes.handle_post_generate(handler, parsed_url, service, batch_id)
            elif action == "draft" and method == "PUT":
                routes.handle_put_draft(handler, parsed_url, service, batch_id)
            elif action == "approve" and method == "POST":
                routes.handle_post_approve(handler, parsed_url, service, batch_id)
            elif action == "execute" and method == "POST":
                routes.handle_post_execute(handler, parsed_url, service, batch_id)
            elif action == "cancel" and method == "POST":
                routes.handle_cancel_batch(handler, parsed_url, service, batch_id)
            elif action == "reject" and method == "POST":
                routes.handle_reject_batch(handler, parsed_url, service, batch_id)
            else:
                handler.send_response(405)
                handler.end_headers()

        context.register_route(
            "ALL", "/api/v1/phone-app/batches/",
            _batch_sub_dispatcher,
            is_prefix=True,
            description="批次详情及审批执行子端点"
        )
        context.register_route(
            "POST", "/api/v1/phone-app/worker/handshake",
            lambda h, u: routes.handle_worker_handshake(h, u, service),
            auth_required=False,
            description="Mac Worker 节点主动握手鉴权与版本锁定"
        )
        context.register_route(
            "POST", "/api/v1/phone-app/worker/heartbeat",
            lambda h, u: routes.handle_worker_heartbeat(h, u, service),
            auth_required=False,
            description="Mac Worker 心跳与设备状态同步"
        )
        context.register_route(
            "POST", "/api/v1/phone-app/worker/poll_task",
            lambda h, u: routes.handle_worker_poll_task(h, u, service),
            auth_required=False,
            description="Mac Worker 任务租约领取"
        )
        context.register_route(
            "POST", "/api/v1/phone-app/worker/submit_receipt",
            lambda h, u: routes.handle_worker_submit_receipt(h, u, service),
            auth_required=False,
            description="Mac Worker 任务执行回执上报"
        )
        context.register_route(
            "POST", "/api/v1/phone-app/device/resume",
            lambda h, u: routes.handle_resume_device(h, u, service),
            description="人工恢复处于暂停状态的手机"
        )

    # 2. 注册 Skills 技能
    if hasattr(context, "register_skill"):
        try:
            from lite_agent.skills import ops_phone_app
            from lite_agent.core.skill_engine import _skill_registry

            for skill_name in ["mobile_product_search", "mobile_task_status"]:
                if skill_name in _skill_registry:
                    context.register_skill(skill_name, _skill_registry[skill_name])
        except Exception as e:
            logger.warning(f"Phone App 技能注册失败: {e}")

    # 3. 注册清理钩子 (关闭数据库、释放互斥锁资源)
    if hasattr(context, "record_cleanup"):
        context.record_cleanup(lambda: service.close(), name="phone_app_service_cleanup")
    elif hasattr(context, "register_cleanup"):
        context.register_cleanup("phone_app_service_cleanup", lambda: service.close())
