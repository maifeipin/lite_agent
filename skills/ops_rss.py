import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import AgentResponse


def handle_rss(msg, args: str, session_mgr) -> AgentResponse:
    import pymongo
    from datetime import date

    c = pymongo.MongoClient('mongodb://root:M1jiqiS1.v@localhost:27017', serverSelectionTimeoutMS=5000)
    db = c['rsslite']
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

    session_mgr.add_message(
        msg.session_key, 'system',
        f'[RSS {g["name"]} 文章列表]\n' + '\n'.join(ctx_brief)
    )

    return AgentResponse('\n'.join(lines), title=f'📰 {g["name"]}', color='blue')


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
