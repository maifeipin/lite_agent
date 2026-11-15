"""
Sys Ops 大模型配额与用量统计服务 (features.sys_ops.llm_ops_service)。
从 skills/ops_llm.py 迁入大模型余额/状态查询与 SQLite api_usage_log 统计逻辑。
"""
import os
import json
import time
import sqlite3
import urllib.request
import urllib.error
from urllib.parse import urlparse
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, Tuple


def _is_official_deepseek_endpoint(base_url: str) -> bool:
    """严格判断是否为 DeepSeek 官方端点，避免将凭据发送到第三方兼容中转或未知端点。"""
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url)
        if parsed.scheme.lower() != "https":
            return False
        if parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 443):
            return False
        host = (parsed.hostname or "").lower()
        if host != "api.deepseek.com":
            return False
        path = parsed.path.rstrip("/")
        return path in ("", "/v1", "/user/balance")
    except Exception:
        return False


def _resolve_default_model_binding(config: Optional[dict] = None) -> Tuple[str, Optional[dict]]:
    """
    从配置中解析当前生效的默认模型及其绑定配置字典。
    严格按 models[default] 获取，绝不静默兜底为其他模型。
    返回: (default_key, model_cfg_or_none)
    """
    if config is None:
        try:
            from lite_agent.core import config_loader
            config = config_loader.load_config() or {}
        except Exception:
            config = {}

    llm_cfg = config.get("llm", {}) or {}
    default_key = llm_cfg.get("default", "")

    # 判断是否为多模型架构 (models 字段在配置中声明，即使为空字典也属于新 schema)
    if "models" in llm_cfg:
        models = llm_cfg["models"]
        if not isinstance(models, dict):
            return default_key or "default", None
        # 当 models 存在时，default 必须显式存在且必须位于 models 中
        # 绝不静默兜底为第一个模型，必须如实报告配置错误！
        if not default_key:
            return "", None
        if default_key not in models:
            return default_key, None
        m_cfg = dict(models[default_key])
        ep_ref = m_cfg.get("endpoint_ref")
        if ep_ref:
            endpoints = llm_cfg.get("endpoints", {}) or {}
            ep_data = endpoints.get(ep_ref, {})
            # 严格以 endpoint 为准强制覆盖，杜绝模型内旧字段污染身份绑定
            m_cfg["driver"] = ep_data.get("provider", "openai")
            m_cfg["base_url"] = ep_data.get("base_url", "")
            m_cfg["management_ref"] = ep_data.get("management_ref")
            from lite_agent.core.model_endpoint import resolve_credential
            cred_ref = ep_data.get("credential_ref", "")
            # endpoint_ref always resolves a reference, never an inline secret fallback.
            key = resolve_credential(cred_ref)
            m_cfg["api_key"] = key
        return default_key, m_cfg

    # 旧平铺单模型结构兼容兜底 (仅在完全没有 models 字段的旧版配置时生效)
    # 严格要求显式声明 driver，绝不猜测缺省为 deepseek
    driver = llm_cfg.get("driver")
    if not driver:
        return default_key or "default", None
    legacy_cfg = {
        "model": llm_cfg.get("model", "unknown"),
        "driver": driver,
        "base_url": llm_cfg.get("base_url", ""),
        "api_key": llm_cfg.get("api_key", ""),
    }
    return default_key or "default", legacy_cfg


def _resolve_session_db_path(config: Optional[dict] = None) -> str:
    """
    统一解析会话与用量数据库路径，与 Agent/SessionManager 保持完全一致的优先级：
    1. 配置字典 session.db_path
    2. 环境变量 LITE_AGENT_SESSION_DB_PATH
    3. 默认项目路径 PROJECT_ROOT/data/sessions.db
    """
    if config is None:
        try:
            from lite_agent.core import config_loader
            config = config_loader.load_config() or {}
        except Exception:
            config = {}

    cfg_db = (config.get("session") or {}).get("db_path")
    if cfg_db:
        return cfg_db
    env_db = os.environ.get("LITE_AGENT_SESSION_DB_PATH")
    if env_db:
        return env_db
    from lite_agent.core.constants import PROJECT_ROOT
    return os.path.join(PROJECT_ROOT, "data", "sessions.db")


