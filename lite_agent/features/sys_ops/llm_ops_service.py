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
        host = (parsed.hostname or "").lower()
        return host == "api.deepseek.com"
    except Exception:
        return False


def _resolve_default_model_binding(config: Optional[dict] = None) -> Tuple[str, dict]:
    """
    从配置中解析当前生效的默认模型及其绑定配置字典。
    严格优先从 models[default] 获取，避免读取无主性的顶层平铺残留。
    返回: (model_key, model_cfg)
    """
    if config is None:
        try:
            from lite_agent.core import config_loader
            config = config_loader.load_config() or {}
        except Exception:
            config = {}

    llm_cfg = config.get("llm", {}) or {}
    default_key = llm_cfg.get("default", "")
    models = llm_cfg.get("models", {}) or {}

    if default_key and default_key in models:
        return default_key, models[default_key]

    # 若 models 中未指明 default，但 models 不为空，取第一个
    if models:
        first_key = next(iter(models))
        return first_key, models[first_key]

    # 旧平铺单模型结构兼容兜底
    legacy_cfg = {
        "model": llm_cfg.get("model", "unknown"),
        "driver": llm_cfg.get("driver", "deepseek"),
        "base_url": llm_cfg.get("base_url", "https://api.deepseek.com/v1"),
        "api_key": llm_cfg.get("api_key", ""),
    }
    return default_key or "default", legacy_cfg


def check_active_llm_status(config: Optional[dict] = None) -> str:
    """
    查询当前配置的默认大模型（Default Model）的计费/余额或状态。
    仅当明确绑定官方 DeepSeek 且端点可信时，才向 DeepSeek 官方余额接口发起请求；
    其余驱动/端点返回明确的不支持提示与指引，坚决防止凭据错配外泄或 401 误报。
    """
    try:
        if config is None:
            from lite_agent.core import config_loader
            config = config_loader.load_config() or {}
    except Exception as e:
        return f"❌ 无法读取配置文件: {e}"

    default_key, model_cfg = _resolve_default_model_binding(config)
    api_model = model_cfg.get("model", default_key or "unknown")
    driver = (model_cfg.get("driver") or "").lower()
    base_url = model_cfg.get("base_url", "")
    api_key = model_cfg.get("api_key", "")

    header = f"🧠 **默认模型**: `{default_key}` (`{api_model}`)"

    # 1. 凭据存在性前置检查
    if not api_key or api_key.startswith("${"):
        return f"{header}\n🔑 **API 凭据**: ❌ 未配置或环境变量未解析"

    # 2. 检查是否为可信的 DeepSeek 官方绑定
    is_official_ds = (driver == "deepseek" or "deepseek" in default_key.lower()) and _is_official_deepseek_endpoint(base_url)

    if is_official_ds:
        return _query_deepseek_official_balance(api_key, header)

    # 3. 非官方 DeepSeek 或其他服务商（Ark、Gemini、OpenAI 兼容端点等）
    provider_name = driver.upper() if driver else "OPENAI-COMPATIBLE"
    if driver == "ark":
        provider_name = "火山引擎方舟 (Ark)"
    elif driver == "gemini_native":
        provider_name = "Google Gemini"

    notice_lines = [
        header,
        f"🏢 **服务商驱动**: {provider_name}",
        "ℹ️ **余额查询**: 当前厂商/端点未提供公开 API 余额查询，请登录对应平台控制台核对账户余量。",
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
                return f"{header}\n⚠️ 获取到的余额数据异常: {json.dumps(data)}"
    except urllib.error.HTTPError as e:
        # 安全规范：不直接透传响应原文，避免泄露请求头或内部凭据信息
        if e.code == 401:
            return f"{header}\n❌ **认证失败 (HTTP 401)**: DeepSeek 官方端点凭据认证失败，请检查绑定的 API Key 是否有效或过期。"
        elif e.code == 403:
            return f"{header}\n❌ **权限拒绝 (HTTP 403)**: DeepSeek 账号无权访问余额接口。"
        elif e.code == 429:
            return f"{header}\n⚠️ **频控限制 (HTTP 429)**: 查询余额过于频繁，请稍后重试。"
        return f"{header}\n❌ **请求失败 (HTTP {e.code})**: DeepSeek 余额接口响应异常。"
    except urllib.error.URLError as e:
        return f"{header}\n❌ 请求 DeepSeek API 网络失败: {e.reason}"
    except Exception as e:
        return f"{header}\n❌ 解析余额数据失败: {e}"


def check_deepseek_balance() -> str:
    """向后兼容入口：查询默认大模型状态/余量"""
    return check_active_llm_status()


def llm_check_balance() -> str:
    """技能入口：查询当前默认大模型账户的剩余余额或状态"""
    return check_active_llm_status()


def llm_usage_report(days: int = 7) -> str:
    from lite_agent.core.constants import PROJECT_ROOT
    db_path = os.path.join(PROJECT_ROOT, "data", "sessions.db")
    if not os.path.exists(db_path):
        return "❌ 找不到数据库文件 data/sessions.db"

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

            lines = [f"📊 **最近 {days} 天大模型 API 消耗统计**", "| 模型名称 | 提示词 Token | 生成 Token | 总 Token |", "|:---|---:|---:|---:|"]
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
    except Exception as e:
        return f"❌ 统计 API 消耗量失败: {e}"
