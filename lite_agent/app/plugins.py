"""
受控插件加载器与插件协议接口 (app.plugins)。
仅通过显式清单登记与依赖注入受控加载，严禁目录级盲扫或任意表达式执行。
提供拓扑排序、重名校验、单插件注册事务与失败回滚机制。
"""

import importlib
import logging
from typing import Dict, Any, Optional, Set, Callable, List, Tuple
from lite_agent.services.commands import CommandRegistry
from lite_agent.services.workflows import WorkflowRegistry
from lite_agent.services.routes import RouteRegistry
from lite_agent.services.cron import CronService

logger = logging.getLogger("app.plugins")


class _PluginTransaction:
    """单插件注册事务记录，严格按发生顺序记录 undo 操作，以在插件失败时逆序真实释放。"""

    def __init__(self, plugin_name: str, host_context: "PluginContext"):
        self.plugin_name = plugin_name
        self.host = host_context
        self.undo_actions: List[Tuple[str, Callable[[], None]]] = []
        self.registered_cleanups: List[str] = []
        self.registered_skills: List[str] = []
        self.registered_crons: List[str] = []
        self.registered_job_handlers: List[str] = []
        self.registered_routes: List[Tuple[str, str]] = []
        self.registered_workflows: List[str] = []
        self.registered_commands: List[str] = []
        self.registered_domains: List[str] = []

    def record_action(self, desc: str, undo_fn: Callable[[], None]):
        self.undo_actions.append((desc, undo_fn))

    def rollback(self):
        """严格逆序回滚本次事务中登记的所有操作，真实调用 cleanup 并注销所有资源。"""
        errors = []
        for desc, undo_fn in reversed(self.undo_actions):
            try:
                undo_fn()
            except Exception as e:
                logger.warning(f"事务回滚操作失败 [{desc}]: {e}")
                errors.append(f"{desc}: {e}")
        self.undo_actions.clear()
        if errors:
            logger.error(f"插件 [{self.plugin_name}] 回滚完成，部分操作失败: {errors}")

    def unregister_non_cleanup_resources(self):
        """正常关闭时注销该插件登记的工具、任务、路由、工作流与指令。"""
        for d_name in reversed(self.registered_domains):
            try:
                self.host._unregister_domain(d_name)
            except Exception:
                pass
        if self.host.cron_service is not None:
            if hasattr(self.host.cron_service, "unregister_handler"):
                for h_name in reversed(self.registered_job_handlers):
                    try:
                        self.host.cron_service.unregister_handler(h_name)
                    except Exception:
                        pass
            for job_name in reversed(self.registered_crons):
                try:
                    self.host.cron_service.remove_job_by_name(job_name)
                except Exception:
                    pass
        if self.host.route_registry is not None:
            for method, pattern in reversed(self.registered_routes):
                try:
                    self.host.route_registry.unregister(method, pattern)
                except Exception:
                    pass
        if self.host.workflow_registry is not None:
            for wf_name in reversed(self.registered_workflows):
                try:
                    self.host.workflow_registry.unregister(wf_name)
                except Exception:
                    pass
        if self.host.skill_engine is not None:
            for sk_name in reversed(self.registered_skills):
                try:
                    self.host.skill_engine.unregister_tool(sk_name)
                except Exception:
                    pass
        if self.host.command_registry is not None:
            for cmd_name in reversed(self.registered_commands):
                try:
                    self.host.command_registry.unregister(cmd_name)
                except Exception:
                    pass


