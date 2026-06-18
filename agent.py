"""
AI Agent 核心 - LLM 调度与 Tool Call Loop
"""

import os
import json
import time
import threading
import traceback
import collections
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from session import SessionManager
from cron_engine import CronManager
from skill_engine import SkillEngine

class LRUCache:
    def __init__(self, maxsize=200):
        self.cache = collections.OrderedDict()
        self.maxsize = maxsize

    def setdefault(self, key, default):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        self.cache[key] = default
        if len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)
        return self.cache[key]

    def get(self, key, default=None):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return default

    def __getitem__(self, key):
        self.cache.move_to_end(key)
        return self.cache[key]

    def __setitem__(self, key, value):
        self.cache[key] = value
        self.cache.move_to_end(key)
        if len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)

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
                 message_id: str, text: str, notify_channels: list = None, is_guest: bool = False, sync_mode: bool = False):
        self.channel = channel
        self.user_id = user_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.notify_channels = notify_channels
        self.is_guest = is_guest
        self.sync_mode = sync_mode
        self.sync_mode = sync_mode

    @property
    def session_key(self) -> str:
        return f"{self.channel}:{self.user_id}"


class AgentResponse:
    """Agent 返回给通道层的标准化回复"""

    def __init__(self, text: str, title: str = "", color: str = "blue", task_id: str = ""):
        self.text = text
        self.title = title
        self.color = color
        self.task_id = task_id


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
        if "models" in llm_cfg:
            default_model = llm_cfg.get("default", "")
            default_cfg = llm_cfg["models"].get(default_model, {})
            self.client = OpenAI(
                api_key=default_cfg.get("api_key", llm_cfg.get("api_key", "")),
                base_url=default_cfg.get("base_url", llm_cfg.get("base_url", "")),
            )
            self.model = default_cfg.get("model", default_model)
            self.max_tokens = default_cfg.get("max_tokens", 2048)
            self.temperature = default_cfg.get("temperature", 0.3)
        else:
            self.client = OpenAI(
                api_key=llm_cfg["api_key"],
                base_url=llm_cfg["base_url"],
            )
            self.model = llm_cfg["model"]
            self.max_tokens = llm_cfg.get("max_tokens", 2048)
            self.temperature = llm_cfg.get("temperature", 0.3)

        self.bot_name = config.get("bot_name", "Agent")
        self.svc_name = config.get("service_name", "feishu-bot")

        # 会话管理
        self.session_mgr = SessionManager(
            ttl_minutes=session_cfg.get("ttl_minutes", 30),
            max_history=session_cfg.get("max_history", 20),
        )
        self.channels = []  # 由 main.py 在初始化通道后注入

        # 技能引擎
        self.skill_engine = SkillEngine()

        # 安全限制
        self.max_steps = session_cfg.get("max_steps_per_goal", 10)
        self.daily_token_limit = session_cfg.get("daily_token_limit", 500000)
        self._dead_loop_counter = LRUCache(maxsize=200)  # session_key -> {tool_fingerprint -> count}
        self.orch_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="AgentOrch")

        # 记忆引擎 (跨会话长期记忆)
        self.memory = AgentMemory() if MEMORY_AVAILABLE else None
        if self.memory:
            # 给蒸馏注入 LLM callback —— 复用 self.client + self.model
            # 这样不用单独维护 LLM_API_KEY 环境变量，配置零硬编码
            def _distill_llm_callback(prompt: str) -> str:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self.max_tokens,
                    temperature=0.3,  # 蒸馏要稳定，不要发散
                )
                return resp.choices[0].message.content or ''
            self.memory.set_llm(_distill_llm_callback)
            self.memory.start_distill_scheduler(interval_hours=24)
            print("  ✅ 记忆引擎已启用 (蒸馏 LLM callback 已注入)")

        # 定时任务引擎
        self.cron = CronManager()

        # 系统提示词
        self.system_prompt = self._build_system_prompt()

    def broadcast(self, response: AgentResponse):
        """将消息广播到所有挂载的通道"""
        for ch in self.channels:
            try:
                ch_response = AgentResponse(
                    text=self._truncate_long_message_if_needed(response.text, ch.name),
                    title=response.title,
                    color=response.color,
                    task_id=response.task_id
                )
                ch.broadcast(ch_response)
            except Exception as e:
                print(f"❌ 通道 {ch.name} 广播异常: {e}")

    def _build_system_prompt(self, is_guest: bool = False) -> str:
        """构建系统提示词 (包含技能列表)"""
        skills_summary = self.skill_engine.list_skills(is_guest=is_guest)
        project_root = self._config.get("project_root", "/root/lite_agent")

        if is_guest:
            return f"""你是 {self.bot_name}，一个运行在 Linux VPS 上的私人智能助手。
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
- 如果工具返回错误，向用户解释原因"""
        else:
            return f"""你是 {self.bot_name}，一个运行在 Linux VPS 上的私人智能助手。

【系统环境自知】:
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
- 如果工具返回错误，向用户解释原因并建议解决方案"""

    # ------------------------------------------------------------------
    #  消息入口
    # ------------------------------------------------------------------
    def handle(self, msg: IncomingMessage) -> AgentResponse:
        """处理一条用户消息，返回 AgentResponse"""
        text = msg.text.strip()

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
            
        # 超长消息拦截：如果不是 Web 端（api），且配置了 HedgeDoc，则尝试上传并截断
        response.text = self._truncate_long_message_if_needed(response.text, msg.channel)
        return response

    def _truncate_long_message_if_needed(self, text: str, channel: str) -> str:
        hc = self._config.get("hedgedoc", {})
        if channel != 'api' and hc.get("enabled") and len(text) > 2500:
            try:
                url = self._upload_to_hedgedoc(text, hc)
                if url:
                    return text[:2000] + f"\n\n... (由于字数超出平台限制，剩余内容已截断)\n\n[🔗 点击此处在 Web 网页中查看完整报告]({url})"
            except Exception as e:
                print(f"❌ 上传至 HedgeDoc 失败: {e}")
        return text

    def _upload_to_hedgedoc(self, markdown_text: str, hc: dict) -> str:
        import requests
        s = requests.Session()
        headers = {'X-Forwarded-Proto': 'https'}
        # 1. 登录换取 Cookie
        login_url = hc.get("internal_url", "http://127.0.0.1:3030").rstrip('/') + "/login"
        r_login = s.post(login_url, data={'email': hc.get("email"), 'password': hc.get("password")}, headers=headers, allow_redirects=False, timeout=10)
        
        # Express.js 会返回 set-cookie，我们需要手动提取由于 HTTP 被忽略的 Secure Cookie
        cookie_str = '; '.join([f'{k}={v}' for k, v in s.cookies.items()])
        
        # 2. 发文
        headers['Cookie'] = cookie_str
        headers['Content-Type'] = 'text/markdown'
        new_url = hc.get("internal_url", "http://127.0.0.1:3030").rstrip('/') + "/new"
        r_new = requests.post(new_url, data=markdown_text.encode('utf-8'), headers=headers, allow_redirects=False, timeout=10)
        
        location = r_new.headers.get('Location')
        if location:
            # location 可能是内部地址或完整的 public URL
            public_url = hc.get("public_url", "https://md.maifeipin.com").rstrip('/')
            if location.startswith("http"):
                # 如果返回了完整的内部 URL，替换为公网 URL
                import urllib.parse
                parsed = urllib.parse.urlparse(location)
                return public_url + parsed.path
            else:
                return public_url + location
        return ""

    # ------------------------------------------------------------------
    #  内置指令
    # ------------------------------------------------------------------
    def _handle_builtin(self, msg: IncomingMessage) -> AgentResponse:
        """处理 /new /status /history /stop /help 等内置指令"""
        parts = msg.text.strip().split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("/cmd", "/balance", "/memory", "/remember", "/persona", "/cron", "/check"):
            if msg.is_guest:
                return AgentResponse("❌ 权限不足：只有管理员可使用该指令", title="⚠️ 权限不足", color="red")

        if cmd == "/new":
            self.session_mgr.reset_session(msg.session_key)
            return AgentResponse("🔄 会话已重置，可以开始新的对话", title="新会话", color="green")

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
                else:
                    result = _run_billing_cmd(args)
                return AgentResponse(result, title=f"执行结果: {args[0]}", color="blue")
            except Exception as e:
                return AgentResponse(f"执行失败: {e}", title="错误", color="red")

        if cmd == "/balance":
            try:
                import sys, os
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from skills.ops_llm import check_deepseek_balance
                return AgentResponse(check_deepseek_balance(), title="💰 账户余额", color="yellow")
            except Exception as e:
                return AgentResponse(f"查询余额失败: {e}", title="错误", color="red")

        if cmd == "/memory":
            if not self.memory:
                return AgentResponse("记忆引擎未安装，请将 memory_engine/ 目录放入项目根目录", title="⚠️ 未启用", color="grey")
            stats = self.memory.stats()
            type_stats = stats.get('by_type', {})
            type_lines = "\n".join(
                f"  {t}: {c} 条" for t, c in type_stats.items()
            )
            type_suffix = f"\n四维分布:\n{type_lines}" if type_lines else ''
            return AgentResponse(
                f"**记忆池状态**\n"
                f"总消息: {stats['total_messages']}\n"
                f"蒸馏产物: {stats['distilled']}\n"
                f"平均质量分: {stats['avg_importance']}\n"
                f"用户数: {stats['users']}"
                f"{type_suffix}",
                title="🧠 记忆池", color="blue"
            )

        if cmd == "/remember":
            # /remember <type> <内容>
            if not self.memory:
                return AgentResponse("记忆引擎未安装", title="⚠️", color="grey")
            if len(args) < 2:
                return AgentResponse(
                    "用法: `/remember <type> <内容>`\n"
                    "type: concept | event | preference | troubleshooting\n"
                    "示例: `/remember troubleshooting 钉钉Stream必须勾选后台开关并发布才能生效`",
                    title="📝 强制记忆", color="grey"
                )
            mem_type = args[0]
            if mem_type not in ('concept', 'event', 'preference', 'troubleshooting'):
                return AgentResponse(
                    f"未知类型 {mem_type}。可选: concept, event, preference, troubleshooting",
                    title="⚠️", color="red"
                )
            content = msg.text[len('/remember ')+len(mem_type)+1:]
            mid = self.memory.force_remember(
                msg.session_key, '', content, memory_type=mem_type
            )
            return AgentResponse(
                f"已存入 [{mem_type}] 记忆池 (id:{mid})",
                title="🧠 已记忆", color="green"
            )

        if cmd == "/persona":
            # /persona              → 显示 persona.md 概览 + 待确认编号列表
            # /persona confirm <N>  → 把"待确认"第 N 条升格到"工作偏好/手动校正"
            # /persona confirm <N> <分类>  → 升格到指定分类
            if not self.memory:
                return AgentResponse("记忆引擎未安装", title="⚠️", color="grey")

            if not args:
                # 概览模式
                content = self.memory.persona_content()
                pending = self.memory.persona_pending()
                if not content:
                    return AgentResponse(
                        "(persona.md 还没生成。lite-agent 启动 5 分钟后会跑首次蒸馏。)",
                        title="🧬 个人画像", color="grey"
                    )
                # IM 卡片字符限制，截断显示 + 列出待确认编号
                preview = content if len(content) < 1800 else content[:1700] + '\n...(已截断，VPS 完整文件: /root/lite_agent/data/persona.md)'
                if pending:
                    pending_lines = '\n'.join(f"  {i+1}. {p.lstrip('- ').strip()}" for i, p in enumerate(pending))
                    preview += f"\n\n---\n📋 待确认条目（用 `/persona confirm <序号>` 升格）：\n{pending_lines}"
                return AgentResponse(preview, title="🧬 个人画像", color="blue")

            sub = args[0].lower()
            if sub == "confirm":
                if len(args) < 2 or not args[1].isdigit():
                    return AgentResponse(
                        "用法: `/persona confirm <序号> [分类]`\n"
                        "分类可选: 身份与角色 / 工作偏好 / 技术栈熟练度 / 当前进行中项目 / 已知决策 / 个人事实\n"
                        "默认升格到 `工作偏好`。\n"
                        "先用 `/persona` 看待确认条目编号。",
                        title="📋 用法", color="grey"
                    )
                idx = int(args[1])
                # 用户输入分类名（不带 ## 前缀）；映射到完整章节标题
                section_short = ' '.join(args[2:]).strip() if len(args) >= 3 else '工作偏好'
                target_section = '## ' + section_short

                moved = self.memory.persona_confirm(idx, target_section=target_section)
                if not moved:
                    return AgentResponse(
                        f"升格失败：序号 {idx} 越界或分类「{section_short}」不存在。\n"
                        "用 `/persona` 看当前待确认列表。",
                        title="⚠️", color="red"
                    )
                return AgentResponse(
                    f"已将下面这条移入 **{section_short}** / 手动校正:\n\n{moved}",
                    title="✅ 升格成功", color="green"
                )

            return AgentResponse(
                "未知子命令。可用: `/persona` (查看), `/persona confirm <序号>` (升格)",
                title="⚠️", color="grey"
            )

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
`/memory` - 查看记忆池状态
`/persona` - 查看个人画像 / `confirm <序号>` 升格待确认条目
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
        args = " ".join(parts[1:]) if len(parts) > 1 else ""

        if cmd in ("rss", "cron"):
            if msg.is_guest:
                return AgentResponse("❌ 权限不足：只有管理员可使用该指令", title="⚠️ 权限不足", color="red")

        if cmd == "goal":
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

        if cmd == "rss":
            if args == "push":
                return self._handle_rss_push()
            if args == "log":
                return self._handle_rss_log()
            return self._handle_rss(msg, args)

        if cmd == "cron" and args == "log":
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
            f"未知指令 `::{cmd}`。可用: `::goal <描述>` / `::goal` / `::goal done` / `::rss [分组]`",
            title="⚠️", color="red"
        )

    def _handle_rss(self, msg, group_filter: str = "") -> AgentResponse:
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from skills.ops_rss import handle_rss
            return handle_rss(msg, group_filter, self.session_mgr)
        except Exception as e:
            return AgentResponse(f'RSS 查询失败: {e}', title='❌ 错误', color='red')

    def _handle_rss_push(self) -> AgentResponse:
        try:
            from skills.ops_rss import rss_brief
            text = rss_brief()
            if text:
                return AgentResponse(text, title='📰 RSS 精选', color='blue')
            return AgentResponse('当前无新文章可推送', title='RSS', color='grey')
        except Exception as e:
            return AgentResponse(f'推送失败: {e}', title='❌', color='red')

    def _handle_rss_log(self) -> AgentResponse:
        import subprocess
        r = subprocess.run(
            f"journalctl -u {self.svc_name} --since '2 hours ago' --no-pager | grep -E 'RSS|缓存|文章|V2EX|Top|预计算' | tail -20",
            shell=True, capture_output=True, text=True, timeout=10
        )
        text = r.stdout.strip() or r.stderr.strip() or '(无日志)'
        if len(text) > 2500:
            text = text[-2500:]
        return AgentResponse(text, title='📋 RSS 日志', color='turquoise')

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



    def _run_orchestrated(self, msg: IncomingMessage) -> AgentResponse:
        from task_orchestrator import TaskOrchestrator
        import uuid

        print(f"  [ORCH] 启动编排引擎 session={msg.session_key} task_len={len(msg.text)}")

        orch = TaskOrchestrator(
            config=self._config,
            skill_engine=self.skill_engine,
            session_mgr=self.session_mgr,
            channels=self.channels,
        )

        task_id = uuid.uuid4().hex[:8]

        def _bg_run():
            try:
                print(f"  [ORCH] 后台线程开始执行 session={msg.session_key} task_id={task_id}")
                result = orch.execute(
                    goal=msg.text,
                    session_key=msg.session_key,
                    progress_callback=self._on_subtask_progress(msg),
                    task_id=task_id,
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
                try:
                    if hasattr(ch, 'send_progress'):
                        ch.send_progress(msg.message_id, text)
                except Exception:
                    pass
        return callback

    def _push_result(self, msg, result: str):
        truncated_result = self._truncate_long_message_if_needed(result, msg.channel)
        response = AgentResponse(truncated_result, title="🤖 多Agent执行报告", color="blue")
        for ch in self.channels:
            if msg.notify_channels is not None and ch.name not in msg.notify_channels:
                continue
            try:
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

    def _run_ai_loop(self, msg: IncomingMessage) -> AgentResponse:
        """
        Tool Call Loop:
        发消息 -> AI 决策 -> 调工具 -> 结果回传 -> AI 继续 -> ... -> 最终回复
        """
        session = self.session_mgr.get_or_create(msg.session_key)

        # 添加用户消息
        self.session_mgr.add_message(msg.session_key, "user", msg.text)

        # 获取所有可用工具 Schema
        if msg.is_guest:
            tools = self.skill_engine.get_guest_schemas()
        else:
            tools = self.skill_engine.get_all_schemas()

        # 动态匹配并获取当前消息命中的所有技能防护提示词（循环外只运行一次）
        guard_prompts = self.skill_engine.get_guard_prompts(msg.text, is_guest=msg.is_guest)

        for step in range(self.max_steps):
            # 构建完整的消息列表
            system_content = self._build_system_prompt(is_guest=msg.is_guest)

            # 动态注入安全防幻觉提示词
            if guard_prompts:
                system_content += "\n\n⚠️【数据忠实执行指令】:\n" + "\n".join(f"- {p}" for p in guard_prompts)

            messages = [{"role": "system", "content": system_content}]

            # 注入长期记忆
            if self.memory:
                memory_ctx = self.memory.before_reply(
                    msg.session_key, msg.text
                )
                if memory_ctx:
                    messages[0]["content"] += memory_ctx

            messages.extend(self.session_mgr.get_history(msg.session_key))
            messages = self._validate_messages(messages)

            # 日额度检查
            if session.token_usage >= self.daily_token_limit:
                print(f"  💸 日 Token 上限: {session.token_usage}/{self.daily_token_limit}")
                warning = f"⚠️ 今日 Token 已达上限 ({self.daily_token_limit})，请明天再试\n当前累计: {session.token_usage}"
                self.session_mgr.add_message(msg.session_key, "assistant", warning)
                return AgentResponse(warning, title="💸 额度耗尽", color="red")

            # 调用 LLM
            try:
                kwargs = {
                    "model": self.model,
                    "messages": messages,
                }
                
                # 特殊处理 deepseek-v4-pro 或带有 reasoning 需求的模型
                if "pro" in self.model.lower() or "reasoner" in self.model.lower():
                    kwargs["reasoning_effort"] = "high"
                    kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                else:
                    kwargs["temperature"] = self.temperature
                    kwargs["max_tokens"] = self.max_tokens

                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                import time
                start_t = time.time()
                print(f"  🧠 [LLM Request] 角色: SyncAgent, 模型: {self.model}")
                kwargs["timeout"] = 600.0
                response = self.client.chat.completions.create(**kwargs)
                print(f"  ✅ [LLM Response] 耗时: {time.time()-start_t:.2f}s, Tokens: {response.usage.total_tokens if response.usage else 0}")
                choice = response.choices[0]

                # 更新 Token 消耗
                if response.usage:
                    self.session_mgr.log_api_usage(
                        msg.session_key,
                        self.model,
                        response.usage.prompt_tokens,
                        response.usage.completion_tokens,
                        response.usage.total_tokens
                    )

            except Exception as e:
                error_msg = f"LLM 调用失败: {e}"
                print(f"  ❌ {error_msg}")
                traceback.print_exc()
                return AgentResponse(error_msg, title="❌ AI 错误", color="red")

            # ----- 情况 1: AI 要调用工具 -----
            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                # 保存 assistant 的 tool_calls 消息 (OpenAI 协议要求)
                tool_calls_data = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in choice.message.tool_calls
                ]
                reasoning = getattr(choice.message, "reasoning_content", None)
                self.session_mgr.add_message(
                    msg.session_key, "assistant",
                    choice.message.content or "",
                    tool_calls_data=tool_calls_data,
                    reasoning_content=reasoning,
                )

                # 逐个执行工具
                for tc in choice.message.tool_calls:
                    self.session_mgr.increment_tool_calls(msg.session_key)

                    fingerprint = f"{tc.function.name}:{tc.function.arguments}"
                    counter = self._dead_loop_counter.setdefault(msg.session_key, {})
                    if fingerprint == counter.get("_last"):
                        counter["_streak"] = counter.get("_streak", 1) + 1
                    else:
                        counter["_streak"] = 1
                    counter["_last"] = fingerprint

                    if counter["_streak"] >= 3:
                        print(f"  🔄 死循环检测: {tc.function.name} 连续 {counter['_streak']} 次, 强制终止")
                        warning = f"🔄 检测到工具 `{tc.function.name}` 连续重复调用 {counter['_streak']} 次，已自动终止以防止死循环"
                        self.session_mgr.add_message(msg.session_key, "assistant", warning)
                        self.session_mgr.mark_done(msg.session_key, "死循环自动终止")
                        return AgentResponse(warning, title="🔄 死循环终止", color="orange")

                    print(f"  🔧 [{step+1}/{self.max_steps}] "
                          f"调用: {tc.function.name}({tc.function.arguments})")

                    if msg.is_guest and not self.skill_engine.is_guest_ok(tc.function.name):
                        print(f"  🚫 访客试图越权调用工具: {tc.function.name}")
                        result = f"❌ 权限不足：当前账户为访客，无权调用工具 {tc.function.name}"
                    else:
                        result = self.skill_engine.execute(
                            tc.function.name, tc.function.arguments
                        )

                    # 将工具结果添加到会话 (带 tool_call_id 关联)
                    self.session_mgr.add_message(
                        msg.session_key, "tool", result,
                        tool_call_id=tc.id,
                        name=tc.function.name,
                    )

                continue  # 回到循环顶部，让 AI 处理工具结果

            # ----- 情况 2: AI 直接给出文本回复 (任务完成或闲聊) -----
            reply_text = choice.message.content or "(空回复)"
            self.session_mgr.add_message(msg.session_key, "assistant", reply_text)

            # 如果之前在执行目标，标记完成
            if session.status == "working":
                self.session_mgr.mark_done(msg.session_key, reply_text[:200])

            # 存入长期记忆 (异步)
            if self.memory:
                self.memory.after_reply(
                    msg.session_key, '', msg.text, reply_text, msg.channel
                )

            title = f"🤖 {self.bot_name} [{session.tool_calls}/{self.max_steps}]" if session.status == "working" else f"🤖 {self.bot_name}"
            return AgentResponse(reply_text, title=title, color="blue")

        # 超出最大步骤数
        warning = "⚠️ 任务执行步骤过多，已自动终止。请尝试拆分为更小的任务。"
        self.session_mgr.add_message(msg.session_key, "assistant", warning)
        self.session_mgr.mark_done(msg.session_key, "超出最大步骤数")

        if self.memory:
            self.memory.after_reply(
                msg.session_key, '', msg.text, warning, msg.channel
            )

        return AgentResponse(warning, title="⚠️ 任务终止", color="orange")
