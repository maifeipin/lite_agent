"""
Sys Ops 系统资源、日志、定时任务与全面健康巡检业务服务 (features.sys_ops.health_service)。
从 skills/ops_sys.py, skills/ops_logs.py, skills/ops_crontab.py, skills/ops_self_check.py 迁入业务实现。
"""
import os
import sys
import subprocess
import re
import socket
import shlex
import time
import sqlite3
import json
from datetime import datetime
from typing import Optional, List, Dict, Any

from lite_agent.core.config_loader import load_config


def ops_sys_status(detail: bool = False) -> str:
    """获取VPS系统状态，包括主机名、运行时间、系统负载、CPU使用率、内存和磁盘信息"""
    sections = []

    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "(获取失败)"
    sections.append(f"主机名: {hostname}")

    try:
        r = subprocess.run(['uptime', '-p'], capture_output=True, text=True, timeout=5)
        uptime = r.stdout.strip() or r.stderr.strip() or "(获取失败)"
    except Exception as e:
        uptime = f"(获取失败: {e})"
    sections.append(f"运行时间: {uptime}")

    try:
        with open('/proc/loadavg', 'r') as f:
            loadavg = f.read().strip().split()
            load = f"{loadavg[0]} {loadavg[1]} {loadavg[2]}"
    except Exception:
        load = "(获取失败)"
    sections.append(f"系统负载: {load}")

    try:
        r = subprocess.run(['top', '-bn1'], capture_output=True, text=True, timeout=5)
        cpu_line = ""
        for line in r.stdout.splitlines():
            if 'Cpu(s)' in line or 'cpu(s)' in line.lower():
                cpu_line = line
                break
        if cpu_line:
            us_match = re.search(r'(\d+\.?\d*)\s*(?:us|用户)', cpu_line)
            sy_match = re.search(r'(\d+\.?\d*)\s*(?:sy|系统)', cpu_line)
            id_match = re.search(r'(\d+\.?\d*)\s*(?:id|空闲)', cpu_line)
            us = us_match.group(1) if us_match else "0.0"
            sy = sy_match.group(1) if sy_match else "0.0"
            idle = id_match.group(1) if id_match else "0.0"
            sections.append(f"CPU使用率: 用户 {us}%, 系统 {sy}%, 空闲 {idle}%")
        else:
            sections.append("CPU使用率: (解析失败)")
    except Exception as e:
        sections.append(f"CPU使用率: (获取失败: {e})")

    try:
        r = subprocess.run(['free', '-h'], capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines()
        if len(lines) >= 2:
            mem_parts = lines[1].split()
            if len(mem_parts) >= 4:
                sections.append(f"内存使用: 总计 {mem_parts[1]}, 已用 {mem_parts[2]}, 空闲 {mem_parts[3]}")
    except Exception as e:
        sections.append(f"内存使用: (获取失败: {e})")

    try:
        r = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines()
        if len(lines) >= 2:
            disk_parts = lines[1].split()
            if len(disk_parts) >= 5:
                sections.append(f"根分区磁盘: 总计 {disk_parts[1]}, 已用 {disk_parts[2]} ({disk_parts[4]}), 空闲 {disk_parts[3]}")
    except Exception as e:
        sections.append(f"根分区磁盘: (获取失败: {e})")

    if detail:
        sections.append("\n--- 资源占用前5的进程 ---")
        try:
            r = subprocess.run(['ps', 'aux', '--sort=-%mem'], capture_output=True, text=True, timeout=5)
            lines = r.stdout.splitlines()
            sections.append("按内存占用排序:")
            for line in lines[:6]:
                sections.append(line)
        except Exception:
            pass

    return "\n".join(sections)


def ops_read_logs(log_path: str, keyword: str = '', lines: int = 50) -> str:
    """读取指定日志文件并支持按关键字过滤"""
    if not os.path.isabs(log_path):
        return f'❌ 拒绝访问: 请提供绝对路径 ({log_path})'
    if not os.path.exists(log_path):
        return f'❌ 文件不存在: {log_path}'
    if not os.path.isfile(log_path):
        return f'❌ 不是一个有效的文件: {log_path}'

    try:
        read_limit = max(lines * 10, 1000)
        r = subprocess.run(["tail", "-n", str(read_limit), log_path], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return f"❌ 读取失败: {r.stderr.strip()}"

        log_lines = r.stdout.splitlines()
        if keyword:
            keyword_lower = keyword.lower()
            filtered = [line for line in log_lines if keyword_lower in line.lower()]
        else:
            filtered = log_lines

        output_lines = filtered[-lines:]
        output = "\n".join(output_lines)
        if not output:
            return f"日志中没有找到包含关键字 '{keyword}' 的行" if keyword else f"日志文件为空: {log_path}"
        return output
    except subprocess.TimeoutExpired:
        return '❌ 读取超时'
    except Exception as e:
        return f'❌ 读取出错: {e}'


def ops_read_journal(service_name: str = '', keyword: str = '', lines: int = 50, unit: str = '', priority: str = '') -> str:
    """读取 systemd journal 日志。兼容历史 service_name/keyword 与 unit/priority 契约"""
    target_service = (service_name or unit or '').strip()
    if target_service:
        cleaned = target_service.replace('-', '').replace('_', '').replace('.', '').replace('@', '')
        if not cleaned.isalnum():
            return f'❌ 拒绝访问: 非法的服务名称 ({target_service})'

    try:
        cmd = ['journalctl', '--no-pager', '-n', str(lines)]
        if target_service:
            cmd.extend(['-u', target_service])
        if priority:
            cmd.extend(['-p', priority])
        if keyword:
            cmd.extend(['--grep', keyword, '-i'])

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        output = r.stdout.strip()
        if not output:
            if target_service and keyword:
                return f"服务 {target_service} 的日志中没有找到包含关键字 '{keyword}' 的行"
            elif target_service:
                return f"服务 {target_service} 目前没有可用的系统日志"
            elif keyword:
                return f"日志中没有找到包含关键字 '{keyword}' 的行"
            return '无匹配日志'
        return output
    except subprocess.TimeoutExpired:
        return '❌ 读取超时 (>10秒)'
    except Exception as e:
        return f'❌ 执行 journalctl 失败: {e}'


def ops_list_crontab() -> str:
    """查看当前用户的 crontab 定时任务列表"""
    try:
        r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            if "no crontab" in r.stderr.lower():
                return '当前用户没有配置 crontab 定时任务'
            return f'❌ 获取失败: {r.stderr.strip()}'
        return r.stdout.strip()
    except Exception as e:
        return f'❌ 获取失败: {e}'


def ops_run_script(script_path: str, args: str = '') -> str:
    """执行 VPS 上的指定白名单脚本文件"""
    if not os.path.isabs(script_path):
        return '❌ 拒绝访问: 请提供绝对路径'
    if not os.path.exists(script_path):
        return f'❌ 脚本不存在: {script_path}'

    ext = os.path.splitext(script_path)[1].lower()
    if ext == '.sh':
        cmd_list = ['bash', script_path]
    elif ext == '.py':
        cmd_list = ['python3', script_path]
    else:
        return f'❌ 安全拦截: 不支持的脚本类型 {ext}，仅支持执行 .sh 和 .py 脚本'

    if args:
        cmd_list.extend(shlex.split(args))

    try:
        r = subprocess.run(cmd_list, capture_output=True, text=True, timeout=60)
        output = r.stdout.strip()
        stderr = r.stderr.strip()
        if r.returncode != 0:
            return f"⚠️ 脚本执行出错 (代码 {r.returncode}):\n{stderr or output}"
        return output or "✅ 脚本执行成功，无输出内容。"
    except subprocess.TimeoutExpired:
        return "❌ 脚本执行超时 (已超过 60 秒)"
    except Exception as e:
        return f"❌ 脚本调用失败: {e}"


def _svc_name() -> str:
    try:
        from lite_agent.core import config_loader
        return config_loader.load_config().get('service_name', 'feishu-bot')
    except Exception:
        return 'feishu-bot'


def get_health_report() -> str:
    """生成系统全方位健康自检报告"""
    report = ["🏥 **Lite Agent 系统全方位健康自检报告**\n"]
    from lite_agent.core.constants import PROJECT_ROOT
    base_dir = PROJECT_ROOT

    # 1. 核心服务进程 (Agent 本身)
    pid = os.getpid()
    try:
        import psutil
        p = psutil.Process(pid)
        mem = p.memory_info().rss / 1024 / 1024
        uptime = time.time() - p.create_time()
        hours, rem = divmod(uptime, 3600)
        minutes, _ = divmod(rem, 60)
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
        if "HTTP Error" in str(e):
            delay = (time.time() - start) * 1000
            report.append(f"🌐 **外网连通性 (API接口)**: ✅ 正常 (延迟: {delay:.0f}ms)")
        else:
            report.append(f"🌐 **外网连通性 (API接口)**: ⚠️ 连接异常 ({str(e)})")

    # 3. 配置文件解析验证
    config_path = os.path.join(base_dir, 'config.json')
    if os.path.exists(config_path):
        try:
            from lite_agent.core import config_loader
            cfg = config_loader.load_config() or {}
            llm_cfg = cfg.get('llm', {})
            default_key = llm_cfg.get('default')
            mc = llm_cfg.get('models', {}).get(default_key, {}) if default_key else {}
            default_model = mc.get('model', default_key or llm_cfg.get('model', 'unknown'))
            llm_key = mc.get('api_key', llm_cfg.get('api_key', ''))

            report.append(f"🧠 **当前配置的主模型 (Default Model)**: ✅ {default_model}")

            if llm_key and not llm_key.startswith('${'):
                report.append("🔑 **大模型 API 密钥**: ✅ 已配置")
            else:
                report.append("🔑 **大模型 API 密钥**: ❌ 配置异常 (未配置或环境变量未解析)")

            feishu = cfg.get('channels', {}).get('feishu', {}).get('enabled', False)
            dingtalk = cfg.get('channels', {}).get('dingtalk', {}).get('enabled', False)
            wecom = cfg.get('channels', {}).get('wecom', {}).get('enabled', False)
            wechat = cfg.get('channels', {}).get('wechat', {}).get('enabled', False)
            tg = cfg.get('channels', {}).get('telegram', {}).get('enabled', False)
            channels = []
            if feishu: channels.append("飞书")
            if dingtalk: channels.append("钉钉")
            if wecom: channels.append("企业微信")
            if wechat: channels.append("个人微信")
            if tg: channels.append("Telegram")
            report.append(f"📡 **启用的通讯通道**: ✅ {', '.join(channels) if channels else '无'}")

            try:
                from lite_agent.features.sys_ops.llm_ops_service import check_deepseek_balance
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

    # 5. 记忆引擎 (ChromaDB)
    memory_path = os.path.join(base_dir, 'data', 'chroma')
    memory_db = os.path.join(base_dir, 'data', 'memory.db')

    try:
        try:
            __import__('pysqlite3')
            import sys
            sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
        except ImportError:
            pass

        import chromadb
        chromadb_available = True
    except ImportError:
        chromadb_available = False

    chroma_initialized = os.path.exists(memory_path) and os.path.isdir(memory_path)

    sqlite_stats = None
    if os.path.exists(memory_db):
        try:
            conn = sqlite3.connect(memory_db)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM memories")
            total = c.fetchone()[0]
            c.execute("SELECT type, COUNT(*) FROM memories GROUP BY type")
            types = c.fetchall()
            conn.close()
            type_str = ", ".join(f"{t}:{c}" for t, c in types)
            sqlite_stats = (total, type_str)
        except Exception:
            pass

    if chromadb_available and chroma_initialized:
        db_size = sum(os.path.getsize(os.path.join(dp, f))
                      for dp, dn, filenames in os.walk(memory_path)
                      for f in filenames) / 1024 / 1024
        report.append(f"🧠 **长期记忆向量库 (Chroma)**: ✅ 正常 (占用: {db_size:.2f} MB)")
        if sqlite_stats:
            report.append(f"   - 记忆总数: {sqlite_stats[0]} ({sqlite_stats[1]})" if sqlite_stats[1] else f"   - 记忆总数: {sqlite_stats[0]}")
    elif chromadb_available and not chroma_initialized:
        report.append("🧠 **长期记忆向量库**: ⚠️ ChromaDB 已安装但等待首次写入激活（发送一条消息后自动初始化）")
    elif not chromadb_available and chroma_initialized:
        report.append("🧠 **长期记忆向量库**: ⚠️ 目录存在但 chromadb 包未安装 (pip install chromadb)")
    else:
        report.append("🧠 **长期记忆向量库**: ⚠️ 未安装 (pip install chromadb sentence-transformers)")

    # 5.5 记忆蒸馏任务状态检查
    try:
        r_distill = subprocess.run(
            ["journalctl", "-u", svc, "--since", "today", "-n", "2", "--grep", "蒸馏"],
            capture_output=True, text=True, timeout=5
        )
        if r_distill.stdout.strip():
            last_line = r_distill.stdout.strip().split('\n')[-1]
            status_text = last_line.split(':')[-1].strip() if ':' in last_line else last_line
            report.append(f"🧪 **记忆蒸馏复盘**: ✅ 运行正常 (最后日志: {status_text})")
        else:
            report.append("🧪 **记忆蒸馏复盘**: ⚠️ 今日暂无执行记录 (或未到触发时间)")
    except Exception as e:
        report.append(f"🧪 **记忆蒸馏复盘**: ⚠️ 日志读取异常 ({str(e)})")

    # 6. 系统底层守护进程
    try:
        r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True, timeout=5)
        if r.stdout.strip() == 'active':
            report.append(f"⚙️ **Systemd 守护进程 ({svc})**: ✅ Active (作为系统级后台服务稳定运行中)")
        else:
            report.append(f"⚙️ **Systemd 守护进程 ({svc})**: ⚠️ 未激活 (当前可能为手动前台运行模式)")
    except Exception:
        report.append(f"⚙️ **Systemd 守护进程 ({svc})**: ⚠️ 状态检查失败")

    # 6.5 备份任务及云同步检查
    try:
        import glob
        backup_dir = os.path.join(base_dir, 'backup')
        status_file = os.path.join(base_dir, 'data', 'backup_status.json')

        status_loaded = False
        if os.path.exists(status_file):
            try:
                with open(status_file, "r", encoding="utf-8") as sf:
                    sdata = json.load(sf)

                last_time_str = sdata.get("last_backup_time")
                last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
                ago = (datetime.now() - last_time).total_seconds() / 3600

                local_files = sdata.get("local_files") or sdata.get("backup_files", [])
                files_exist = True
                missing_files = []
                for lf in local_files:
                    if not os.path.exists(lf):
                        files_exist = False
                        missing_files.append(os.path.basename(lf))

                sync_info = sdata.get("baidu_pcs_sync", {})
                sync_status = sync_info.get("status", "pending")
                sync_err = sync_info.get("error_message")

                local_desc = "✅ 已完成" if (ago < 25 and files_exist) else "⚠️ 异常"
                if not files_exist:
                    local_desc += f" (分卷丢失: {', '.join(missing_files)})"
                elif ago >= 25:
                    local_desc += f" (超24h未备份, {ago:.0f}h 前)"
                else:
                    local_desc += f" ({ago:.1f}h 前)"

                if sync_status == "success":
                    cloud_desc = "✅ 成功"
                elif sync_status == "pending":
                    cloud_desc = "⏳ 进行中/等待"
                else:
                    cloud_desc = f"❌ 失败 ({sync_err})"

                report.append(f"💾 **数据备份**: {local_desc}")
                report.append(f"   - ☁️ **云端同步**: {cloud_desc}")

                checklist = sdata.get("backup_checklist") or sdata.get("checklist", {})
                checklist_desc = ", ".join(f"{k}: {v}" for k, v in checklist.items())
                report.append(f"   - 📋 **备份清单**: {checklist_desc}")

                status_loaded = True
            except Exception as je:
                print(f"Failed to parse backup_status.json in self check: {je}")

        if not status_loaded:
            backups = sorted(
                glob.glob(os.path.join(backup_dir, 'lite_agent_backup_*.zip')) +
                glob.glob(os.path.join(backup_dir, 'backup_*.zip')) +
                glob.glob(os.path.join(backup_dir, 'data_*.zip')),
                reverse=True
            )
            if backups:
                latest = backups[0]
                mtime = datetime.fromtimestamp(os.path.getmtime(latest))
                ago = (datetime.now() - mtime).total_seconds() / 3600
                if ago < 25:
                    report.append(f"💾 **数据备份**: ✅ 已完成 (最新: {os.path.basename(latest)}, {ago:.1f}h 前)")
                else:
                    report.append(f"💾 **数据备份**: ⚠️ 超过24h未备份 (最新: {os.path.basename(latest)}, {ago:.0f}h 前)")
            else:
                report.append("💾 **数据备份**: ⚠️ 暂无备份文件")
            report.append("   - ☁️ **云端同步**: ⚠️ 无法获取同步状态 (未找到状态文件)")

    except Exception as e:
        report.append(f"💾 **数据备份**: ⚠️ 检查失败 ({str(e)})")

    # 7. 技能模块加载状态
    try:
        skill_dir = os.path.join(base_dir, 'skills')
        skill_files = len([f for f in os.listdir(skill_dir) if f.startswith('ops_') and f.endswith('.py')])
        from lite_agent.core.skill_engine import _skill_registry
        skill_count = len(_skill_registry)
        report.append(f"🛠️ **技能引擎 (Skill Engine)**: ✅ 注册 {skill_count} 个工具 (来自 {skill_files} 个文件)")
    except Exception:
        pass

    # 8. 磁盘与负载概况
    report.append(f"\n💻 **系统负载与资源**:\n{ops_sys_status(detail=False)}")
    return "\n".join(report)


def ops_self_check() -> str:
    return get_health_report()
