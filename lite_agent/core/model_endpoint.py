"""端点绑定与配置归一化契约 (Schema Version 2)。

核心概念：
1. EndpointDefinition: 协议 + 服务接入点 + 凭据引用的受信任绑定定义
2. ModelDefinition: 业务别名、实际 API 模型名、端点引用、能力与 profile
3. normalize_llm_config: 无缝兼容旧版平铺/单模型与 schema 1 配置，归一化输出标准版本化快照
"""

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from lite_agent.core.model_config import (
    DRIVER_ARK,
    DRIVER_DEEPSEEK,
    DRIVER_GEMINI,
    DRIVER_OPENAI,
    is_gemini_driver,
)

PROTOCOL_OPENAI = "openai_chat_completions"
PROTOCOL_GEMINI = "gemini_generate_content"
SUPPORTED_PROTOCOLS = frozenset({PROTOCOL_OPENAI, PROTOCOL_GEMINI})

_ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_valid_credential_ref(ref: str) -> bool:
    """校验凭据引用是否为合法的环境变量名称（支持 ${VAR} 或 VAR）。"""
    if not ref or not isinstance(ref, str):
        return False
    var_name = ref.strip()
    if var_name.startswith("${") and var_name.endswith("}"):
        var_name = var_name[2:-1].strip()
    return bool(_ENV_VAR_PATTERN.match(var_name))


def extract_env_var_name(ref: str) -> str:
    """提取凭据引用中的真实环境变量名。"""
    if not ref:
        return ""
    var_name = ref.strip()
    if var_name.startswith("${") and var_name.endswith("}"):
        var_name = var_name[2:-1].strip()
    return var_name


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
    var_name = extract_env_var_name(credential_ref)
    if not var_name:
        return fallback_value
    return os.environ.get(var_name, fallback_value)


def normalize_llm_config(llm_cfg: dict) -> Tuple[Dict[str, EndpointDefinition], Dict[str, ModelDefinition], str]:
    """将 llm 配置字典归一化为 (endpoints, models, default_model)。
    同时支持 schema_version >= 2 的原生端点绑定，以及旧版的平铺/models 结构。
    返回完全独立的结构副本。
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
                PROTOCOL_GEMINI if is_gemini_driver(provider) else PROTOCOL_OPENAI
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
            caps = dict(m_data.get("capabilities", {}) or {})
            tags = list(m_data.get("tags", []) or [])
            # 双向同步 capabilities 与 tags
            if caps.get("vision") or caps.get("multimodal"):
                if "vision" not in tags:
                    tags.append("vision")
            elif "vision" in tags or "multimodal" in tags:
                caps["vision"] = True

            # 流式能力同步：Gemini 默认 stream=False，OpenAI 默认 stream=True
            ep_def = endpoints.get(m_data.get("endpoint_ref", ""))
            if ep_def and is_gemini_driver(ep_def.provider):
                caps.setdefault("streaming", False)
                caps.setdefault("stream", False)
            elif "streaming" in caps:
                caps.setdefault("stream", caps["streaming"])
            elif "stream" in caps:
                caps.setdefault("streaming", caps["stream"])

            models[m_id] = ModelDefinition(
                id=m_id,
                endpoint_ref=m_data.get("endpoint_ref", ""),
                model=m_data.get("model", m_id),
                aliases=list(m_data.get("aliases", []) or []),
                capabilities=caps,
                max_tokens=int(m_data.get("max_tokens", 4096) or 4096),
                profiles=copy.deepcopy(m_data.get("profiles", {}) or {}),
                tags=tags,
                output_recovery=copy.deepcopy(m_data.get("output_recovery", {}) or {}),
            )
        return copy.deepcopy(endpoints), copy.deepcopy(models), default_model

    # === 旧版 Schema 1 / 平铺结构无缝归一化迁移 ===
    raw_models = llm_cfg.get("models", {}) or {}
    if not raw_models:
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
            protocol=PROTOCOL_GEMINI if is_gemini_driver(driver) else PROTOCOL_OPENAI,
            base_url=llm_cfg.get("base_url", ""),
            credential_ref=cred_ref,
            management_ref="deepseek-official-balance" if driver == DRIVER_DEEPSEEK else None,
            proxy=llm_cfg.get("proxy"),
            extra={"legacy_key": raw_key if not cred_ref else ""},
        )
        caps = {"streaming": False} if is_gemini_driver(driver) else {"streaming": True}
        models["default"] = ModelDefinition(
            id="default",
            endpoint_ref=ep_id,
            model=api_model,
            capabilities=caps,
            max_tokens=int(llm_cfg.get("max_tokens", 4096) or 4096),
        )
        return copy.deepcopy(endpoints), copy.deepcopy(models), default_model or "default"

    for m_id, m_data in raw_models.items():
        if not isinstance(m_data, dict):
            continue
        driver = m_data.get("driver", DRIVER_OPENAI)
        base_url = m_data.get("base_url", "")
        proxy = m_data.get("proxy")
        raw_key = m_data.get("api_key", "")
        cred_ref = raw_key[2:-1] if (raw_key.startswith("${") and raw_key.endswith("}")) else ""

        ep_id = f"endpoint_{driver}_{m_id}"
        endpoints[ep_id] = EndpointDefinition(
            id=ep_id,
            provider=driver,
            protocol=PROTOCOL_GEMINI if is_gemini_driver(driver) else PROTOCOL_OPENAI,
            base_url=base_url,
            credential_ref=cred_ref,
            management_ref="deepseek-official-balance" if (driver == DRIVER_DEEPSEEK and "api.deepseek.com" in base_url) else None,
            proxy=proxy,
            extra={"legacy_key": raw_key if not cred_ref else ""},
        )
        caps = dict(m_data.get("capabilities", {}) or {})
        tags = list(m_data.get("tags", []) or [])
        if is_gemini_driver(driver):
            caps.setdefault("streaming", False)
            caps.setdefault("stream", False)
        if "vision" in tags or "multimodal" in tags:
            caps["vision"] = True

        models[m_id] = ModelDefinition(
            id=m_id,
            endpoint_ref=ep_id,
            model=m_data.get("model", m_id),
            aliases=list(m_data.get("aliases", []) or []),
            capabilities=caps,
            max_tokens=int(m_data.get("max_tokens", 4096) or 4096),
            profiles=copy.deepcopy(m_data.get("profiles", {}) or {}),
            tags=tags,
            output_recovery=copy.deepcopy(m_data.get("output_recovery", {}) or {}),
        )

    return copy.deepcopy(endpoints), copy.deepcopy(models), default_model
