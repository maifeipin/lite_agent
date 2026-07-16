import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import AgentResponse
from core.command_registry import slash_command

_config = None


def _rss_config():
    global _config
    if _config is None:
        from core import config_loader
        _config = config_loader.load_config()
    return _config


def _get_db():
    """从 config.json 读取 MongoDB 连接，不硬编码密码"""
    import pymongo
    rssdb = _rss_config().get('rssdb', {})
    return pymongo.MongoClient(rssdb.get('uri', 'mongodb://localhost:27017'), serverSelectionTimeoutMS=5000), rssdb.get('database', 'rsslite')


def _v2ex_token():
    v2ex = _rss_config().get('v2ex', {})
    return v2ex.get('token', '') or os.environ.get('V2EX_TOKEN', '')


def _clean_excerpt(exc: str, limit: int = 120) -> str:
    """清理摘要中的 HTML 标签，并将换行替换为空格，避免排版断层"""
    if not exc or exc == 'None':
        return ""
    exc_clean = re.sub(r'<[^>]+>', '', exc)
    exc_clean = exc_clean.replace('\n', ' ').replace('\r', '').strip()
    return exc_clean[:limit]


def handle_rss(msg, args: str, session_mgr) -> AgentResponse:
    import pymongo
    from datetime import date

    c, db_name = _get_db()
    db = c[db_name]
    today = date.today().strftime('%Y-%m-%d')
    month = date.today().strftime('%Y%m')

    col_name = f'FeedItem_{month}'
    if col_name not in db.list_collection_names():
        return AgentResponse(f'表 {col_name} 不存在', title='❌ 错误', color='red')

    groups = {g['code']: g for g in db['FeedGroup'].find()}
    nodes = {int(n['id']): n.get('sitename', '?') for n in db['RssNode'].find()}

    group_filter = args
    item_number = 0
    gf_parts = group_filter.split()
    if gf_parts and gf_parts[-1].isdigit():
        item_number = int(gf_parts[-1])
        group_filter = ' '.join(gf_parts[:-1])

    if group_filter:
        gf = group_filter.lower()
        matched = {k: v for k, v in groups.items() if gf in k or gf in v.get('name', '').lower()}
        if not matched:
            group_list = ', '.join(f'{g["code"]}' for g in groups.values())
            return AgentResponse(
                f'未找到分组 "{group_filter}"。可用: {group_list}',
                title='⚠️', color='grey'
            )
        g = list(matched.values())[0]
        gid = int(g['id'])

        items = list(db[col_name].find(
            {'groupid': gid, 'pubdate': {'$regex': '^' + today}}
        ).sort('pubdate', -1).limit(max(8, item_number)))

        total = db[col_name].count_documents(
            {'groupid': gid, 'pubdate': {'$regex': '^' + today}}
        )

        if item_number > 0:
            return _detail_view(item_number, items, g, nodes, db)

        return _list_view(items, g, total, nodes, db, msg, session_mgr)

    return _overview_view(groups, col_name, today, db)


def _detail_view(item_number, items, g, nodes, db):
    if item_number > len(items):
        return AgentResponse(
            f'{g["name"]} 今日只有 {len(items)} 篇，没有第 {item_number} 篇',
            title='⚠️', color='grey'
        )
    item = items[item_number - 1]
    nid = item.get('rssNodeId', 0)
    site = nodes.get(int(nid) if nid else 0, '?')
    title = item.get('title', '(无标题)')
    link = item.get('link', '')
    exc = (item.get('excerpt') or '')
    content = item.get('content', '')
    detail = [f'**{g["name"]}** · 第 {item_number} 篇\n',
              f'📡 **{site}**',
              f'📌 {title}']
    if link:
        detail.append(f'🔗 {link}')
    if exc and exc != 'None':
        detail.append(f'\n📝 摘要:\n{exc[:500]}')
    if content and content != 'None':
        detail.append(f'\n📄 正文:\n{content[:800]}')
    detail.append(f'\n🕐 {item.get("pubdate", "?")}')
    detail.append(f'\n💡 想看原文? 复制链接到浏览器，或用 `::goal 帮我总结这篇文章 {link}` 让 AI 读')
    return AgentResponse('\n'.join(detail), title='📰 详情', color='violet')


