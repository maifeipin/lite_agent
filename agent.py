"""
AI Agent 核心 - LLM 调度与 Tool Call Loop
"""

import os
import json
import re
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from openai import OpenAI
import openai
from session import SessionManager

from core.lru_cache import LRUCache
from core.loop_detector import LoopDetector
from core.model_invoker import OpenAIInvoker
from core.model_config import is_gemini_driver
from core.model_router import ModelRouter
from core.model_event import ModelEventType
from core.agent_runtime import AgentRuntime, RuntimeEventType
from core.execution import ExecutionContext, ActorType, ExecutionSource
from core.execution_ledger import ExecutionLedger
from core.runtime_recorder import RuntimeRecorder
from core.llm_gateway import LLMGateway
from core.output_delivery import prepare_channel_output

# 定义不可重试的大模型接口异常类型（4xx 客户端错误、鉴权、限流等）
_NON_RETRYABLE_EXCEPTIONS = (
    openai.BadRequestError,
    openai.AuthenticationError,
    openai.NotFoundError,
    openai.PermissionDeniedError,
    openai.UnprocessableEntityError,
    openai.RateLimitError,
)
from core.cron_engine import CronManager
from core.skill_engine import SkillEngine
from core.request_selector import (
    MissMarkStreamFilter,
    RequestSelector,
    detect_miss,
    strip_miss_mark,
)
from core.command_registry import dispatch as _registry_dispatch
from core.command_registry import _registry

# 仪表盘可见的系统指令
_registry.register('/new', lambda a,m,args: None,
                   category='系统', description='重置当前会话记忆，开启全新对话上下文',
                   show_in_dashboard=True)

# 记忆引擎 (可选 — 缺失时优雅降级)
try:
    from memory_engine.lite_integration import AgentMemory
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False


# ============================================================
#  内部消息格式
# ============================================================
class IncomingMessage:
    """从通道层传入的标准化消息"""

    def __init__(self, channel: str, user_id: str, chat_id: str,
                 message_id: str, text: str, notify_channels: list = None, is_guest: bool = False, sync_mode: bool = False,
                 channel_payload: dict = None, output_mode: str = ""):
        self.channel = channel
        self.user_id = user_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.is_guest = is_guest
        self.sync_mode = sync_mode
        # 如果未指定通知通道，默认只回复给来源通道 (避免跨通道广播)
        self.notify_channels = notify_channels if notify_channels is not None else [channel]
        # channel_payload: 通道层原始上下文, 供异步推送 (_push_result/send_progress) 使用
        # 如 钉钉的 msg_data(含sessionWebhook)、飞书的 sender open_id 等。默认 None 向后兼容。
        self.channel_payload = channel_payload or {}
        self.output_mode = output_mode
        self._session_key = ""

    @property
    def scope_key(self) -> str:
        """Stable channel/user identity used to find the active conversation."""
        return f"{self.channel}:{self.user_id}"

    @property
    def session_key(self) -> str:
        return self._session_key or self.scope_key

    def bind_session(self, session_key: str) -> None:
        self._session_key = session_key


class AgentResponse:
    """Agent 返回给通道层的标准化回复"""

    def __init__(self, text: str, title: str = "", color: str = "blue", task_id: str = "", logs: list = None,
                 new_session_key: str = ""):
        self.text = text
        self.title = title
        self.color = color
        self.task_id = task_id
        self.logs = logs if logs is not None else []
        self.new_session_key = new_session_key


