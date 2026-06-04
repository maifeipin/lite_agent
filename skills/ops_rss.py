import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import AgentResponse

_config = None


def _rss_config():
    global _config
    if _config is None:
        import config_loader
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
        summary = exc[:120].strip() if exc and exc != 'None' else ''
        lines.append(f'**[{i}] {site}**\n{title}')
        if summary:
            lines.append(f'_{summary}_')
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

HOT_KEYWORDS = [
    '大模型', 'Agent', 'Agent', '英伟达', 'OpenAI', 'DeepSeek',
    '架构', '开源', '离职', '模型', '训练', '推理', '多模态',
    '机器人', '具身', 'GPU', '蒸馏', 'RAG', '向量',
]

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
    import re, subprocess, json
    m = re.search(r'/t/(\d+)', link)
    if not m:
        return 0
    tid = m.group(1)
    url = f'https://www.v2ex.com/api/v2/topics/{tid}/replies?p=1'
    try:
        r = subprocess.run(
            ['curl', '-x', 'socks5h://127.0.0.1:18988', '-k', '-s', '-m', '10',
             '-H', f'Authorization: Bearer {V2EX_TOKEN}', url],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(r.stdout)
        if isinstance(data, dict):
            result = data.get('result', data)
            return len(result) if isinstance(result, list) else 0
        if isinstance(data, list):
            return len(data)
        return 0
    except Exception:
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
            pushed_ids = set(json.load(f).get('ids', []))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    scored = []
    seen_titles = set()
    for item in articles:
        sid = str(item['_id'])
        if sid in pushed_ids:
            continue

        site = nodes.get(int(item.get('rssNodeId', 0)), '?')
        exc = (item.get('excerpt') or '')
        title = item.get('title', '')
        if not exc or exc == 'None' or len(exc) < 10:
            continue

        title_key = title[:80]
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)

        score = SITE_QUALITY.get(site, 5)
        score += sum(1 for kw in HOT_KEYWORDS if kw.lower() in (title + exc).lower())
        for tag in V2EX_LOW_TAGS:
            if f'[{tag}]' in title:
                score -= 10
                break
        link = item.get('link', '')
        scored.append((score, item, site, exc[:120], sid, link))

    v2ex_calls = 0
    for i, (score, item, site, exc, sid, link) in enumerate(scored):
        if 'V2EX' in site or '话题' in site:
            if v2ex_calls >= 20:
                break
            v2ex_calls += 1
            replies = _v2ex_reply_count(link)
            if replies >= 100:
                scored[i] = (score + 5, item, site, exc, sid, link)
            elif replies >= 50:
                scored[i] = (score + 3, item, site, exc, sid, link)
            elif replies >= 20:
                scored[i] = (score + 1, item, site, exc, sid, link)

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:5]

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
            lines.append(f'_{exc}_')
        lines.append('')
        new_pushed.add(sid)

    os.makedirs(os.path.dirname(PUSHED_FILE), exist_ok=True)
    with open(PUSHED_FILE, 'w') as f:
        json.dump({'ids': list(new_pushed), 'updated': today}, f)

    c.close()
    return '\n'.join(lines)


def _overview_view(groups, col_name, today, db):
    lines = [f'**RSS 今日采集概览** · {today}\n']
    for g in sorted(groups.values(), key=lambda x: int(x.get('sortid', '99'))):
        gid = int(g['id'])
        cnt = db[col_name].count_documents(
            {'groupid': gid, 'pubdate': {'$regex': '^' + today}}
        )
        lines.append(f'`::rss {g["code"]}` **{g["name"]}**: {cnt} 篇')
    lines.append('\n发送 `::rss <分组>` 查看详情')
    return AgentResponse('\n'.join(lines), title='📊 RSS 概览', color='blue')