def _list_view(items, g, total, nodes, db, msg, session_mgr):
    lines = [f'**{g["name"]}** · 今日 {total} 篇\n']
    ctx_brief = []
    for i, item in enumerate(items, 1):
        nid = item.get('rssNodeId', 0)
        site = nodes.get(int(nid) if nid else 0, '?')
        title = item.get('title', '(无标题)')
        exc = (item.get('excerpt') or '')
        
        summary = _clean_excerpt(exc, 120)
            
        lines.append(f'**[{i}] {site}**\n{title}')
        if summary:
            # 使用 blockquote 引用排版，过滤换行防止 markdown 的 > 断层
            lines.append(f'> {summary}')
        lines.append('')
        ctx_brief.append(f'[{i}] {title[:60]} ({site})')
        ctx_brief.append(f'     link: {item.get("link", "N/A")}')
        ctx_brief.append(f'     excerpt: {summary if summary else "(无)"}')

    session_mgr.add_message(
        msg.session_key, 'system',
        f'[RSS {g["name"]} 文章列表]\n' + '\n'.join(ctx_brief)
    )

    return AgentResponse('\n'.join(lines), title=f'📰 {g["name"]}', color='blue')


SITE_QUALITY = {
    '量子位': 9, '机器之心': 9, '虎嗅': 7, '36氪': 7, '新智元 - BAAI': 6,
    'IT之家': 5, '百度热搜': 4, '快问快答': 3, '虫部落': 3,
    'V2EX-全站': 6, '最新话题': 5,
}

BRIEF_GROUPS = [5, 3]

KEYWORD_WEIGHTS = {
    '大模型': 2, '多模态': 2, 'DeepSeek': 2, '英伟达': 1, 'OpenAI': 1,
    'Agent': 1, 'GPU': 1, 'RAG': 1, '向量': 1, '蒸馏': 1, '架构': 1,
    '开源': 1, '离职': 1, '模型': 1, '训练': 1, '推理': 1, '机器人': 1, '具身': 1
}

def _keyword_score(text, cap=4):
    t = text.lower()
    n = len(t)
    used = [False] * n
    total = 0
    for kw in sorted(KEYWORD_WEIGHTS, key=len, reverse=True):
        lk = kw.lower()
        i = t.find(lk)
        while i != -1:
            if not any(used[i:i+len(lk)]):
                for j in range(i, i+len(lk)):
                    used[j] = True
                total += KEYWORD_WEIGHTS[kw]
            i = t.find(lk, i+1)
    return min(total, cap)

V2EX_LOW_TAGS = ['推广', '交易', '外包', '酷工作', '招聘', '广告']

PUSHED_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'workspace', 'pushed_rss.json')
CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'workspace', 'rss_cache.json')

V2EX_TOKEN = _v2ex_token()


def rss_precompute() -> str:
    import json, time
    text = _rss_brief_compute()
    with open(CACHE_FILE, 'w') as f:
        json.dump({'text': text, 'ts': time.time()}, f)
    return 'RSS 预计算完成' if text else '(无新文章)'


def rss_brief() -> str:
    import json, time
    try:
        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)
            age = time.time() - cache.get('ts', 0)
            if age < 900:
                print(f'  📦 RSS 使用缓存 (已缓存 {age:.0f}s)')
                return cache.get('text', '')
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    print('  🔄 RSS 缓存过期/不存在，实时计算...')
    text = _rss_brief_compute()
    import json as _json, time as _time
    with open(CACHE_FILE, 'w') as f:
        _json.dump({'text': text, 'ts': _time.time()}, f)
    return text


