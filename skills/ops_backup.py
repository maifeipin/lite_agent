import sys, os, subprocess
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill
from cron_engine import CronManager
from config_loader import load_config

# 账单解析程序目录：与 ops_billing 共用 config.json 的 billing.script_dir，
# 未配置时回退默认路径（vps1: /home/liteagent/mail-statement-parser）。
_cfg = load_config() or {}
_BILLING_DIR = _cfg.get("billing", {}).get("script_dir", "/home/liteagent/mail-statement-parser")

# Vaultwarden 密码库数据目录
_VAULTWARDEN_DATA = "/opt/vaultwarden/vw-data"

def _backup_vaultwarden() -> str:
    """对 Vaultwarden 的 SQLite 数据库做一致性快照，返回临时备份文件路径或 None"""
    db_path = os.path.join(_VAULTWARDEN_DATA, "db.sqlite3")
    if not os.path.exists(db_path):
        return None
    snapshot_path = os.path.join(_VAULTWARDEN_DATA, "db_backup.sqlite3")
    try:
        subprocess.run(
            ["sqlite3", db_path, f".backup '{snapshot_path}'"],
            check=True, capture_output=True, timeout=30
        )
        return snapshot_path
    except Exception:
        # 如果 sqlite3 命令不可用，直接使用原文件（仍然安全，因为是加密密文）
        return db_path

def do_backup() -> str:
    """内部函数：执行备份逻辑"""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    backup_dir = os.path.join(base_dir, "backup")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"backup_{timestamp}.zip"
    zip_path = os.path.join(backup_dir, zip_name)

    # 待备份的目录或文件
    targets = [
        os.path.join(base_dir, "data"), # lite_agent/data/sessions.db
        os.path.join(_BILLING_DIR, "statements.db"),
        os.path.join(_BILLING_DIR, "email-downloads"),
        os.path.join(_BILLING_DIR, "validation-reports")
    ]

    # Vaultwarden 密码库快照
    vw_snapshot = _backup_vaultwarden()
    vw_included = False
    if vw_snapshot:
        targets.append(vw_snapshot)
        vw_included = True
    
    # 过滤掉不存在的路径
    valid_targets = []
    for t in targets:
        if os.path.exists(t):
            valid_targets.append(t)
            
    if not valid_targets:
        return "❌ 找不到任何需要备份的源文件或目录。"

    try:
        # 使用 zip 命令压缩 (VPS 环境下通常有 zip 工具)
        cmd = ["zip", "-r", zip_path] + valid_targets
        subprocess.run(cmd, check=True, capture_output=True)
        
        # 清理 Vaultwarden 临时快照
        if vw_included and vw_snapshot and vw_snapshot.endswith("db_backup.sqlite3"):
            try:
                os.remove(vw_snapshot)
            except Exception:
                pass

        # 获取压缩包大小
        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        
        # 清理旧备份 (保留最近 30 天)
        retention_days = 30
        now = time.time()
        cleaned_count = 0
        for f in os.listdir(backup_dir):
            if f.startswith("backup_") and f.endswith(".zip"):
                f_path = os.path.join(backup_dir, f)
                if os.stat(f_path).st_mtime < now - retention_days * 86400:
                    os.remove(f_path)
                    cleaned_count += 1

        vw_status = "✅ 已包含" if vw_included else "⚠️ 未找到"
        return (f"✅ 备份成功！\n"
                f"- 备份文件: `{zip_name}`\n"
                f"- 大小: `{size_mb:.2f} MB`\n"
                f"- Vaultwarden 密码库: {vw_status}\n"
                f"- 清理了 {cleaned_count} 个过期备份。")
        
    except Exception as e:
        return f"❌ 备份失败: {e}"


def do_backup_and_sync() -> str:
    """执行备份并同步到百度网盘"""
    # 第一步：执行备份
    backup_result = do_backup()
    if not backup_result.startswith("✅"):
        return backup_result

    # 第二步：同步到百度网盘
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    backup_dir = os.path.join(base_dir, "backup")
    try:
        sync_result = subprocess.run(
            ["bypy", "syncup", backup_dir, "lite_agent/backup"],
            capture_output=True, text=True, timeout=3600
        )
        if sync_result.returncode == 0:
            return backup_result + "\n\n📤 百度网盘同步: ✅ 已上传至 `lite_agent/backup`"
        else:
            return backup_result + f"\n\n📤 百度网盘同步: ❌ 失败\n{sync_result.stderr}"
    except subprocess.TimeoutExpired:
        return backup_result + "\n\n📤 百度网盘同步: ❌ 超时 (3600s)"
    except Exception as e:
        return backup_result + f"\n\n📤 百度网盘同步: ❌ 异常: {e}"


@skill(
    name='ops_backup_data',
    description='手动执行数据备份，打包最新的聊天记录数据库、邮件账单和Vaultwarden密码库。'
)
def ops_backup_data() -> str:
    return do_backup()


@skill(
    name='ops_backup_cloud',
    description='执行数据备份并同步到百度网盘，包含聊天记录、账单、Vaultwarden密码库。'
)
def ops_backup_cloud() -> str:
    return do_backup_and_sync()


# ==========================================
# 自动注册为定时任务：每天凌晨 03:00 执行备份+云同步
# ==========================================
CronManager().add_job("数据打包备份+云同步", "03:00", do_backup_and_sync)
