#!/usr/bin/env python3
"""Stage 5b 话题变化 diff (vps1, weekly)。命名(Stage 4)之后跑。
本轮 /tmp/topic_labels.json vs 上轮 /tmp/topic_labels_prev.json,
指纹 = topic中文名 + top5关键词, 匹配延续话题, 输出新/消亡/涨跌。
把本轮存为下轮基线 (topic_labels_prev.json)。
"""
import os, json, urllib.request
from datetime import datetime
from paths import cfg

CUR = cfg("vps_tmp_dir", "/tmp") + "/topic_labels.json"
PREV = cfg("vps_work_dir", "/home/liteagent/rss_topic_work") + "/topic_labels_prev.json"  # 跨周基线, 必须持久(不放 /tmp)


def fingerprint(name, keywords):
    kw = ",".join(sorted((keywords or [])[:5]))
    return "{}|{}".format(name, kw)


def load_topics(path):
    if not os.path.exists(path):
        return {}
    d = json.load(open(path, encoding="utf-8"))
    names = d.get("topic_names_cn", {})          # cat::tid -> name
    clusters = d.get("clusters", {})              # cat::tid -> {count, keywords, ...}
    out = {}
    for key, info in clusters.items():
        if key.endswith("::-1"):
            continue
        name = names.get(key, "主题" + key.split("::")[-1])
        fp = fingerprint(name, info.get("keywords", []))
        out[fp] = {"name": name, "category": key.split("::")[0], "count": info.get("count", 0),
                   "keywords": info.get("keywords", [])[:5]}
    return out


cur = load_topics(CUR)
prev = load_topics(PREV)
cur_fps, prev_fps = set(cur), set(prev)
same = cur_fps & prev_fps
new = cur_fps - prev_fps
gone = prev_fps - cur_fps

print("\n📊 话题变化报告 ({})".format(datetime.now().strftime("%Y-%m-%d")), flush=True)
print("  延续: {}  新增: {}  消亡: {}".format(len(same), len(new), len(gone)), flush=True)

if new:
    print("\n🆕 新增话题 ({}):".format(len(new)), flush=True)
    for fp in sorted(new, key=lambda f: cur[f]["count"], reverse=True)[:10]:
        t = cur[fp]
        print("  [{}] {} ({}篇)".format(t["category"], t["name"], t["count"]), flush=True)

if gone:
    print("\n💨 消亡话题 ({}):".format(len(gone)), flush=True)
    for fp in sorted(gone)[:10]:
        t = prev[fp]
        print("  [{}] {}".format(t["category"], t["name"]), flush=True)

if same:
    changes = []
    for fp in same:
        c, p = cur[fp], prev[fp]
        pc, cc = p["count"], c["count"]
        if pc > 10 and cc > 0:
            changes.append((c["name"], c["category"], cc, pc, (cc - pc) / pc * 100))
    changes.sort(key=lambda x: x[4], reverse=True)
    if changes:
        print("\n📈 涨幅最大:", flush=True)
        for name, cat, cc, pc, chg in changes[:5]:
            print("  [{}] {}: {}->{} (+{:.0f}%)".format(cat, name, pc, cc, chg), flush=True)
        print("📉 跌幅最大:", flush=True)
        for name, cat, cc, pc, chg in changes[-5:]:
            print("  [{}] {}: {}->{} ({:.0f}%)".format(cat, name, pc, cc, chg), flush=True)

# 话题质量波动检查: 离群率变化 > 20% 暂存不切基线 + 告警, 否则切基线
import shutil


def _outlier_rate(path):
    if not os.path.exists(path):
        return None
    dt = json.load(open(path, encoding="utf-8")).get("doc_topic", {})
    return (sum(v.endswith("::-1") for v in dt.values()) / len(dt)) if dt else None


def push_alert(msg):
    try:
        urllib.request.urlopen(urllib.request.Request(
            cfg("lite_agent_alert_url", "http://127.0.0.1:8887/api/v1/alert"),
            data=json.dumps({"title": "📊 话题质量波动", "text": msg, "color": "orange",
                             "dedup_key": "topic_diff_swing:{}".format(datetime.now().strftime("%Y%m%d"))}).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Authorization": "Bearer " + os.environ.get("API_AUTH_TOKEN", "")}), timeout=10)
        print("  -> alerted via /api/v1/alert", flush=True)
    except Exception as e:
        print("  alert skip: {}".format(e), flush=True)


_co, _po = _outlier_rate(CUR), _outlier_rate(PREV)
_swing = abs(_co - _po) if (_co is not None and _po is not None) else 0.0
if _swing < 0.2:
    shutil.copy(CUR, PREV)
    print("\n基线已更新 -> {} (离群率 {}->{}, 下周 diff 用)".format(PREV, _po, _co), flush=True)
else:
    push_alert("⚠️ 每周话题质量波动 {:.0%} (离群率 {}->{}), 已暂存不切基线, 回复 /accept 切换".format(_swing, _po, _co))