def _v2ex_reply_count(link: str) -> int:
    import subprocess, json
    m = re.search(r'/t/(\d+)', link)
    if not m:
        return 0
    tid = m.group(1)
    url = f'https://www.v2ex.com/api/v2/topics/{tid}/replies?p=1'
    
    # Strict regex validation on generated URL to prevent command argument injection
    if not re.match(r'^https://www\.v2ex\.com/api/v2/topics/\d+/replies\?p=1$', url):
        return 0
        
    # Validate token character set
    token = V2EX_TOKEN.strip() if V2EX_TOKEN else ""
    if token and not re.match(r'^[a-zA-Z0-9_\-]+$', token):
        return 0

    try:
        r = subprocess.run(
            ['curl', '-x', 'socks5h://127.0.0.1:18988', '-k', '-s', '-m', '10',
             '-H', f'Authorization: Bearer {token}', url],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(r.stdout)
        if isinstance(data, dict):
            result = data.get('result', data)
            return len(result) if isinstance(result, list) else 0
        if isinstance(data, list):
            return len(data)
        return 0
    except Exception as e:
        try:
            print(f"  [Error] V2EX API: {e}")
        except:
            pass
        return 0


def _rss_brief_compute() -> str:
    import pymongo, json
    from datetime import date

    c, db_name = _get_db()
    db = c[db_name]
    today = date.today().strftime('%Y-%m-%d')
    month = date.today().strftime('%Y%m')
    col_name = f'FeedItem_{month}'

    nodes = {int(n['id']): n.get('sitename', '?') for n in db['RssNode'].find()}

    articles = list(db[col_name].find(
        {'groupid': {'$in': BRIEF_GROUPS}, 'pubdate': {'$regex': '^' + today}}
    ).sort('pubdate', -1))
    print(f'  📊 今日文章: {len(articles)} 篇 (分组 {BRIEF_GROUPS})')

    pushed_ids = set()
    try:
        with open(PUSHED_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, dict) and data.get('updated') == today:
                pushed_ids = set(data.get('ids', []))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    scored = []
    seen = set()
    for item in articles:
        sid = str(item['_id'])
        if sid in pushed_ids:
            continue

        site = nodes.get(int(item.get('rssNodeId', 0)), '?')
        exc = (item.get('excerpt') or '')
        title = item.get('title', '')
        
        # 1. 先进行摘要清理 (不截断)
        exc_clean = _clean_excerpt(exc, limit=10**9)
        
        # 2. 检查有效性 (依据清理后的摘要)
        if not exc_clean or len(exc_clean) < 10:
            continue
            
        link = item.get('link', '')
        link_base = link.split('#')[0].split('?')[0] if link else ""
        clean_title = re.sub(r'^(\[.*?\]\s*)+', '', title).strip()
        
        # 3. 去重逻辑: 单主键 (有链接按链接，无链接按清理后的标题前 80 字)
        key = link_base or clean_title[:80]
        if key and key in seen:
            continue
        if key:
            seen.add(key)

        score = SITE_QUALITY.get(site, 5)
        score += _keyword_score(title + " " + exc_clean)
        
        pubdate_str = item.get("pubdate", "")
        if pubdate_str and len(pubdate_str) >= 13:
            try:
                score += int(pubdate_str[11:13]) * 0.02
            except ValueError:
                pass
                
        skip = False
        for tag in V2EX_LOW_TAGS:
            if f'[{tag}]' in title:
                skip = True
                break
        if skip:
            continue
                
        # 截取最终摘要
        scored.append((score, item, site, exc_clean[:120], sid, link))

    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = scored[:30]

    v2ex_calls = 0
    for i, (score, item, site, exc, sid, link) in enumerate(candidates):
        if 'V2EX' in site or '话题' in site:
            if v2ex_calls >= 20:
                continue
            v2ex_calls += 1
            replies = _v2ex_reply_count(link)
            if replies >= 100:
                candidates[i] = (score + 5, item, site, exc, sid, link)
            elif replies >= 50:
                candidates[i] = (score + 3, item, site, exc, sid, link)
            elif replies >= 20:
                candidates[i] = (score + 1, item, site, exc, sid, link)

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:5]

    print(f'  📡 V2EX API 调用: {v2ex_calls} 次')
    print(f'  🏆 Top 5:')
    for score, item, site, exc, sid, link in top:
        print(f'     ⭐{score} {site} | {item.get("title","")[:50]}')

    if not top:
        c.close()
        return ''

    new_pushed = pushed_ids.copy()
    lines = [f'**RSS 精选** · {today}\n']
    for score, item, site, exc, sid, link in top:
        title = item.get('title', '(无标题)')[:80]
        lines.append(f'⭐{score} **[{site}]** {title}')
        if link and 'http' not in (exc or ''):
            lines.append(link)
        if exc:
            # 使用 blockquote 引用排版，避免使用 _ 包裹导致 URL 吞掉下划线破坏 Markdown 闭合
            lines.append(f'> {exc}')
        lines.append('')
        new_pushed.add(sid)

    os.makedirs(os.path.dirname(PUSHED_FILE), exist_ok=True)
    with open(PUSHED_FILE, 'w') as f:
        json.dump({'ids': list(new_pushed), 'updated': today}, f)

    c.close()
    
    # 增加对 RSS 节点状态的检查
    try:
        from skills.ops_rss_node import ops_rss_node_status
        node_status = ops_rss_node_status()
        if node_status and "注意" in node_status:
            lines.append(node_status)
    except Exception as e:
        print(f"  ❌ RSS Node check failed: {e}")
        
    return '\n'.join(lines)


def _overview_view(groups, col_name, today, db):
    lines = [f'**RSS 今日采集概览** · {today}\n']
    for g in sorted(groups.values(), key=lambda x: int(x.get('sortid', '99'))):
        gid = int(g['id'])
        cnt = db[col_name].count_documents(
            {'groupid': gid, 'pubdate': {'$regex': '^' + today}}
        )
        lines.append(f'`/rss_list {g["code"]}` **{g["name"]}**: {cnt} 篇')
    lines.append('\n发送 `/rss_list <分组>` 查看详情')
    return AgentResponse('\n'.join(lines), title='📊 RSS 概览', color='blue')

slash_command('/rss_fetch', category='RSS',
              description='获取今日 RSS 精选摘要 (Top 5)',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: rss_brief() or '(今日暂无精选文章)')


# ---- Meilisearch 标签查询 (category/topic) ----
_meili_cfg = _rss_config().get("meilisearch", {})
_MEILI_URL = _meili_cfg.get("url", "http://127.0.0.1:7700")
_MEILI_KEY = _meili_cfg.get("master_key", "")


