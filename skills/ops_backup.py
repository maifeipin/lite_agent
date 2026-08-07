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

# bdpan CLI 路径（百度官方 CLI，权限完整；bypy 依赖的旧 PCS API 已被百度限制）
_BDPAN_BIN = "/usr/local/bin/bdpan"
_BDPAN_REMOTE_DIR = "backup"  # bdpan 授权目录 /apps/bdpan/ 下的子目录

# Halo 数据目录（H2 嵌入式数据库）
_HALO_DATA = "/root/.halo"

# 远端分类子目录（对应 /apps/bdpan/lite-agent/ 下的子目录）
_BDPAN_CATEGORIES = {
    "data": "data",
    "meilisearch": "meilisearch",
    "rsslite": "rsslite",
    "halo": "halo",
    "hedgedoc": "hedgedoc",
    "vaultwarden": "vaultwarden",
}

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

def _backup_halo() -> str:
    """备份 Halo 的 H2 数据库，返回数据库文件路径或 None"""
    db_path = os.path.join(_HALO_DATA, "db", "halo.mv.db")
    if not os.path.exists(db_path):
        print("  [halo] halo.mv.db not found, skipping")
        return None
    # H2 数据库文件可以直接复制（Halo 运行时 H2 使用文件锁，但 mv.db 文件可以安全拷贝）
    return db_path


# ==========================================
# 百度网盘上传（通过官方 bdpan CLI，权限完整）
# bypy 依赖的旧 PCS API 已被百度限制，第三方应用 API 也无上传权限
# ==========================================

def _bdpan_sync_dir(backup_dir):
    """用 bdpan CLI 按分类上传备份 zip 到百度网盘，返回 (success, detail_str)"""
    import shutil

    bdpan = _BDPAN_BIN if os.path.exists(_BDPAN_BIN) else shutil.which("bdpan")
    if not bdpan:
        return False, "bdpan 未安装，请先运行 install.sh"

    files = sorted(os.listdir(backup_dir))
    uploads, skipped, failed = [], [], []
    for fname in files:
        if not fname.endswith(".zip"):
            continue
        fpath = os.path.join(backup_dir, fname)
        if not os.path.isfile(fpath):
            continue

        # 根据文件名前缀确定远端子目录
        remote_subdir = None
        for prefix, category in [("data_", "data"), ("meilisearch_", "meilisearch"),
            ("rsslite_", "rsslite"), ("halo_", "halo"),
            ("hedgedoc_", "hedgedoc"), ("vaultwarden_", "vaultwarden")]:
            if fname.startswith(prefix):
                remote_subdir = f"lite-agent/{category}"
                break
        if not remote_subdir:
            # 兼容旧格式
            remote_subdir = _BDPAN_REMOTE_DIR

        remote_path = f"{remote_subdir}/{fname}"
        try:
            result = subprocess.run(
                [bdpan, "upload", fpath, remote_path],
                capture_output=True, text=True, timeout=3600
            )
            if result.returncode == 0:
                uploads.append(f"{remote_subdir}/{fname}")
                print(f"  [bdpan] 上传 {fname} -> {remote_subdir}/: 成功")
            else:
                err = result.stderr or result.stdout or f"exit code {result.returncode}"
                if "已存在" in err or "exist" in err.lower():
                    skipped.append(fname)
                else:
                    failed.append((fname, err.strip()[:200]))
                    print(f"  [bdpan] 上传 {fname} 失败: {err.strip()[:200]}")
        except subprocess.TimeoutExpired:
            failed.append((fname, "上传超时 (3600s)"))
        except Exception as e:
            failed.append((fname, str(e)))

    if failed:
        detail = f"成功 {len(uploads)} 个，跳过 {len(skipped)} 个，失败 {len(failed)} 个: " + "; ".join(f"{n}({m})" for n, m in failed)
        return False, detail
    detail = f"上传 {len(uploads)} 个，跳过 {len(skipped)} 个"
    return True, detail


