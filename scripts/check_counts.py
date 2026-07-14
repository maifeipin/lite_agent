import sqlite3, json, urllib.request, os

# SQLite email counts
db = sqlite3.connect("/home/liteagent/mail-statement-parser/statements.db")
summaries = db.execute("SELECT COUNT(*) FROM email_summaries").fetchone()[0]
bodies = db.execute("SELECT COUNT(*) FROM email_bodies").fetchone()[0]
db.close()
print(f"SQLite: email_summaries={summaries}, email_bodies={bodies}")

# Meilisearch stats
req = urllib.request.Request(
    "http://127.0.0.1:7700/indexes/emails/stats",
    headers={"Authorization": "Bearer MeiliUnifiedSearchSecureKey2026!Awesome"}
)
r = urllib.request.urlopen(req)
d = json.loads(r.read())
print(f"Meilisearch emails: {d.get('numberOfDocuments', '?')}")

# Sync state
state_path = "/home/liteagent/lite_agent/data/meili_sync_state.json"
if os.path.exists(state_path):
    with open(state_path) as f:
        s = json.load(f)
    print(f"Sync state: email={s.get('last_email_sync','?')}, rss={s.get('last_rss_sync','?')}")