def _meili_search(payload):
    """POST /indexes/rss/search; 失败返回 None (镜像 ops_meili_sync._meili_request, 供调用方降级)。"""
    import urllib.request, json
    try:
        req = urllib.request.Request(_MEILI_URL + "/indexes/rss/search",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + _MEILI_KEY, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  ⚠️ [rss_topic] Meili search failed: {e}")
        return None


def _cmd_rss_list(agent, msg, args):
    """查看 RSS 分组文章列表 (无参=概览, <分组>[ N]=列表/详情)。走 MongoDB FeedGroup。"""
    return handle_rss(msg, " ".join(args).strip(), agent.session_mgr)


def _cmd_rss_log(agent, msg, args):
    """查看 RSS 推送/预计算日志 (journalctl 近 2h)。"""
    import subprocess
    svc = _rss_config().get("service_name", "lite-agent")
    r = subprocess.run(
        f"journalctl -u {svc} --since '2 hours ago' --no-pager | grep -E 'RSS|缓存|文章|V2EX|Top|预计算' | tail -20",
        shell=True, capture_output=True, text=True, timeout=10)
    text = (r.stdout.strip() or r.stderr.strip() or '(无日志)')[-2500:]
    return AgentResponse(text, title='📋 RSS 日志', color='turquoise')


def _cmd_rss_topic(agent, msg, args):
    """按分类/主题查 RSS (Meilisearch)。无参=标签 facet 概览; 参数命中分类->按分类, 否则按主题。"""
    q = " ".join(args).strip()
    if not q:
        d = _meili_search({"q": "", "limit": 0, "facets": ["category", "topics"]})
        if not d:
            return AgentResponse("Meilisearch 搜索引擎暂时不可用, 请联系管理员检查",
                                 title="❌ 错误", color="red")
        fd = d.get("facetDistribution", {})
        lines = ["📊 RSS 标签概览 (Meilisearch)\n", "🗂 分类:"]
        for c, n in sorted(fd.get("category", {}).items(), key=lambda x: -x[1]):
            lines.append(f"  {c}: {n}")
        lines.append("\n🏷 主题 (top 15):")
        for t, n in sorted(fd.get("topics", {}).items(), key=lambda x: -x[1])[:15]:
            lines.append(f"  {t}: {n}")
        lines.append("\n用法: `/rss_topic <分类或主题名>` 查该标签下文章")
        return AgentResponse("\n".join(lines), title="📊 RSS 标签", color="violet")
    cats_d = _meili_search({"q": "", "limit": 0, "facets": ["category"]})
    if not cats_d:
        return AgentResponse("Meilisearch 搜索引擎暂时不可用, 请联系管理员检查",
                             title="❌ 错误", color="red")
    cats = set(cats_d.get("facetDistribution", {}).get("category", {}))
    if q in cats:
        d = _meili_search({"q": "", "limit": 10, "filter": f'category = "{q}"', "sort": ["date:desc"]})
        title = f"🗂 分类: {q}"
    else:
        d = _meili_search({"q": "", "limit": 10, "filter": f'topics = "{q}"', "sort": ["date:desc"]})
        title = f"🏷 主题: {q}"
    if not d:
        return AgentResponse("Meilisearch 搜索引擎暂时不可用, 请联系管理员检查",
                             title="❌ 错误", color="red")
    hits = d.get("hits", [])
    if not hits:
        return AgentResponse(f"未找到标签 `{q}` 下文章。\n可用分类: {', '.join(sorted(cats))}",
                             title="⚠️", color="grey")
    lines = [f"**{title}** · 约 {d.get('estimatedTotalHits', len(hits))} 篇 (显示 {len(hits)})\n"]
    for i, h in enumerate(hits, 1):
        lines.append(f"**[{i}] {h.get('source', '?')}**\n{h.get('title', '(无标题)')}")
        topics = h.get("topics") or []
        if topics:
            lines.append(f"🏷 {' / '.join(topics)}")
        link = h.get("link", "")
        if link:
            lines.append(f"🔗 {link}")
        lines.append("")
    return AgentResponse("\n".join(lines), title=title, color="blue")


slash_command('/rss_list', category='RSS',
              description='查看 RSS 分组文章列表 (无参=概览, <分组>=列表)',
              show_in_dashboard=True, guest_ok=False)(_cmd_rss_list)


slash_command('/rss_log', category='RSS',
              description='查看 RSS 推送/预计算日志',
              show_in_dashboard=False, guest_ok=False)(_cmd_rss_log)


slash_command('/rss_topic', category='RSS',
              description='按分类/主题查 RSS (Meilisearch, 无参=标签概览)',
              show_in_dashboard=True, guest_ok=False)(_cmd_rss_topic)
