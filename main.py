"""
Lite Agent 主程序入口
- 读取配置
- 初始化 Agent
- 启动启用的通道 (Feishu, Telegram等)
"""

import os
import json
import time
import threading
from agent import Agent
from channels.feishu import FeishuChannel
from channels.telegram import TelegramChannel

def load_config() -> dict:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, 'config.json')
    
    if not os.path.exists(config_path):
        print(f"❌ 找不到配置文件: {config_path}")
        print("💡 请复制 config.example.json 为 config.json 并修改相关配置")
        exit(1)
        
    import config_loader
    return config_loader.load_config()


def session_cleanup_task(agent: Agent, interval: int = 300):
    """后台定时清理过期会话的任务"""
    while True:
        try:
            time.sleep(interval)
            agent.session_mgr.cleanup_expired()
        except Exception as e:
            print(f"⚠️ 清理任务异常: {e}")


def _register_cron_jobs(agent: Agent, config: dict):
    """从 config.json 的 cron_jobs 列表注册定时任务"""
    import subprocess, sys

    # 确保 ops_backup 的模块级 CronManager().add_job 生效
    try:
        import skills.ops_backup
    except ImportError:
        pass

    cron = agent.cron
    root = config['project_root']
    feishu_cfg = config.get('channels', {}).get('feishu', {})
    admin_open_id = feishu_cfg.get('admin_open_id', '')

    from agent import AgentResponse

    def _import_skill(module: str, fn_name: str):
        sys.path.insert(0, root)
        mod = __import__(f'skills.{module}', fromlist=[fn_name])
        return getattr(mod, fn_name)

    def _send_card(text, title, color='blue'):
        tg_chat_id = config.get('channels', {}).get('telegram', {}).get('admin_chat_id', '')
        for ch_name in ('feishu', 'dingtalk', 'wecom', 'telegram'):
            ch = next((c for c in agent.channels if c.name == ch_name), None)
            if not ch or not hasattr(ch, 'send_to'):
                continue
            uid = tg_chat_id if ch_name == 'telegram' else admin_open_id
            if uid and ch.send_to(uid, AgentResponse(text, title=title, color=color)):
                return True
        return False

    for job in config.get('cron_jobs', []):
        name = job['name']
        if 'command' in job:
            cmd = job['command'].format(root=root)
            cron.add_job(name, job['time'],
                         lambda c=cmd: subprocess.run(c, shell=True, capture_output=True,
                                                      text=True, timeout=120).stdout.strip() or "(无输出)")
        elif 'skill' in job:
            module, fn_name = job['skill'].split('::')
            if fn_name == 'rss_push':
                def _rss_push():
                    text = _import_skill('ops_rss', 'rss_brief')()
                    if text:
                        _send_card(text, '📰 RSS 精选')
                        return 'RSS 精选已推送'
                    return '(无新文章)'
                fn = _rss_push
            elif fn_name == 'rss_precompute':
                def _rss_pre():
                    return _import_skill('ops_rss', 'rss_precompute')()
                fn = _rss_pre
            elif fn_name == 'daily_health':
                def _health():
                    try:
                        text = _import_skill('ops_self_check', '_get_health_report')()
                        _send_card(text, '🌙 每日系统体检报告', 'wathet')
                        return "巡检广播已推送"
                    except Exception as e:
                        return f"巡检广播失败: {e}"
                fn = _health
            else:
                def _generic():
                    return _import_skill(module, fn_name)()
                fn = _generic

            if 'time_range' in job:
                tr = job['time_range']
                for h in range(tr['start'], tr['end'] + 1):
                    cron.add_job(name, f'{h:02d}:{tr["minute"]:02d}', fn)
            else:
                cron.add_job(name, job['time'], fn)

    cron.start()
    print(f"📅 定时任务引擎就绪: 共注册 {len(cron.jobs)} 个任务")


def main():
    print("🤖 正在启动 Lite Agent...")
    config = load_config()
    
    # 1. 初始化 AI 核心
    agent = Agent(config)
    
    # 2. 启动会话清理线程
    threading.Thread(
        target=session_cleanup_task,
        args=(agent,),
        daemon=True,
        name="SessionCleanupThread"
    ).start()
    
    # 3. 初始化并启动通道
    channels = []
    
    # -- 飞书通道 --
    feishu_cfg = config.get('channels', {}).get('feishu', {})
    if feishu_cfg.get('enabled'):
        feishu_channel = FeishuChannel(feishu_cfg, agent)
        feishu_channel.start()
        channels.append(feishu_channel)
        
    # -- Telegram 通道 --
    tg_cfg = config.get('channels', {}).get('telegram', {})
    if tg_cfg.get('enabled'):
        tg_channel = TelegramChannel(tg_cfg, agent)
        tg_channel.start()
        channels.append(tg_channel)
        
    # -- 钉钉通道 --
    ding_cfg = config.get('channels', {}).get('dingtalk', {})
    if ding_cfg.get('enabled'):
        from channels.dingtalk import DingTalkChannel
        ding_channel = DingTalkChannel(ding_cfg, agent)
        ding_channel.start()
        channels.append(ding_channel)

    # -- 企业微信通道 (send-only, 复用 pushmsg) --
    wecom_cfg = config.get('channels', {}).get('wecom', {})
    if wecom_cfg.get('enabled'):
        from channels.wecom import WeComChannel
        wecom_ch = WeComChannel(wecom_cfg, agent)
        wecom_ch.start()
        channels.append(wecom_ch)

    # -- API 开放通道 --
    api_server = None
    if config.get('channels', {}).get('api', {}).get('enabled'):
        from channels.api import ApiServer
        api_server = ApiServer(config.get('channels', {}), agent)
        api_server.start()

    # 将所有激活的通道实例绑定到 Agent，以便后续广播
    agent.channels = channels

    if not channels and not api_server:
        print("⚠️ 没有启用任何通信通道，程序将退出。")
        return

    # 4. 注册定时任务并启动 Cron 引擎
    _register_cron_jobs(agent, config)

    print("✨ Lite Agent 启动完成！按 Ctrl+C 停止")
    
    # 保持主线程运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 正在停止服务...")
        for ch in channels:
            ch.stop()
        if api_server:
            api_server.stop()
        print("👋 再见！")

if __name__ == "__main__":
    main()