def _get_model_local_usage(model_name: str, days: int = 7, config: Optional[dict] = None) -> str:
    """查询指定模型最近 N 天在本地 api_usage_log 的 Token 消耗总量"""
    from lite_agent.core.sqlite_utils import connect_sqlite
    db_path = _resolve_session_db_path(config)
    if not os.path.exists(db_path):
        return f"📊 **近 {days} 天本地已记录用量**: 暂无调用记录"
    start_time = time.time() - (days * 24 * 3600)
    try:
        with connect_sqlite(db_path, close_on_exit=True) as conn:
            table_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='api_usage_log'").fetchone()
            if not table_check:
                return f"📊 **近 {days} 天本地已记录用量**: 暂无调用记录"
            row = conn.execute(
                "SELECT SUM(total_tokens) FROM api_usage_log WHERE model = ? AND created_at >= ?",
                (model_name, start_time)
            ).fetchone()
            total = (row[0] if row and row[0] is not None else 0)
            if total == 0:
                return f"📊 **近 {days} 天本地已记录用量**: 暂无调用记录"
            return f"📊 **近 {days} 天本地已记录用量**: **{total:,}** Tokens (本地已记录，非账户全量)"
    except Exception:
        # F2: 查询失败时不能伪装为“暂无调用记录”，应返回明确的暂不可用状态，且不泄露底层异常
        return f"📊 **近 {days} 天本地已记录用量**: 用量统计暂不可用"


def check_active_llm_status(config: Optional[dict] = None) -> str:
    """
    查询当前配置的默认大模型（Default Model）的计费/余额或状态。
    仅当明确绑定官方 DeepSeek 且端点可信时，才向 DeepSeek 官方余额接口发起请求；
    其余驱动/端点返回明确的不支持提示与本地统计指引，坚决防止凭据错配外泄或 401 误报。
    """
    try:
        if config is None:
            from lite_agent.core import config_loader
            config = config_loader.load_config() or {}
    except Exception:
        return "❌ 无法读取配置文件: 配置解析异常"

    default_key, model_cfg = _resolve_default_model_binding(config)
    if not default_key or model_cfg is None:
        target_name = f"`{default_key}`" if default_key else "(未设置)"
        return f"🧠 **默认模型**: {target_name}\n❌ **配置错误**: 默认模型未配置或在 `llm.models` 中不存在，已终止查询。"

    api_model = model_cfg.get("model", default_key or "unknown")
    driver = (model_cfg.get("driver") or "").lower()
    base_url = model_cfg.get("base_url", "")
    api_key = model_cfg.get("api_key", "")

    header = f"🧠 **默认模型**: `{default_key}` (`{api_model}`)"

    # 1. 凭据存在性前置检查
    if not api_key or api_key.startswith("${"):
        return f"{header}\n🔑 **API 凭据**: ❌ 未配置或环境变量未解析"

    # 2. 检查是否为可信的 DeepSeek 官方绑定 (消费 management_ref 或显式官方端点，别名或模型名绝不参与判定)
    management_ref = model_cfg.get("management_ref")
    management_allowed = (
        not model_cfg.get("endpoint_ref")
        or management_ref == "deepseek-official-balance"
    )
    is_official_ds = driver == "deepseek" and management_allowed and _is_official_deepseek_endpoint(base_url)

    if is_official_ds:
        return _query_deepseek_official_balance(api_key, header)

    # 3. 非官方 DeepSeek 或其他服务商（Ark、Gemini、OpenAI 兼容端点等）
    provider_name = driver.upper() if driver else "OPENAI-COMPATIBLE"
    if driver == "ark":
        provider_name = "火山引擎方舟 (Ark)"
    elif driver == "gemini_native":
        provider_name = "Google Gemini"

    local_usage = _get_model_local_usage(api_model, config=config)
    notice_lines = [
        header,
        f"🏢 **服务商驱动**: {provider_name}",
        "ℹ️ **余额查询**: 当前厂商/端点未提供公开 API 余额查询，请登录对应平台控制台核对账户余量。",
        local_usage,
    ]
    return "\n".join(notice_lines)


