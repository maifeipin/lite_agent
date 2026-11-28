"""
Features Sys Ops 备份与云同步业务服务 (features.sys_ops.backup_service)。
提供本地各数据库/服务数据打包与百度网盘异地快照备份。
为原 skills/ops_backup.py 的 canonical 业务实现。
"""

import sys, os, subprocess
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from lite_agent.core.config_loader import load_config

# SQLite 在线一致性快照参数（试运行基线：busy_timeout=5s，单库总预算 60s，最多 2 次尝试）
_SQLITE_BUSY_TIMEOUT_MS = 5000
_SQLITE_BACKUP_BUDGET_SEC = 60.0
_SQLITE_BACKUP_MAX_ATTEMPTS = 2
_SQLITE_BACKUP_PAGES_PER_STEP = 256
_SQLITE_EXCLUDED_SUFFIXES = (".db-wal", ".db-shm", ".db-journal", ".lock")

_cfg = load_config() or {}
_default_billing_dir = os.environ.get("MAIL_STATEMENT_PARSER_DIR") or os.path.expanduser("~/mail-statement-parser")
_BILLING_DIR = _cfg.get("billing", {}).get("script_dir", _default_billing_dir)

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
    "webmusic_media": "webmusic_media",
}

from lite_agent.core.constants import PROJECT_ROOT

# 上传日志文件路径
_BASE_DIR = PROJECT_ROOT
_BDPAN_SYNC_LOG = os.path.join(_BASE_DIR, "data", "bdpan_sync.log")

# 远端分类子目录列表（上传前幂等 mkdir 用）
_BDPAN_REMOTE_SUBDIRS = [
    "lite-agent/data", "lite-agent/meilisearch", "lite-agent/rsslite",
    "lite-agent/halo", "lite-agent/hedgedoc", "lite-agent/vaultwarden",
    "lite-agent/webmusic_media",
]


def _backup_vaultwarden() -> Optional[str]:
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


def _get_meili_key_from_docker() -> Optional[str]:
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


def _create_meili_dump() -> Optional[str]:
    """触发 Meilisearch 生成备份 Dump，返回最新的 .dump 文件路径或 None"""
    import urllib.request
    import urllib.error
    import json

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

    # 2. 轮询等待成功 (最大等待 180s，适应 200MB+ 索引数据 dump 耗时)
    headers = _make_headers(key)
    dumps_dir = "/home/liteagent/meilisearch/meili_data/dumps"
    for _ in range(180):
        req_status = urllib.request.Request(f"{url_base}/tasks/{task_uid}", headers=headers)
        try:
            with urllib.request.urlopen(req_status) as resp:
                task_data = json.loads(resp.read().decode('utf-8'))
                status = task_data.get("status")
                if status == "succeeded":
                    if os.path.exists(dumps_dir):
                        # 严格按本次任务返回的 dumpUid 精准定位，坚决不使用 mtime 兜底防止误选并发或手工 dump
                        dump_uid = (task_data.get("details") or {}).get("dumpUid")
                        if dump_uid:
                            exact_path = os.path.join(dumps_dir, f"{dump_uid}.dump")
                            if os.path.isfile(exact_path):
                                return exact_path
                            print(f"Meilisearch dump task succeeded but dump file not found: {exact_path}")
                            return None
                        print(f"Meilisearch dump task succeeded but details.dumpUid is missing: {task_data}")
                        return None
                    return None
                elif status == "failed":
                    print(f"Meilisearch dump task failed: {task_data.get('error')}")
                    return None
        except Exception as e:
            print(f"Error checking dump task status: {e}")

        time.sleep(1.0)

    print("Meilisearch dump timed out (180s)")
    return None


def _clean_meili_dumps_after_archive(current_dump: str) -> None:
    """ZIP 归档成功后清理：仅清理确认由本服务任务生成且已成功归档的当前 dump 文件。

    坚决不扫描或误删目录中任何其他文件（包括人工通过 API 创建的 dump、未归档的恢复文件等）。
    """
    try:
        if current_dump and os.path.isfile(current_dump):
            os.remove(current_dump)
    except Exception as e:
        print(f"Failed to remove archived meili dump {current_dump}: {e}")


_HEDGEDOC_COMPOSE = "/app/hedgedoc/docker-compose.yml"
_HEDGEDOC_DB_CONTAINER = "hedgedoc_database_1"
_HEDGEDOC_UPLOADS_VOLUME = "hedgedoc_hedgedoc_uploads"

