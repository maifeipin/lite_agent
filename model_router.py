import os
from typing import Optional

from openai import OpenAI


class ModelRouter:

    def __init__(self, config: dict):
        llm_cfg = config.get("llm", {})
        self.models_cfg: dict[str, dict] = llm_cfg.get("models", {})
        self.default_model = llm_cfg.get("default", "")
        routing = config.get("task_routing", {})
        self.rules: list[dict] = routing.get("route_rules", [])
        self._clients: dict[str, object] = {}
        self._providers: dict[str, str] = {}
        self._init_clients()

    def _init_clients(self):
        for name, cfg in self.models_cfg.items():
            provider = self._detect_provider(cfg)
            self._providers[name] = provider

            if provider == "gemini":
                self._clients[name] = self._make_gemini_client(name, cfg)
            else:
                self._clients[name] = OpenAI(
                    api_key=cfg["api_key"],
                    base_url=cfg["base_url"],
                )

    def _detect_provider(self, cfg: dict) -> str:
        tags = cfg.get("tags", [])
        if "gemini" in tags:
            return "gemini"
        base = cfg.get("base_url", "")
        if "generativelanguage" in base or "googleapis" in base:
            return "gemini"
        return "openai"

    def _make_gemini_client(self, name: str, cfg: dict):
        try:
            from google import genai
        except ImportError:
            raise ImportError(
                "Gemini 模型需要 google-genai 库: pip install google-genai"
            )

        proxy = cfg.get("proxy", os.environ.get("HTTPS_PROXY", ""))
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

    @staticmethod
    def _setup_socks_proxy(proxy_url: str, name: str):
        try:
            import socks
            url = proxy_url.replace("socks5h://", "").replace("socks5://", "")
            host, port = url.split(":")
            socks.set_default_proxy(socks.SOCKS5, host, int(port))
            import socket
            socket.socket = socks.socksocket
            print(f"  🌐 Gemini[{name}] SOCKS5 代理: {host}:{port}")
        except ImportError:
            print(f"  ⚠️ PySocks 未安装，无法使用 SOCKS 代理")
        except Exception as e:
            print(f"  ⚠️ SOCKS 代理设置失败: {e}")

    def get_provider(self, model_name: str) -> str:
        return self._providers.get(model_name, "openai")

    def route(self, subtask_type: str) -> tuple[str, object, list[str]]:
        for rule in self.rules:
            if rule.get("type") == subtask_type:
                model_name = rule["model"]
                client = self._clients.get(model_name)
                tools = rule.get("tools", [])
                return (model_name, client, tools)
        default_client = self._clients.get(self.default_model)
        return (self.default_model, default_client, [])

    def get_fallback(self, model_name: str) -> Optional[tuple[str, object]]:
        for rule in self.rules:
            if rule.get("model") == model_name and rule.get("fallback"):
                fb_name = rule["fallback"]
                fb_client = self._clients.get(fb_name)
                if fb_client:
                    return (fb_name, fb_client)
        return None

    def get_client(self, model_name: str) -> Optional[object]:
        return self._clients.get(model_name)

    def supports_vision(self, model_name: str) -> bool:
        cfg = self.models_cfg.get(model_name, {})
        tags = cfg.get("tags", [])
        return "multimodal" in tags or "vision" in tags
