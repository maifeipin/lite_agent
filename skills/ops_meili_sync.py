import sys
import os
import sqlite3
import json
import urllib.request
import urllib.parse
import hashlib
from datetime import datetime, timezone, timedelta
import pymongo
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from core.config_loader import load_config

# Meilisearch Config (loaded from config.json)
_cfg = load_config() or {}
_meili_cfg = _cfg.get("meilisearch", {})
MEILI_URL = _meili_cfg.get("url", "http://127.0.0.1:7700")
MEILI_KEY = _meili_cfg.get("master_key", "")

# Data Sources
_BILLING_DIR = _cfg.get("billing", {}).get("script_dir", "/home/liteagent/mail-statement-parser")
_DB_PATH = os.path.join(_BILLING_DIR, "statements.db")

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "meili_sync_state.json")

def _meili_request(path, method="GET", data=None):
    """发送 HTTP 请求到 Meilisearch API"""
    url = f"{MEILI_URL}{path}"
    headers = {
        "Authorization": f"Bearer {MEILI_KEY}",
        "Content-Type": "application/json"
    }
    req_data = json.dumps(data).encode('utf-8') if data is not None else None
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Meilisearch request to {path} failed: {e}")
        return None

def _get_sync_state():
    """获取增量同步时间状态"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_email_sync": "1970-01-01T00:00:00Z", "last_rss_sync": "1970-01-01T00:00:00Z"}

def _save_sync_state(state):
    """保存同步状态"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        print(f"Failed to save sync state: {e}")

def _get_mongodb():
    """连接到 MongoDB"""
    rssdb = _cfg.get('rssdb', {})
    uri = rssdb.get('uri', 'mongodb://localhost:27017')
    db_name = rssdb.get('database', 'rsslite')
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    return client[db_name]

@skill("sync_meili", "同步数据到 Meilisearch 搜索引擎")
def sync_meili() -> str:
    """全量/增量同步邮件和 RSS 到 Meilisearch"""
    state = _get_sync_state()
    now_str = datetime.now(timezone.utc).isoformat()
    
    # 确保索引存在
    _meili_request("/indexes", "POST", {"uid": "emails", "primaryKey": "id"})
    _meili_request("/indexes", "POST", {"uid": "rss", "primaryKey": "id"})
    
    # --- 1. 同步邮件 ---
    email_count = 0
    if os.path.exists(_DB_PATH):
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 增量查询
            cursor.execute(
                "SELECT eb.account_name, eb.uid, eb.plain_text, eb.fetched_at, "
                "es.subject, es.sender, es.email_date, es.category, es.importance "
                "FROM email_bodies eb "
                "JOIN email_summaries es ON eb.account_name=es.account_name AND eb.uid=es.uid "
                "WHERE eb.fetched_at > ? ORDER BY eb.fetched_at ASC", (state["last_email_sync"],)
            )
            rows = cursor.fetchall()
            
            if rows:
                docs = []
                for r in rows:
                    doc_id = hashlib.md5(f"{r['account_name']}_{r['uid']}".encode()).hexdigest()
                    docs.append({
                        "id": doc_id,
                        "account_name": r["account_name"],
                        "uid": r["uid"],
                        "subject": r["subject"],
                        "sender": r["sender"],
                        "email_date": r["email_date"],
                        "category": r["category"],
                        "importance": r["importance"],
                        "plain_text": (r["plain_text"] or "")[:50000],  # 截断超长文本
                        "fetched_at": r["fetched_at"]
                    })
                
                # 分批推送到 Meilisearch 防止 Payload 过大
                batch_size = 100
                for i in range(0, len(docs), batch_size):
                    batch = docs[i:i + batch_size]
                    res = _meili_request("/indexes/emails/documents", "POST", batch)
                    if res:
                        email_count += len(batch)
                        # 取最新的一封邮件的时间作为 last_email_sync
                        state["last_email_sync"] = batch[-1]["fetched_at"]
            conn.close()
        except Exception as e:
            print(f"Error syncing emails: {e}")
            
    # --- 2. 同步 RSS ---
    rss_count = 0
    try:
        db = _get_mongodb()
        # 获取当前月和上个月的 collection
        current_date = datetime.now()
        months = [
            current_date.strftime('%Y%m'),
            # 上个月
            (current_date.replace(day=1) - timedelta(days=1)).strftime('%Y%m')
        ]
        
        # 增量过滤 ObjectId
        last_rss_time = datetime.fromisoformat(state["last_rss_sync"].replace("Z", "+00:00"))
        oid_filter = ObjectId.from_datetime(last_rss_time)
        
        docs = []
        for m in months:
            col_name = f"FeedItem_{m}"
            if col_name in db.list_collection_names():
                for item in db[col_name].find({"_id": {"$gt": oid_filter}}).sort("_id", 1):
                    # 获取 node 名字
                    node_id = item.get("rssNodeId")
                    node_name = "?"
                    if node_id:
                        node_doc = db["RssNode"].find_one({"id": int(node_id)})
                        if node_doc:
                            node_name = node_doc.get("sitename", "?")
                            
                    docs.append({
                        "id": str(item["_id"]),
                        "title": item.get("title", ""),
                        "link": item.get("link", ""),
                        "author": item.get("author", ""),
                        "content": item.get("content", "") or item.get("description", ""),
                        "published": item.get("published", ""),
                        "node_name": node_name,
                        "group_code": item.get("group_code", ""),
                        "fetched_at": item["_id"].generation_time.isoformat()
                    })
        
        if docs:
            docs.sort(key=lambda x: x["fetched_at"])
            batch_size = 200
            for i in range(0, len(docs), batch_size):
                batch = docs[i:i + batch_size]
                res = _meili_request("/indexes/rss/documents", "POST", batch)
                if res:
                    rss_count += len(batch)
                    state["last_rss_sync"] = batch[-1]["fetched_at"]
    except Exception as e:
        print(f"Error syncing RSS: {e}")

    # 保存最新状态
    _save_sync_state(state)
    
    return f"✅ 同步完成！导入新邮件 {email_count} 封，新 RSS 文章 {rss_count} 篇。"

if __name__ == "__main__":
    print(sync_meili())
