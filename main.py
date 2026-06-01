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
        
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def session_cleanup_task(agent: Agent, interval: int = 300):
    """后台定时清理过期会话的任务"""
    while True:
        try:
            time.sleep(interval)
            agent.session_mgr.cleanup_expired()
        except Exception as e:
            print(f"⚠️ 清理任务异常: {e}")


def _register_cron_jobs(agent: Agent):
    """注册系统定时任务到 CronManager"""
    import subprocess

    cron = agent.cron

    # 1. 证书过期检查 — 每天 09:00
    def check_cert():
        r = subprocess.run("bash /root/down/check_cert_expiry.sh",
                           shell=True, capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or r.stderr.strip() or "(无输出)"

    cron.add_job("证书过期检查", "09:00", check_cert)

    # 2. 记忆蒸馏复盘 — 每天 03:00
    def daily_distill():
        r = subprocess.run(
            "python3 /root/lite_agent/skills/ops_memory_distiller.py --mode daily",
            shell=True, capture_output=True, text=True, timeout=120)
        return r.stdout.strip() or r.stderr.strip() or "(无输出)"

    cron.add_job("记忆蒸馏复盘", "03:00", daily_distill)

    # 3. 系统状态巡检 — 每天 08:00
    def sys_status():
        r = subprocess.run(
            "python3 -c \"import sys; sys.path.insert(0,'/root/lite_agent'); "
            "from skills.ops_sys import ops_sys_status; print(ops_sys_status(detail=True))\"",
            shell=True, capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or r.stderr.strip() or "(无输出)"

    cron.add_job("系统状态巡检", "08:00", sys_status)

    # 4. 系统自动巡检与推送 — 每天 23:50
    def daily_health_check():
        import sys, os
        from agent import AgentResponse
        try:
            sys.path.insert(0, '/root/lite_agent')
            from skills.ops_self_check import _get_health_report
            report_text = _get_health_report()
            agent.broadcast(AgentResponse(report_text, title="🌙 每日系统体检报告", color="wathet"))
            return "巡检广播已推送"
        except Exception as e:
            return f"巡检广播失败: {e}"

    cron.add_job("每日健康巡检广播", "23:50", daily_health_check)

    # 5. RSS 精选推送 — 每天 9:00-22:00 每小时过 3 分钟
    def rss_push():
        import sys
        sys.path.insert(0, '/root/lite_agent')
        from agent import AgentResponse
        from skills.ops_rss import rss_brief
        text = rss_brief()
        if text:
            feishu_ch = next((ch for ch in agent.channels if ch.name == 'feishu'), None)
            if feishu_ch:
                feishu_ch.broadcast(AgentResponse(text, title='📰 RSS 精选', color='blue'))
                return 'RSS 精选已推送'
            return '(飞书通道未启用)'
        return '(无新文章)'

    for h in range(9, 23):
        cron.add_job(f'RSS 精选推送', f'{h:02d}:03', rss_push)

    # 启动 Cron 引擎后台线程
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

    # 将所有激活的通道实例绑定到 Agent，以便后续广播
    agent.channels = channels

    if not channels:
        print("⚠️ 没有启用任何通信通道，程序将退出。")
        return

    # 4. 注册定时任务并启动 Cron 引擎
    _register_cron_jobs(agent)

    print("✨ Lite Agent 启动完成！按 Ctrl+C 停止")
    
    # 保持主线程运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 正在停止服务...")
        for ch in channels:
            ch.stop()
        print("👋 再见！")

if __name__ == "__main__":
    main()