def _query_deepseek_official_balance(api_key: str, header: str) -> str:
    """调用 DeepSeek 官方余额查询接口并脱敏处理错误。"""
    url = "https://api.deepseek.com/user/balance"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}"
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get("is_available"):
                balance_info = data.get("balance_infos", [])
                lines = [header, "✅ **DeepSeek 账户余额 (官方实时)**"]
                has_zero = False
                for info in balance_info:
                    currency = info.get("currency", "")
                    total_str = info.get("total_balance", "0")
                    try:
                        amount = Decimal(str(total_str))
                    except (InvalidOperation, TypeError):
                        amount = Decimal("0")
                    if amount <= Decimal("0"):
                        has_zero = True
                    lines.append(f"- {currency}: **{amount}**")
                if has_zero and len(balance_info) == 1:
                    lines.append("\n⚠️ **警告: 您的余额可能已耗尽，请及时充值！**")
                return "\n".join(lines)
            else:
                return f"{header}\n⚠️ **账户不可用**: DeepSeek 官方返回账户不可用 (is_available=false)，请登录控制台检查充值状态与配额。"
    except urllib.error.HTTPError as e:
        # 安全规范：不直接透传响应原文，避免泄露请求头或内部凭据信息
        if e.code == 401:
            return f"{header}\n❌ **认证失败 (HTTP 401)**: DeepSeek 官方端点凭据认证失败，请检查绑定的 API Key 是否有效或过期。"
        elif e.code == 403:
            return f"{header}\n❌ **权限拒绝 (HTTP 403)**: DeepSeek 账号无权访问余额接口。"
        elif e.code == 429:
            return f"{header}\n⚠️ **频控限制 (HTTP 429)**: 查询余额过于频繁，请稍后重试。"
        return f"{header}\n❌ **请求失败 (HTTP {e.code})**: DeepSeek 余额接口响应异常。"
    except urllib.error.URLError:
        return f"{header}\n❌ **网络异常**: 请求 DeepSeek 官方端点失败，请检查网络连通性与代理设置。"
    except Exception:
        return f"{header}\n❌ **数据解析异常**: 解析 DeepSeek 余额数据格式失败。"


def check_deepseek_balance() -> str:
    """向后兼容入口：查询默认大模型状态/余量"""
    return check_active_llm_status()


def llm_check_balance() -> str:
    """技能入口：查询当前默认大模型账户的剩余余额或状态"""
    return check_active_llm_status()


def llm_usage_report(days: int = 7, config: Optional[dict] = None) -> str:
    if config is None:
        try:
            from lite_agent.core import config_loader
            config = config_loader.load_config() or {}
        except Exception:
            return "❌ 统计 API 消耗量失败: 无法读取配置文件"

    db_path = _resolve_session_db_path(config)
    if not os.path.exists(db_path):
        return f"❌ 找不到会话数据库: {db_path}"

    start_time = time.time() - (days * 24 * 3600)

    try:
        from lite_agent.core.sqlite_utils import connect_sqlite
        with connect_sqlite(db_path, close_on_exit=True) as conn:
            table_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='api_usage_log'").fetchone()
            if not table_check:
                return "ℹ️ 数据库中尚未存在 API 使用量日志表 (可能是功能刚上线，还没有产生新调用)。"

            rows = conn.execute(
                "SELECT model, SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens) "
                "FROM api_usage_log WHERE created_at >= ? GROUP BY model ORDER BY SUM(total_tokens) DESC",
                (start_time,)
            ).fetchall()

            if not rows:
                return f"ℹ️ 最近 {days} 天内没有任何 API 调用记录。"

            lines = [f"📊 **最近 {days} 天大模型 API 消耗统计 (本地已记录)**", "| 模型名称 | 提示词 Token | 生成 Token | 总 Token |", "|:---|---:|---:|---:|"]
            total_all = 0
            for row in rows:
                model = row[0] or "unknown"
                prompt = row[1] or 0
                comp = row[2] or 0
                total = row[3] or 0
                total_all += total
                lines.append(f"| `{model}` | {prompt:,} | {comp:,} | **{total:,}** |")

            lines.append(f"\n💡 **期间累计总消耗**: **{total_all:,}** Tokens")
            return "\n".join(lines)
    except Exception:
        return "❌ 统计 API 消耗量失败: 数据库查询异常"
