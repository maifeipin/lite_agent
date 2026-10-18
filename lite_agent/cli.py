"""
Lite Agent 生产唯一主入口 (main.py)。
负责命令行参数解析、应用引导 (App Bootstrap)、通道挂载、信号捕获与生命周期优雅退出。
支持 --mode core (基础内核模式) 与 --mode full (完整生产模式)。
"""

import argparse
import logging
import os
import re
import signal
import sys
import threading
import time
from typing import Dict, Any, List

from lite_agent.agent import Agent
from lite_agent.app.bootstrap import create_app
from lite_agent.app.manifests import FULL_MODE_PLUGINS, CORE_MODE_PLUGINS
from lite_agent.core.skill_engine import SkillEngine

logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lite Agent 运行时宿主",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["core", "full"],
        default=None,
        help="运行模式: core (纯基础内核, 零业务插件与空工具集) 或 full (完整生产模式)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="指定配置文件路径 (默认查找根目录 config.json 或 LITE_AGENT_CONFIG_FILE)",
    )
    parser.add_argument(
        "--plugins",
        type=str,
        default=None,
        help="显式指定启用的插件列表 (逗号分隔，如 'billing,todo'，空字符串 '' 表示禁用全部业务插件)",
    )
    return parser.parse_args()


def load_config(config_override: str = None) -> dict:
    if config_override:
        os.environ["LITE_AGENT_CONFIG_FILE"] = config_override

    from lite_agent.core.constants import PROJECT_ROOT
    base_dir = PROJECT_ROOT
    config_path = os.environ.get("LITE_AGENT_CONFIG_FILE") or os.path.join(base_dir, "config.json")

    if not os.path.exists(config_path):
        print(f"❌ 找不到配置文件: {config_path}")
        print("💡 请复制 config/examples/agent.json 为 config.json 并修改相关配置")
        sys.exit(1)

    from lite_agent.core import config_loader
    return config_loader.load_config()


def session_cleanup_task(agent: Agent, interval: int = 300, stop_event: threading.Event = None):
    """后台定时清理过期会话的任务。"""
    while stop_event is None or not stop_event.is_set():
        try:
            if stop_event:
                if stop_event.wait(timeout=interval):
                    break
            else:
                time.sleep(interval)
            expired_keys = agent.session_mgr.cleanup_expired()
            if expired_keys:
                agent.cleanup_locks(expired_keys)
        except Exception as e:
            logger.warning(f"清理任务异常: {e}")


def _is_noop_enrich_output(text: str) -> bool:
    """识别 mail_llm_enrich 的纯空转输出（pending=0 且成功/降噪/失败/跳过全为 0）。

    仅当输出**完整等于**已知空转结构时才算空转（re.fullmatch，含数值边界）：
    “📋 待处理 pending 邮件: 0 封 (限量 N)” + “🏁 处理完成: ✅成功 0, 🔕降噪 0, ❌失败 0, ⏭️跳过 0”。
    末尾附带任何额外内容（如 `Error: database unavailable`）或任一计数非零，
    都不匹配、照常推送——异常通知不得被空转过滤吞掉。
    """
    if not isinstance(text, str):
        return False
    return re.fullmatch(
        r"📋 待处理 pending 邮件: 0 封 \(限量 \d+\)\s*"
        r"🏁 处理完成: ✅成功 0, 🔕降噪 0, ❌失败 0, ⏭️跳过 0\s*",
        text,
    ) is not None