def _backup_hedgedoc(include_uploads: bool = True) -> list:
    """备份 HedgeDoc：数据库 pg_dump + 可选 uploads 卷 tar + docker-compose.yml，返回临时文件路径列表"""
    import tempfile
    import shutil

    tmp_dir = tempfile.mkdtemp(prefix="hedgedoc_backup_")
    results = []

    # 1. 数据库 pg_dump（非空才计入成功）
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
        if os.path.isfile(db_dump) and os.path.getsize(db_dump) > 0:
            results.append(db_dump)
            print(f"  [hedgedoc] DB dump: {os.path.getsize(db_dump) // 1024} KB")
        else:
            try:
                os.remove(db_dump)
            except Exception:
                pass
            print("  [hedgedoc] DB dump failed: empty output")
    except Exception as e:
        print(f"  [hedgedoc] DB dump failed: {e}")

    # 2. uploads 卷打包（按需执行，非空才计入成功）
    if include_uploads:
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
            if os.path.isfile(uploads_tar) and os.path.getsize(uploads_tar) > 0:
                results.append(uploads_tar)
                print(f"  [hedgedoc] uploads: {os.path.getsize(uploads_tar) // (1024*1024)} MB")
            else:
                try:
                    os.remove(uploads_tar)
                except Exception:
                    pass
                print("  [hedgedoc] uploads backup failed: empty archive")
        except Exception as e:
            print(f"  [hedgedoc] uploads backup failed: {e}")

    # 3. docker-compose.yml
    if os.path.exists(_HEDGEDOC_COMPOSE):
        compose_dst = os.path.join(tmp_dir, "docker-compose.yml")
        shutil.copy2(_HEDGEDOC_COMPOSE, compose_dst)
        results.append(compose_dst)

    if not results:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    return results


def _is_excluded_core_entry(name: str) -> bool:
    """排除在线 SQLite WAL/SHM/journal 附属文件及运行态锁文件（.pre-* 迁移备份先保留待盘点）"""
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in _SQLITE_EXCLUDED_SUFFIXES)


def _snapshot_sqlite_db(
    src_path: str,
    dst_path: str,
    *,
    busy_timeout_ms: int = _SQLITE_BUSY_TIMEOUT_MS,
    budget_sec: float = _SQLITE_BACKUP_BUDGET_SEC,
    max_attempts: int = _SQLITE_BACKUP_MAX_ATTEMPTS,
    pages_per_step: int = _SQLITE_BACKUP_PAGES_PER_STEP,
) -> Dict[str, Any]:
    """使用 SQLite Online Backup API 导出单文件一致性快照并执行 PRAGMA quick_check。

    失败时绝不降级为热拷贝原文件，清理半成品快照并返回 failed 状态。
    """
    if not os.path.isfile(src_path):
        return {
            "status": "failed",
            "size_bytes": 0,
            "snapshot_time": None,
            "error": "source file missing",
        }
    if os.path.getsize(src_path) == 0:
        return {
            "status": "skipped_empty",
            "size_bytes": 0,
            "snapshot_time": None,
            "error": None,
        }

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    deadline = time.monotonic() + max(0.1, float(budget_sec))
    last_error = "unknown error"

    for attempt in range(1, max(1, int(max_attempts)) + 1):
        if time.monotonic() > deadline:
            last_error = f"exceeded {budget_sec}s total budget before attempt {attempt}"
            break

        src_conn = None
        dst_conn = None
        verify_conn = None
        try:
            for stale in (dst_path, f"{dst_path}-wal", f"{dst_path}-shm", f"{dst_path}-journal"):
                if os.path.exists(stale):
                    os.remove(stale)

            src_conn = sqlite3.connect(src_path, timeout=max(0.1, busy_timeout_ms / 1000.0))
            src_conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            src_conn.execute("PRAGMA query_only = ON")
            dst_conn = sqlite3.connect(dst_path)

            def _progress_guard(_status: int, _remaining: int, _total: int) -> None:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"SQLite backup exceeded {budget_sec}s total budget")

            src_conn.backup(
                dst_conn,
                pages=max(1, int(pages_per_step)),
                progress=_progress_guard,
                sleep=0.05,
            )
            dst_conn.commit()
            # 源库若为 WAL 模式，backup() 会复制 WAL 头标记；转为 DELETE 模式以产出纯净单文件快照
            dst_conn.execute("PRAGMA journal_mode = DELETE")
            dst_conn.close()
            dst_conn = None
            src_conn.close()
            src_conn = None

            for sidecar in (f"{dst_path}-wal", f"{dst_path}-shm", f"{dst_path}-journal"):
                if os.path.exists(sidecar):
                    try:
                        os.remove(sidecar)
                    except Exception:
                        pass

            if time.monotonic() > deadline:
                raise TimeoutError(f"SQLite backup exceeded {budget_sec}s total budget before quick_check")

            verify_uri = f"{Path(dst_path).resolve().as_uri()}?mode=ro"
            verify_conn = sqlite3.connect(verify_uri, uri=True, timeout=5.0)
            verify_conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            try:
                qc_rows = verify_conn.execute("PRAGMA quick_check").fetchall()
            finally:
                try:
                    verify_conn.set_progress_handler(None, 0)
                except Exception:
                    pass
            verify_conn.close()
            verify_conn = None

            if time.monotonic() > deadline:
                raise TimeoutError(f"SQLite quick_check exceeded {budget_sec}s total budget")

            if qc_rows != [("ok",)]:
                raise RuntimeError(f"PRAGMA quick_check failed: {qc_rows[:3]}")

            return {
                "status": "verified",
                "size_bytes": os.path.getsize(dst_path),
                "snapshot_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "error": None,
            }
        except Exception as e:
            last_error = str(e)
            for conn in (verify_conn, dst_conn, src_conn):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            for stale in (dst_path, f"{dst_path}-wal", f"{dst_path}-shm", f"{dst_path}-journal"):
                if os.path.exists(stale):
                    try:
                        os.remove(stale)
                    except Exception:
                        pass
            if attempt < max_attempts and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    return {
        "status": "failed",
        "size_bytes": 0,
        "snapshot_time": None,
        "error": last_error,
    }