def do_backup() -> str:
    """执行备份逻辑：每个数据源单独打 zip"""
    import json

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    backup_dir = os.path.join(base_dir, "backup")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    checklist = {
        "lite_agent_core": "Failed",
        "rsslite_mongodb": "Missing",
        "vaultwarden": "Missing",
        "meilisearch": "Missing",
        "hedgedoc": "Missing",
        "halo": "Missing",
    }
    backup_files = []  # [(zip_path, remote_subdir), ...]
    total_size_mb = 0

    import tempfile
    import shutil

    # 清理旧备份
    for f in os.listdir(backup_dir):
        if f.endswith(".zip") and (f.startswith("data_") or f.startswith("meilisearch_")
            or f.startswith("rsslite_") or f.startswith("halo_")
            or f.startswith("hedgedoc_") or f.startswith("vaultwarden_")
            or f.startswith("lite_agent_backup_") or f.startswith("backup_")):
            os.remove(os.path.join(backup_dir, f))

    # A. Lite Agent Core (data 目录 + billing)
    try:
        tree_dir = tempfile.mkdtemp(prefix="backup_data_")
        core_targets = [
            (os.path.join(base_dir, "data"), os.path.join(tree_dir, "data")),
            (os.path.join(_BILLING_DIR, "statements.db"), os.path.join(tree_dir, "statements.db")),
            (os.path.join(_BILLING_DIR, "email-downloads"), os.path.join(tree_dir, "email-downloads")),
            (os.path.join(_BILLING_DIR, "validation-reports"), os.path.join(tree_dir, "validation-reports"))
        ]
        core_ok = False
        for src, dst in core_targets:
            if os.path.exists(src):
                _safe_symlink(src, dst)
                core_ok = True
        if core_ok:
            zip_path = os.path.join(backup_dir, f"data_{timestamp}.zip")
            subprocess.run(["zip", "-r", zip_path, "."], cwd=tree_dir, check=True, capture_output=True)
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            total_size_mb += size_mb
            backup_files.append((zip_path, "data"))
            checklist["lite_agent_core"] = "OK"
            print(f"  [data] {size_mb:.1f} MB")
        shutil.rmtree(tree_dir, ignore_errors=True)
    except Exception as e:
        print(f"  [data] backup failed: {e}")

    # B. MongoDB rsslite
    mongo_snapshot = _backup_mongodb()
    if mongo_snapshot:
        try:
            zip_path = os.path.join(backup_dir, f"rsslite_{timestamp}.zip")
            subprocess.run(["zip", "-r", zip_path, "."],
                cwd=os.path.dirname(mongo_snapshot), check=True, capture_output=True)
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            total_size_mb += size_mb
            backup_files.append((zip_path, "rsslite"))
            checklist["rsslite_mongodb"] = "OK"
            print(f"  [rsslite] {size_mb:.1f} MB")
        except Exception as e:
            print(f"  [rsslite] zip failed: {e}")
        finally:
            # 清理 MongoDB 临时文件
            parent_dir = os.path.dirname(mongo_snapshot)
            if os.path.isdir(parent_dir) and 'mongo_backup_' in os.path.basename(parent_dir):
                shutil.rmtree(parent_dir, ignore_errors=True)

    # C. Vaultwarden
    vw_snapshot = _backup_vaultwarden()
    if vw_snapshot:
        try:
            tree_dir = tempfile.mkdtemp(prefix="backup_vw_")
            _safe_symlink(vw_snapshot, os.path.join(tree_dir, "db_backup.sqlite3"))
            zip_path = os.path.join(backup_dir, f"vaultwarden_{timestamp}.zip")
            subprocess.run(["zip", "-r", zip_path, "."], cwd=tree_dir, check=True, capture_output=True)
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            total_size_mb += size_mb
            backup_files.append((zip_path, "vaultwarden"))
            checklist["vaultwarden"] = "OK"
            print(f"  [vaultwarden] {size_mb:.1f} MB")
            shutil.rmtree(tree_dir, ignore_errors=True)
        except Exception as e:
            print(f"  [vaultwarden] zip failed: {e}")
        finally:
            if vw_snapshot.endswith("db_backup.sqlite3"):
                try:
                    os.remove(vw_snapshot)
                except Exception:
                    pass

    # D. Meilisearch
    meili_snapshot = _create_meili_dump()
    if meili_snapshot:
        try:
            zip_path = os.path.join(backup_dir, f"meilisearch_{timestamp}.zip")
            # meili_snapshot 是 dump 目录路径
            subprocess.run(["zip", "-r", zip_path, "."],
                cwd=meili_snapshot, check=True, capture_output=True)
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            total_size_mb += size_mb
            backup_files.append((zip_path, "meilisearch"))
            checklist["meilisearch"] = "OK"
            print(f"  [meilisearch] {size_mb:.1f} MB")
        except Exception as e:
            print(f"  [meilisearch] zip failed: {e}")

    # E. HedgeDoc
    hedgedoc_files = _backup_hedgedoc()
    if hedgedoc_files:
        try:
            tree_dir = tempfile.mkdtemp(prefix="backup_hd_")
            for hf in hedgedoc_files:
                name = os.path.basename(hf)
                _safe_symlink(hf, os.path.join(tree_dir, name))
            zip_path = os.path.join(backup_dir, f"hedgedoc_{timestamp}.zip")
            subprocess.run(["zip", "-r", zip_path, "."], cwd=tree_dir, check=True, capture_output=True)
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            total_size_mb += size_mb
            backup_files.append((zip_path, "hedgedoc"))
            checklist["hedgedoc"] = "OK"
            print(f"  [hedgedoc] {size_mb:.1f} MB")
            shutil.rmtree(tree_dir, ignore_errors=True)
        except Exception as e:
            print(f"  [hedgedoc] zip failed: {e}")
        finally:
            for hf in hedgedoc_files:
                try:
                    parent_dir = os.path.dirname(hf)
                    if os.path.isdir(parent_dir) and 'hedgedoc_backup_' in os.path.basename(parent_dir):
                        shutil.rmtree(parent_dir, ignore_errors=True)
                        break
                except Exception:
                    pass

    # F. Halo
    halo_db = _backup_halo()
    if halo_db:
        try:
            tree_dir = tempfile.mkdtemp(prefix="backup_halo_")
            _safe_symlink(halo_db, os.path.join(tree_dir, "halo.mv.db"))
            zip_path = os.path.join(backup_dir, f"halo_{timestamp}.zip")
            subprocess.run(["zip", "-r", zip_path, "."], cwd=tree_dir, check=True, capture_output=True)
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            total_size_mb += size_mb
            backup_files.append((zip_path, "halo"))
            checklist["halo"] = "OK"
            print(f"  [halo] {size_mb:.1f} MB")
            shutil.rmtree(tree_dir, ignore_errors=True)
        except Exception as e:
            print(f"  [halo] zip failed: {e}")

    if not backup_files:
        return "❌ 找不到任何需要备份的源文件或目录。"

    # 写入 backup_status.json
    status_file = os.path.join(base_dir, "data", "backup_status.json")
    os.makedirs(os.path.dirname(status_file), exist_ok=True)
    status_data = {
        "last_backup_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "local_files": [os.path.abspath(p) for p, _ in backup_files],
        "total_size_mb": round(total_size_mb, 2),
        "backup_checklist": checklist,
        "baidu_pcs_sync": {
            "status": "pending",
            "error_message": None,
            "completion_time": None
        }
    }
    with open(status_file, "w", encoding="utf-8") as sf:
        json.dump(status_data, sf, ensure_ascii=False, indent=2)

    # 构建返回消息
    lines = [f"✅ 备份成功！"]
    lines.append(f"- 总大小: `{total_size_mb:.2f} MB`")
    for cat, status in checklist.items():
        icon = "✅" if status == "OK" else "⚠️"
        lines.append(f"- {cat}: {icon} {status}")
    return "\n".join(lines)


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
        # 使用百度官方 bdpan CLI 上传（bypy 依赖的旧 PCS API 已被百度限制）
        sync_status, sync_detail = _bdpan_sync_dir(backup_dir)
        if sync_status != "success":
            err_msg = sync_detail
    except Exception as e:
        sync_status = "failed"
        err_msg = f"百度网盘上传异常: {e}"

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
        return backup_result + f"\n\n📤 百度网盘同步: ✅ 已上传至 `lite-agent/` 各分类目录\n{sync_detail}"
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