class PluginContext:
    """
    提供给插件的受限宿主上下文。
    仅按需暴露命令登记、工具注册、工作流、路由、定时作业、生命周期等句柄，
    绝不直接暴露完整 Agent 私有对象或任意模块反射。
    """

    def __init__(
        self,
        command_registry: Optional[CommandRegistry] = None,
        skill_engine: Optional[Any] = None,
        workflow_registry: Optional[WorkflowRegistry] = None,
        route_registry: Optional[RouteRegistry] = None,
        cron_service: Optional[CronService] = None,
        lifecycle: Optional[Any] = None,
        config: Optional[Dict[str, Any]] = None,
        plugin_name: str = "host",
        agent: Optional[Any] = None,
    ):
        self.command_registry = command_registry
        self.skill_engine = skill_engine
        self.workflow_registry = workflow_registry
        self.route_registry = route_registry
        self.cron_service = cron_service
        self.lifecycle = lifecycle
        self._raw_config = config or {}
        self.plugin_name = plugin_name
        self.config = self._project_config(plugin_name)
        self.agent = agent
        self.domain_registry: Dict[str, Any] = {}
        self._active_tx: Optional[_PluginTransaction] = None

    def _project_config(self, name: str) -> Dict[str, Any]:
        """按插件所需投影配置，避免不必要地暴露其它插件凭据。"""
        if not self._raw_config:
            return {}
        common_keys = {"models", "api", "system", "logging"}
        projected = {k: v for k, v in self._raw_config.items() if k in common_keys}
        if name in self._raw_config and isinstance(self._raw_config[name], dict):
            projected[name] = self._raw_config[name]

        plugin_extra_keys = {
            "review_meeting": [
                "review_meeting_notifications",
                "channels",
                "database",
                "review_meeting_base_url",
                "review_meeting_allowed_hosts",
                "review_allow_legacy_invites",
            ],
            "todo": ["todo", "database"],
            "mail_ops": ["mail", "mail_reader", "billing"],
            "network_ops": ["network", "socks5", "edge"],
            "content_ops": ["rss", "content"],
            "billing": ["billing"],
            "phone_app": ["phone_app", "database"],
        }
        for k in plugin_extra_keys.get(name, []):
            if k in self._raw_config:
                projected[k] = self._raw_config[k]
        return projected

    def send_owner_response(self, response: Any) -> bool:
        """Use this host's configured recipients and actual channel receipt."""
        from lite_agent.services.owner_delivery import send_owner_response
        channels = getattr(self.agent, "channels", []) or []
        return send_owner_response(channels, self._raw_config.get("channels", {}) or {}, response)

    def record_cleanup(self, cleanup_fn: Callable[[], None], name: Optional[str] = None):
        """登记插件停用/关闭时的清理钩子 (同 register_cleanup 接口，便于插件显式注册可停止句柄)。"""
        res_name = name or f"cleanup_{id(cleanup_fn)}"
        self.register_cleanup(res_name, cleanup_fn)

    def _begin_transaction(self, plugin_name: str) -> _PluginTransaction:
        tx = _PluginTransaction(plugin_name, self)
        self._active_tx = tx
        return tx

    def _commit_transaction(self):
        self._active_tx = None

    def register_command(self, name: str, handler: Callable, **kwargs):
        """向宿主指令注册表登记一个 Slash 指令。"""
        if self.command_registry is None:
            raise RuntimeError("宿主未提供命令注册服务")
        if "plugin" not in kwargs and self.plugin_name:
            kwargs["plugin"] = self.plugin_name
        self.command_registry.register(name, handler, **kwargs)
        if self._active_tx is not None:
            self._active_tx.registered_commands.append(name)
            self._active_tx.record_action(
                f"command:{name}",
                lambda: self.command_registry.unregister(name),
            )

    def register_skill(self, name: str, skill_def: Dict[str, Any]):
        """向宿主技能引擎登记一个工具。"""
        if self.skill_engine is None:
            raise RuntimeError("宿主未提供工具注册服务")
        self.skill_engine.register_tool(name, skill_def)
        if self._active_tx is not None:
            self._active_tx.registered_skills.append(name)
            self._active_tx.record_action(
                f"skill:{name}",
                lambda: self.skill_engine.unregister_tool(name),
            )

    def register_workflow(self, name: str, factory: Callable[..., Any], matcher: Callable[[dict, str], bool], **kwargs):
        """向宿主工作流注册表登记一个确定性流程。"""
        if self.workflow_registry is None:
            raise RuntimeError("宿主未提供工作流注册服务")
        self.workflow_registry.register(name, factory, matcher, **kwargs)
        if self._active_tx is not None:
            self._active_tx.registered_workflows.append(name)
            self._active_tx.record_action(
                f"workflow:{name}",
                lambda: self.workflow_registry.unregister(name),
            )

    def register_route(self, method: str, pattern: str, handler: Callable[..., Any], **kwargs):
        """向宿主路由注册表登记一个 HTTP 端点。"""
        if self.route_registry is None:
            raise RuntimeError("宿主未提供路由注册服务")
        self.route_registry.register(method, pattern, handler, **kwargs)
        if self._active_tx is not None:
            m = method.upper()
            self._active_tx.registered_routes.append((m, pattern))
            self._active_tx.record_action(
                f"route:{m}:{pattern}",
                lambda: self.route_registry.unregister(m, pattern),
            )

    def register_cron(self, name: str, cron_expr: str, func: Callable, **kwargs):
        """向宿主定时调度引擎登记一个后台作业。"""
        if self.cron_service is None:
            raise RuntimeError("宿主未提供定时调度服务")
        self.cron_service.add_job(name, cron_expr, func, **kwargs)
        if self._active_tx is not None:
            self._active_tx.registered_crons.append(name)
            self._active_tx.record_action(
                f"cron:{name}",
                lambda: self.cron_service.remove_job_by_name(name),
            )

    def register_job_handler(self, name: str, handler: Callable):
        """向宿主定时调度引擎登记一个专用作业回调处理器。"""
        if self.cron_service is None or not hasattr(self.cron_service, "register_handler"):
            raise RuntimeError("宿主未提供专用作业处理器注册服务")
        self.cron_service.register_handler(name, handler)
        if self._active_tx is not None:
            self._active_tx.registered_job_handlers.append(name)
            self._active_tx.record_action(
                f"job_handler:{name}",
                lambda: self.cron_service.unregister_handler(name),
            )

    def register_cleanup(self, name: str, cleanup_fn: Callable[[], None]):
        """登记插件停用/关闭时的清理钩子。"""
        if self.lifecycle is None:
            raise RuntimeError("宿主未提供生命周期服务")
        scoped_name = f"{self._active_tx.plugin_name}:{name}" if self._active_tx else name
        self.lifecycle.register_resource(scoped_name, cleanup_fn)
        if self._active_tx is not None:
            self._active_tx.registered_cleanups.append(scoped_name)
            def _rollback_cleanup():
                self.lifecycle.unregister_resource(scoped_name)
                cleanup_fn()
            self._active_tx.record_action(f"cleanup:{scoped_name}", _rollback_cleanup)

    def register_domain(self, domain_name: str, domain_spec: Dict[str, Any]):
        """向宿主登记业务领域路由规则（解耦 core/request_selector 业务硬编码）。"""
        if domain_name in self.domain_registry:
            raise ValueError(f"领域已登记，禁止覆盖: {domain_name}")
        # 同步给 agent.request_selector（若已存在）
        if self.agent is not None:
            selector = getattr(self.agent, "request_selector", None)
            if selector is not None and hasattr(selector, "register_domain"):
                selector.register_domain(domain_name, domain_spec)
        self.domain_registry[domain_name] = domain_spec
        if self._active_tx is not None:
            self._active_tx.registered_domains.append(domain_name)
            self._active_tx.record_action(
                f"domain:{domain_name}",
                lambda: self._unregister_domain(domain_name),
            )

    def _unregister_domain(self, domain_name: str):
        self.domain_registry.pop(domain_name, None)
        if self.agent is not None:
            selector = getattr(self.agent, "request_selector", None)
            if selector is not None and hasattr(selector, "unregister_domain"):
                selector.unregister_domain(domain_name)


