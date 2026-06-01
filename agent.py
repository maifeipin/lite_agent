"""
AI Agent 核心 - LLM 调度与 Tool Call Loop
"""

import json
import time
import traceback
from openai import OpenAI
from session import SessionManager
from cron_engine import CronManager
from skill_engine import SkillEngine

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
                 message_id: str, text: str):
        self.channel = channel
        self.user_id = user_id
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text

    @property
    def session_key(self) -> str:
        return f"{self.channel}:{self.user_id}"


class AgentResponse:
    """Agent 返回给通道层的标准化回复"""

    def __init__(self, text: str, title: str = "", color: str = "blue"):
        self.text = text
        self.title = title
        self.color = color


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
        llm_cfg = config["llm"]
        session_cfg = config.get("session", {})

        # LLM 客户端
        self.client = OpenAI(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
        )
        self.model = llm_cfg["model"]
        self.max_tokens = llm_cfg.get("max_tokens", 2048)
        self.temperature = llm_cfg.get("temperature", 0.3)
        self.bot_name = config.get("bot_name", "Agent")

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
        self._dead_loop_counter: dict = {}  # session_key -> {tool_fingerprint -> count}

        # 记忆引擎 (跨会话长期记忆)
        self.memory = AgentMemory() if MEMORY_AVAILABLE else None
        if self.memory:
            self.memory.start_distill_scheduler(interval_hours=24)
            print("  ✅ 记忆引擎已启用")

        # 定时任务引擎
        self.cron = CronManager()

        # 系统提示词
        self.system_prompt = self._build_system_prompt()

    def broadcast(self, response: AgentResponse):
        """将消息广播到所有挂载的通道"""
        for ch in self.channels:
            try:
                ch.broadcast(response)
            except Exception as e:
                print(f"❌ 通道 {ch.name} 广播异常: {e}")

    def _build_system_prompt(self) -> str:
        """构建系统提示词 (包含技能列表)"""
        skills_summary = self.skill_engine.list_skills()
        return f"""你是 {self.bot_name}，一个运行在 Linux VPS 上的私人智能助手。

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
- 不要编造数据，一切以工具返回的真实结果为准
- 如果工具返回错误，向用户解释原因并建议解决方案"""

    # ------------------------------------------------------------------
    #  消息入口
    # ------------------------------------------------------------------
    def handle(self, msg: IncomingMessage) -> AgentResponse:
        """处理一条用户消息，返回 AgentResponse"""
        text = msg.text.strip()

        if text.startswith("::"):
            return self._handle_double_colon(msg)

        if text.startswith("/"):
            return self._handle_builtin(msg)

        return self._run_ai_loop(msg)

    # ------------------------------------------------------------------
    #  内置指令
    # ------------------------------------------------------------------
    def _handle_builtin(self, msg: IncomingMessage) -> AgentResponse:
        """处理 /new /status /history /stop /help 等内置指令"""
        parts = msg.text.strip().split()
        cmd = parts[0].lower()
        args = parts[1:]

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
            skills_list = self.skill_engine.list_skills()
            help_text = f"""**内置指令:**
`/new` - 重置会话
`/status` - 查看会话状态
`/history` - 查看最近对话
`/stop` - 终止当前任务
`/help` - 显示帮助
`/balance` - 查询大模型账户余额
`/memory` - 查看记忆池状态
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
            return AgentResponse(
                f"🎯 目标已设定: **{args}**\n"
                f"进入任务模式，最多 {self.max_steps} 步，上下文不会截断\n"
                f"完成任务后发送 `::goal done` 标记结束",
                title="🎯 新目标", color="green"
            )

        return AgentResponse(
            f"未知指令 `::{cmd}`。可用: `::goal <描述>` / `::goal` / `::goal done`",
            title="⚠️", color="red"
        )

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
        tools = self.skill_engine.get_all_schemas()

        for step in range(self.max_steps):
            # 构建完整的消息列表
            messages = [{"role": "system", "content": self.system_prompt}]

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
                
                print("  [DEBUG] 发送大模型请求...")
                kwargs["timeout"] = 30.0
                response = self.client.chat.completions.create(**kwargs)
                print("  [DEBUG] 请求返回成功!")
                choice = response.choices[0]

                # 更新 Token 消耗
                if response.usage:
                    session.token_usage += response.usage.total_tokens

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
