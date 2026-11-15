"""模型配置的中央枚举与强校验（轻量重构，不引入 Adapter 类体系）。

职责：
- 显式 driver 枚举，替代旧版 tags/URL 字符串推测 Provider 的方式。
- 能力标签枚举，统一散落在 model_router / worker_agent / model_management 里的字符串判断。
- 启动时配置强校验：幽灵别名、越界引用、非法 driver 直接暴露。

协议分界只有一条：driver == gemini_native 走 google-genai 原生 SDK，
其余（openai / deepseek / ark）都走 OpenAI SDK，仅 base_url / 参数细节不同。
"""

DRIVER_OPENAI = "openai"
DRIVER_DEEPSEEK = "deepseek"
DRIVER_ARK = "ark"
DRIVER_GEMINI = "gemini_native"

DRIVERS = (DRIVER_OPENAI, DRIVER_DEEPSEEK, DRIVER_ARK, DRIVER_GEMINI)

# 能力标签中央枚举。模型配置的 tags 必须取自这里，未知标签启动告警。
CAP_TEXT = "text"
CAP_CODE = "code"
CAP_MULTIMODAL = "multimodal"
CAP_VISION = "vision"
CAP_REASONING = "complex_reasoning"
CAP_FAST = "fast"
CAP_CREATIVE = "creative"

KNOWN_CAPABILITIES = frozenset({
    CAP_TEXT, CAP_CODE, CAP_MULTIMODAL, CAP_VISION,
    CAP_REASONING, CAP_FAST, CAP_CREATIVE,
})

# driver -> 友好展示名（脱敏目录用）
DRIVER_LABELS = {
    DRIVER_OPENAI: "OpenAI-compatible",
    DRIVER_DEEPSEEK: "DeepSeek",
    DRIVER_ARK: "Volcengine Ark",
    DRIVER_GEMINI: "Gemini Native",
}


def is_gemini_driver(driver: str) -> bool:
    """唯一协议分界：是否使用 google-genai 原生 SDK。"""
    return driver == DRIVER_GEMINI


def supports_vision(tags_or_cfg) -> bool:
    """统一的能力判断：是否支持图片输入。
    兼容 tags 列表或包含 tags / capabilities 的模型配置字典。
    """
    if isinstance(tags_or_cfg, dict):
        caps = tags_or_cfg.get("capabilities") or {}
        if caps.get("vision") or caps.get("multimodal"):
            return True
        tags = set(tags_or_cfg.get("tags") or [])
    elif isinstance(tags_or_cfg, (list, tuple, set)):
        tags = set(tags_or_cfg)
    else:
        tags = set()
    return CAP_VISION in tags or CAP_MULTIMODAL in tags