class PluginLoader:
    """
    受控插件加载器。
    严格基于显式白名单与清单加载，阻断未受管动态导入。
    支持拓扑依赖检查与应用级启动失败回滚。
    """

    def __init__(self, context: PluginContext, allowed_entries: Optional[Set[str]] = None):
        self.context = context
        self.allowed_entries = frozenset(allowed_entries or ())
        self.loaded_plugins: Dict[str, Any] = {}
        self._transactions: Dict[str, _PluginTransaction] = {}

    def validate_manifest(self, plugins: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        验证插件清单的合法性与依赖拓扑：
        - 验证未知入口
        - 验证重复 ID
        - 验证缺失依赖
        - 验证依赖环 (拓扑排序)
        """
        seen_names = set()
        seen_entries = set()
        by_name: Dict[str, Dict[str, Any]] = {}

        for p in plugins:
            name = p.get("name")
            entry = p.get("entry")
            if not name or not entry:
                raise ValueError(f"插件清单项缺少 name 或 entry: {p}")
            if name in seen_names:
                raise ValueError(f"插件清单存在重复名称: {name}")
            if entry in seen_entries:
                raise ValueError(f"插件清单存在重复入口: {entry}")
            if entry not in self.allowed_entries:
                raise PermissionError(f"清单包含未受管的插件入口拒绝加载: {entry}")
            seen_names.add(name)
            seen_entries.add(entry)
            by_name[name] = p

        # 检查依赖项
        adj: Dict[str, List[str]] = {name: [] for name in seen_names}
        in_degree: Dict[str, int] = {name: 0 for name in seen_names}

        for name, p in by_name.items():
            deps = p.get("dependencies", [])
            for dep in deps:
                if dep not in by_name:
                    raise ValueError(f"插件 [{name}] 缺少必需的依赖插件: [{dep}]")
                adj[dep].append(name)
                in_degree[name] += 1

        # Kahn 算法拓扑排序 (按名称稳定字典序输出)
        queue = sorted([n for n in seen_names if in_degree[n] == 0])
        sorted_names = []
        while queue:
            node = queue.pop(0)
            sorted_names.append(node)
            for neighbor in sorted(adj[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
            queue.sort()

        if len(sorted_names) != len(seen_names):
            raise ValueError("插件清单中存在循环依赖，拒绝加载")

        return [by_name[n] for n in sorted_names]

    def load_plugin_by_entry(self, entry_name: str, plugin_name: Optional[str] = None) -> Any:
        """根据受控 entry 导入并执行其 register(context) 契约，支持单插件失败回滚。"""
        if entry_name not in self.allowed_entries:
            raise PermissionError(f"未受管的插件入口拒绝加载: {entry_name}")

        if entry_name in self.loaded_plugins:
            return self.loaded_plugins[entry_name]

        if self.context.lifecycle is not None and self.context.lifecycle.is_shutdown:
            raise RuntimeError("宿主已关闭，不能加载插件")

        p_name = plugin_name or entry_name.split(".")[-1]
        self.context.plugin_name = p_name
        self.context.config = self.context._project_config(p_name)
        tx = self.context._begin_transaction(p_name)

        try:
            mod = importlib.import_module(entry_name)
        except Exception as e:
            self.context._commit_transaction()
            raise ImportError(f"加载插件模块失败 [{entry_name}]: {e}") from e

        register = getattr(mod, "register", None)
        if not callable(register):
            self.context._commit_transaction()
            raise TypeError(f"插件必须提供 register(context): {entry_name}")

        try:
            register(self.context)
        except Exception as e:
            logger.error(f"插件 [{entry_name}] 执行 register 失败，立即回滚已注册资源: {e}")
            tx.rollback()
            self.context._commit_transaction()
            raise

        self.context._commit_transaction()
        self.loaded_plugins[entry_name] = mod
        self._transactions[entry_name] = tx

        if self.context.lifecycle is not None:
            def _make_cleanup(transaction):
                def _do_clean():
                    transaction.unregister_non_cleanup_resources()
                return _do_clean
            self.context.lifecycle.register_resource(
                f"plugin_resources:{p_name}",
                _make_cleanup(tx),
            )

        return mod

    def load_all(self, plugins: List[Dict[str, Any]]) -> List[Any]:
        """按拓扑排序加载所有插件。若中途失败，全量回滚此前已加载的插件。"""
        sorted_manifest = self.validate_manifest(plugins)
        loaded = []

        try:
            for p in sorted_manifest:
                mod = self.load_plugin_by_entry(p["entry"], plugin_name=p["name"])
                loaded.append(mod)
        except Exception as e:
            logger.error(f"插件全量加载失败，正在回滚所有已加载插件: {e}")
            for entry_name, tx in reversed(list(self._transactions.items())):
                try:
                    tx.rollback()
                except Exception as rb_err:
                    logger.warning(f"回滚插件 [{entry_name}] 失败: {rb_err}")
            self.loaded_plugins.clear()
            self._transactions.clear()
            raise

        return loaded
