#!/usr/bin/env python3
"""一次性完整备份脚本：HedgeDoc 全量 + RSSlite MongoDB 全量
命名含 full 区分日常备份，上传到网盘 lite-agent/ 对应目录
"""
import sys, os, time, subprocess, tempfile, shutil, json

# 确保 PATH 包含 bdpan
os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")

BASE_DIR = "/home/liteagent/lite_agent"
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
os.makedirs(BACKUP_DIR, exist_ok=True)
TIMESTAMP = time.strftime("%Y%m%d_%H%M%S")

# 加载配置获取 mongo uri（需先加载 .env 再替换 ${...} 变量）
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip("'").strip('"')
cfg = json.load(open(os.path.join(BASE_DIR, "config.json")))
MONGO_URI = cfg.get("rssdb", {}).get("uri", "")
# 替换 ${...} 环境变量引用
import re
MONGO_URI = re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), ''), MONGO_URI)
if "?" in MONGO_URI:
    MONGO_URI += "&authSource=admin" if "authSource=" not in MONGO_URI else ""
else:
    MONGO_URI += "/?authSource=admin"

BDPAN = "/usr/local/bin/bdpan"
results = []

# ========== 1. HedgeDoc 完整备份 ==========
print(f"\n=== HedgeDoc 完整备份 ===", flush=True)
hd_tmp = tempfile.mkdtemp(prefix="hd_full_")
try:
    # a. docker-compose.yml + .bak
    for f in ["docker-compose.yml", "docker-compose.v1.bak.yml"]:
        src = f"/app/hedgedoc/{f}"
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(hd_tmp, f))
            print(f"  [hedgedoc] copied {f}", flush=True)

    # b. pg_dump 完整数据库
    db_dump = os.path.join(hd_tmp, "hedgedoc_full_db.sql")
    with open(db_dump, "w") as out:
        subprocess.run(
            ["docker", "exec", "hedgedoc_database_1",
             "pg_dump", "-U", "hedgedoc", "hedgedoc"],
            stdout=out, stderr=subprocess.PIPE, timeout=300, check=True
        )
    print(f"  [hedgedoc] DB dump: {os.path.getsize(db_dump) // 1024} KB", flush=True)

    # c. uploads volume 完整打包
    uploads_tar = os.path.join(hd_tmp, "hedgedoc_full_uploads.tar.gz")
    subprocess.run(
        ["docker", "run", "--rm",
         "-v", "hedgedoc_hedgedoc_uploads:/data:ro",
         "-v", f"{hd_tmp}:/backup",
         "postgres:13-alpine",
         "tar", "czf", "/backup/hedgedoc_full_uploads.tar.gz", "/data"],
        capture_output=True, timeout=600, check=True
    )
    print(f"  [hedgedoc] uploads: {os.path.getsize(uploads_tar) // (1024*1024)} MB", flush=True)

    # d. 打包成 zip
    hd_zip = os.path.join(BACKUP_DIR, f"hedgedoc_full_{TIMESTAMP}.zip")
    subprocess.run(["zip", "-r", hd_zip, "."], cwd=hd_tmp, check=True, capture_output=True)
    hd_size = os.path.getsize(hd_zip) / (1024*1024)
    print(f"  [hedgedoc] full zip: {hd_size:.1f} MB -> {os.path.basename(hd_zip)}", flush=True)
    results.append(("hedgedoc_full", hd_zip, "lite-agent/hedgedoc"))

except Exception as e:
    print(f"  [hedgedoc] ERROR: {e}", flush=True)
finally:
    shutil.rmtree(hd_tmp, ignore_errors=True)

# ========== 2. RSSlite MongoDB 完整备份 ==========
print(f"\n=== RSSlite MongoDB 完整备份 ===", flush=True)
rss_tmp = tempfile.mkdtemp(prefix="rss_full_")
try:
    archive = os.path.join(rss_tmp, "rsslite_full_dump.gz")
    subprocess.run(
        ["mongodump", f"--uri={MONGO_URI}", f"--archive={archive}",
         "--gzip", "--db", "rsslite"],
        check=True, capture_output=True, timeout=600
    )
    rss_size = os.path.getsize(archive) / (1024*1024)
    print(f"  [rsslite] dump: {rss_size:.1f} MB", flush=True)

    # 打包成 zip
    rss_zip = os.path.join(BACKUP_DIR, f"rsslite_full_{TIMESTAMP}.zip")
    subprocess.run(["zip", "-r", rss_zip, "."], cwd=rss_tmp, check=True, capture_output=True)
    print(f"  [rsslite] full zip: {os.path.getsize(rss_zip) / (1024*1024):.1f} MB -> {os.path.basename(rss_zip)}", flush=True)
    results.append(("rsslite_full", rss_zip, "lite-agent/rsslite"))

except Exception as e:
    print(f"  [rsslite] ERROR: {e}", flush=True)
finally:
    shutil.rmtree(rss_tmp, ignore_errors=True)

# ========== 3. 上传到百度网盘 ==========
print(f"\n=== 上传到百度网盘 ===", flush=True)
for name, local_path, remote_dir in results:
    if not os.path.exists(local_path):
        continue
    # 确保远端目录存在
    subprocess.run([BDPAN, "mkdir", remote_dir], capture_output=True, timeout=30)
    remote_path = f"{remote_dir}/{os.path.basename(local_path)}"
    print(f"  [bdpan] uploading {name} -> {remote_path} ...", flush=True)
    r = subprocess.run(
        [BDPAN, "upload", local_path, remote_path],
        capture_output=True, text=True, timeout=3600
    )
    if r.returncode == 0:
        print(f"  [bdpan] {name} uploaded OK ({os.path.getsize(local_path)/(1024*1024):.1f} MB)", flush=True)
    else:
        err = (r.stderr or r.stdout or "").strip()[:200]
        print(f"  [bdpan] {name} FAILED: {err}", flush=True)

print(f"\n=== 完成: {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
print(f"备份文件:", flush=True)
for name, path, _ in results:
    if os.path.exists(path):
        print(f"  {name}: {path} ({os.path.getsize(path)/(1024*1024):.1f} MB)", flush=True)
