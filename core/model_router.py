import copy
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
        profile_name = str(kwargs.get("profile") or "")
        profile = self.get_call_profile(model_name, profile_name)
        actual_model = cfg.get("model", model_name)
        common = {
            "client": client,
            "model_name": actual_model,
            "temperature": kwargs.get("temperature", profile["temperature"]),
            "max_tokens": kwargs.get("max_tokens", profile["max_tokens"]),
            "output_recovery": cfg.get("output_recovery", {}),
        }
        if is_gemini_driver(self.get_driver(model_name)):
            return GeminiInvoker(**common)
        return OpenAIInvoker(
            **common,
            timeout=kwargs.get("timeout", profile["timeout"]),
        )

    def get_call_profile(self, model_name: str,
                         profile_name: str = "") -> dict:
        """Resolve one model's reusable call contract.

        ``profiles.default`` supplies provider/model defaults and the named
        profile adds role semantics such as structured JSON or a tool loop.
        Unknown profile names intentionally fall back to model defaults.
        """
        cfg = self.models_cfg.get(model_name, {}) or {}
        is_structured = profile_name == "structured_json"
        resolved = {
            "temperature": float(cfg.get("temperature", 0.3)),
            "max_tokens": int(cfg.get("max_tokens", 2048)),
            "timeout": 120.0 if is_structured else 60.0,
            "max_retries": 0 if is_structured else 1,
            "invoke_kwargs": {},
        }
        profiles = cfg.get("profiles") or {}
        layers = [profiles.get("default") or {}]
        if profile_name:
            layers.append(profiles.get(profile_name) or {})
        for raw_layer in layers:
            if not isinstance(raw_layer, dict):
                continue
            layer = copy.deepcopy(raw_layer)
            resolved["invoke_kwargs"].update(
                layer.pop("invoke_kwargs", {}) or {}
            )
            for key in ("temperature", "max_tokens", "timeout", "max_retries"):
                if key in layer:
                    resolved[key] = layer[key]
        resolved["temperature"] = float(resolved["temperature"])
        resolved["max_tokens"] = max(1, int(resolved["max_tokens"]))
        resolved["timeout"] = max(1.0, float(resolved["timeout"]))
        resolved["max_retries"] = max(0, int(resolved["max_retries"]))
        return resolved

    def supports_vision(self, model_name: str) -> bool:
        cfg = self.models_cfg.get(model_name, {})
        return supports_vision(cfg.get("tags", []))