def validate_model_config(config: dict, require_credentials: bool = True) -> list[tuple[str, str]]:
    """校验模型相关配置，返回 [(level, message)]。level 为 error 或 warning。

    error 级：缺 driver、非法 driver、引用不存在的模型、主/回退越界、凭据格式非法、协议不匹配。
    warning 级：未知能力标签（不阻断启动，但提示拼写错误）。
    """
    issues: list[tuple[str, str]] = []
    llm = config.get("llm", {}) or {}
    models = llm.get("models", {}) or {}
    routing = config.get("task_routing", {}) or {}

    if not models:
        issues.append(("error", "llm.models 为空，未配置任何模型"))
        return issues

    def _error(msg: str):
        issues.append(("error", msg))

    def _warning(msg: str):
        issues.append(("warning", msg))

    from lite_agent.core.model_endpoint import (
        PROTOCOL_GEMINI,
        PROTOCOL_OPENAI,
        SUPPORTED_PROTOCOLS,
        extract_env_var_name,
        is_valid_credential_ref,
        resolve_credential,
    )

    # 模型规范名和自然语言别名必须一一对应，避免用户锁定模型时歧义。
    from lite_agent.core.model_policy import normalize_model_alias
    aliases: dict[str, str] = {}
    for name, cfg in models.items():
        if not isinstance(cfg, dict):
            continue
        raw_aliases = cfg.get("aliases") or []
        if not isinstance(raw_aliases, list):
            _error(f"模型 {name!r} 的 aliases 必须是数组")
            raw_aliases = []
        for value in [name] + raw_aliases:
            if not isinstance(value, str) or not value.strip():
                _error(f"模型 {name!r} 含无效别名 {value!r}")
                continue
            normalized = normalize_model_alias(value)
            existing = aliases.get(normalized)
            if existing and existing != name:
                _error(f"模型别名 {value!r} 冲突: {existing!r} 与 {name!r}")
            else:
                aliases[normalized] = name

    # 同一 provider endpoint 下的多个模型通常共享一份凭据。
    # 先收集缺失项，再按凭据域聚合报错，避免为同一个缺失密钥重复输出。
    missing_api_keys: dict[tuple[str, str], list[str]] = {}

    endpoints = llm.get("endpoints", {}) or {}
    schema_version = llm.get("schema_version", 1)
    if schema_version >= 2 and endpoints:
        for ep_name, ep_cfg in endpoints.items():
            if not isinstance(ep_cfg, dict):
                _error(f"端点 {ep_name!r} 的配置不是对象")
                continue
            prov = ep_cfg.get("provider", "")
            if not prov or prov not in DRIVERS:
                _error(f"端点 {ep_name!r} 缺少有效 provider（可选: {', '.join(DRIVERS)}）")

            # protocol 强校验
            proto = ep_cfg.get("protocol")
            if not proto:
                _error(f"端点 {ep_name!r} 缺少 protocol 协议声明")
            elif proto not in SUPPORTED_PROTOCOLS:
                _error(f"端点 {ep_name!r} 使用了不支持的协议 {proto!r}（可选: {', '.join(sorted(SUPPORTED_PROTOCOLS))}）")
            else:
                if is_gemini_driver(prov) and proto != PROTOCOL_GEMINI:
                    _error(f"端点 {ep_name!r} 的 provider {prov!r} 与 protocol {proto!r} 不兼容，Gemini 需使用 {PROTOCOL_GEMINI!r}")
                elif not is_gemini_driver(prov) and proto != PROTOCOL_OPENAI:
                    _error(f"端点 {ep_name!r} 的 provider {prov!r} 与 protocol {proto!r} 不兼容，应使用 {PROTOCOL_OPENAI!r}")

            if not is_gemini_driver(prov) and not ep_cfg.get("base_url"):
                _error(f"端点 {ep_name!r} 缺少 base_url")

            # v2 严禁明文 api_key
            if "api_key" in ep_cfg:
                _error(f"端点 {ep_name!r} (Schema v2) 禁止直接配置明文 api_key，必须使用 credential_ref 环境变量引用")

            cred_ref = ep_cfg.get("credential_ref")
            if not cred_ref:
                _error(f"端点 {ep_name!r} 缺少 credential_ref 凭据引用")
            elif not is_valid_credential_ref(cred_ref):
                _error(f"端点 {ep_name!r} 的 credential_ref {cred_ref!r} 格式非法，必须为合法环境变量名")
            elif require_credentials:
                var_name = extract_env_var_name(cred_ref)
                if not resolve_credential(var_name):
                    _error(f"端点 {ep_name!r} 引用的凭据环境变量 {var_name!r} 未配置或为空")

    # 1. 每个模型的 driver/endpoint 与必需字段
    for name, cfg in models.items():
        if not isinstance(cfg, dict):
            _error(f"模型 {name!r} 的配置不是对象")
            continue
        ep_ref = cfg.get("endpoint_ref")
        if ep_ref:
            if ep_ref not in endpoints:
                _error(f"模型 {name!r} 引用了未配置的 endpoint: {ep_ref!r}")
            # V1: 杜绝 v2 混合配置冲突
            conflicting_keys = [k for k in ("driver", "base_url", "api_key") if k in cfg]
            if conflicting_keys:
                _error(f"模型 {name!r} 既配置了 endpoint_ref 又声明了内联字段 ({', '.join(conflicting_keys)})，二者冲突")
        else:
            driver = cfg.get("driver", "")
            if not driver:
                _error(f"模型 {name!r} 缺少显式 driver 或 endpoint_ref 字段（可选: {', '.join(DRIVERS)}）")
            elif driver not in DRIVERS:
                _error(f"模型 {name!r} 的 driver {driver!r} 非法（可选: {', '.join(DRIVERS)}）")
            if not cfg.get("api_key"):
                credential_scope = (driver, cfg.get("base_url", ""))
                missing_api_keys.setdefault(credential_scope, []).append(name)
            if driver and not is_gemini_driver(driver) and not cfg.get("base_url"):
                _error(f"模型 {name!r} 缺少 base_url（OpenAI-compatible 必需）")
        if not cfg.get("model"):
            _error(f"模型 {name!r} 缺少 model 字段（实际 API model 名）")
        for tag in (cfg.get("tags") or []):
            if tag not in KNOWN_CAPABILITIES:
                _warning(f"模型 {name!r} 含未知能力标签 {tag!r}")
        profiles = cfg.get("profiles") or {}
        if not isinstance(profiles, dict):
            _error(f"模型 {name!r} 的 profiles 必须是对象")
            profiles = {}
        for profile_name, profile in profiles.items():
            if not isinstance(profile, dict):
                _error(
                    f"模型 {name!r} 的 profile {profile_name!r} 必须是对象"
                )
                continue
            invoke_kwargs = profile.get("invoke_kwargs", {})
            if not isinstance(invoke_kwargs, dict):
                _error(
                    f"模型 {name!r} 的 profile {profile_name!r}.invoke_kwargs "
                    "必须是对象"
                )
            for key in ("max_tokens", "timeout"):
                if key in profile:
                    try:
                        if float(profile[key]) <= 0:
                            raise ValueError
                    except (TypeError, ValueError):
                        _error(
                            f"模型 {name!r} 的 profile {profile_name!r}."
                            f"{key} 必须是正数"
                        )
            if "max_retries" in profile:
                try:
                    if int(profile["max_retries"]) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    _error(
                        f"模型 {name!r} 的 profile {profile_name!r}."
                        "max_retries 必须是非负整数"
                    )

    for names in missing_api_keys.values():
        if len(names) == 1:
            _error(f"模型 {names[0]!r} 缺少 api_key（或对应环境变量未配置）")
        else:
            quoted = ", ".join(repr(name) for name in names)
            _error(
                f"共享凭据的模型 [{quoted}] 缺少 api_key"
                "（或对应环境变量未配置）"
            )

    # 2. 顶层角色引用校验 + 调用路径约束
    def _check_ref(label: str, name):
        if name and name not in models:
            _error(f"{label} 引用了不存在的模型: {name!r}")

    default = llm.get("default", "")
    if not default:
        _error("llm.default 未配置")
    else:
        _check_ref("llm.default", default)

    planner = routing.get("planner_model", "")
    if planner:
        _check_ref("task_routing.planner_model", planner)

    _check_ref("task_routing.classifier_model", routing.get("classifier_model"))
    _check_ref("task_routing.simple_model", routing.get("simple_model"))

    # 3. 路由规则校验
    for rule in (routing.get("route_rules") or []):
        if not isinstance(rule, dict):
            continue
        task_type = rule.get("type", "unknown")
        primary = rule.get("model", "")
        fallback = rule.get("fallback", "")
        allowed = rule.get("allowed_models")
        _check_ref(f"{task_type} 主模型", primary)
        _check_ref(f"{task_type} 回退模型", fallback)
        if isinstance(allowed, list):
            for name in allowed:
                _check_ref(f"{task_type} 允许范围", name)
            if primary and primary not in allowed:
                _error(f"{task_type} 主模型 {primary!r} 不在允许范围内")
            if fallback and fallback not in allowed:
                _error(f"{task_type} 回退模型 {fallback!r} 不在允许范围内")

    # 4. committee 引用
    for name in (config.get("committee", {}).get("models") or []):
        _check_ref("committee.models", name)

    # 5. TaskSpec author/reviewer and manually configured cost tiers
    task_specs = config.get("task_specs", {}) or {}
    _check_ref("task_specs.author_model", task_specs.get("author_model"))
    _check_ref("task_specs.validator_model", task_specs.get("validator_model"))
    for tier, names in (task_specs.get("model_tiers") or {}).items():
        if not isinstance(names, list):
            _error(f"task_specs.model_tiers.{tier} 必须是模型名数组")
            continue
        for name in names:
            _check_ref(f"task_specs.model_tiers.{tier}", name)

    return issues