def _stage_core_backup_tree(
    base_dir: str,
    billing_dir: str,
    tree_dir: str,
    prev_sqlite_snapshots: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any], List[str], List[str]]:
    """按用途构建 lite_agent_core 暂存树：逐库在线快照 + quick_check，排除 WAL/SHM/journal/lock。"""
    prev_map = prev_sqlite_snapshots if isinstance(prev_sqlite_snapshots, dict) else {}
    sqlite_snapshots: Dict[str, Any] = {}
    failed_dbs: List[str] = []
    staged_non_db: List[str] = []
    has_staged_content = False

    def _record_db_snapshot(rel_key: str, src_file: str, dst_file: str) -> None:
        nonlocal has_staged_content
        prev_entry = prev_map.get(rel_key) if isinstance(prev_map.get(rel_key), dict) else {}
        prev_verified = prev_entry.get("last_verified_at")
        prev_archive = prev_entry.get("last_verified_archive")
        res = _snapshot_sqlite_db(src_file, dst_file)
        # 暂存阶段先继承旧恢复点，待新归档实际打包并验证成功后再由发布阶段推进
        res["last_verified_at"] = prev_verified
        res["last_verified_archive"] = prev_archive
        if res["status"] == "verified":
            has_staged_content = True
        elif res["status"] == "failed":
            failed_dbs.append(rel_key)
            print(f"  [sqlite] snapshot failed for {rel_key}: {res['error']} (last_verified_at={prev_verified})")
        sqlite_snapshots[rel_key] = res

    data_dir = os.path.join(base_dir, "data")
    if os.path.isdir(data_dir):
        staged_data_dir = os.path.join(tree_dir, "data")
        os.makedirs(staged_data_dir, exist_ok=True)
        for name in sorted(os.listdir(data_dir)):
            if _is_excluded_core_entry(name):
                continue
            src = os.path.join(data_dir, name)
            dst = os.path.join(staged_data_dir, name)
            if os.path.isfile(src) and name.lower().endswith(".db"):
                _record_db_snapshot(f"data/{name}", src, dst)
            else:
                _safe_symlink(src, dst)
                staged_non_db.append(f"data/{name}")
                has_staged_content = True

    statements_src = os.path.join(billing_dir, "statements.db")
    if os.path.isfile(statements_src):
        _record_db_snapshot("statements.db", statements_src, os.path.join(tree_dir, "statements.db"))

    for extra_name in ("email-downloads", "validation-reports"):
        extra_src = os.path.join(billing_dir, extra_name)
        if os.path.exists(extra_src):
            _safe_symlink(extra_src, os.path.join(tree_dir, extra_name))
            staged_non_db.append(extra_name)
            has_staged_content = True

    return has_staged_content, sqlite_snapshots, failed_dbs, staged_non_db


