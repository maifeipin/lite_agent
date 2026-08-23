from typing import Optional

from openai import OpenAI

from core.model_config import (
    DRIVER_OPENAI,
    is_gemini_driver,
    supports_vision,
)
from core.model_invoker import GeminiInvoker, OpenAIInvoker


class ModelRouter:

    def __init__(self, config: dict):
        llm_cfg = config.get("llm", {})
        self.models_cfg: dict[str, dict] = llm_cfg.get("models", {})
        self.default_model = llm_cfg.get("default", "")
        routing = config.get("task_routing", {})
        self.rules: list[dict] = routing.get("route_rules", [])
        self._clients: dict[str, object] = {}
        self._drivers: dict[str, str] = {}
        self._init_clients()

    def _init_clients(self):
        for name, cfg in self.models_cfg.items():
            # 显式 driver，不再靠 tags/URL 字符串推测。
            driver = cfg.get("driver", DRIVER_OPENAI)
            self._drivers[name] = driver

            if is_gemini_driver(driver):
                self._clients[name] = self._make_gemini_client(name, cfg)
            else:
                import httpx
                proxy_url = cfg.get("proxy")
                http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
                self._clients[name] = OpenAI(
                    api_key=cfg["api_key"],
                    base_url=cfg["base_url"],
                    http_client=http_client
                )

    def _make_gemini_client(self, name: str, cfg: dict):
        try:
            from google import genai
        except ImportError:
            raise ImportError(
                "Gemini 模型需要 google-genai 库: pip install google-genai"
            )

        # 代理只来自显式配置，不再读取 HTTPS_PROXY 环境变量兜底。
        proxy = cfg.get("proxy", "")
        http_options = {}
        if proxy:
            http_options = {
                "clientArgs": {"proxy": proxy},
                "asyncClientArgs": {"proxy": proxy}
            }

        client = genai.Client(
            api_key=cfg["api_key"],
            http_options=http_options,
        )
        print(f"  🤖 Gemini[{name}] 客户端就绪 model={cfg.get('model', name)}")
        return client

    def get_driver(self, model_name: str) -> str:
        return self._drivers.get(model_name, DRIVER_OPENAI)

    def route(self, subtask_type: str) -> tuple[str, object, list[str]]:
        for rule in self.rules:
            if rule.get("type") == subtask_type:
                tools = rule.get("tools", [])
                primary = rule.get("model", "")
                fallback = rule.get("fallback", "")
                allowed = rule.get("allowed_models")

                # allowed_models 是强制边界，不是 UI 元数据。
                # 没有该字段的历史规则保持原有 primary/fallback 行为。
                candidates = [primary, fallback]
                if isinstance(allowed, list):
                    candidates.extend(allowed)
                    candidates = [name for name in candidates if name in allowed]
                seen = set()
                for model_name in candidates:
                    if not model_name or model_name in seen:
                        continue
                    seen.add(model_name)
                    client = self._clients.get(model_name)
                    if client is not None:
                        if model_name != primary:
                            print(
                                f"  ⚠️ 路由 {subtask_type} 的主模型 {primary!r} 不可用，"
                                f"自动选择允许范围内的 {model_name!r}"
                            )
                        return (model_name, client, tools)
                return (primary, None, tools)
        default_client = self._clients.get(self.default_model)
        return (self.default_model, default_client, [])

    def get_fallback(self, model_name: str, subtask_type: str = "") -> Optional[tuple[str, object]]:
        for rule in self.rules:
            if subtask_type and rule.get("type") != subtask_type:
                continue
            if rule.get("model") == model_name and rule.get("fallback"):
                fb_name = rule["fallback"]
                allowed = rule.get("allowed_models")
                if isinstance(allowed, list) and fb_name not in allowed:
                    continue
                fb_client = self._clients.get(fb_name)
                if fb_client:
                    return (fb_name, fb_client)
        return None

    def get_client(self, model_name: str) -> Optional[object]:
        return self._clients.get(model_name)

    def get_invoker(self, model_name: str, **kwargs):
        """Build the existing protocol adapter for a configured model.

        Keeping this factory here prevents Planner/Worker callers from duplicating
        driver checks while leaving routing policy in configuration.
        """
        client = self.get_client(model_name)
        if client is None:
            return None
        cfg = self.models_cfg.get(model_name, {})
        actual_model = cfg.get("model", model_name)
        common = {
            "client": client,
            "model_name": actual_model,
            "temperature": kwargs.get("temperature", cfg.get("temperature", 0.3)),
            "max_tokens": kwargs.get("max_tokens", cfg.get("max_tokens", 2048)),
            "output_recovery": cfg.get("output_recovery", {}),
        }
        if is_gemini_driver(self.get_driver(model_name)):
            return GeminiInvoker(**common)
        return OpenAIInvoker(
            **common,
            timeout=kwargs.get("timeout", 60.0),
        )

    def supports_vision(self, model_name: str) -> bool:
        cfg = self.models_cfg.get(model_name, {})
        return supports_vision(cfg.get("tags", []))
