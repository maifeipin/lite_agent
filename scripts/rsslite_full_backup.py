#!/usr/bin/env python3
"""单独执行 rsslite MongoDB 完整备份"""
import sys, os, time, subprocess, tempfile, shutil, json, re

os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")
BASE_DIR = "/home/liteagent/lite_agent"
BACKUP_DIR = os.path.join(BASE_DIR, "backup")
os.makedirs(BACKUP_DIR, exist_ok=True)
TIMESTAMP = time.strftime("%Y%m%d_%H%M%S")

# 加载 .env
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
MONGO_URI = re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), ''), MONGO_URI)
if "?" in MONGO_URI:
    MONGO_URI += "&authSource=admin" if "authSource=" not in MONGO_URI else ""
else:
    MONGO_URI += "/?authSource=admin"

print(f"MONGO_URI: {MONGO_URI[:40]}...", flush=True)

# RSSlite MongoDB 完整备份
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

    rss_zip = os.path.join(BACKUP_DIR, f"rsslite_full_{TIMESTAMP}.zip")
    subprocess.run(["zip", "-r", rss_zip, "."], cwd=rss_tmp, check=True, capture_output=True)
    print(f"  [rsslite] full zip: {os.path.getsize(rss_zip) / (1024*1024):.1f} MB", flush=True)

    # 上传到网盘
    print(f"  [bdpan] uploading rsslite_full ...", flush=True)
    subprocess.run(["/usr/local/bin/bdpan", "mkdir", "lite-agent/rsslite"], capture_output=True, timeout=30)
    r = subprocess.run(
        ["/usr/local/bin/bdpan", "upload", rss_zip, f"lite-agent/rsslite/{os.path.basename(rss_zip)}"],
        capture_output=True, text=True, timeout=3600
    )
    if r.returncode == 0:
        print(f"  [bdpan] rsslite_full uploaded OK", flush=True)
    else:
        print(f"  [bdpan] rsslite_full FAILED: {(r.stderr or r.stdout).strip()[:200]}", flush=True)

except Exception as e:
    print(f"  [rsslite] ERROR: {e}", flush=True)
finally:
    shutil.rmtree(rss_tmp, ignore_errors=True)

print(f"\n=== 完成: {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
