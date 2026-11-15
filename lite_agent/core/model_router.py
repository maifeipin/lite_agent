import copy
from typing import Optional

from openai import OpenAI

from lite_agent.core.model_config import (
    DRIVER_OPENAI,
    is_gemini_driver,
    supports_vision,
)
from lite_agent.core.model_endpoint import (
    EndpointDefinition,
    ModelDefinition,
    normalize_llm_config,
    resolve_credential,
)
from lite_agent.core.model_invoker import GeminiInvoker, OpenAIInvoker


class ModelRouter:

    def __init__(self, config: dict):
        llm_cfg = config.get("llm", {}) or {}
        self.endpoints, self.models, self.default_model = normalize_llm_config(llm_cfg)
        self.models_cfg: dict[str, dict] = llm_cfg.get("models", {}) or {}
        self._endpoint_clients: dict[str, object] = {}
        self._clients: dict[str, object] = {}
        self._drivers: dict[str, str] = {}
        self._init_clients()

    def _init_clients(self):
        # 1. 优先按归一化后的 Endpoints 初始化并共享客户端
        for ep_id, ep in self.endpoints.items():
            key = resolve_credential(ep.credential_ref, ep.extra.get("legacy_key", ""))
            if not key:
                continue
            if is_gemini_driver(ep.provider):
                self._endpoint_clients[ep_id] = self._make_gemini_client(ep_id, key, ep.proxy, ep_id)
            else:
                import httpx
                http_client = httpx.Client(proxy=ep.proxy) if ep.proxy else None
                self._endpoint_clients[ep_id] = OpenAI(
                    api_key=key,
                    base_url=ep.base_url,
                    http_client=http_client,
                )

        # 2. 将端点客户端绑定至模型规范名
        for name, m_def in self.models.items():
            ep = self.endpoints.get(m_def.endpoint_ref)
            driver = ep.provider if ep else DRIVER_OPENAI
            self._drivers[name] = driver
            if m_def.endpoint_ref in self._endpoint_clients:
                self._clients[name] = self._endpoint_clients[m_def.endpoint_ref]

        # 3. 兜底旧版直接按 models_cfg 初始化的场景
        for name, cfg in self.models_cfg.items():
            if name not in self._drivers:
                self._drivers[name] = cfg.get("driver", DRIVER_OPENAI)
            if name not in self._clients:
                driver = cfg.get("driver", DRIVER_OPENAI)
                raw_key = cfg.get("api_key", "")
                if not raw_key:
                    continue
                if is_gemini_driver(driver):
                    self._clients[name] = self._make_gemini_client(name, raw_key, cfg.get("proxy"), cfg.get("model", name))
                else:
                    import httpx
                    proxy_url = cfg.get("proxy")
                    http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
                    self._clients[name] = OpenAI(
                        api_key=raw_key,
                        base_url=cfg.get("base_url", ""),
                        http_client=http_client,
                    )

    def _make_gemini_client(self, name: str, api_key: str, proxy: Optional[str] = None, model_name: str = ""):
        try:
            from google import genai
        except ImportError:
            raise ImportError(
                "Gemini 模型需要 google-genai 库: pip install google-genai"
            )

        http_options = {}
        if proxy:
            http_options = {
                "clientArgs": {"proxy": proxy},
                "asyncClientArgs": {"proxy": proxy}
            }

        client = genai.Client(
            api_key=api_key,
            http_options=http_options,
        )
        print(f"  🤖 Gemini[{name}] 客户端就绪 model={model_name or name}")
        return client

    def get_driver(self, model_name: str) -> str:
        return self._drivers.get(model_name, DRIVER_OPENAI)

    def get_client(self, model_name: str) -> Optional[object]:
        return self._clients.get(model_name)

    def close(self):
        """关闭所有打开的 HTTP 客户端"""
        seen = set()
        for client in list(self._endpoint_clients.values()) + list(self._clients.values()):
            if client and id(client) not in seen:
                seen.add(id(client))
                close_fn = getattr(client, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass

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
            provider=self.get_driver(model_name),
            infer_reasoning_from_name=False,
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

    def supports_streaming(self, model_name: str) -> bool:
        """检查模型是否支持流式输出。Gemini 原生驱动及显式声明 capabilities.stream=False 的模型均不支持流式。"""
        if not model_name:
            return True
        driver = self.get_driver(model_name)
        if is_gemini_driver(driver):
            return False
        cfg = self.models_cfg.get(model_name, {}) or {}
        caps = cfg.get("capabilities", {})
        if isinstance(caps, dict) and caps.get("stream") is False:
            return False
        return True
