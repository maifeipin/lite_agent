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
        
    if not channels:
        print("⚠️ 没有启用任何通信通道，程序将退出")
        return
        
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
