import sys, os, subprocess
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from core.cron_engine import CronManager
from core.config_loader import load_config

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

def _create_meili_dump() -> str:
    """触发 Meilisearch 生成备份 Dump，返回 dump 目录路径或 None"""
    import urllib.request
    import urllib.parse
    import json
    import time
    
    cfg = load_config() or {}
    meili_cfg = cfg.get("meilisearch", {})
    url_base = meili_cfg.get("url", "http://127.0.0.1:7700")
    key = meili_cfg.get("master_key", "")
    
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    
    # 1. 触发 Dump
    req = urllib.request.Request(f"{url_base}/dumps", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            task_uid = data.get("taskUid")
    except Exception as e:
        print(f"Failed to trigger Meilisearch dump: {e}")
        return None
        
    if task_uid is None:
        return None
        
    # 2. 轮询等待成功 (最大等待 15s)
    for _ in range(15):
        req_status = urllib.request.Request(f"{url_base}/tasks/{task_uid}", headers=headers)
        try:
            with urllib.request.urlopen(req_status) as resp:
                task_data = json.loads(resp.read().decode('utf-8'))
                status = task_data.get("status")
                if status == "succeeded":
                    # Dump 导出成功，返回宿主机挂载的 dump 目录
                    dumps_dir = "/home/liteagent/meilisearch/meili_data/dumps"
                    if os.path.exists(dumps_dir):
                        return dumps_dir
                    return None
                elif status == "failed":
                    print(f"Meilisearch dump task failed: {task_data.get('error')}")
                    return None
        except Exception as e:
            print(f"Error checking dump task status: {e}")
            
        time.sleep(1.0)
        
    print("Meilisearch dump timed out")
    return None


# HedgeDoc 数据目录
_HEDGEDOC_COMPOSE = "/app/hedgedoc/docker-compose.yml"
_HEDGEDOC_DB_CONTAINER = "hedgedoc_database_1"
_HEDGEDOC_UPLOADS_VOLUME = "hedgedoc_hedgedoc_uploads"

def _backup_hedgedoc() -> list:
    """备份 HedgeDoc：数据库 pg_dump + uploads 卷 tar + docker-compose.yml，返回临时文件路径列表"""
    import tempfile
    import shutil

    tmp_dir = tempfile.mkdtemp(prefix="hedgedoc_backup_")
    results = []

    # 1. 数据库 pg_dump
    try:
        db_dump = os.path.join(tmp_dir, "hedgedoc_db.sql")
        with open(db_dump, "w") as f:
            subprocess.run(
                ["docker", "exec", _HEDGEDOC_DB_CONTAINER,
                 "pg_dump", "-U", "hedgedoc", "hedgedoc"],
                stdout=f,
                stderr=subprocess.PIPE,
                timeout=300,
                check=True
            )
        results.append(db_dump)
        print(f"  [hedgedoc] DB dump: {os.path.getsize(db_dump) // 1024} KB")
    except Exception as e:
        print(f"  [hedgedoc] DB dump failed: {e}")

    # 2. uploads 卷打包
    try:
        uploads_tar = os.path.join(tmp_dir, "hedgedoc_uploads.tar.gz")
        subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{_HEDGEDOC_UPLOADS_VOLUME}:/data:ro",
             "-v", f"{tmp_dir}:/backup",
             "postgres:13-alpine",
             "tar", "czf", "/backup/hedgedoc_uploads.tar.gz", "/data"],
            capture_output=True,
            timeout=600,
            check=True
        )
        results.append(uploads_tar)
        print(f"  [hedgedoc] uploads: {os.path.getsize(uploads_tar) // (1024*1024)} MB")
    except Exception as e:
        print(f"  [hedgedoc] uploads backup failed: {e}")

    # 3. docker-compose.yml
    if os.path.exists(_HEDGEDOC_COMPOSE):
        compose_dst = os.path.join(tmp_dir, "docker-compose.yml")
        shutil.copy2(_HEDGEDOC_COMPOSE, compose_dst)
        results.append(compose_dst)

    # 若全失败无产出，清理创建的临时目录
    if not results:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    return results


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
        
    # Meilisearch 索引库快照
    meili_snapshot = _create_meili_dump()
    meili_included = False
    if meili_snapshot:
        targets.append(meili_snapshot)
        meili_included = True
    
    # HedgeDoc 备份（数据库 + 附件卷 + 配置）
    hedgedoc_files = _backup_hedgedoc()
    for hf in hedgedoc_files:
        if os.path.exists(hf):
            targets.append(hf)

    # 过滤掉不存在的路径
    valid_targets = []
    for t in targets:
        if os.path.exists(t):
            valid_targets.append(t)
            
    if not valid_targets:
        # 即使找不到有效目标，也要清理创建的 HedgeDoc 临时目录及 Vaultwarden 快照
        for hf in hedgedoc_files:
            try:
                if os.path.isdir(os.path.dirname(hf)) and 'hedgedoc_backup_' in os.path.dirname(hf):
                    import shutil
                    shutil.rmtree(os.path.dirname(hf), ignore_errors=True)
                    break
            except Exception:
                pass
        if vw_included and vw_snapshot and vw_snapshot.endswith("db_backup.sqlite3"):
            try:
                os.remove(vw_snapshot)
            except Exception:
                pass
        return "❌ 找不到任何需要备份的源文件或目录。"

    try:
        # 使用 zip 命令压缩 (VPS 环境下通常有 zip 工具)
        cmd = ["zip", "-r", zip_path] + valid_targets
        subprocess.run(cmd, check=True, capture_output=True)

        # 获取压缩包大小
        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        
        # 清理旧备份 (本地仅保留最新 1 份，历史备份由百度网盘保留)
        cleaned_count = 0
        for f in os.listdir(backup_dir):
            if f.startswith("backup_") and f.endswith(".zip"):
                f_path = os.path.join(backup_dir, f)
                if os.path.abspath(f_path) != os.path.abspath(zip_path):
                    os.remove(f_path)
                    cleaned_count += 1

        meili_status = "✅ 已包含" if meili_included else "⚠️ 失败"
        hd_status = "✅ 已包含" if hedgedoc_files else "⚠️ 失败"
        vw_status = "✅ 已包含" if vw_included else "⚠️ 未找到"
        return (f"✅ 备份成功！\n"
                f"- 备份文件: `{zip_name}`\n"
                f"- 大小: `{size_mb:.2f} MB`\n"
                f"- Meilisearch 索引库: {meili_status}\n"
                f"- Vaultwarden 密码库: {vw_status}\n"
                f"- HedgeDoc 数据库+附件: {hd_status}\n"
                f"- 清理了 {cleaned_count} 个过期备份。")
        
    except Exception as e:
        return f"❌ 备份失败: {e}"
    finally:
        # 清理 HedgeDoc 临时文件
        for hf in hedgedoc_files:
            try:
                if os.path.isdir(os.path.dirname(hf)) and 'hedgedoc_backup_' in os.path.dirname(hf):
                    import shutil
                    shutil.rmtree(os.path.dirname(hf), ignore_errors=True)
                    break
            except Exception:
                pass

        # 清理 Vaultwarden 临时快照
        if vw_included and vw_snapshot and vw_snapshot.endswith("db_backup.sqlite3"):
            try:
                os.remove(vw_snapshot)
            except Exception:
                pass


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
    description='手动执行数据备份，包含聊天记录、邮件账单、Vaultwarden密码库、HedgeDoc数据库和附件。'
)
def ops_backup_data() -> str:
    return do_backup()


@skill(
    name='ops_backup_cloud',
    description='执行数据备份并同步到百度网盘，包含聊天记录、账单、Vaultwarden密码库、HedgeDoc数据库和附件。'
)
def ops_backup_cloud() -> str:
    return do_backup_and_sync()


# ==========================================
# 自动注册为定时任务：每天凌晨 03:00 执行备份+云同步
# ==========================================
CronManager().add_job("数据打包备份+云同步", "03:00", do_backup_and_sync)
