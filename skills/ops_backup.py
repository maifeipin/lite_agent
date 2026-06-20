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
                    
        return f"✅ 备份成功！\n- 备份文件: `{zip_name}`\n- 大小: `{size_mb:.2f} MB`\n- 清理了 {cleaned_count} 个过期备份。"
        
    except Exception as e:
        return f"❌ 备份失败: {e}"

@skill(
    name='ops_backup_data',
    description='手动执行数据备份，打包最新的聊天记录数据库和邮件账单内容。'
)
def ops_backup_data() -> str:
    return do_backup()

# ==========================================
# 自动注册为定时任务：每天凌晨 03:00 执行备份
# ==========================================
CronManager().add_job("数据打包备份", "03:00", do_backup)