def _prune_old_archives(backup_dir: str, prefixes: Tuple[str, ...], keep_names: set) -> None:
    """仅在新归档生成并验证成功后清理同前缀旧归档，且绝不删除 keep_names 中仍被依赖的恢复点。"""
    if not os.path.isdir(backup_dir):
        return
    for fname in os.listdir(backup_dir):
        if fname in keep_names:
            continue
        if fname.startswith(prefixes):
            fpath = os.path.join(backup_dir, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass


def _promote_core_snapshots(sqlite_snapshots: Dict[str, Any], published_archive_name: Optional[str]) -> set:
    """仅当新 core 归档成功发布时，才为本次 verified 的库推进 last_verified_at 与 last_verified_archive。

    返回需要保留的本地 core 归档文件名集合（新归档 + 降级/失败库仍依赖的上一份已验证归档）。
    """
    keep_archives: set = set()
    if published_archive_name:
        keep_archives.add(published_archive_name)
    for entry in sqlite_snapshots.values():
        if not isinstance(entry, dict):
            continue
        if published_archive_name and entry.get("status") == "verified":
            entry["last_verified_at"] = entry.get("snapshot_time")
            entry["last_verified_archive"] = published_archive_name
        ref_archive = entry.get("last_verified_archive")
        if ref_archive:
            keep_archives.add(ref_archive)
    return keep_archives


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


def _backup_mongodb() -> Optional[str]:
    """对 MongoDB 中的 rsslite 数据库做增量备份：只 dump 当月分区表 FeedItem_YYYYMM"""
    import tempfile
    cfg = load_config() or {}
    uri = cfg.get("rssdb", {}).get("uri", "")
    if not uri:
        print("  [mongodb] rsslite backup skipped: RSSDB_URI not configured.")
        return None

    if "authSource=" not in uri:
        if "?" in uri:
            uri += "&authSource=admin"
        else:
            uri += "/?authSource=admin"

    current_month = datetime.now().strftime("%Y%m")
    collection = f"FeedItem_{current_month}"

    try:
        tmp_dir = tempfile.mkdtemp(prefix="mongo_backup_")
        archive_path = os.path.join(tmp_dir, f"rsslite_{current_month}.gz")

        cmd = [
            "mongodump",
            f"--uri={uri}",
            f"--archive={archive_path}",
            "--gzip",
            "--db", "rsslite",
            "--collection", collection,
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        size_kb = os.path.getsize(archive_path) // 1024
        print(f"  [mongodb] {collection} dump: {size_kb} KB")
        return archive_path
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode('utf-8', errors='ignore') if e.stderr else ""
        if "does not exist" in stderr or "ns not found" in stderr:
            prev_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime("%Y%m")
            collection = f"FeedItem_{prev_month}"
            print(f"  [mongodb] 当月表不存在，尝试上月 {collection}")
            try:
                archive_path = os.path.join(tmp_dir, f"rsslite_{prev_month}.gz")
                cmd[-1] = collection
                subprocess.run(cmd, check=True, capture_output=True, timeout=300)
                size_kb = os.path.getsize(archive_path) // 1024
                print(f"  [mongodb] {collection} dump: {size_kb} KB")
                return archive_path
            except Exception as e2:
                print(f"  [mongodb] {collection} dump failed: {e2}")
                return None
        print(f"  [mongodb] dump failed (exit {e.returncode}): {stderr[:200]}")
        return None
    except Exception as e:
        print(f"  [mongodb] dump failed: {e}")
        return None


def _backup_halo() -> Optional[str]:
    """备份 Halo 的 H2 数据库，返回临时副本路径或 None"""
    import tempfile
    tmp_dir = tempfile.mkdtemp(prefix="halo_backup_")
    tmp_db = os.path.join(tmp_dir, "halo.mv.db")
    try:
        subprocess.run(
            ["docker", "cp", "halo:/root/.halo/db/halo.mv.db", tmp_db],
            check=True, capture_output=True, timeout=60
        )
        if os.path.exists(tmp_db):
            print(f"  [halo] H2 db: {os.path.getsize(tmp_db) // (1024*1024)} MB")
            return tmp_db
    except Exception as e:
        print(f"  [halo] docker cp failed: {e}")
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def _bdpan_sync_dir(backup_dir: str) -> Tuple[bool, str]:
    """用 bdpan CLI 按分类上传备份 zip 到百度网盘，返回 (success: bool, detail: str)"""
    import shutil

    bdpan = _BDPAN_BIN if os.path.exists(_BDPAN_BIN) else shutil.which("bdpan")
    if not bdpan:
        return False, "bdpan 未安装，请先运行 install.sh"

    for subdir in _BDPAN_REMOTE_SUBDIRS:
        try:
            subprocess.run([bdpan, "mkdir", subdir], capture_output=True, timeout=30)
        except Exception:
            pass

    files = []
    for root, _, names in os.walk(backup_dir):
        for fname in names:
            files.append((os.path.join(root, fname), os.path.relpath(os.path.join(root, fname), backup_dir)))
    files.sort(key=lambda item: item[1])
    uploads, skipped, failed = [], [], []
    for fpath, relative_name in files:
        fname = os.path.basename(relative_name)
        if not (fname.endswith(".zip") or fname.endswith(".dump")):
            continue
        if not os.path.isfile(fpath):
            continue
        if os.path.getsize(fpath) == 0:
            failed.append((fname, "本地文件大小为 0 字节，跳过上传"))
            continue

        remote_subdir = None
        if relative_name.startswith("webmusic_media/"):
            remote_subdir = "lite-agent/webmusic_media"
        for prefix, category in [("data_", "data"), ("meilisearch_", "meilisearch"),
            ("rsslite_", "rsslite"), ("halo_", "halo"),
            ("hedgedoc_", "hedgedoc"), ("vaultwarden_", "vaultwarden")]:
            if fname.startswith(prefix):
                remote_subdir = f"lite-agent/{category}"
                break
        if not remote_subdir:
            remote_subdir = _BDPAN_REMOTE_DIR

        remote_path = f"{remote_subdir}/{fname}"
        max_retries = 2
        file_uploaded = False
        last_err = ""
        for attempt in range(max_retries + 1):
            try:
                result = subprocess.run(
                    [bdpan, "upload", fpath, remote_path],
                    capture_output=True, text=True, timeout=3600
                )
                if result.returncode == 0:
                    uploads.append(f"{remote_subdir}/{fname}")
                    print(f"  [bdpan] 上传 {fname} -> {remote_subdir}/: 成功")
                    file_uploaded = True
                    break
                else:
                    err = result.stderr or result.stdout or f"exit code {result.returncode}"
                    if "已存在" in err or "exist" in err.lower():
                        skipped.append(fname)
                        file_uploaded = True
                        break
                    last_err = err.strip()[:200]
                    print(f"  [bdpan] 上传 {fname} 失败 (尝试 {attempt + 1}/{max_retries + 1}): {last_err}")
            except subprocess.TimeoutExpired:
                last_err = "上传超时 (3600s)"
                print(f"  [bdpan] 上传 {fname} 超时 (尝试 {attempt + 1}/{max_retries + 1})")
            except Exception as e:
                last_err = str(e)
                print(f"  [bdpan] 上传 {fname} 异常 (尝试 {attempt + 1}/{max_retries + 1}): {e}")

            if attempt < max_retries:
                time.sleep(5)

        if not file_uploaded:
            failed.append((fname, last_err))

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


_ASYNC_UPLOAD_SCRIPT = '''#!/usr/bin/env python3
"""异步上传脚本：由 do_backup_and_sync() 生成并后台执行"""
import sys, os, json
from datetime import datetime

os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")
sys.path.insert(0, __BASE_DIR__)

from lite_agent.features.sys_ops.backup_service import _bdpan_sync_dir

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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    checklist = {
        "lite_agent_core": "Failed",
        "rsslite_mongodb": "Missing",
        "vaultwarden": "Missing",
        "meilisearch": "Missing",
        "hedgedoc": "Missing",
        "hedgedoc_db": "Missing",
        "hedgedoc_uploads": "Missing",
        "halo": "Missing",
        "webmusic_media_postgres": "Missing",
    }
    backup_files = []
    total_size_mb = 0
    sqlite_snapshots: Dict[str, Any] = {}
    core_non_db_assets: List[str] = []

    status_file = os.path.join(base_dir, "data", "backup_status.json")
    prev_status: Dict[str, Any] = {}
    if os.path.exists(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as sf:
                loaded_prev = json.load(sf)
            if isinstance(loaded_prev, dict):
                prev_status = loaded_prev
        except Exception:
            prev_status = {}
    prev_sqlite_snapshots = prev_status.get("sqlite_snapshots") if isinstance(prev_status.get("sqlite_snapshots"), dict) else {}
    prev_hedgedoc_status = prev_status.get("hedgedoc_status") if isinstance(prev_status.get("hedgedoc_status"), dict) else {}
    hedgedoc_status: Dict[str, Any] = {
        "db": {
            "status": "Missing",
            "last_ok_time": (prev_hedgedoc_status.get("db") or {}).get("last_ok_time"),
        },
        "uploads": {
            "status": "Missing",
            "last_ok_time": (prev_hedgedoc_status.get("uploads") or {}).get("last_ok_time"),
        },
    }

    import tempfile
    import shutil

    try:
        media_dir = os.path.join(backup_dir, "webmusic_media")
        os.makedirs(media_dir, exist_ok=True)
        media_dump = os.path.join(media_dir, f"webmusic_postgres_{timestamp}.dump")
        with open(media_dump, "wb") as dump_file:
            subprocess.run(
                ["ssh", "-i", "/home/liteagent/.ssh/id_ed25519_lite_agent",
                 "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                 "-o", "UserKnownHostsFile=/home/liteagent/.ssh/known_hosts", "-o", "StrictHostKeyChecking=yes",
                 "-l", "root", "media",
                 "docker", "exec", "webmusic-postgres", "pg_dump", "-U", "postgres", "-d", "webmusic", "-Fc"],
                stdout=dump_file, stderr=subprocess.PIPE, timeout=900, check=True
            )
        if os.path.getsize(media_dump) == 0:
            os.remove(media_dump)
            raise RuntimeError("empty pg_dump output")
        total_size_mb += os.path.getsize(media_dump) / (1024 * 1024)
        backup_files.append((media_dump, "webmusic_media"))
        checklist["webmusic_media_postgres"] = "OK"
        print(f"  [webmusic_media] PostgreSQL dump: {os.path.getsize(media_dump) // 1024} KB")
    except Exception as e:
        if 'media_dump' in locals() and os.path.exists(media_dump) and os.path.getsize(media_dump) == 0:
            try:
                os.remove(media_dump)
            except Exception:
                pass
        print(f"  [webmusic_media] PostgreSQL dump failed: {e}")

    halo_current_mtime = None
    halo_should_backup = True
    prev_mtime = prev_status.get("halo_last_mtime")
    try:
        mtime_str = subprocess.run(
            ["docker", "exec", "halo", "stat", "-c", "%Y", "/root/.halo/db/halo.mv.db"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if mtime_str:
            halo_current_mtime = float(mtime_str)
    except Exception:
        pass
    if halo_current_mtime is not None and prev_mtime is not None and prev_mtime == halo_current_mtime:
        halo_should_backup = False
        checklist["halo"] = "Skipped (no change)"
        print(f"  [halo] skipped: mtime unchanged ({prev_mtime})")

    # A. Lite Agent Core (逐库 SQLite 在线一致性快照 + quick_check + 非 DB 资产；先打包验证再发布状态与清理旧归档)
    tree_dir = None
    data_zip = os.path.join(backup_dir, f"data_{timestamp}.zip")
    try:
        tree_dir = tempfile.mkdtemp(prefix="backup_data_")
        core_ok, sqlite_snapshots, failed_dbs, core_non_db_assets = _stage_core_backup_tree(
            base_dir=base_dir,
            billing_dir=_BILLING_DIR,
            tree_dir=tree_dir,
            prev_sqlite_snapshots=prev_sqlite_snapshots,
        )

        if core_ok:
            subprocess.run(
                ["zip", "-r", data_zip, "."],
                cwd=tree_dir, check=True, capture_output=True, timeout=300
            )
            if not (os.path.isfile(data_zip) and os.path.getsize(data_zip) > 0):
                raise RuntimeError("generated data zip is empty")
            total_size_mb += os.path.getsize(data_zip) / (1024 * 1024)
            backup_files.append((data_zip, "data"))
            published_name = os.path.basename(data_zip)
            keep_core_archives = _promote_core_snapshots(sqlite_snapshots, published_name)
            _prune_old_archives(backup_dir, ("data_", "lite_agent_backup_", "backup_"), keep_core_archives)
            if failed_dbs:
                checklist["lite_agent_core"] = f"Degraded (failed: {', '.join(failed_dbs)})"
            else:
                checklist["lite_agent_core"] = "OK"
        elif failed_dbs:
            _promote_core_snapshots(sqlite_snapshots, None)
            checklist["lite_agent_core"] = f"Failed (all db snapshots failed: {', '.join(failed_dbs)})"
    except Exception as e:
        if os.path.exists(data_zip):
            try:
                os.remove(data_zip)
            except Exception:
                pass
        _promote_core_snapshots(sqlite_snapshots, None)
        checklist["lite_agent_core"] = "Failed"
        print(f"Core data backup failed: {e}")
    finally:
        if tree_dir:
            shutil.rmtree(tree_dir, ignore_errors=True)

    # B. MongoDB (rsslite 增量备份)
    rsslite_zip = os.path.join(backup_dir, f"rsslite_{timestamp}.zip")
    try:
        mongo_archive = _backup_mongodb()
        if mongo_archive:
            mongo_dir = os.path.dirname(mongo_archive)
            subprocess.run(
                ["zip", "-r", rsslite_zip, "."],
                cwd=mongo_dir, check=True, capture_output=True, timeout=300
            )
            if not (os.path.isfile(rsslite_zip) and os.path.getsize(rsslite_zip) > 0):
                raise RuntimeError("generated rsslite zip is empty")
            total_size_mb += os.path.getsize(rsslite_zip) / (1024 * 1024)
            backup_files.append((rsslite_zip, "rsslite"))
            checklist["rsslite_mongodb"] = "OK"
            _prune_old_archives(backup_dir, ("rsslite_",), {os.path.basename(rsslite_zip)})
            shutil.rmtree(mongo_dir, ignore_errors=True)
    except Exception as e:
        if os.path.exists(rsslite_zip):
            try:
                os.remove(rsslite_zip)
            except Exception:
                pass
        print(f"rsslite MongoDB backup failed: {e}")

    # C. Vaultwarden
    vw_zip = os.path.join(backup_dir, f"vaultwarden_{timestamp}.zip")
    try:
        vw_db = _backup_vaultwarden()
        if vw_db:
            tree_dir = tempfile.mkdtemp(prefix="backup_vw_")
            _safe_symlink(vw_db, os.path.join(tree_dir, "db_backup.sqlite3"))
            subprocess.run(
                ["zip", "-r", vw_zip, "."],
                cwd=tree_dir, check=True, capture_output=True, timeout=120
            )
            if not (os.path.isfile(vw_zip) and os.path.getsize(vw_zip) > 0):
                raise RuntimeError("generated vaultwarden zip is empty")
            total_size_mb += os.path.getsize(vw_zip) / (1024 * 1024)
            backup_files.append((vw_zip, "vaultwarden"))
            checklist["vaultwarden"] = "OK"
            _prune_old_archives(backup_dir, ("vaultwarden_",), {os.path.basename(vw_zip)})
            shutil.rmtree(tree_dir, ignore_errors=True)
            if "db_backup.sqlite3" in vw_db and os.path.exists(vw_db):
                try:
                    os.remove(vw_db)
                except Exception:
                    pass
    except Exception as e:
        if os.path.exists(vw_zip):
            try:
                os.remove(vw_zip)
            except Exception:
                pass
        print(f"Vaultwarden backup failed: {e}")

    # D. Meilisearch
    meili_zip = os.path.join(backup_dir, f"meilisearch_{timestamp}.zip")
    try:
        meili_dump = _create_meili_dump()
        if meili_dump:
            if os.path.isdir(meili_dump):
                subprocess.run(
                    ["zip", "-r", meili_zip, "."],
                    cwd=meili_dump, check=True, capture_output=True, timeout=300
                )
            else:
                tree_dir = tempfile.mkdtemp(prefix="backup_meili_")
                _safe_symlink(meili_dump, os.path.join(tree_dir, os.path.basename(meili_dump)))
                subprocess.run(
                    ["zip", "-r", meili_zip, "."],
                    cwd=tree_dir, check=True, capture_output=True, timeout=300
                )
                shutil.rmtree(tree_dir, ignore_errors=True)
            if not (os.path.isfile(meili_zip) and os.path.getsize(meili_zip) > 0):
                raise RuntimeError("generated meilisearch zip is empty")
            total_size_mb += os.path.getsize(meili_zip) / (1024 * 1024)
            backup_files.append((meili_zip, "meilisearch"))
            checklist["meilisearch"] = "OK"
            _prune_old_archives(backup_dir, ("meilisearch_",), {os.path.basename(meili_zip)})
            # ZIP 归档成功并记录 OK 后，才清理本次归档文件并安全清理确认由系统生成的过期 dump
            _clean_meili_dumps_after_archive(meili_dump)
    except Exception as e:
        if os.path.exists(meili_zip):
            try:
                os.remove(meili_zip)
            except Exception:
                pass
        print(f"Meilisearch dump backup failed: {e}")

    # E. HedgeDoc（拆分为 db + uploads 两个独立 zip，并分别记录状态与最近成功时间）
    try:
        is_monday = datetime.today().weekday() == 0
        hedgedoc_files = _backup_hedgedoc(include_uploads=is_monday)
        db_dump = None
        uploads_tar = None
        compose_file = None
        for hf in (hedgedoc_files or []):
            basename = os.path.basename(hf)
            if basename == "hedgedoc_db.sql" and os.path.isfile(hf) and os.path.getsize(hf) > 0:
                db_dump = hf
            elif basename == "hedgedoc_uploads.tar.gz" and os.path.isfile(hf) and os.path.getsize(hf) > 0:
                uploads_tar = hf
            elif basename == "docker-compose.yml" and os.path.isfile(hf):
                compose_file = hf

        # E1. hedgedoc_db_{timestamp}.zip：必须实际取得非空 db_dump 才可标 OK（仅拿到 compose 文件严禁标 OK）
        if db_dump:
            zip_path = os.path.join(backup_dir, f"hedgedoc_db_{timestamp}.zip")
            try:
                tree_dir = tempfile.mkdtemp(prefix="backup_hd_db_")
                _safe_symlink(db_dump, os.path.join(tree_dir, "hedgedoc_db.sql"))
                if compose_file:
                    _safe_symlink(compose_file, os.path.join(tree_dir, "docker-compose.yml"))
                subprocess.run(["zip", "-r", zip_path, "."], cwd=tree_dir, check=True, capture_output=True, timeout=300)
                if not (os.path.isfile(zip_path) and os.path.getsize(zip_path) > 0):
                    raise RuntimeError("generated hedgedoc_db zip is empty")
                size_mb = os.path.getsize(zip_path) / (1024 * 1024)
                total_size_mb += size_mb
                backup_files.append((zip_path, "hedgedoc"))
                checklist["hedgedoc_db"] = "OK"
                hedgedoc_status["db"] = {"status": "OK", "last_ok_time": now_str}
                _prune_old_archives(backup_dir, ("hedgedoc_db_",), {os.path.basename(zip_path)})
                shutil.rmtree(tree_dir, ignore_errors=True)
            except Exception as e:
                if os.path.exists(zip_path):
                    try:
                        os.remove(zip_path)
                    except Exception:
                        pass
                checklist["hedgedoc_db"] = "Failed"
                hedgedoc_status["db"]["status"] = "Failed"
                print(f"  [hedgedoc_db] zip failed: {e}")
        else:
            checklist["hedgedoc_db"] = "Failed (db dump missing)"
            hedgedoc_status["db"]["status"] = "Failed (db dump missing)"

        # E2. hedgedoc_uploads_{timestamp}.zip：仅周一执行并单独记账
        if is_monday:
            if uploads_tar:
                zip_path = os.path.join(backup_dir, f"hedgedoc_uploads_{timestamp}.zip")
                try:
                    tree_dir = tempfile.mkdtemp(prefix="backup_hd_uploads_")
                    _safe_symlink(uploads_tar, os.path.join(tree_dir, "hedgedoc_uploads.tar.gz"))
                    subprocess.run(["zip", "-r", zip_path, "."], cwd=tree_dir, check=True, capture_output=True, timeout=300)
                    if not (os.path.isfile(zip_path) and os.path.getsize(zip_path) > 0):
                        raise RuntimeError("generated hedgedoc_uploads zip is empty")
                    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
                    total_size_mb += size_mb
                    backup_files.append((zip_path, "hedgedoc"))
                    checklist["hedgedoc_uploads"] = "OK"
                    hedgedoc_status["uploads"] = {"status": "OK", "last_ok_time": now_str}
                    _prune_old_archives(backup_dir, ("hedgedoc_uploads_",), {os.path.basename(zip_path)})
                    shutil.rmtree(tree_dir, ignore_errors=True)
                except Exception as e:
                    if os.path.exists(zip_path):
                        try:
                            os.remove(zip_path)
                        except Exception:
                            pass
                    checklist["hedgedoc_uploads"] = "Failed"
                    hedgedoc_status["uploads"]["status"] = "Failed"
                    print(f"  [hedgedoc_uploads] zip failed: {e}")
            else:
                checklist["hedgedoc_uploads"] = "Failed (uploads missing)"
                hedgedoc_status["uploads"]["status"] = "Failed (uploads missing)"
        else:
            checklist["hedgedoc_uploads"] = "Skipped (scheduled Monday)"
            hedgedoc_status["uploads"]["status"] = "Skipped (scheduled Monday)"

        # 汇总兼容字段 checklist["hedgedoc"]
        if checklist["hedgedoc_db"] == "OK":
            if checklist["hedgedoc_uploads"] == "OK" or checklist["hedgedoc_uploads"].startswith("Skipped"):
                checklist["hedgedoc"] = "OK"
            else:
                checklist["hedgedoc"] = "Degraded (uploads failed)"
        else:
            checklist["hedgedoc"] = "Failed"

        # 清理 HedgeDoc 临时文件
        for hf in (hedgedoc_files or []):
            try:
                parent_dir = os.path.dirname(hf)
                if os.path.isdir(parent_dir) and 'hedgedoc_backup_' in os.path.basename(parent_dir):
                    shutil.rmtree(parent_dir, ignore_errors=True)
                    break
            except Exception:
                pass
    except Exception as e:
        checklist["hedgedoc"] = "Failed"
        checklist["hedgedoc_db"] = "Failed"
        print(f"HedgeDoc backup failed: {e}")

    # F. Halo
    if halo_should_backup:
        halo_zip = os.path.join(backup_dir, f"halo_{timestamp}.zip")
        try:
            halo_db = _backup_halo()
            if halo_db:
                tree_dir = tempfile.mkdtemp(prefix="backup_halo_")
                _safe_symlink(halo_db, os.path.join(tree_dir, "halo.mv.db"))
                subprocess.run(
                    ["zip", "-r", halo_zip, "."],
                    cwd=tree_dir, check=True, capture_output=True, timeout=120
                )
                if not (os.path.isfile(halo_zip) and os.path.getsize(halo_zip) > 0):
                    raise RuntimeError("generated halo zip is empty")
                total_size_mb += os.path.getsize(halo_zip) / (1024 * 1024)
                backup_files.append((halo_zip, "halo"))
                checklist["halo"] = "OK"
                _prune_old_archives(backup_dir, ("halo_",), {os.path.basename(halo_zip)})
                shutil.rmtree(tree_dir, ignore_errors=True)
                halo_parent = os.path.dirname(halo_db)
                if os.path.isdir(halo_parent) and 'halo_backup_' in os.path.basename(halo_parent):
                    shutil.rmtree(halo_parent, ignore_errors=True)
        except Exception as e:
            if os.path.exists(halo_zip):
                try:
                    os.remove(halo_zip)
                except Exception:
                    pass
            print(f"Halo backup failed: {e}")

    # 保存状态
    os.makedirs(os.path.dirname(status_file), exist_ok=True)
    status_data = {
        "last_backup_time": now_str,
        "local_files": [os.path.abspath(p) for p, _ in backup_files],
        "total_size_mb": round(total_size_mb, 2),
        "backup_checklist": checklist,
        "sqlite_snapshots": sqlite_snapshots,
        "core_non_db_assets": core_non_db_assets,
        "hedgedoc_status": hedgedoc_status,
        "halo_last_mtime": halo_current_mtime if checklist.get("halo") == "OK" else (
            prev_mtime if not halo_should_backup else None
        ),
        "baidu_pcs_sync": {
            "status": "pending",
            "error_message": None,
            "completion_time": None
        },
        "checklist": checklist,
        "backup_files": [f[0] for f in backup_files]
    }
    with open(status_file, "w", encoding="utf-8") as sf:
        json.dump(status_data, sf, ensure_ascii=False, indent=2)

    has_abnormal = any(
        not (str(v) == "OK" or str(v).startswith("Skipped"))
        for v in checklist.values()
    )
    if not backup_files:
        header = "❌ 备份失败（未生成任何归档文件）"
    elif has_abnormal:
        header = "⚠️ 备份完成（存在降级、缺失或失败项）"
    else:
        header = "✅ 备份成功！"
    lines = [header]
    lines.append(f"- 总大小: `{total_size_mb:.2f} MB`")
    for cat, status in checklist.items():
        icon = "✅" if (status == "OK" or str(status).startswith("Skipped")) else "⚠️"
        lines.append(f"- {cat}: {icon} {status}")
    return "\n".join(lines)


def do_backup_and_sync() -> str:
    """执行备份并异步同步到百度网盘（上传后台进行，不阻塞）"""
    import tempfile

    backup_result = do_backup()
    if not backup_result.startswith(("✅", "⚠️")):
        return backup_result

    base_dir = _BASE_DIR
    backup_dir = os.path.join(base_dir, "backup")
    status_file = os.path.join(base_dir, "data", "backup_status.json")
    log_file = _BDPAN_SYNC_LOG

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

    script = _ASYNC_UPLOAD_SCRIPT
    script = script.replace("__BASE_DIR__", repr(base_dir))
    script = script.replace("__BACKUP_DIR__", repr(backup_dir))
    script = script.replace("__STATUS_FILE__", repr(status_file))
    script = script.replace("__LOG_FILE__", repr(log_file))

    script_fd, script_path = tempfile.mkstemp(suffix="_upload.py", prefix="bdpan_async_")
    with os.fdopen(script_fd, "w", encoding="utf-8") as f:
        f.write(script)

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
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as lf:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                lf.write(f"[{ts}] ERROR: 后台进程启动失败: {e}\n")
        except Exception:
            pass
        return backup_result + f"\n\n📤 百度网盘同步: ⚠️ 后台上传启动失败: {e}"

    return backup_result + "\n\n📤 百度网盘同步: 后台上传进行中，结果将写入 `data/bdpan_sync.log`"


__all__ = [
    "do_backup",
    "do_backup_and_sync",
    "_bdpan_sync_dir",
    "_backup_vaultwarden",
    "_backup_mongodb",
    "_backup_halo",
    "_backup_hedgedoc",
]