def validate_and_print(config: dict) -> bool:
    """启动时校验模型配置并打印实际生效摘要。

    返回是否通过（无 error 级问题）。error 级问题会阻断启动，
    让配置错误「启动即暴露」，而非运行时静默走错模型。
    """
    issues = validate_model_config(config)
    errors = [m for lvl, m in issues if lvl == "error"]
    warnings = [m for lvl, m in issues if lvl == "warning"]

    llm = config.get("llm", {}) or {}
    models = llm.get("models", {}) or {}
    routing = config.get("task_routing", {}) or {}

    print("🧠 模型配置校验")
    print(
        f"  default={llm.get('default', '')}  "
        f"planner={routing.get('planner_model', '')}  "
        f"classifier={routing.get('classifier_model', '')}"
    )
    for name, cfg in models.items():
        if not isinstance(cfg, dict):
            continue
        driver = cfg.get("driver", "?")
        api_model = cfg.get("model", name)
        print(f"  - {name} [{driver}] -> {api_model}")

    for w in warnings:
        print(f"  ⚠️ {w}")
    if errors:
        for e in errors:
            print(f"  ❌ {e}")
        print("  ⛔ 模型配置校验失败，终止启动。请修正 conf.d/*.json 后重启。")
        return False

    print("  ✅ 模型配置校验通过")
    return True