def _register_configured_cron_jobs(app, agent, config: dict):
    """从 config.json 的 cron_jobs 列表注册通用定时任务。"""
    import subprocess
    from lite_agent.core.constants import PROJECT_ROOT
    root = config.get("project_root", PROJECT_ROOT)
    cron_srv = app.cron_service

    for job in config.get("cron_jobs", []):
        if "name" not in job:
            continue
        name = job["name"]

        # 时间时段展开
        times = []
        if "time_range" in job and isinstance(job["time_range"], dict):
            tr = job["time_range"]
            start_h = tr.get("start", 0)
            end_h = tr.get("end", 23)
            minute = tr.get("minute", 0)
            for h in range(start_h, end_h + 1):
                times.append(f"{h:02d}:{minute:02d}")
        elif "times" in job and isinstance(job["times"], list):
            times = list(job["times"])
        elif "time" in job:
            times = [job["time"]]
        else:
            raise ValueError(f"Cron 作业 [{name}] 缺少时间配置 (time/time_range/times)")

        # 检查是否为合法任务定义
        if "command" not in job and "skill" not in job:
            raise ValueError(f"无效的 Cron 作业配置: [{name}] 缺少 command 或 skill 目标")

        if "command" in job:
            cmd = job["command"].format(root=root)
            for t in times:
                j_name = name if len(times) <= 1 else f"{name}#{t}"
                try:
                    cron_srv.add_job(
                        j_name,
                        t,
                        lambda c=cmd: subprocess.run(
                            c, shell=True, capture_output=True, text=True, timeout=120
                        ).stdout.strip() or "(无输出)",
                    )
                except Exception as e:
                    logger.warning(f"注册 Cron 命令作业 [{j_name}] 失败: {e}")
        elif "skill" in job:
            skill_target = job["skill"]
            kwargs = job.get("kwargs", {})
            for t in times:
                j_name = name if len(times) <= 1 else f"{name}#{t}"
                def _make_skill_task(target=skill_target, kw=kwargs,
                                     job_name=name, push=job.get("push_on_output", False)):
                    def _task():
                        # 1. 优先从 cron_service 的专属 job_handlers 获取
                        handler = cron_srv.get_handler(target) if hasattr(cron_srv, "get_handler") else None
                        if handler is not None:
                            return handler(**kw) if kw else handler()
                        # 2. 从 skill_engine 获取模型工具技能
                        fn_name = target.split("::")[-1]
                        engine = getattr(app, "skill_engine", None)
                        tool_info = engine.get_tool(fn_name) if engine else None
                        if tool_info and "func" in tool_info:
                            text = tool_info["func"](**kw) if kw else tool_info["func"]()
                            if push and text and not text.startswith("✅") and not text.startswith("(") \
                                    and not (fn_name == "mail_llm_enrich" and _is_noop_enrich_output(text)):
                                from lite_agent.contracts.message import AgentResponse
                                from lite_agent.services.owner_delivery import send_owner_response
                                color = "red" if any(err in text for err in (
                                    "✉️", "⚠️", "❌", "失败", "错误", "Error", "Exception"
                                )) else "blue"
                                send_owner_response(
                                    getattr(agent, "channels", []) or [],
                                    config.get("channels", {}) or {},
                                    AgentResponse(text, title=job_name, color=color),
                                )
                            return text
                        raise RuntimeError(f"未找到指定的 Cron 技能: {target}")
                    return _task
                try:
                    # 去重：若已注册同名或同槽任务先移除，防止重复触发
                    cron_srv.remove_job_by_name(j_name)
                    cron_srv.add_job(j_name, t, _make_skill_task())
                except Exception as e:
                    logger.warning(f"注册 Cron 技能作业 [{j_name}] 失败: {e}")


