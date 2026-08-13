"""模型目录脱敏快照（只读）。

轻量重构后，模型配置由 conf.d/*.json 手动维护，启动时由
core.model_config.validate_model_config 强校验。本模块不再提供运行时写配置
能力，仅保留脱敏后的只读目录与路由快照，供 dashboard 展示或脚本巡检。
"""

from __future__ import annotations

from urllib.parse import urlparse

from core.model_config import DRIVER_LABELS, validate_model_config


def _provider_label(driver: str) -> str:
    return DRIVER_LABELS.get(driver, driver or "openai-compatible")


def _safe_endpoint(model_cfg: dict) -> str:
    base_url = model_cfg.get("base_url") or ""
    if not base_url:
        return "SDK managed"
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "configured"


def build_model_snapshot(config: dict) -> dict:
    """返回脱敏后的模型目录、全局角色、路由与配置问题。不含 API Key 明文。"""
    llm_cfg = config.get("llm", {}) or {}
    models_cfg = llm_cfg.get("models", {}) or {}
    routing = config.get("task_routing", {}) or {}

    routes = []
    for raw in (routing.get("route_rules") or []):
        if not isinstance(raw, dict):
            continue
        routes.append({
            "type": raw.get("type", ""),
            "model": raw.get("model", ""),
            "fallback": raw.get("fallback", ""),
            "allowed_models": list(raw.get("allowed_models") or []),
            "tools": list(raw.get("tools") or []),
        })

    models = []
    for name, cfg in models_cfg.items():
        if not isinstance(cfg, dict):
            continue
        driver = cfg.get("driver", "")
        models.append({
            "name": name,
            "model": cfg.get("model", name),
            "provider": _provider_label(driver),
            "driver": driver,
            "endpoint": _safe_endpoint(cfg),
            "tags": list(cfg.get("tags") or []),
            "max_tokens": cfg.get("max_tokens"),
            "max_steps": cfg.get("max_steps"),
            "temperature": cfg.get("temperature"),
            "configured": bool(cfg.get("api_key")),
        })

    # 校验问题复用中央强校验器，这里只取 message，保留原有 issues 展示格式。
    issues = [msg for _level, msg in validate_model_config(config)]

    return {
        "models": models,
        "defaults": {
            "default_model": llm_cfg.get("default", ""),
            "planner_model": routing.get("planner_model", llm_cfg.get("default", "")),
            "classifier_model": routing.get("classifier_model", llm_cfg.get("default", "")),
            "committee_models": list(config.get("committee", {}).get("models", []) or []),
        },
        "routes": routes,
        "issues": issues,
        "apply_scope": "模型配置由 conf.d/*.json 手动维护，修改后重启生效",
    }