def _estimate_tokens(messages: list, completion_text: str) -> dict:
    """流式下 provider 不返回 usage 时的本地估算兜底。
    DeepSeek/Gemini/Doubao 的 tokenizer 与 cl100k_base 不同, 估算有偏差,
    调用方应据此把 total_usage["estimated"] 置 True 以便审计区分。"""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        prompt_tokens = sum(len(enc.encode(m.get("content", "") or "")) for m in messages)
        completion_tokens = len(enc.encode(completion_text or ""))
        return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens}
    except Exception:
        # tiktoken 未安装时退化为字符启发式 (误差更大, 但好过 0)
        prompt_chars = sum(len((m.get("content", "") or "")) for m in messages)
        comp_chars = len(completion_text or "")
        pt = max(1, prompt_chars // 4)
        ct = max(1, comp_chars // 4)
        return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}

# ============================================================
#  Agent 核心
# ============================================================
class Agent:
    """
    AI Agent - 接收用户消息，通过 LLM + Tool Calling 完成任务
    - 内置指令 (/new, /status 等) 不经过 AI，直接处理
    - 自然语言走 Tool Call Loop，自动调度技能
    """

    def __init__(self, config: dict):
        self._config = config
        llm_cfg = config["llm"]
        session_cfg = config.get("session", {})

        # LLM 客户端
        import httpx
        request_router = None
        if "models" in llm_cfg:
            default_model = llm_cfg.get("default", "")
            default_cfg = llm_cfg["models"].get(default_model, {})
            request_router = ModelRouter(config)
            self.client = request_router.get_client(default_model)
            self.model_invoker = request_router.get_invoker(default_model)
            if self.model_invoker is None:
                raise ValueError(f"默认模型未配置或不可用: {default_model}")
            self.model = self.model_invoker.model_name
            self.model_driver = request_router.get_driver(default_model)
            self.max_tokens = default_cfg.get("max_tokens", 2048)
            self.temperature = default_cfg.get("temperature", 0.3)
        else:
            proxy_url = llm_cfg.get("proxy")
            http_client = httpx.Client(proxy=proxy_url) if proxy_url else None
            self.client = OpenAI(
                api_key=llm_cfg["api_key"],
                base_url=llm_cfg["base_url"],
                http_client=http_client
            )
            self.model = llm_cfg["model"]
            self.model_driver = "openai"
            self.max_tokens = llm_cfg.get("max_tokens", 2048)
            self.temperature = llm_cfg.get("temperature", 0.3)
            self.model_invoker = OpenAIInvoker(
                client=self.client,
                model_name=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

        self.bot_name = config.get("bot_name", "Agent")
        self.svc_name = config.get("service_name", "feishu-bot")

        # 会话管理
        self.session_mgr = SessionManager(
            ttl_minutes=session_cfg.get("ttl_minutes", 30),
            max_history=session_cfg.get("max_history", 20),
            max_history_bytes=session_cfg.get("max_history_bytes", 40000),
        )
        self.channels = []  # 由 main.py 在初始化通道后注入

        # 会话级互斥锁: 同一 session_key 的消息串行处理, 防止并发写 messages 表
        # 导致 OpenAI 协议违规 (assistant tool_calls 后必须紧跟 tool 消息)
        self._session_locks = {}  # session_key -> threading.Lock
        self._session_locks_guard = threading.Lock()
        self._request_model_router = request_router
        self._request_model_router_lock = threading.Lock()

        # 技能引擎
        self.skill_engine = SkillEngine()

        # RequestSelector: 动态工具选择（设计 3.5，仅在此初始化一次，启动校验随构造完成）
        # 代码能力一次交付；Phase 1/2 仅通过环境变量切换 Shadow/Enabled。
        self.request_selector = RequestSelector(self.skill_engine)

        # 安全限制
        self.max_steps = session_cfg.get("max_steps_per_goal", 30)
        self.daily_token_limit = session_cfg.get("daily_token_limit", 500000)
        self._dead_loop_counter = LRUCache(maxsize=200)  # session_key -> {tool_fingerprint -> count}
        self.orch_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="AgentOrch")

        # AgentRuntime: 事件驱动执行引擎
        # Runtime 负责: 模型循环、工具执行、死循环检测、max_steps/token 预算熔断
        # Agent 负责: Session 持久化、Memory 回调、日额度、动态超时、通道事件转换
        self.runtime = AgentRuntime(
            model_invoker=self.model_invoker,
            skill_engine=self.skill_engine,
            max_steps=self.max_steps,
            max_tokens=self.max_tokens,
            max_retries=1,
            non_retryable_exceptions=_NON_RETRYABLE_EXCEPTIONS,
        )

        # ExecutionLedger: 旁路执行账本 (不阻断主流程)
        # Runtime 产生事实事件 → Recorder 持久化 → SQLite
        self.ledger = ExecutionLedger()
        self.llm = LLMGateway(ledger=self.ledger)

        # 记忆引擎 (跨会话长期记忆)
        self.memory = AgentMemory() if MEMORY_AVAILABLE else None
        if self.memory:
            # 给蒸馏注入 LLM callback —— 复用 self.client + self.model
            # 这样不用单独维护 LLM_API_KEY 环境变量，配置零硬编码
            def _distill_llm_callback(prompt: str) -> str:
                resp = self.llm.invoke_sync(
                    [{"role": "user", "content": prompt}],
                    invoker=self.model_invoker, role="memory_distiller",
                    provider=self.model_driver, session_key="system:memory_distiller",
                    max_tokens=self.max_tokens,
                )
                return resp["content"] or ''
            self.memory.set_llm(_distill_llm_callback)
            self.memory.start_distill_scheduler(interval_hours=24)
            print("  ✅ 记忆引擎已启用 (蒸馏 LLM callback 已注入)")

            # 注册记忆指令（需要 self.memory 实例）
            self._register_memory_commands()

    def _register_memory_commands(self):
        """将依赖 self.memory 的指令注册到 CommandRegistry"""
        agent_self = self

        def _cmd_memory_stats(agent, msg, args):
            if not agent_self.memory:
                return "记忆引擎未安装，请将 memory_engine/ 目录放入项目根目录"
            stats = agent_self.memory.stats()
            type_stats = stats.get('by_type', {})
            type_lines = "\n".join(f"  {t}: {c} 条" for t, c in type_stats.items())
            type_suffix = f"\n四维分布:\n{type_lines}" if type_lines else ''
            return (
                f"**记忆池状态**\n"
                f"总消息: {stats['total_messages']}\n"
                f"蒸馏产物: {stats['distilled']}\n"
                f"平均质量分: {stats['avg_importance']}\n"
                f"用户数: {stats['users']}"
                f"{type_suffix}"
            )

        def _cmd_memory_add(agent, msg, args):
            if not agent_self.memory:
                return "记忆引擎未安装"
            if len(args) < 2:
                return (
                    "用法: `/memory_add <type> <内容>`\n"
                    "type: concept | event | preference | troubleshooting\n"
                    "示例: `/memory_add troubleshooting 钉钉Stream必须勾选后台开关并发布才能生效`"
                )
            mem_type = args[0]
            if mem_type not in ('concept', 'event', 'preference', 'troubleshooting'):
                return f"未知类型 {mem_type}。可选: concept, event, preference, troubleshooting"
            content = msg.text[len('/memory_add ') + len(mem_type) + 1:]
            mid = agent_self.memory.force_remember(
                msg.session_key, '', content, memory_type=mem_type
            )
            return f"已存入 [{mem_type}] 记忆池 (id:{mid})"

        def _cmd_memory_persona(agent, msg, args):
            if not agent_self.memory:
                return "记忆引擎未安装"
            if not args:
                content = agent_self.memory.persona_content()
                pending = agent_self.memory.persona_pending()
                if not content:
                    return "(persona.md 还没生成。lite-agent 启动 5 分钟后会跑首次蒸馏。)"
                preview = content if len(content) < 1800 else content[:1700] + '\n...(已截断，VPS 完整文件: /root/lite_agent/data/persona.md)'
                if pending:
                    pending_lines = '\n'.join(f"  {i+1}. {p.lstrip('- ').strip()}" for i, p in enumerate(pending))
                    preview += f"\n\n---\n📋 待确认条目（用 `/memory_persona confirm <序号>` 升格）：\n{pending_lines}"
                return preview

            sub = args[0].lower()
            if sub == "confirm":
                if len(args) < 2 or not args[1].isdigit():
                    return (
                        "用法: `/memory_persona confirm <序号> [分类]`\n"
                        "分类可选: 身份与角色 / 工作偏好 / 技术栈熟练度 / 当前进行中项目 / 已知决策 / 个人事实\n"
                        "默认升格到 `工作偏好`。\n"
                        "先用 `/memory_persona` 看待确认条目编号。"
                    )
                idx = int(args[1])
                section_short = ' '.join(args[2:]).strip() if len(args) >= 3 else '工作偏好'
                target_section = '## ' + section_short
                moved = agent_self.memory.persona_confirm(idx, target_section=target_section)
                if not moved:
                    return f"升格失败：序号 {idx} 越界或分类「{section_short}」不存在。\n用 `/memory_persona` 看当前待确认列表。"
                return f"已将下面这条移入 **{section_short}** / 手动校正:\n\n{moved}"
            return "未知子命令。可用: `/memory_persona` (查看), `/memory_persona confirm <序号>` (升格)"

        _registry.register('/memory_stats', _cmd_memory_stats,
                           category='记忆管理', description='查看记忆池统计信息',
                           show_in_dashboard=True, guest_ok=False)
        _registry.register('/memory_add', _cmd_memory_add,
                           category='记忆管理', description='手动添加记忆条目',
                           show_in_dashboard=False, guest_ok=False)
        _registry.register('/memory_persona', _cmd_memory_persona,
                           category='记忆管理', description='查看/管理个人画像',
                           show_in_dashboard=False, guest_ok=False)

        # 定时任务引擎
        self.cron = CronManager()

        # 系统提示词
        self.system_prompt = self._build_system_prompt()

    def cleanup_locks(self, expired_keys: list):
        """清理已过期会话的互斥锁，防止内存泄漏"""
        with self._session_locks_guard:
            for key in expired_keys:
                lock = self._session_locks.get(key)
                if lock and not lock.locked():
                    self._session_locks.pop(key, None)

    def broadcast(self, response: AgentResponse):
        """将消息广播到所有挂载的通道"""
        for ch in self.channels:
            try:
                ch_response = AgentResponse(
                    text=self._prepare_output(response.text, ch.name),
                    title=response.title,
                    color=response.color,
                    task_id=response.task_id
                )
                ch.broadcast(ch_response)
            except Exception as e:
                print(f"❌ 通道 {ch.name} 广播异常: {e}")

    def _build_system_prompt(self, is_guest: bool = False, skill_names: list = None,
                             read_only_mode: bool = False) -> str:
        """构建系统提示词 (包含技能列表)"""
        now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)")
        if skill_names is not None:
            skills_summary = self.skill_engine.list_skills_filtered(skill_names)
        else:
            skills_summary = self.skill_engine.list_skills(is_guest=is_guest)
        project_root = self._config.get("project_root", "/root/lite_agent")

        extra_hint = ""
        if skill_names:
            extra_hint = (
                "\n\n⚠️ 当前仅为你注入了相关领域的工具。"
                "如果用户的需求超出当前可用工具，请在回复开头包含 [TOOLSET_MISS] 标记，"
                "并说明缺失的能力。"
            )
        if read_only_mode:
            extra_hint += (
                "\n\n🔒 当前为只读模式：未注入修改/推送/同步类工具。"
                "如需此类操作，请用户明确表达意图后再请求。"
            )

        if is_guest:
            base_prompt = f"""你是 {self.bot_name}，一个运行在 Linux VPS 上的私人智能助手。
【当前系统时间】: `{now_str}`
目前你正在与普通访客（非管理员）对话。

你的职责:
1. 理解用户的自然语言请求，调用合适的工具来完成任务。
2. 你只被授予了访问基础公开查询和网页剪藏工具的权限。任何涉及 VPS 系统管理、本地文件操作、命令执行、配置编辑或管理员专属功能的敏感请求，你都必须礼貌地予以拒绝（声明无权限）。
3. 如果用户只是闲聊或提问，正常回复即可，不必强行调用工具。

可用工具:
{skills_summary}

注意事项:
- 回复使用 Markdown 格式
- 涉及数据时用表格或列表展示
- 不要编造数据，一切以工具返回的真实结果为准。对于任何你不确定的时效性事实、数据、新闻，你必须调用 `web_search` 进行联网搜索，严禁凭预训练记忆编造数据。
- 如果工具返回错误，向用户解释原因。如果提示中包含学到的偏好，优先遵循。"""
        else:
            base_prompt = f"""你是 {self.bot_name}，一个运行在 Linux VPS 上的私人智能助手。

【系统环境自知】:
- 当前系统精确绝对时间: `{now_str}`
- 你的源代码工作区位于: `{project_root}`
- 你的 Systemd 后台守护进程名为: `{self.svc_name}`
- 当用户要求你拉取代码、Review 本地代码或重启系统时，请直接在上述路径和进程名上进行操作。

你的职责:
1. 理解用户的自然语言请求，调用合适的工具来完成任务
2. 如果用户描述了一个需要多步骤才能完成的明确目标，先以 🎯 开头确认目标，主动切换到任务模式
3. 任务模式下逐步执行并汇报进展，每一步检查是否接近目标
4. 执行完毕后，用简洁的中文给出结论和建议
5. 如果用户只是闲聊或提问，正常回复即可，不必强行调用工具

可用工具:
{skills_summary}

注意事项:
- 回复使用 Markdown 格式
- 涉及数据时用表格或列表展示
- 发现异常时主动提醒并给出建议
- 不要编造数据，一切以工具返回的真实结果为准。在生成最终答复时，必须完全忠实于工具返回的内容，严禁添加任何未查询到的虚假数据。对于任何你不确定的时效性事实、数据、新闻，你必须调用 `web_search` 进行联网搜索，严禁凭预训练记忆编造数据。
- 如果系统提示中包含「从历史纠正中学到的工具/行为偏好」，在选工具前先检查是否匹配，匹配则严格遵循——不要重蹈用户已经纠正过的错误。
- 选择工具的优先级：(1) 历史纠正中学到的偏好 (2) 领域专用工具（先搜索工具列表，找到语义匹配的专用工具直接调用）(3) ops_workspace_run 仅作最后手段。ops_workspace_run 每一步都要 LLM 生成代码→执行→读结果，成本高、步骤多、Token 消耗大；如果发现自己已经用 ops_workspace_run 走了 3 步以上仍未完成，说明大概率有专用工具被忽略了，应重新审视工具列表。
- **回答排版原则**：在回复工具调用结果时，请直接给出精炼的结构化结论、表格或建议，**严禁在最终回答正文中原样复制/复读工具返回的中间原始步骤与命令行日志**（例如 "--- 步骤1: ... ---" 等细节），中间原始日志已由系统的执行过程日志框独立承载。
- 如果工具返回错误，向用户解释原因并建议解决方案"""

        return base_prompt + extra_hint

    def _full_tools_and_names(self, msg: IncomingMessage):
        """Return the pre-selector tool set; shared by default, shadow and fallback paths."""
        tools = (self.skill_engine.get_guest_schemas() if msg.is_guest
                 else self.skill_engine.get_all_schemas())
        return tools, None

    def _select_request_tools(self, msg: IncomingMessage, history: list):
        """Apply selector flags without changing the default or shadow tool injection."""
        selector_enabled = os.environ.get("LITE_AGENT_SELECTOR_ENABLED") == "1"
        selector_shadow = os.environ.get("LITE_AGENT_SELECTOR_SHADOW") == "1"
        try:
            candidate = self.request_selector.select(
                text=msg.text,
                history=history,
                is_guest=msg.is_guest,
            )
        except Exception as exc:
            print(f"⚠️ [RequestSelector] 单请求回退全量: {type(exc).__name__}")
            candidate = None

        selector_result = None
        if selector_enabled and candidate is not None:
            selector_result = candidate
            if candidate.names is None:
                tools, system_names = self._full_tools_and_names(msg)
            elif not candidate.names:
                tools, system_names = [], []
            else:
                tools = self.skill_engine.get_schemas_by_names(candidate.names)
                system_names = candidate.names
        else:
            tools, system_names = self._full_tools_and_names(msg)
            if selector_shadow and candidate is not None:
                selected_count = len(candidate.names) if candidate.names is not None else None
                print(
                    f"[Selector-Shadow] domains={candidate.domains}, names={selected_count}, "
                    f"confidence={candidate.confidence}, read_only={candidate.read_only_mode}"
                )
        return tools, system_names, selector_result

    # ------------------------------------------------------------------
    #  消息入口
    # ------------------------------------------------------------------
    def _get_session_lock(self, session_key: str) -> threading.Lock:
        """获取/创建一个 session_key 专属的锁; 双重检查锁定避免每次都加 guard"""
        lock = self._session_locks.get(session_key)
        if lock is None:
            with self._session_locks_guard:
                lock = self._session_locks.get(session_key)
                if lock is None:
                    lock = threading.Lock()
                    self._session_locks[session_key] = lock
        return lock

    def handle(self, msg: IncomingMessage) -> AgentResponse:
        """处理一条用户消息，返回 AgentResponse

        同一 session_key 的消息串行处理: 防止并发线程同时写 messages 表
        造成 assistant tool_calls 与 tool 消息错位 (导致 LLM API 400 错误)。
        不同 session_key (不同用户/会话) 之间不互斥, 仍可并行。
        """
        if not msg._session_key:
            msg.bind_session(self.session_mgr.resolve_active_session(msg.scope_key))

        directive_mode, cleaned_text = self._extract_output_mode(msg.text)
        if directive_mode:
            msg.output_mode = directive_mode
            msg.text = cleaned_text

        lock = self._get_session_lock(msg.session_key)
        wait_start = time.time()
        with lock:
            wait_ms = (time.time() - wait_start) * 1000
            if wait_ms > 100:
                print(f"  ⏳ 会话锁等待 {wait_ms:.0f}ms session={msg.session_key}")
            response = self._handle_locked(msg)

        # External delivery remains outside the session lock. Async task
        # acknowledgements are left untouched; their final result is handled by
        # _push_result with the same request-level output mode.
        if not response.task_id:
            overrides = {"full_delivery": msg.output_mode} if msg.output_mode else None
            response.text = self._prepare_output(
                response.text, msg.channel, overrides=overrides,
                title=response.title, session_key=msg.session_key,
            )
        return response

    @staticmethod
    def _extract_output_mode(text: str) -> tuple[str, str]:
        """Read one portable request directive without teaching model code UI syntax."""
        match = re.search(
            r"\[output=(auto|email|hedgedoc|sqlite|store|inline)\]",
            text, flags=re.IGNORECASE,
        )
        if match:
            mode = match.group(1).lower()
            mode = "sqlite" if mode == "store" else mode
            return mode, (text[:match.start()] + text[match.end():]).strip()
        lowered = text.lower()
        if "完整回复发到邮件" in text or "完整内容发送到邮件" in text:
            return "email", text
        if "发送到hedgedoc" in lowered or "上传到hedgedoc" in lowered:
            return "hedgedoc", text
        return "", text

    def _handle_locked(self, msg: IncomingMessage) -> AgentResponse:
        """实际处理逻辑, 调用方必须已持有 session 锁"""
        text = msg.text.strip()

        session = self.session_mgr.get_or_create(msg.session_key)

        # 标题自动生成 (两阶段：阶段1即时截取，阶段2后台LLM精炼)
        if not session.title and text and not text.startswith("/") and not text.startswith("::"):
            initial_title = text.replace("\n", " ").strip()[:20]
            self.session_mgr.set_title(msg.session_key, initial_title)
            self._async_refine_title(msg.session_key, text)

        if text.startswith("::"):
            response = self._handle_double_colon(msg)
        elif msg.text.startswith("/"):
            response = self._handle_builtin(msg)
        elif msg.is_guest:
            print(f"  [ROUTE] 访客消息 → 走同步AI Loop (已限制工具权限): {text[:60]}")
            response = self._run_ai_loop(msg)
        elif self._is_complex_task(text):
            print(f"  [ROUTE] 复杂任务检测命中 → 走多Agent编排: {text[:60]}")
            response = self._run_orchestrated(msg)
        else:
            print(f"  [ROUTE] 简单任务 → 走同步AI Loop: {text[:60]}")
            response = self._run_ai_loop(msg)

        return response

    def _async_refine_title(self, session_key: str, text: str):
        def _task():
            try:
                prompt = f"请用 10 个字以内精炼概括用户的这句提问，不要包含标点符号、引号或多余前缀。提问内容：{text[:200]}"
                res = self.llm.invoke_sync(
                    [{"role": "user", "content": prompt}],
                    invoker=self.model_invoker, role="title_refiner",
                    provider=self.model_driver, session_key=session_key,
                    max_tokens=30,
                    timeout=5.0,
                )
                if res and res["content"]:
                    cleaned = res["content"].strip().strip('"\'“”')
                    if cleaned:
                        self.session_mgr.set_title(session_key, cleaned[:20])
            except Exception as e:
                import sys
                print(f"  ⚠️ [TitleRefine] 标题精炼异常: {e}", file=sys.stderr)
        threading.Thread(target=_task, daemon=True, name=f"TitleRefinement-{session_key[:8]}").start()

    def _summarize_output(self, text: str, max_chars: int,
                          session_key: str = "") -> str:
        cfg = self._config.get("output_delivery", {}) or {}
        model = str((cfg.get("long_output", {}) or {}).get("summary_model") or "")
        if not model:
            return ""
        router = self._get_request_model_router()
        invoker = router.get_invoker(model, max_tokens=512)
        if invoker is None:
            return ""
        result = self.llm.invoke_sync(
            [
                {
                    "role": "system",
                    "content": (
                        "你只负责压缩完整回复。忠实保留结论、关键数字、风险和下一步；"
                        "不要补充原文没有的信息，不要提及自己在总结。"
                    ),
                },
                {"role": "user", "content": text},
            ],
            invoker=invoker, role="output_summarizer",
            provider=router.get_driver(model),
            session_key=session_key or "system:output_delivery",
            max_tokens=512,
        )
        return str(result.get("content") or "")[:max_chars]

    def _prepare_output(self, text: str, channel: str, overrides: dict = None,
                        title: str = "", session_key: str = "") -> str:
        """Apply the shared output policy before channel transport."""
        return prepare_channel_output(
            text, channel, self._config, overrides=overrides, title=title,
            summarize=lambda value, limit: self._summarize_output(
                value, limit, session_key=session_key
            ),
        )

    # ------------------------------------------------------------------
    #  内置指令
    # ------------------------------------------------------------------
    def _handle_builtin(self, msg: IncomingMessage) -> AgentResponse:
        """处理 /new /status /history /stop /help 等内置指令"""
        parts = msg.text.strip().split()
        cmd = parts[0].lower()
        args = parts[1:]

        is_guest = getattr(msg, "is_guest", True)

        # 权限检查优先走注册表
        perm_denied = _registry.check_permission(cmd, is_guest)
        if perm_denied:
            return AgentResponse(perm_denied, title="⚠️ 权限不足", color="red")

        # 注册表未覆盖的敏感指令兜底
        if is_guest and not cmd.startswith('/mail_') and cmd in ("/cmd", "/balance", "/cron", "/check"):
            return AgentResponse("❌ 权限不足：只有管理员可使用该指令", title="⚠️ 权限不足", color="red")
 
 
        # 优先查注册表（@slash_command 装饰器注册的指令）
        try:
            reg_resp = _registry_dispatch(cmd, self, msg, args)
        except Exception as e:
            return AgentResponse(f"指令执行异常: {e}", title="❌ 错误", color="red")
        if reg_resp is not None:
            if isinstance(reg_resp, str):
                return AgentResponse(reg_resp, title="执行结果", color="blue")
            return reg_resp
 
        if cmd == "/new":
            new_key = self.session_mgr.reset_session(
                msg.session_key, scope_key=msg.scope_key
            )
            msg.bind_session(new_key)
            return AgentResponse(
                f"🔄 已开启新会话\n\n会话 ID：`{new_key}`",
                title="新会话", color="green", new_session_key=new_key,
            )

        if cmd == "/status":
            info = self.session_mgr.get_session_info(msg.session_key)
            lines = [
                f"**状态:** {info['status']}",
                f"**消息数:** {info['message_count']}",
                f"**工具调用:** {info['tool_calls']} 次",
                f"**Token 消耗:** {info['token_usage']}",
            ]
            if info.get("goal"):
                lines.insert(0, f"**当前目标:** {info['goal']}")
            return AgentResponse("\n".join(lines), title="📊 会话状态", color="violet")

        if cmd == "/history":
            session = self.session_mgr.get_or_create(msg.session_key)
            recent = [m for m in session.messages[-10:] if m["role"] in ("user", "assistant")]
            if not recent:
                return AgentResponse("暂无对话记录", title="📜 历史", color="grey")
            lines = []
            for m in recent:
                prefix = "👤" if m["role"] == "user" else "🤖"
                content = m["content"][:100]
                if len(m["content"]) > 100:
                    content += "..."
                lines.append(f"{prefix} {content}")
            return AgentResponse("\n".join(lines), title="📜 最近对话", color="blue")

        if cmd == "/stop":
            session = self.session_mgr.get_or_create(msg.session_key)
            if session.status == "working":
                self.session_mgr.mark_done(msg.session_key, "用户主动终止")
                return AgentResponse("⏹️ 当前任务已终止", title="任务终止", color="orange")
            return AgentResponse("当前没有正在执行的任务", title="提示", color="grey")

        if cmd == "/ai":
            # 飞书有些场景（如群组配置）可能只允许 / 开头的命令
            # 提供 /ai 指令来强行传递自然语言给大模型
            msg.text = msg.text[3:].strip()
            if not msg.text:
                return AgentResponse("请在 /ai 后面输入您想对 AI 说的话，例如：/ai 查一下系统负载", title="提示", color="grey")
            return self._run_ai_loop(msg)

        if cmd == "/cmd":
            args = parts[1:]
            if not args:
                return AgentResponse("请提供具体账单指令，例如：`/cmd report 3`\n可用命令: report, due_soon_bills, reconcile, recent, fetch (结合了exec和validate)", title="提示", color="grey")
            try:
                import sys, os
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from skills.ops_billing import _run_billing_cmd, billing_fetch
                
                if args[0] == "fetch":
                    months = int(args[1]) if len(args) > 1 else 1
                    result = billing_fetch(months)
                elif args[0] in ("fetch_summaries", "fs"):
                    # 邮件 LLM 摘要走 mail_fetch_summaries (解析 JSON_PUSH)
                    months = int(args[1]) if len(args) > 1 else 1
                    from skills.ops_mail_reader import mail_fetch_summaries
                    result = mail_fetch_summaries(months=months)
                else:
                    result = _run_billing_cmd(args)
                return AgentResponse(result, title=f"执行结果: {args[0]}", color="blue")
            except Exception as e:
                return AgentResponse(f"执行失败: {e}", title="错误", color="red")

        if cmd == "/cron":
            if not args:
                return AgentResponse(self.cron.list_jobs(), title="📅 定时任务", color="blue")
            if args[0] == "toggle" and len(args) > 1:
                try:
                    job_id = int(args[1])
                except ValueError:
                    return AgentResponse("序号必须是数字", title="⚠️", color="red")
                return AgentResponse(self.cron.toggle_job(job_id), title="📅 定时任务", color="blue")
            # /cron <序号> → 手动执行
            try:
                job_id = int(args[0])
            except ValueError:
                return AgentResponse(
                    "用法:\n`/cron` — 列出所有任务\n`/cron <序号>` — 手动执行\n`/cron toggle <序号>` — 开启/暂停",
                    title="📅 定时任务", color="grey"
                )
            return AgentResponse(self.cron.run_job_manually(job_id), title="🚀 手动执行", color="green")

        if cmd == "/check":
            try:
                import sys, os
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from skills.ops_self_check import _get_health_report
                return AgentResponse(_get_health_report(), title="🏥 健康检查", color="green")
            except Exception as e:
                return AgentResponse(f"自检失败: {e}", title="⚠️", color="red")

        if cmd == "/help":
            if msg.is_guest:
                skills_list = self.skill_engine.list_skills(is_guest=True)
                help_text = f"""**内置指令:**
`/new` - 重置会话
`/status` - 查看会话状态
`/history` - 查看最近对话
`/stop` - 终止当前任务
`/help` - 显示帮助
`/ai` - 强行调用 AI（例如：`/ai 网页剪藏 https://example.com`）

**任务模式 (双冒号指令):**
`::goal <目标描述>` - 设定查询目标，AI 逐步执行，上下文不截断
`::goal` - 查看当前目标状态与进度
`::goal done` - 手动标记目标完成并归档

**可用工具:**
{skills_list}

💡 **提示:** 直接用自然语言告诉我你想做什么
例如: "帮我剪藏这个网页" / "帮我查询相关的公开数据"
如果查询步骤较多，可以先用 `::goal` 锁定目标"""
            else:
                skills_list = self.skill_engine.list_skills(is_guest=False)
                help_text = f"""**内置指令:**
`/new` - 重置会话
`/status` - 查看会话状态
`/history` - 查看最近对话
`/stop` - 终止当前任务
`/help` - 显示帮助
`/balance` - 查询大模型账户余额
`/memory_stats` - 查看记忆池状态
`/memory_persona` - 查看个人画像 / `confirm <序号>` 升格待确认条目
`/cron` - 查看定时任务列表，`/cron <序号>` 手动执行，`/cron toggle <序号>` 开关
`/check` - 执行全方位健康自检，检查系统各模块状态
`/ai` - 强行调用 AI（适用于飞书只能接收命令的场景，如 `/ai 查一下账单`）
`/cmd` - 精确执行账单旧版指令（不经过 AI，如 `/cmd report 3` 或 `/cmd fetch`）

**任务模式 (双冒号指令):**
`::goal <目标描述>` - 设定任务目标，AI 进入 working 模式，上下文不截断
`::goal` - 查看当前目标状态与进度
`::goal done` - 手动标记目标完成并归档

**AI 技能 (直接用自然语言描述即可):**
{skills_list}

💡 **提示:** 直接用自然语言告诉我你想做什么
例如: "帮我看看系统状态" / "查一下有没有异常登录" / "看看证书还有多久过期"
复杂任务可以先用 `::goal` 锁定目标避免上下文丢失"""
            return AgentResponse(help_text, title="📖 帮助", color="turquoise")

        # 未知 / 指令也交给 AI
        return self._run_ai_loop(msg)

    # ------------------------------------------------------------------
    #  双冒号指令 (绕过飞书/钉钉斜杠拦截)
    # ------------------------------------------------------------------
    def _handle_double_colon(self, msg: IncomingMessage) -> AgentResponse:
        text = msg.text.strip()[2:].strip()
        parts = text.split()
        cmd = parts[0].lower() if parts else ""
        args_list = parts[1:]                            # list, 对齐 _handle_builtin 传给注册 handler
        args = " ".join(args_list) if args_list else ""  # string, 框架指令(goal/cron)用

        # 优先查注册表 (@slash_command 注册的指令; ::xxx 映射到 /xxx)
        reg_cmd = cmd if cmd.startswith("/") else "/" + cmd
        is_guest = getattr(msg, "is_guest", True)
        perm_denied = _registry.check_permission(reg_cmd, is_guest)
        if perm_denied:
            return AgentResponse(perm_denied, title="⚠️ 权限不足", color="red")
        try:
            reg_resp = _registry_dispatch(reg_cmd, self, msg, args_list)
        except Exception as e:
            return AgentResponse(f"指令执行异常: {e}", title="❌ 错误", color="red")
        if reg_resp is not None:
            if isinstance(reg_resp, str):
                return AgentResponse(reg_resp, title="执行结果", color="blue")
            return reg_resp

        # 以下为未迁注册表的框架指令 (goal/cron)
        if cmd in ("goal", "/goal"):
            if not args:
                session = self.session_mgr.get_or_create(msg.session_key)
                if session.goal:
                    return AgentResponse(
                        f"🎯 **当前目标:** {session.goal}\n"
                        f"**状态:** {session.status} | **步骤:** {session.tool_calls}/{self.max_steps}\n"
                        f"发送 `::goal <新描述>` 更换目标，`::goal done` 标记完成",
                        title="🎯 目标状态", color="blue"
                    )
                return AgentResponse(
                    "当前没有进行中的目标。\n"
                    "用法: `::goal <目标描述>` — 开始新任务\n"
                    "　　　`::goal done` — 标记完成",
                    title="提示", color="grey"
                )

            if args.lower() == "done":
                session = self.session_mgr.get_or_create(msg.session_key)
                if session.goal:
                    goal_text = session.goal
                    self.session_mgr.mark_done(msg.session_key, "用户手动标记完成")
                    return AgentResponse(
                        f"✅ 目标已完成并归档: **{goal_text}**",
                        title="目标完成", color="green"
                    )
                return AgentResponse("当前没有进行中的目标", title="提示", color="grey")

            self.session_mgr.set_goal(msg.session_key, args)
            msg.text = args
            return self._run_ai_loop(msg)

        if cmd == "cron" and args == "log":
            if is_guest:
                return AgentResponse("❌ 权限不足：只有管理员可使用该指令", title="⚠️ 权限不足", color="red")
            import subprocess
            r = subprocess.run(
                f"journalctl -u {self.svc_name} --since '24 hours ago' --no-pager | grep '定时任务' | tail -30",
                shell=True, capture_output=True, text=True, timeout=10
            )
            text = r.stdout.strip() or r.stderr.strip() or '(无日志)'
            if len(text) > 2500:
                text = text[-2500:]
            return AgentResponse(text, title='📋 定时任务日志', color='turquoise')

        return AgentResponse(
            f"未知指令 `::{cmd}`。可用: `::goal <描述>` / `/rss_list [分组]` / `/rss_topic <标签>`",
            title="⚠️", color="red"
        )

    # ------------------------------------------------------------------
    #  复杂任务检测 + 编排路由
    # ------------------------------------------------------------------
    @staticmethod
    def _is_complex_task(text: str) -> bool:
        if len(text) < 8:
            return False
        keywords = [
            "分析并", "整理并", "检查所有", "批量", "对比",
            "生成报表", "全面检查", "逐一", "遍历", "排查",
            "巡视", "巡检", "汇总", "统计并", "扫描",
        ]
        return any(kw in text for kw in keywords)

    def _extract_model_override(self, text: str):
        """Return a configured model key explicitly requested by the user.

        Supported forms are deterministic: ``[model=name]`` and natural
        ``用/使用 name`` with spaces accepted in place of hyphens.
        """
        models = (self._config.get("llm", {}) or {}).get("models", {}) or {}
        bracket = re.search(r"\[model=([^\]]+)\]", text or "", re.IGNORECASE)
        if bracket:
            requested = bracket.group(1).strip()
            if requested not in models:
                raise ValueError(f"未配置模型: {requested}")
            return requested
        lowered = (text or "").lower()
        for name in sorted(models, key=len, reverse=True):
            alias = re.escape(name.lower()).replace(r"\-", r"[\s_-]+")
            if re.search(rf"(?:用|使用|指定)\s*{alias}(?:\s|来|去|，|,|$)", lowered):
                return name
        return None

    def _get_request_model_router(self):
        router = self._request_model_router
        if router is None:
            with self._request_model_router_lock:
                router = self._request_model_router
                if router is None:
                    from core.model_router import ModelRouter
                    router = ModelRouter(self._config)
                    self._request_model_router = router
        return router



    def _run_orchestrated(self, msg: IncomingMessage) -> AgentResponse:
        from core.task_orchestrator import TaskOrchestrator
        import uuid
        import re

        # 解析用户指定的步数覆盖 [steps=N], 如 "备份日志 [steps=50]"
        goal = msg.text
        step_override = None
        m = re.search(r'\[steps=(\d+)\]', goal)
        if m:
            step_override = int(m.group(1))
            goal = re.sub(r'\s*\[steps=\d+\]', '', goal).strip()
            print(f"  [ORCH] 用户指定步数覆盖: {step_override}")

        print(f"  [ORCH] 启动编排引擎 session={msg.session_key} task_len={len(goal)}")

        orch = TaskOrchestrator(
            config=self._config,
            skill_engine=self.skill_engine,
            session_mgr=self.session_mgr,
            channels=self.channels,
            ledger=self.ledger,
        )

        task_id = uuid.uuid4().hex[:8]

        def _bg_run():
            try:
                print(f"  [ORCH] 后台线程开始执行 session={msg.session_key} task_id={task_id}")
                result = orch.execute(
                    goal=goal,
                    session_key=msg.session_key,
                    progress_callback=self._on_subtask_progress(msg),
                    task_id=task_id,
                    step_override=step_override,
                )
                print(f"  [ORCH] 后台线程执行完成 session={msg.session_key} result_len={len(result)}")
                self._push_result(msg, result)
            except Exception as e:
                print(f"  [ORCH] 后台线程异常 session={msg.session_key}: {e}")
                traceback.print_exc()
                self._push_result(msg, f"❌ 编排执行异常: {e}")

        threading.Thread(target=_bg_run, daemon=True, name=f"Orch-{msg.session_key}").start()

        print(f"  [ORCH] 已返回受理回执 session={msg.session_key}")
        return AgentResponse(
            "🎯 复杂任务已受理，正在拆解并行执行中...\n"
            "完成后将自动推送结果，请稍候。",
            title="🤖 多Agent编排", color="blue", task_id=task_id
        )

    def _on_subtask_progress(self, msg):
        def callback(progress: dict):
            text = (
                f"📊 进度: {progress['done']}/{progress['total']} 完成"
            )
            if progress.get("failed", 0) > 0:
                text += f", {progress['failed']} 失败"
            if progress.get("running", 0) > 0:
                text += f", {progress['running']} 执行中"
            for ch in self.channels:
                # 进度条属于交互过程，只对发起的来源通道可见，绝不跨通道广播
                if ch.name != msg.channel:
                    continue
                try:
                    # 优先用 push_progress (携带完整 msg 以免除上下文丢失)
                    if hasattr(ch, 'push_progress') and ch.push_progress(msg, text):
                        continue
                    if hasattr(ch, 'send_progress'):
                        ch.send_progress(msg.message_id, text)
                except Exception as e:
                    print(f"  ⚠️ [{ch.name}] 进度发送失败: {e}")
        return callback

    def _push_result(self, msg, result: str):
        overrides = {"full_delivery": msg.output_mode} if msg.output_mode else None
        prepared_result = self._prepare_output(
            result, msg.channel, overrides=overrides,
            title="多Agent执行报告", session_key=msg.session_key,
        )
        response = AgentResponse(prepared_result, title="🤖 多Agent执行报告", color="blue")
        for ch in self.channels:
            if msg.notify_channels is not None and ch.name not in msg.notify_channels:
                continue
            try:
                # 优先用 push_result (携带 channel_payload, 解决异步推送上下文丢失)
                if hasattr(ch, 'push_result'):
                    if ch.push_result(msg, response):
                        continue
                if hasattr(ch, 'send_to'):
                    ch.send_to(msg.chat_id, response)
                elif hasattr(ch, 'send_response'):
                    ch.send_response(msg.message_id, response)
            except Exception as e:
                print(f"  ⚠️ 推送结果失败 [{ch.name}]: {e}")

    # ------------------------------------------------------------------
    #  核心 AI 循环
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_messages(messages: list) -> list:
        pending_tool_call_ids = set()
        valid = []
        for m in messages:
            if m["role"] == "assistant" and "tool_calls" in m:
                for tc in m["tool_calls"]:
                    pending_tool_call_ids.add(tc["id"])
            if m["role"] == "tool":
                tid = m.get("tool_call_id", "")
                if tid not in pending_tool_call_ids:
                    continue
                pending_tool_call_ids.discard(tid)
            valid.append(m)
        return valid

    def handle_stream(self, msg: IncomingMessage):
        """内部控制台 SSE 流式入口。
        AI Loop 路径真流式; 指令 (::/)/编排等路径降级为单次 token 事件,
        保持与 handle() 一致的路由语义。全程持 session 锁 (锁拆分留待后续 PR)。"""
        lock = self._get_session_lock(msg.session_key)
        with lock:
            text = msg.text.strip()
            if text.startswith("::"):
                yield from self._wrap_sync_response(self._handle_double_colon(msg))
                return
            if text.startswith("/"):
                yield from self._wrap_sync_response(self._handle_builtin(msg))
                return
            if not msg.is_guest and self._is_complex_task(text):
                # 复杂任务: 仅流式回执, 真实结果仍由 _push_result 异步推送 (路径 C 不做 token 流)
                yield from self._wrap_sync_response(self._run_orchestrated(msg))
                return
            yield from self._stream_ai_loop(msg)

    def _wrap_sync_response(self, resp):
        """把非流式 AgentResponse 包成单次 token + done 事件, 供 handle_stream 降级使用。"""
        empty_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated": False}
        if resp is None:
            yield {"type": "token", "delta": ""}
        else:
            yield {"type": "token", "delta": resp.text}
        yield {"type": "done", "usage": empty_usage}

    def _stream_ai_loop(self, msg: IncomingMessage):
        """流式 AI Loop 生成器 (基于 AgentRuntime)。

        边界划分：
          Runtime 负责: 模型循环、工具执行、死循环检测、max_steps/token 预算熔断
          Agent  负责: Session 持久化、Memory 回调、日额度、动态超时、通道事件转换

        yield 统一事件 dict, 供 handle_stream(SSE) 与 _run_ai_loop(兼容老接口) 共用。
        事件类型: token / reasoning_token / tool_start / tool_result / done / error
        """
        session = self.session_mgr.get_or_create(msg.session_key)
        self.session_mgr.add_message(msg.session_key, "user", msg.text)

        request_invoker = self.model_invoker
        request_model = self.model
        request_driver = self.model_driver
        request_max_tokens = self.max_tokens
        request_runtime = self.runtime
        request_stream = not is_gemini_driver(request_driver)
        override = None
        if not msg.is_guest:
            try:
                override = self._extract_model_override(msg.text)
            except ValueError as exc:
                yield {"type": "error", "msg": f"❌ {exc}"}
                yield {"type": "done", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated": False}}
                return
            selected_model = override or str(
                (self._config.get("task_routing", {}) or {}).get("simple_model") or ""
            )
            if selected_model:
                router = self._get_request_model_router()
                request_invoker = router.get_invoker(selected_model)
                if request_invoker is None:
                    yield {"type": "error", "msg": f"❌ 模型当前不可用: {selected_model}"}
                    yield {"type": "done", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated": False}}
                    return
                cfg = router.models_cfg.get(selected_model, {})
                request_model = request_invoker.model_name
                request_driver = router.get_driver(selected_model)
                request_max_tokens = cfg.get("max_tokens", self.max_tokens)
                request_stream = not is_gemini_driver(request_driver)
                request_runtime = AgentRuntime(
                    model_invoker=request_invoker,
                    skill_engine=self.skill_engine,
                    max_steps=self.max_steps,
                    max_tokens=request_max_tokens,
                    max_retries=1,
                    non_retryable_exceptions=_NON_RETRYABLE_EXCEPTIONS,
                )
                label = "MODEL OVERRIDE" if override else "COST ROUTE"
                print(f"  🎛️ [{label}] {selected_model} -> {request_model}")

        # ---- 构建工具集与 guard prompts ----
        history = self.session_mgr.get_history(msg.session_key)
        tools, system_names, selector_result = self._select_request_tools(msg, history)
        allowed_tools = (
            frozenset(s["function"]["name"] for s in tools)
            if msg.is_guest else None
        )

        guard_prompts = self.skill_engine.get_guard_prompts(msg.text, is_guest=msg.is_guest)

        # ---- 构建初始消息 (system + history) ----
        system_content = self._build_system_prompt(
            is_guest=msg.is_guest,
            skill_names=system_names,
            read_only_mode=bool(selector_result and selector_result.read_only_mode),
        )
        if override:
            system_content += (
                f"\n\n用户已显式指定本次使用模型 `{override}`，必须尊重该选择。"
                "如果你能高置信判断低成本模型完全胜任，可在最终答复末尾给出一句非阻断的成本建议；"
                "不得自行更换本次模型，也不要要求用户再次确认后才开始。"
            )
        if guard_prompts:
            system_content += "\n\n⚠️【数据忠实执行指令】:\n" + "\n".join(f"- {p}" for p in guard_prompts)
        if self.memory:
            memory_ctx = self.memory.before_reply(msg.session_key, msg.text)
            if memory_ctx:
                system_content += memory_ctx

        messages = [{"role": "system", "content": system_content}]
        messages.extend(history)
        messages = self._validate_messages(messages)

        # ---- 日额度拦截 ----
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated": False}
        if session.token_usage >= self.daily_token_limit:
            warning = f"⚠️ 今日 Token 已达上限 ({self.daily_token_limit})，请明天再试\n当前累计: {session.token_usage}"
            self.session_mgr.add_message(msg.session_key, "assistant", warning)
            yield {"type": "error", "msg": warning}
            yield {"type": "done", "usage": total_usage}
            return

        # ---- 动态超时 (Agent 负责，不进 Runtime) ----
        is_pro = "pro" in request_model.lower() or "reasoner" in request_model.lower()
        dynamic_timeout = 180.0 if is_pro else 45.0
        total_chars = sum(
            len(m["content"]) if isinstance(m.get("content"), str) else 0
            for m in messages
        )
        dynamic_timeout += (total_chars // 10000) * 5.0
        dynamic_timeout = min(dynamic_timeout, 300.0)

        # ---- 执行上下文 ----
        ctx = ExecutionContext(
            actor_id=msg.user_id,
            actor_type=ActorType.GUEST if msg.is_guest else ActorType.USER,
            source=ExecutionSource.STREAM,
            allowed_tools=allowed_tools,
            session_key=msg.session_key,
            max_steps=self.max_steps,
            max_output_tokens=request_max_tokens,
        )

        # ---- 启动账本记录 (旁路, 不阻断主流程) ----
        execution = self.ledger.start(
            ctx, model_name=request_model, provider=request_driver,
            stream_mode=request_stream,
        )
        # 将 execution_id 注入 ctx，便于下游传播 (Worker/Cron)
        import dataclasses
        ctx = dataclasses.replace(ctx, execution_id=execution.id)

        print(f"  🧠 [LLM Stream] 角色: {'Guest' if msg.is_guest else 'SyncAgent'}, 模型: {request_model}")

        # ---- 状态追踪 ----
        terminal_error = False  # 是否已收到终止性错误事件 (ERROR/DEAD_LOOP/MAX_STEPS/TOKEN_BUDGET)
        usage_logged_this_step = False  # 防止 USAGE 重复记账
        step_text = ""        # 本步累积的 TEXT (供估算兜底使用)
        step_reasoning = ""   # 本步累积的 REASONING (供估算兜底使用)
        had_tool_calls = False
        # 默认/Shadow 路径不改变流式分块；仅真实裁剪且注入了工具时才过滤内部标记。
        miss_mark_filter = (
            MissMarkStreamFilter()
            if selector_result is not None and bool(selector_result.names)
            else None
        )

        # ---- 消费 Runtime 事件流 (经 Recorder 包装) ----
        runtime_iter = request_runtime.run(
            messages=messages,
            tools=tools,
            ctx=ctx,
            timeout=dynamic_timeout,
            stream=request_stream,
        )
        recorder = RuntimeRecorder(self.ledger, execution.id)
        for event in recorder.wrap(runtime_iter):
            t = event.type

            if t == RuntimeEventType.STEP_START:
                step = event.data.get("step", 0)
                max_steps = event.data.get("max_steps", 0)
                print(f"  🧠 [LLM Stream] 步骤: {step}/{max_steps}")
                usage_logged_this_step = False
                step_text = ""
                step_reasoning = ""

            elif t == RuntimeEventType.TEXT:
                step_text += event.data
                visible_text = (miss_mark_filter.feed(event.data)
                                if miss_mark_filter is not None else event.data)
                if visible_text:
                    yield {"type": "token", "delta": visible_text}

            elif t == RuntimeEventType.REASONING:
                step_reasoning += event.data
                yield {"type": "reasoning_token", "delta": event.data}

            elif t == RuntimeEventType.TOOL_CALLS_READY:
                had_tool_calls = True
                # 持久化完整 assistant tool_calls 到 Session (含 provider_metadata)
                data = event.data
                tool_calls_data = [dict(tc) for tc in data.get("tool_calls", [])]
                self.session_mgr.add_message(
                    msg.session_key, "assistant", data.get("content", ""),
                    tool_calls_data=tool_calls_data,
                    reasoning_content=data.get("reasoning_content") or None,
                )

            elif t == RuntimeEventType.TOOL_CALL:
                had_tool_calls = True
                self.session_mgr.increment_tool_calls(msg.session_key)
                yield {"type": "tool_start", "name": event.data["name"], "args": event.data["arguments"]}

            elif t == RuntimeEventType.TOOL_RESULT:
                data = event.data
                # output 写 Session (完整结果), display 只用于前端事件
                output = data["output"]
                if not data["ok"] and not output.startswith("❌"):
                    output = f"❌ {output}"
                self.session_mgr.add_message(
                    msg.session_key, "tool", output,
                    tool_call_id=data["id"], name=data["name"],
                )
                yield {"type": "tool_result", "name": data["name"], "ok": data["ok"], "result": data["display"]}

            elif t == RuntimeEventType.USAGE:
                # 每步只记账一次 (Runtime 已延迟到成功后发射)
                if not usage_logged_this_step:
                    step_usage = dict(event.data)
                    self.session_mgr.log_api_usage(
                        msg.session_key, request_model,
                        step_usage.get("prompt_tokens", 0),
                        step_usage.get("completion_tokens", 0),
                        step_usage.get("total_tokens", 0),
                        provider=request_driver, estimated=False,
                    )
                    total_usage["prompt_tokens"] += step_usage.get("prompt_tokens", 0)
                    total_usage["completion_tokens"] += step_usage.get("completion_tokens", 0)
                    total_usage["total_tokens"] += step_usage.get("total_tokens", 0)
                    usage_logged_this_step = True

            elif t == RuntimeEventType.STEP_END:
                # 若 Runtime 未发射 USAGE (provider 不支持), 做本地估算兜底
                # 估算必须包含本步实际 TEXT/REASONING 输出，否则 completion_tokens=0 漏计
                if not usage_logged_this_step:
                    step_usage = _estimate_tokens(messages, step_text + step_reasoning)
                    self.session_mgr.log_api_usage(
                        msg.session_key, request_model,
                        step_usage["prompt_tokens"], step_usage["completion_tokens"], step_usage["total_tokens"],
                        provider=request_driver, estimated=True,
                    )
                    total_usage["prompt_tokens"] += step_usage["prompt_tokens"]
                    total_usage["completion_tokens"] += step_usage["completion_tokens"]
                    total_usage["total_tokens"] += step_usage["total_tokens"]
                    total_usage["estimated"] = True
                    usage_logged_this_step = True

                # P0 日额度贯通: 本步记账后立即检查剩余额度
                # 若超额，必须在 TOOL_CALLS_READY 之前终止，避免执行本轮工具和进入下一轮
                if session.token_usage >= self.daily_token_limit:
                    warning = (f"⚠️ 今日 Token 已达上限 ({self.daily_token_limit})，请明天再试\n"
                               f"当前累计: {session.token_usage}")
                    self.session_mgr.add_message(msg.session_key, "assistant", warning)
                    self.session_mgr.mark_done(msg.session_key, "日额度耗尽")
                    terminal_error = True
                    # 标记 ledger 为失败 (Runtime 未发终态事件)
                    self.ledger.finish(execution.id,
                                       status="failed",
                                       terminal_reason="daily_quota_exceeded")
                    yield {"type": "error", "msg": warning}
                    yield {"type": "done", "usage": total_usage}
                    # 关闭 Runtime 生成器，阻止后续工具执行和下一轮
                    runtime_iter.close()
                    return

            elif t == RuntimeEventType.DONE:
                pending_text = miss_mark_filter.flush() if miss_mark_filter is not None else ""
                if pending_text:
                    yield {"type": "token", "delta": pending_text}
                # Runtime 的 DONE 不能覆盖更具体的终止原因
                if not terminal_error:
                    # 正常回复: 持久化 + mark_done + after_reply
                    raw_content = event.data.get("content", "")
                    content = strip_miss_mark(raw_content)
                    if detect_miss(selector_result, raw_content, had_tool_calls):
                        self.request_selector.record_miss(selector_result)
                    if not content.strip():
                        content = (
                            f"⚠️ 模型未生成有效回复 (finish_reason={event.data.get('finish_reason', 'stop')})。"
                            f"\n\n这通常因为对话上下文累积过长或工具返回结果太大。"
                            f"\n\n建议：发送 `/new` 开启新会话后重试，或用更精确的关键词缩小工具调用范围。"
                        )
                    self.session_mgr.add_message(msg.session_key, "assistant", content)
                    if session.status == "working":
                        self.session_mgr.mark_done(msg.session_key, content[:200])
                    if self.memory:
                        self.memory.after_reply(msg.session_key, '', msg.text, content, msg.channel)
                yield {"type": "done", "usage": total_usage}

            elif t == RuntimeEventType.ERROR:
                terminal_error = True
                error_msg = event.data.get("msg", "未知错误")
                print(f"  ❌ [Runtime] ERROR: {error_msg}")
                # 与 DEAD_LOOP/MAX_STEPS/TOKEN_BUDGET 保持一致: 持久化错误终态 + mark_done
                self.session_mgr.add_message(
                    msg.session_key, "assistant", f"❌ {error_msg}")
                self.session_mgr.mark_done(msg.session_key, "模型调用错误")
                yield {"type": "error", "msg": error_msg}

            elif t == RuntimeEventType.DEAD_LOOP:
                terminal_error = True
                warning = event.data.get("msg") or f"⚠️ 检测到死循环：工具 '{event.data.get('tool_name', '')}' 连续重复调用相同参数。"
                if not warning.startswith("⚠️"):
                    warning = f"⚠️ {warning}"
                self.session_mgr.add_message(msg.session_key, "assistant", warning)
                self.session_mgr.mark_done(msg.session_key, "死循环自动终止")
                yield {"type": "error", "msg": warning}

            elif t == RuntimeEventType.MAX_STEPS:
                terminal_error = True
                warning = "⚠️ 任务执行步骤过多，已自动终止。请尝试拆分为更小的任务。"
                self.session_mgr.add_message(msg.session_key, "assistant", warning)
                self.session_mgr.mark_done(msg.session_key, "超出最大步骤数")
                yield {"type": "error", "msg": warning}

            elif t == RuntimeEventType.TOKEN_BUDGET_EXCEEDED:
                terminal_error = True
                budget = event.data.get("budget", 0)
                used = event.data.get("used", 0)
                warning = f"⚠️ Token 预算已耗尽 (预算: {budget}, 已用: {used})"
                self.session_mgr.add_message(msg.session_key, "assistant", warning)
                self.session_mgr.mark_done(msg.session_key, "Token 预算耗尽")
                yield {"type": "error", "msg": warning}

    def _run_ai_loop(self, msg: IncomingMessage) -> AgentResponse:
        """Tool Call Loop (兼容老接口): 消费 _stream_ai_loop 事件, 拼回 AgentResponse。
        对 api.py / IM 通道零感知。收集技能调用事件为 logs。"""
        final_text = ""
        logs = []
        for event in self._stream_ai_loop(msg):
            t = event.get("type")
            if t == "token":
                final_text += event["delta"]
            elif t == "tool_start":
                logs.append(f"🔧 调用技能: {event.get('name')}({str(event.get('args', ''))[:100]})")
            elif t == "tool_result":
                ok_str = "成功" if event.get("ok", True) else "失败"
                res_len = len(str(event.get("result", "")))
                logs.append(f"✓ 技能 {event.get('name')} 执行{ok_str} (结果长度: {res_len})")
            elif t == "error":
                return AgentResponse(event["msg"], title="❌ AI 错误", color="red", logs=logs)

        if not final_text.strip():
            final_text = "⚠️ AI 完成了工具调用，但未返回任何文本。"

        session = self.session_mgr.get_or_create(msg.session_key)
        title = (f"🤖 {self.bot_name} [{session.tool_calls}/{self.max_steps}]"
                 if session.status == "working" else f"🤖 {self.bot_name}")
        return AgentResponse(final_text, title=title, color="blue", logs=logs)