def main():
    args = parse_args()

    # 1. 配置加载与优先级解析
    config = load_config(args.config)

    # 模式优先级: CLI 参数 > 环境变量 LITE_AGENT_MODE > config.app.mode > 默认 full
    mode = (
        args.mode
        or os.environ.get("LITE_AGENT_MODE")
        or (config.get("app", {}) or {}).get("mode")
        or "full"
    )

    print(f"🤖 正在启动 Lite Agent (模式: {mode.upper()})...")

    # 2. 模型配置校验 (仅在 full 模式或显式配置 llm 时执行强阻断)
    from lite_agent.core.model_config import validate_and_print
    if config.get("llm") and not validate_and_print(config):
        sys.exit(1)

    # 3. 插件启用/禁用列表解析与合法性强校验
    known_plugins = {p["name"]: p for p in FULL_MODE_PLUGINS}
    plugins_cfg = config.get("plugins", {}) or {}
    disabled_list = plugins_cfg.get("disabled", [])

    # 校验 disabled 中的未知插件项
    for d in disabled_list:
        if d not in known_plugins:
            raise ValueError(f"配置错误: 未知禁用插件项 [{d}]，已知插件: {list(known_plugins.keys())}")

    # 解析 enabled 列表
    enabled_list = None
    if args.plugins is not None:
        raw_p = args.plugins.strip()
        enabled_list = [p.strip() for p in raw_p.split(",") if p.strip()] if raw_p else []
    elif "LITE_AGENT_PLUGINS" in os.environ:
        raw_p = os.environ["LITE_AGENT_PLUGINS"].strip()
        enabled_list = [p.strip() for p in raw_p.split(",") if p.strip()] if raw_p else []
    elif "enabled" in plugins_cfg:
        enabled_list = list(plugins_cfg["enabled"])

    if enabled_list is not None:
        for ep in enabled_list:
            if ep not in known_plugins:
                raise ValueError(f"配置错误: 未知启用插件项 [{ep}]，已知插件: {list(known_plugins.keys())}")
        if mode == "core" and len(enabled_list) > 0:
            raise ValueError(f"冲突错误: core 基础模式下禁止启用业务插件 (尝试启用: {enabled_list})")

    # 4. 技能引擎实例化：无论 core 还是 full 均从实例空工具集合开始，严禁盲扫
    skill_engine = SkillEngine(tools={}, auto_load=False)

    app = None
    try:
        # 5. 创建统一宿主应用 (生产环境初始化唯一的活体 Agent)
        app = create_app(mode=mode, config=config, skill_engine=skill_engine)

        # 6. 加载对应模式插件 (由启用的业务插件显式登记各自工具技能与路由)
        if mode == "full":
            disabled_set = set(disabled_list)
            if enabled_list is not None:
                manifest = [known_plugins[name] for name in enabled_list if name not in disabled_set]
            else:
                manifest = [p for p in FULL_MODE_PLUGINS if p["name"] not in disabled_set]
            print(f"📦 正在受控加载生产插件集合 ({len(manifest)} 个插件)...")
            app.plugin_loader.load_all(manifest)

        # 7. 取得应用拥有的唯一活体 Agent 实例
        agent = app.agent
        if agent is None:
            raise RuntimeError("应用启动失败: 生产运行时缺少活体 Agent 实例")
        agent.route_registry = app.route_registry

        # 8. 启动会话清理线程并登记到 lifecycle 优雅管理
        stop_cleanup = threading.Event()
        cleanup_thread = threading.Thread(
            target=session_cleanup_task,
            args=(agent, 300, stop_cleanup),
            daemon=True,
            name="SessionCleanupThread",
        )
        cleanup_thread.start()
        app.lifecycle.register_resource(
            "session_cleanup",
            lambda: (stop_cleanup.set(), cleanup_thread.join(timeout=2.0)),
        )

        # 9. 初始化并挂载通信通道
        channels = []

        # -- 飞书通道 --
        feishu_cfg = config.get("channels", {}).get("feishu", {})
        if feishu_cfg.get("enabled"):
            from lite_agent.channels.feishu import FeishuChannel
            feishu_channel = FeishuChannel(feishu_cfg, agent)
            feishu_channel.start()
            channels.append(feishu_channel)
            app.lifecycle.register_resource("feishu_channel", feishu_channel.stop)

        # -- Telegram 通道 --
        tg_cfg = config.get("channels", {}).get("telegram", {})
        if tg_cfg.get("enabled"):
            from lite_agent.channels.telegram import TelegramChannel
            tg_channel = TelegramChannel(tg_cfg, agent)
            tg_channel.start()
            channels.append(tg_channel)
            app.lifecycle.register_resource("telegram_channel", tg_channel.stop)

        # -- 钉钉通道 --
        ding_cfg = config.get("channels", {}).get("dingtalk", {})
        if ding_cfg.get("enabled"):
            from lite_agent.channels.dingtalk import DingTalkChannel
            ding_channel = DingTalkChannel(ding_cfg, agent)
            ding_channel.start()
            channels.append(ding_channel)
            app.lifecycle.register_resource("dingtalk_channel", ding_channel.stop)

        # -- 企业微信通道 --
        wecom_cfg = config.get("channels", {}).get("wecom", {})
        if wecom_cfg.get("enabled"):
            from lite_agent.channels.wecom import WeComChannel
            wecom_ch = WeComChannel(wecom_cfg, agent)
            wecom_ch.start()
            channels.append(wecom_ch)
            app.lifecycle.register_resource("wecom_channel", wecom_ch.stop)

        # -- 个人微信通道 --
        wx_channel = None
        wx_cfg = config.get("channels", {}).get("wechat", {})
        if wx_cfg.get("enabled"):
            from lite_agent.channels.wechat import WeChatChannel
            wx_channel = WeChatChannel(wx_cfg, agent)
            wx_channel.start()
            channels.append(wx_channel)
            app.lifecycle.register_resource("wechat_channel", wx_channel.stop)

        # -- API 开放通道 --
        api_server = None
        if config.get("channels", {}).get("api", {}).get("enabled"):
            from lite_agent.channels.api import ApiServer
            api_server = ApiServer(config.get("channels", {}), agent)
            api_server.start()
            app.lifecycle.register_resource("api_server", api_server.stop)

        agent.channels = channels

        if mode == "full":
            # 注册通用配置级 cron 作业并启动 cron 引擎
            _register_configured_cron_jobs(app, agent, config)

        app.cron_service.start()

        print("✨ Lite Agent 启动就绪！按 Ctrl+C 优雅停止")

        # 10. 信号处理与优雅退出
        is_stopping = False

        def handle_signal(signum, frame):
            nonlocal is_stopping
            if is_stopping:
                return
            is_stopping = True
            print(f"\n🛑 捕获信号 ({signum})，正在优雅停止 Lite Agent 宿主应用...")
            failures = app.shutdown()
            if failures:
                print(f"⚠️ 部分资源清理异常: {failures}")
            print("👋 Lite Agent 已安全退出。")
            sys.exit(0)

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        # 保持主线程存活
        while not is_stopping:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 捕获 Ctrl+C，正在退出...")
        app.shutdown()
    except Exception as e:
        print(f"❌ Lite Agent 启动或运行异常: {e}，正在回滚释放已分配资源...")
        app.shutdown()
        raise


if __name__ == "__main__":
    main()
