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

# 上传日志文件路径
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BDPAN_SYNC_LOG = os.path.join(_BASE_DIR, "data", "bdpan_sync.log")

# 远端分类子目录列表（上传前幂等 mkdir 用）
_BDPAN_REMOTE_SUBDIRS = [
    "lite-agent/data", "lite-agent/meilisearch", "lite-agent/rsslite",
    "lite-agent/halo", "lite-agent/hedgedoc", "lite-agent/vaultwarden",
]

# ==========================================
# 各数据源备份函数
# ==========================================

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

def _get_meili_key_from_docker() -> str:
    """尝试从 docker inspect 获取 Meilisearch 容器的实际 master_key（fallback）"""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None
        meili_container = None
        for name in result.stdout.strip().split('\n'):
            if 'meili' in name.lower():
                meili_container = name.strip()
                break
        if not meili_container:
            return None
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{range .Config.Env}}{{println .}}{{end}}", meili_container],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.split('\n'):
            if line.startswith('MEILI_MASTER_KEY='):
                return line.split('=', 1)[1].strip()
        return None
    except Exception:
        return None

def _create_meili_dump() -> str:
    """触发 Meilisearch 生成备份 Dump，返回最新的 .dump 文件路径或 None"""
    import urllib.request
    import urllib.error
    import json
    import time

    cfg = load_config() or {}
    meili_cfg = cfg.get("meilisearch", {})
    url_base = meili_cfg.get("url", "http://127.0.0.1:7700")
    key = meili_cfg.get("master_key", "")

    def _make_headers(api_key):
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def _trigger_dump(api_key):
        """触发 dump，返回 task_uid 或 "invalid_api_key" 或 None"""
        headers = _make_headers(api_key)
        req = urllib.request.Request(f"{url_base}/dumps", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data.get("taskUid")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                try:
                    err_body = e.read().decode('utf-8', errors='ignore')
                except Exception:
                    err_body = ""
                if "invalid_api_key" in err_body or "invalid_api_key" in err_body.lower():
                    return "invalid_api_key"
                print(f"Failed to trigger Meilisearch dump: HTTP {e.code} - {err_body[:200]}")
                return None
            print(f"Failed to trigger Meilisearch dump: HTTP {e.code}")
            return None
        except Exception as e:
            print(f"Failed to trigger Meilisearch dump: {e}")
            return None

    # 1. 触发 Dump（先尝试 config 中的 key，invalid_api_key 时 fallback 到 docker inspect）
    task_uid = _trigger_dump(key)
    if task_uid == "invalid_api_key":
        print("  [meilisearch] config key invalid, trying docker inspect fallback...")
        docker_key = _get_meili_key_from_docker()
        if docker_key:
            key = docker_key
            task_uid = _trigger_dump(key)
        else:
            print("  [meilisearch] docker inspect fallback failed: container not found")
            return None

    if not task_uid or task_uid == "invalid_api_key":
        return None

    # 2. 轮询等待成功 (最大等待 60s)
    headers = _make_headers(key)
    for _ in range(60):
        req_status = urllib.request.Request(f"{url_base}/tasks/{task_uid}", headers=headers)
        try:
            with urllib.request.urlopen(req_status) as resp:
                task_data = json.loads(resp.read().decode('utf-8'))
                status = task_data.get("status")
                if status == "succeeded":
                    # Dump 导出成功，通过 os.listdir 找最新的 dump 文件
                    dumps_dir = "/home/liteagent/meilisearch/meili_data/dumps"
                    if os.path.exists(dumps_dir):
                        dump_items = [f for f in os.listdir(dumps_dir)
                                      if not f.startswith('.')]
                        if dump_items:
                            latest = max(
                                dump_items,
                                key=lambda f: os.path.getmtime(os.path.join(dumps_dir, f))
                            )
                            return os.path.join(dumps_dir, latest)
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
    """用 bdpan CLI 按分类上传备份 zip 到百度网盘，返回 (success: bool, detail: str)"""
    import shutil

    bdpan = _BDPAN_BIN if os.path.exists(_BDPAN_BIN) else shutil.which("bdpan")
    if not bdpan:
        return False, "bdpan 未安装，请先运行 install.sh"

    # 上传前先 mkdir 创建所有分类目录（幂等，已存在不报错）
    for subdir in _BDPAN_REMOTE_SUBDIRS:
        try:
            subprocess.run([bdpan, "mkdir", subdir], capture_output=True, timeout=30)
        except Exception:
            pass

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

    # 写上传日志
    try:
        os.makedirs(os.path.dirname(_BDPAN_SYNC_LOG), exist_ok=True)
        with open(_BDPAN_SYNC_LOG, "a", encoding="utf-8") as lf:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if failed:
                lf.write(f"[{ts}] FAILED: 成功 {len(uploads)}, 跳过 {len(skipped)}, 失败 {len(failed)}\n")
            else:
                lf.write(f"[{ts}] SUCCESS: 上传 {len(uploads)}, 跳过 {len(skipped)}\n")
    except Exception:
        pass

    if failed:
        detail = f"成功 {len(uploads)} 个，跳过 {len(skipped)} 个，失败 {len(failed)} 个: " + "; ".join(f"{n}({m})" for n, m in failed)
        return False, detail
    detail = f"上传 {len(uploads)} 个，跳过 {len(skipped)} 个"
    return True, detail


# ==========================================
# 异步上传脚本模板（由 do_backup_and_sync() 生成并后台执行）
# ==========================================

_ASYNC_UPLOAD_SCRIPT = '''#!/usr/bin/env python3
"""异步上传脚本：由 do_backup_and_sync() 生成并后台执行"""
import sys, os, json
from datetime import datetime

# 确保 PATH 包含 /usr/local/bin（bdpan 所在路径）
os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")

# 添加项目根目录到 sys.path 以便导入 _bdpan_sync_dir
sys.path.insert(0, __BASE_DIR__)

from skills.ops_backup import _bdpan_sync_dir

backup_dir = __BACKUP_DIR__
status_file = __STATUS_FILE__
log_file = __LOG_FILE__

def write_log(msg):
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write("[" + ts + "] " + msg + "\\n")
    except Exception:
        pass

def update_status(success, detail):
    if not os.path.exists(status_file):
        return
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["baidu_pcs_sync"] = {
            "status": "success" if success else "failed",
            "error_message": None if success else detail,
            "completion_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        write_log("Failed to update status: " + str(e))

try:
    write_log("开始后台上传...")
    success, detail = _bdpan_sync_dir(backup_dir)
    write_log(("SUCCESS" if success else "FAILED") + ": " + detail)
    update_status(success, detail)
except Exception as e:
    write_log("ERROR: " + str(e))
    update_status(False, str(e))
'''


def do_backup() -> str:
    """执行备份逻辑：每个数据源单独打 zip"""
    import json

    base_dir = _BASE_DIR
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

    # Halo mtime 增量检测：读取上次备份时记录的 mtime
    status_file = os.path.join(base_dir, "data", "backup_status.json")
    halo_db_path = os.path.join(_HALO_DATA, "db", "halo.mv.db")
    halo_current_mtime = None
    halo_should_backup = True
    if os.path.exists(halo_db_path):
        halo_current_mtime = os.path.getmtime(halo_db_path)
        if os.path.exists(status_file):
            try:
                with open(status_file, "r", encoding="utf-8") as sf:
                    prev_status = json.load(sf)
                prev_mtime = prev_status.get("halo_last_mtime")
                if prev_mtime is not None and prev_mtime == halo_current_mtime:
                    halo_should_backup = False
                    checklist["halo"] = "Skipped (no change)"
                    print(f"  [halo] skipped: mtime unchanged ({prev_mtime})")
            except Exception:
                pass

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
            if os.path.isdir(meili_snapshot):
                # dump 目录：zip 目录内容
                subprocess.run(["zip", "-r", zip_path, "."],
                    cwd=meili_snapshot, check=True, capture_output=True)
            else:
                # dump 文件：通过临时目录软链接后 zip
                tree_dir = tempfile.mkdtemp(prefix="backup_meili_")
                _safe_symlink(meili_snapshot,
                    os.path.join(tree_dir, os.path.basename(meili_snapshot)))
                subprocess.run(["zip", "-r", zip_path, "."],
                    cwd=tree_dir, check=True, capture_output=True)
                shutil.rmtree(tree_dir, ignore_errors=True)
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            total_size_mb += size_mb
            backup_files.append((zip_path, "meilisearch"))
            checklist["meilisearch"] = "OK"
            print(f"  [meilisearch] {size_mb:.1f} MB")
        except Exception as e:
            print(f"  [meilisearch] zip failed: {e}")

    # E. HedgeDoc（拆分为 db + uploads 两个独立 zip）
    hedgedoc_files = _backup_hedgedoc()
    if hedgedoc_files:
        # 识别各类文件
        db_dump = None
        uploads_tar = None
        compose_file = None
        for hf in hedgedoc_files:
            basename = os.path.basename(hf)
            if basename == "hedgedoc_db.sql":
                db_dump = hf
            elif basename == "hedgedoc_uploads.tar.gz":
                uploads_tar = hf
            elif basename == "docker-compose.yml":
                compose_file = hf

        # E1. hedgedoc_db_{timestamp}.zip：db_dump + compose_file（每天）
        if db_dump or compose_file:
            try:
                tree_dir = tempfile.mkdtemp(prefix="backup_hd_db_")
                if db_dump:
                    _safe_symlink(db_dump, os.path.join(tree_dir, "hedgedoc_db.sql"))
                if compose_file:
                    _safe_symlink(compose_file, os.path.join(tree_dir, "docker-compose.yml"))
                zip_path = os.path.join(backup_dir, f"hedgedoc_db_{timestamp}.zip")
                subprocess.run(["zip", "-r", zip_path, "."], cwd=tree_dir, check=True, capture_output=True)
                size_mb = os.path.getsize(zip_path) / (1024 * 1024)
                total_size_mb += size_mb
                backup_files.append((zip_path, "hedgedoc"))
                checklist["hedgedoc"] = "OK"
                print(f"  [hedgedoc_db] {size_mb:.1f} MB")
                shutil.rmtree(tree_dir, ignore_errors=True)
            except Exception as e:
                print(f"  [hedgedoc_db] zip failed: {e}")

        # E2. hedgedoc_uploads_{timestamp}.zip：uploads_tar（仅周一）
        if uploads_tar and datetime.today().weekday() == 0:
            try:
                tree_dir = tempfile.mkdtemp(prefix="backup_hd_uploads_")
                _safe_symlink(uploads_tar, os.path.join(tree_dir, "hedgedoc_uploads.tar.gz"))
                zip_path = os.path.join(backup_dir, f"hedgedoc_uploads_{timestamp}.zip")
                subprocess.run(["zip", "-r", zip_path, "."], cwd=tree_dir, check=True, capture_output=True)
                size_mb = os.path.getsize(zip_path) / (1024 * 1024)
                total_size_mb += size_mb
                backup_files.append((zip_path, "hedgedoc"))
                print(f"  [hedgedoc_uploads] {size_mb:.1f} MB")
                shutil.rmtree(tree_dir, ignore_errors=True)
            except Exception as e:
                print(f"  [hedgedoc_uploads] zip failed: {e}")
        elif uploads_tar:
            print(f"  [hedgedoc_uploads] skipped: not Monday (weekday={datetime.today().weekday()})")

        # 清理 HedgeDoc 临时文件
        for hf in hedgedoc_files:
            try:
                parent_dir = os.path.dirname(hf)
                if os.path.isdir(parent_dir) and 'hedgedoc_backup_' in os.path.basename(parent_dir):
                    shutil.rmtree(parent_dir, ignore_errors=True)
                    break
            except Exception:
                pass

    # F. Halo（带 mtime 增量检测）
    if halo_should_backup:
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
    os.makedirs(os.path.dirname(status_file), exist_ok=True)
    status_data = {
        "last_backup_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "local_files": [os.path.abspath(p) for p, _ in backup_files],
        "total_size_mb": round(total_size_mb, 2),
        "backup_checklist": checklist,
        "halo_last_mtime": halo_current_mtime,
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
    """执行备份并异步同步到百度网盘（上传后台进行，不阻塞）"""
    import tempfile

    # 第一步：同步执行备份（本地打包）
    backup_result = do_backup()
    if not backup_result.startswith("✅"):
        return backup_result

    # 第二步：异步上传（nohup 后台执行，不等待）
    base_dir = _BASE_DIR
    backup_dir = os.path.join(base_dir, "backup")
    status_file = os.path.join(base_dir, "data", "backup_status.json")
    log_file = _BDPAN_SYNC_LOG

    # 先更新状态为 uploading
    if os.path.exists(status_file):
        try:
            import json
            with open(status_file, "r", encoding="utf-8") as sf:
                data = json.load(sf)
            data["baidu_pcs_sync"] = {
                "status": "uploading",
                "error_message": None,
                "completion_time": None
            }
            with open(status_file, "w", encoding="utf-8") as sf:
                json.dump(data, sf, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # 生成后台上传脚本
    script = _ASYNC_UPLOAD_SCRIPT
    script = script.replace("__BASE_DIR__", repr(base_dir))
    script = script.replace("__BACKUP_DIR__", repr(backup_dir))
    script = script.replace("__STATUS_FILE__", repr(status_file))
    script = script.replace("__LOG_FILE__", repr(log_file))

    script_fd, script_path = tempfile.mkstemp(suffix="_upload.py", prefix="bdpan_async_")
    with os.fdopen(script_fd, "w", encoding="utf-8") as f:
        f.write(script)

    # 用 nohup + subprocess.Popen 启动后台进程
    env = os.environ.copy()
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    try:
        subprocess.Popen(
            ["nohup", "python3", script_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True
        )
    except Exception as e:
        # 如果后台启动失败，记录到日志
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as lf:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                lf.write(f"[{ts}] ERROR: 后台进程启动失败: {e}\n")
        except Exception:
            pass
        return backup_result + f"\n\n📤 百度网盘同步: ⚠️ 后台上传启动失败: {e}"

    return backup_result + "\n\n📤 百度网盘同步: 后台上传进行中，结果将写入 `data/bdpan_sync.log`"


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
