"""端点绑定与配置归一化契约 (Schema Version 2)。

核心概念：
1. EndpointDefinition: 协议 + 服务接入点 + 凭据引用的受信任绑定定义
2. ModelDefinition: 业务别名、实际 API 模型名、端点引用、能力与 profile
3. normalize_llm_config: 无缝兼容旧版平铺/单模型与 schema 1 配置，归一化输出标准版本化快照
"""

import copy
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from lite_agent.core.model_config import (
    DRIVER_ARK,
    DRIVER_DEEPSEEK,
    DRIVER_GEMINI,
    DRIVER_OPENAI,
    is_gemini_driver,
)


@dataclass
class EndpointDefinition:
    id: str
    provider: str                      # "deepseek", "ark", "openai", "gemini_native"
    protocol: str                      # "openai_chat_completions", "gemini_generate_content"
    base_url: str
    credential_ref: str                # 环境变量名，例如 "DEEPSEEK_API_KEY"
    management_ref: Optional[str] = None # 显式管理接口，例如 "deepseek-official-balance"
    proxy: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelDefinition:
    id: str
    endpoint_ref: str
    model: str                         # 实际 API 模型标识
    aliases: List[str] = field(default_factory=list)
    capabilities: Dict[str, bool] = field(default_factory=dict)
    max_tokens: int = 4096
    profiles: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    output_recovery: Dict[str, Any] = field(default_factory=dict)


def resolve_credential(credential_ref: str, fallback_value: str = "") -> str:
    """仅在受信任调用边界根据凭据引用名称从环境解析实际 API Key。
    坚决不把解析后的真实 Key 反写回配置字典、日志或状态。
    """
    if not credential_ref:
        return fallback_value
    # 兼容传入 "${VAR_NAME}" 形式的引用
    var_name = credential_ref
    if var_name.startswith("${") and var_name.endswith("}"):
        var_name = var_name[2:-1]
    return os.environ.get(var_name, fallback_value)


def normalize_llm_config(llm_cfg: dict) -> Tuple[Dict[str, EndpointDefinition], Dict[str, ModelDefinition], str]:
    """将 llm 配置字典归一化为 (endpoints, models, default_model)。
    同时支持 schema_version >= 2 的原生端点绑定，以及旧版的平铺/models 结构。
    """
    if not isinstance(llm_cfg, dict):
        return {}, {}, ""

    schema_version = llm_cfg.get("schema_version", 1)
    default_model = str(llm_cfg.get("default") or "")

    endpoints: Dict[str, EndpointDefinition] = {}
    models: Dict[str, ModelDefinition] = {}

    # === Schema Version 2 原生格式 ===
    if schema_version >= 2 and "endpoints" in llm_cfg:
        raw_endpoints = llm_cfg.get("endpoints", {}) or {}
        for ep_id, ep_data in raw_endpoints.items():
            if not isinstance(ep_data, dict):
                continue
            provider = ep_data.get("provider", DRIVER_OPENAI)
            protocol = ep_data.get(
                "protocol",
                "gemini_generate_content" if is_gemini_driver(provider) else "openai_chat_completions"
            )
            endpoints[ep_id] = EndpointDefinition(
                id=ep_id,
                provider=provider,
                protocol=protocol,
                base_url=ep_data.get("base_url", ""),
                credential_ref=ep_data.get("credential_ref", ""),
                management_ref=ep_data.get("management_ref"),
                proxy=ep_data.get("proxy"),
                extra=copy.deepcopy(ep_data.get("extra", {}) or {}),
            )

        raw_models = llm_cfg.get("models", {}) or {}
        for m_id, m_data in raw_models.items():
            if not isinstance(m_data, dict):
                continue
            models[m_id] = ModelDefinition(
                id=m_id,
                endpoint_ref=m_data.get("endpoint_ref", ""),
                model=m_data.get("model", m_id),
                aliases=list(m_data.get("aliases", []) or []),
                capabilities=dict(m_data.get("capabilities", {}) or {}),
                max_tokens=int(m_data.get("max_tokens", 4096) or 4096),
                profiles=copy.deepcopy(m_data.get("profiles", {}) or {}),
                tags=list(m_data.get("tags", []) or []),
                output_recovery=copy.deepcopy(m_data.get("output_recovery", {}) or {}),
            )
        return endpoints, models, default_model

    # === 旧版 Schema 1 / 平铺结构无缝归一化迁移 ===
    raw_models = llm_cfg.get("models", {}) or {}
    if not raw_models:
        # 仅当显式声明了 driver 或 model 时才归一化旧版单模型根级平铺配置
        if "driver" not in llm_cfg and "model" not in llm_cfg:
            return {}, {}, default_model
        driver = llm_cfg.get("driver", DRIVER_DEEPSEEK)
        api_model = llm_cfg.get("model", "default")
        ep_id = f"endpoint_{driver}_legacy"
        raw_key = llm_cfg.get("api_key", "")
        cred_ref = raw_key[2:-1] if (raw_key.startswith("${") and raw_key.endswith("}")) else ""
        endpoints[ep_id] = EndpointDefinition(
            id=ep_id,
            provider=driver,
            protocol="gemini_generate_content" if is_gemini_driver(driver) else "openai_chat_completions",
            base_url=llm_cfg.get("base_url", ""),
            credential_ref=cred_ref,
            management_ref="deepseek-official-balance" if driver == DRIVER_DEEPSEEK else None,
            proxy=llm_cfg.get("proxy"),
            extra={"legacy_key": raw_key if not cred_ref else ""},
        )
        models["default"] = ModelDefinition(
            id="default",
            endpoint_ref=ep_id,
            model=api_model,
            max_tokens=int(llm_cfg.get("max_tokens", 4096) or 4096),
        )
        return endpoints, models, default_model or "default"

    for m_id, m_data in raw_models.items():
        if not isinstance(m_data, dict):
            continue
        driver = m_data.get("driver", DRIVER_OPENAI)
        base_url = m_data.get("base_url", "")
        proxy = m_data.get("proxy")
        raw_key = m_data.get("api_key", "")
        cred_ref = raw_key[2:-1] if (raw_key.startswith("${") and raw_key.endswith("}")) else ""

        # 归一化 endpoint：同驱动、同 base_url 与同凭据引用的模型共享 endpoint
        ep_id = f"endpoint_{driver}_{m_id}"
        endpoints[ep_id] = EndpointDefinition(
            id=ep_id,
            provider=driver,
            protocol="gemini_generate_content" if is_gemini_driver(driver) else "openai_chat_completions",
            base_url=base_url,
            credential_ref=cred_ref,
            management_ref="deepseek-official-balance" if (driver == DRIVER_DEEPSEEK and "api.deepseek.com" in base_url) else None,
            proxy=proxy,
            extra={"legacy_key": raw_key if not cred_ref else ""},
        )
        models[m_id] = ModelDefinition(
            id=m_id,
            endpoint_ref=ep_id,
            model=m_data.get("model", m_id),
            aliases=list(m_data.get("aliases", []) or []),
            capabilities=dict(m_data.get("capabilities", {}) or {}),
            max_tokens=int(m_data.get("max_tokens", 4096) or 4096),
            profiles=copy.deepcopy(m_data.get("profiles", {}) or {}),
            tags=list(m_data.get("tags", []) or []),
            output_recovery=copy.deepcopy(m_data.get("output_recovery", {}) or {}),
        )

    return endpoints, models, default_model
