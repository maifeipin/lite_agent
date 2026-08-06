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

def _safe_symlink(src, dst):
    """安全地创建软链接，若目标已存在则先删除，若源不存在则忽略"""
    if src and os.path.exists(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.lexists(dst):
            try:
                if os.path.isdir(dst) and not os.path.islink(dst):
                    import shutil
                    shutil.rmtree(dst, ignore_errors=True)
                else:
                    os.remove(dst)
            except Exception:
                pass
        try:
            os.symlink(src, dst)
        except Exception as e:
            print(f"Failed to create symlink from {src} to {dst}: {e}")

def _backup_mongodb() -> str:
    """对 MongoDB 中的 rsslite 数据库做 dump 备份，返回临时备份文件路径或 None"""
    import tempfile
    cfg = load_config() or {}
    uri = cfg.get("rssdb", {}).get("uri", "")
    if not uri:
        print("  [mongodb] rsslite backup skipped: RSSDB_URI not configured.")
        return None

    # 自动适配 authSource=admin
    if "authSource=" not in uri:
        if "?" in uri:
            uri += "&authSource=admin"
        else:
            uri += "/?authSource=admin"

    try:
        tmp_dir = tempfile.mkdtemp(prefix="mongo_backup_")
        archive_path = os.path.join(tmp_dir, "mongo_dump_rsslite.gz")
        
        # 运行 mongodump
        cmd = [
            "mongodump",
            f"--uri={uri}",
            f"--archive={archive_path}",
            "--gzip",
            "--db", "rsslite"
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        print(f"  [mongodb] DB dump size: {os.path.getsize(archive_path) // 1024} KB")
        return archive_path
    except subprocess.CalledProcessError as e:
        print(f"  [mongodb] DB dump failed (exit {e.returncode}): {e.stderr.decode('utf-8', errors='ignore')}")
        return None
    except Exception as e:
        print(f"  [mongodb] DB dump failed: {e}")
        return None


def do_backup() -> str:
    """内部函数：执行备份逻辑"""
    import tempfile
    import shutil
    import json

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    backup_dir = os.path.join(base_dir, "backup")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"lite_agent_backup_{timestamp}.zip"
    zip_path = os.path.join(backup_dir, zip_name)

    checklist = {
        "lite_agent_core": "Failed",
        "rsslite_mongodb": "Missing",
        "vaultwarden": "Missing",
        "meilisearch": "Missing",
        "hedgedoc": "Missing"
    }

    # 1. 准备符号链接临时目录树以避免磁盘双倍占用
    tree_dir = tempfile.mkdtemp(prefix="backup_tree_")

    try:
        # A. Lite Agent Core
        core_targets = [
            (os.path.join(base_dir, "data"), os.path.join(tree_dir, "lite_agent", "data")),
            (os.path.join(_BILLING_DIR, "statements.db"), os.path.join(tree_dir, "lite_agent", "statements.db")),
            (os.path.join(_BILLING_DIR, "email-downloads"), os.path.join(tree_dir, "lite_agent", "email-downloads")),
            (os.path.join(_BILLING_DIR, "validation-reports"), os.path.join(tree_dir, "lite_agent", "validation-reports"))
        ]
        core_ok = False
        for src, dst in core_targets:
            if os.path.exists(src):
                _safe_symlink(src, dst)
                core_ok = True
        if core_ok:
            checklist["lite_agent_core"] = "OK"

        # B. MongoDB rsslite
        mongo_snapshot = _backup_mongodb()
        if mongo_snapshot:
            _safe_symlink(mongo_snapshot, os.path.join(tree_dir, "rsslite", "mongo_dump_rsslite.gz"))
            checklist["rsslite_mongodb"] = "OK"

        # C. Vaultwarden
        vw_snapshot = _backup_vaultwarden()
        vw_included = False
        if vw_snapshot:
            _safe_symlink(vw_snapshot, os.path.join(tree_dir, "vaultwarden", "db_backup.sqlite3"))
            checklist["vaultwarden"] = "OK"
            vw_included = True

        # D. Meilisearch
        meili_snapshot = _create_meili_dump()
        meili_included = False
        if meili_snapshot:
            _safe_symlink(meili_snapshot, os.path.join(tree_dir, "meilisearch", "meilisearch_dump"))
            checklist["meilisearch"] = "OK"
            meili_included = True

        # E. HedgeDoc
        hedgedoc_files = _backup_hedgedoc()
        if hedgedoc_files:
            for hf in hedgedoc_files:
                name = os.path.basename(hf)
                _safe_symlink(hf, os.path.join(tree_dir, "hedgedoc", name))
            checklist["hedgedoc"] = "OK"

        # 检查是否没有任何有效文件可打包
        has_files = False
        for root, dirs, files in os.walk(tree_dir):
            if files or dirs:
                has_files = True
                break
        if not has_files:
            return "❌ 找不到任何需要备份的源文件或目录。"

        # 使用 zip -s 1g 命令分卷压缩，跟随符号链接保存（即不加 -y），cwd=tree_dir
        cmd = ["zip", "-s", "1g", "-r", zip_path, "."]
        subprocess.run(cmd, cwd=tree_dir, check=True, capture_output=True)

        # 计算这一组备份所有分卷的总大小
        size_mb = 0
        current_base = os.path.splitext(os.path.basename(zip_path))[0]
        local_files = []
        for f in os.listdir(backup_dir):
            if f.startswith(current_base):
                file_path = os.path.join(backup_dir, f)
                size_mb += os.path.getsize(file_path) / (1024 * 1024)
                local_files.append(os.path.abspath(file_path))
        local_files.sort()

        # 写入 data/backup_status.json
        status_file = os.path.join(base_dir, "data", "backup_status.json")
        os.makedirs(os.path.dirname(status_file), exist_ok=True)
        status_data = {
            "last_backup_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "local_files": local_files,
            "total_size_mb": round(size_mb, 2),
            "backup_checklist": checklist,
            "baidu_pcs_sync": {
                "status": "pending",
                "error_message": None,
                "completion_time": None
            }
        }
        with open(status_file, "w", encoding="utf-8") as sf:
            json.dump(status_data, sf, ensure_ascii=False, indent=2)

        # 清理旧备份 (本地仅保留最新一组备份分卷，兼容旧格式 backup_ 和新格式 lite_agent_backup_ 文件的清除)
        cleaned_count = 0
        for f in os.listdir(backup_dir):
            if (f.startswith("backup_") or f.startswith("lite_agent_backup_")) and not f.startswith(current_base):
                os.remove(os.path.join(backup_dir, f))
                cleaned_count += 1

        meili_status = "✅ 已包含" if meili_included else "⚠️ 失败"
        hd_status = "✅ 已包含" if hedgedoc_files else "⚠️ 失败"
        vw_status = "✅ 已包含" if vw_included else "⚠️ 未找到"
        mongo_status = "✅ 已包含" if mongo_snapshot else "⚠️ 未找到"
        return (f"✅ 备份成功！\n"
                f"- 备份文件: `{zip_name}`\n"
                f"- 大小: `{size_mb:.2f} MB`\n"
                f"- Lite Agent 数据库: {checklist['lite_agent_core']}\n"
                f"- Meilisearch 索引库: {meili_status}\n"
                f"- Vaultwarden 密码库: {vw_status}\n"
                f"- HedgeDoc 数据库+附件: {hd_status}\n"
                f"- RSS (rsslite) MongoDB数据库: {mongo_status}\n"
                f"- 清理了 {cleaned_count} 个过期备份。")

    except Exception as e:
        return f"❌ 备份失败: {e}"
    finally:
        # 清理临时链接目录树
        if 'tree_dir' in locals() and os.path.exists(tree_dir):
            shutil.rmtree(tree_dir, ignore_errors=True)

        # 清理 HedgeDoc 临时文件
        if 'hedgedoc_files' in locals() and hedgedoc_files:
            for hf in hedgedoc_files:
                try:
                    parent_dir = os.path.dirname(hf)
                    if os.path.isdir(parent_dir) and 'hedgedoc_backup_' in os.path.basename(parent_dir):
                        shutil.rmtree(parent_dir, ignore_errors=True)
                        break
                except Exception:
                    pass

        # 清理 Vaultwarden 临时快照
        if 'vw_snapshot' in locals() and vw_snapshot and vw_snapshot.endswith("db_backup.sqlite3"):
            try:
                os.remove(vw_snapshot)
            except Exception:
                pass

        # 清理 MongoDB 临时文件
        if 'mongo_snapshot' in locals() and mongo_snapshot:
            try:
                parent_dir = os.path.dirname(mongo_snapshot)
                if os.path.isdir(parent_dir) and 'mongo_backup_' in os.path.basename(parent_dir):
                    shutil.rmtree(parent_dir, ignore_errors=True)
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
    status_file = os.path.join(base_dir, "data", "backup_status.json")

    sync_status = "success"
    err_msg = None

    try:
        # 指定 --slice 3G 强制单文件上传，规避百度 PCS 分片 API (type=tmpfile) 的 31064 权限报错
        sync_result = subprocess.run(
            ["bypy", "--slice", "3G", "syncup", backup_dir, "lite_agent/backup"],
            capture_output=True, text=True, timeout=3600
        )
        if sync_result.returncode != 0:
            sync_status = "failed"
            err_msg = sync_result.stderr or "bypy returned non-zero code"
    except subprocess.TimeoutExpired:
        sync_status = "failed"
        err_msg = "bypy upload timeout (3600s)"
    except Exception as e:
        sync_status = "failed"
        err_msg = f"bypy exception: {e}"

    # 更新 backup_status.json 中的网盘同步状态
    if os.path.exists(status_file):
        try:
            import json
            with open(status_file, "r", encoding="utf-8") as sf:
                data = json.load(sf)
            data["baidu_pcs_sync"] = {
                "status": sync_status,
                "error_message": err_msg,
                "completion_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(status_file, "w", encoding="utf-8") as sf:
                json.dump(data, sf, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Failed to update backup_status.json: {e}")

    if sync_status == "success":
        return backup_result + "\n\n📤 百度网盘同步: ✅ 已上传至 `lite_agent/backup`"
    else:
        return backup_result + f"\n\n📤 百度网盘同步: ❌ 失败\n{err_msg}"


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
