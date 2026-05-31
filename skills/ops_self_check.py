import os
import sqlite3
import subprocess
import time
import json
from skill_engine import skill

def _get_health_report() -> str:
    """生成系统全方位健康自检报告"""
    report = ["🏥 **Lite Agent 系统全方位健康自检报告**\n"]
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    # 1. 核心服务进程 (Agent 本身)
    pid = os.getpid()
    try:
        import psutil
        p = psutil.Process(pid)
        mem = p.memory_info().rss / 1024 / 1024
        uptime = time.time() - p.create_time()
        hours, rem = divmod(uptime, 3600)
        minutes, seconds = divmod(rem, 60)
        report.append(f"🤖 **核心进程**: ✅ 正常运行 (PID: {pid})")
        report.append(f"   - 运行时长: {int(hours)}小时 {int(minutes)}分钟")
        report.append(f"   - 内存占用: {mem:.2f} MB")
    except ImportError:
        report.append(f"🤖 **核心进程**: ✅ 正常运行 (PID: {pid})")

    # 2. 网络连通性
    try:
        import urllib.request
        start = time.time()
        urllib.request.urlopen("https://api.deepseek.com/", timeout=3)
        delay = (time.time() - start) * 1000
        report.append(f"🌐 **外网连通性 (API接口)**: ✅ 正常 (延迟: {delay:.0f}ms)")
    except Exception as e:
        # DeepSeek根路径可能返回404，但只要不是网络超时/拒绝连接说明通的
        if "HTTP Error" in str(e):
            delay = (time.time() - start) * 1000
            report.append(f"🌐 **外网连通性 (API接口)**: ✅ 正常 (延迟: {delay:.0f}ms)")
        else:
            report.append(f"🌐 **外网连通性 (API接口)**: ⚠️ 连接异常 ({str(e)})")

    # 3. 配置文件解析验证
    config_path = os.path.join(base_dir, 'config.json')
    if os.path.exists(config_path):
        try:
            cfg = json.load(open(config_path, encoding='utf-8'))
            llm_key = cfg.get('llm', {}).get('api_key', '')
            if llm_key.startswith('sk-') and len(llm_key) > 10:
                report.append("🔑 **大模型 API 密钥**: ✅ 已配置 (格式正确)")
            else:
                report.append("🔑 **大模型 API 密钥**: ❌ 配置异常 (格式不合规或缺失)")
                
            feishu = cfg.get('channels', {}).get('feishu', {}).get('enabled', False)
            dingtalk = cfg.get('channels', {}).get('dingtalk', {}).get('enabled', False)
            channels = []
            if feishu: channels.append("飞书")
            if dingtalk: channels.append("钉钉")
            report.append(f"📡 **启用的通讯通道**: ✅ {', '.join(channels) if channels else '无'}")
            
            # --- 检查 LLM 余额 ---
            try:
                from skills.ops_llm import check_deepseek_balance
                balance_text = check_deepseek_balance()
                report.append(f"💰 **大模型计费与余额**: \n{balance_text}")
            except Exception as e:
                report.append(f"💰 **大模型计费与余额**: ❌ 查询失败 ({e})")
                
        except Exception as e:
            report.append(f"🔑 **配置文件解析**: ❌ 失败 ({e})")
    else:
        report.append("🔑 **配置文件**: ❌ 未找到 config.json")

    # 4. SQLite 会话数据库
    db_path = os.path.join(base_dir, 'data', 'sessions.db')
    if os.path.exists(db_path):
        size_kb = os.path.getsize(db_path) / 1024
        try:
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM sessions")
            cnt = c.fetchone()[0]
            conn.close()
            report.append(f"🗄️ **会话数据库 (SQLite)**: ✅ 读写正常 (大小: {size_kb:.1f} KB, 会话记录: {cnt}条)")
        except Exception as e:
            report.append(f"🗄️ **会话数据库 (SQLite)**: ❌ 读写异常 ({e})")
    else:
        report.append("🗄️ **会话数据库 (SQLite)**: ⚠️ 文件暂未创建 (暂无记录)")

    # 5. 记忆引擎 (ChromaDB + JSONL)
    memory_path = os.path.join(base_dir, 'data', 'memory')
    if os.path.exists(memory_path):
        db_size = sum(os.path.getsize(os.path.join(dp, f)) for dp, dn, filenames in os.walk(memory_path) for f in filenames) / 1024 / 1024
        report.append(f"🧠 **长期记忆向量库 (Chroma)**: ✅ 正常挂载 (占用: {db_size:.2f} MB)")
    else:
        report.append("🧠 **长期记忆向量库**: ⚠️ 暂未初始化")

    # 6. 系统底层守护进程
    r = subprocess.run("systemctl is-active feishu-bot", shell=True, capture_output=True, text=True)
    if r.stdout.strip() == 'active':
        report.append("⚙️ **Systemd 守护进程**: ✅ Active (作为系统级后台服务稳定运行中)")
    else:
        report.append("⚙️ **Systemd 守护进程**: ⚠️ 未激活 (当前可能为手动前台运行模式)")

    # 7. 技能模块加载状态
    skill_dir = os.path.dirname(__file__)
    skills_count = len([f for f in os.listdir(skill_dir) if f.startswith('ops_') and f.endswith('.py')])
    report.append(f"🛠️ **技能引擎 (Skill Engine)**: ✅ 状态健康 (已挂载 {skills_count} 个扩展技能文件)")
    
    return "\n".join(report)

@skill(
    name='ops_self_check',
    description='对 Agent 机器人进行全方位健康自检，检查网络、配置、数据库、内存和后台服务状态。'
)
def ops_self_check() -> str:
    """返回机器人系统的健康自检报告"""
    return _get_health_report()
