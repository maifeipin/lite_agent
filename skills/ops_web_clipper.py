"""
Web Clipper 技能 — 通过 RssAdapter (Mac) 的 Playwright 有头浏览器抓取网页内容
转成 Markdown 并自动推送到 HedgeDoc

支持：
- 单个 URL 抓取
- 批量 URL 抓取（最多 10 条）
- LLM 可通过 rss_list_subscriptions 先查订阅列表再批量调用
"""

import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill

# ============================================================
#  配置
# ============================================================
_config = None

def _load_config():
    global _config
    if _config is None:
        import config_loader
        _config = config_loader.load_config()
    return _config

def _get_rssadapter_url():
    """获取 RssAdapter 的内网地址（Tailscale 直连 Mac）"""
    cfg = _load_config()
    return cfg.get("rssadapter", {}).get("url", "http://100.103.70.97:5216")

def _get_hedgedoc_config():
    cfg = _load_config()
    return cfg.get("hedgedoc", {})


# ============================================================
#  SQLite 缓存层 — 防止 LLM 对同一 URL 重复抓取
# ============================================================
import sqlite3, hashlib, threading
import urllib.parse as _urlparse

_CACHE_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'web_clips.db')
_cache_lock = threading.Lock()
_cache_inited = False

# 跟踪参数黑名单（规范化时剥掉）— 命中即视为同一资源
_TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'spss', 'spsnuid', 'spsdevid', 'spsvid', 'spsshare', 'spsts', 'spstoken',  # 网易
    'fr', 'from', 'share_token', 'share_from',  # 通用分享
    'wxshare_appkey', 'scene', 'srcid', 'mid',   # 微信/朋友圈
    'gclid', 'fbclid', 'msclkid',                # 广告点击
}

def _normalize_url(url: str) -> str:
    """规范化 URL：剥掉跟踪参数 + 排序剩余参数，保证同一资源命中同一缓存键"""
    try:
        p = _urlparse.urlparse(url)
        qs = _urlparse.parse_qsl(p.query, keep_blank_values=True)
        kept = [(k, v) for k, v in qs if k.lower() not in _TRACKING_PARAMS]
        kept.sort()
        new_q = _urlparse.urlencode(kept)
        # 移除 fragment、规范化路径
        return _urlparse.urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, '', new_q, ''))
    except Exception:
        return url

def _cache_init():
    """建表（幂等）"""
    global _cache_inited
    if _cache_inited:
        return
    os.makedirs(os.path.dirname(_CACHE_DB_PATH), exist_ok=True)
    with _cache_lock:
        if _cache_inited:
            return
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10.0) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS web_clips (
                    url_key       TEXT PRIMARY KEY,
                    original_url  TEXT NOT NULL,
                    success       INTEGER NOT NULL,
                    title         TEXT,
                    markdown      TEXT,
                    image_count   INTEGER DEFAULT 0,
                    error         TEXT,
                    hedgedoc_url  TEXT,
                    created_at    REAL NOT NULL,
                    expires_at    REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_expires ON web_clips(expires_at);
            """)
        _cache_inited = True

# TTL 策略：成功 7 天，失败 5 分钟（避免反爬误判被永久缓存）
_TTL_SUCCESS = 7 * 24 * 3600
_TTL_FAILURE = 5 * 60

def _cache_get(url: str):
    """查缓存。命中且未过期则返回 dict（含 hedgedoc_url），否则 None"""
    _cache_init()
    key = _normalize_url(url)
    now = time.time()
    try:
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10.0) as conn:
            row = conn.execute(
                'SELECT success, title, markdown, image_count, error, hedgedoc_url, created_at, expires_at '
                'FROM web_clips WHERE url_key = ?',
                (key,)
            ).fetchone()
        if not row:
            return None
        success, title, markdown, image_count, error, hedgedoc_url, created_at, expires_at = row
        if expires_at < now:
            return None  # 过期，让上层重抓
        age_sec = int(now - created_at)
        age_str = f'{age_sec//60}m' if age_sec >= 60 else f'{age_sec}s'
        print(f'  🗄️ web_clip 缓存命中 ({age_str} ago, key={key[:60]}...)', flush=True)
        return {
            'success': bool(success),
            'title': title or '',
            'markdown': markdown or '',
            'imageCount': image_count or 0,
            'error': error,
            'url': url,
            'hedgedoc_url': hedgedoc_url or '',
            '_cached': True,
        }
    except Exception as e:
        print(f'  ⚠️ 缓存读取异常: {e}', flush=True)
        return None

def _cache_put(url: str, result: dict, hedgedoc_url: str = ''):
    """写缓存"""
    _cache_init()
    key = _normalize_url(url)
    now = time.time()
    success = bool(result.get('success', result.get('Success')))
    ttl = _TTL_SUCCESS if success else _TTL_FAILURE
    try:
        with sqlite3.connect(_CACHE_DB_PATH, timeout=10.0) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO web_clips '
                '(url_key, original_url, success, title, markdown, image_count, error, hedgedoc_url, created_at, expires_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    key, url, 1 if success else 0,
                    result.get('title', ''),
                    result.get('markdown', ''),
                    int(result.get('imageCount', 0) or 0),
                    result.get('error') or result.get('Error'),
                    hedgedoc_url,
                    now, now + ttl,
                )
            )
            # 顺手清理过期项（轻量，每次写入都做）
            conn.execute('DELETE FROM web_clips WHERE expires_at < ?', (now,))
    except Exception as e:
        print(f'  ⚠️ 缓存写入异常: {e}', flush=True)

# ============================================================
#  HedgeDoc 上传（复用 agent.py 中的逻辑）
# ============================================================
def _upload_to_hedgedoc(markdown_text: str) -> str:
    """上传 Markdown 到 HedgeDoc，返回公网链接"""
    import requests
    hc = _get_hedgedoc_config()
    if not hc.get("enabled"):
        return ""

    s = requests.Session()
    headers = {'X-Forwarded-Proto': 'https'}

    # 1. 登录获取 Cookie
    login_url = hc.get("internal_url", "http://127.0.0.1:3030").rstrip('/') + "/login"
    s.post(login_url, data={
        'email': hc.get("email"),
        'password': hc.get("password")
    }, headers=headers, allow_redirects=False, timeout=10)

    cookie_str = '; '.join([f'{k}={v}' for k, v in s.cookies.items()])

    # 2. 创建文档
    headers['Cookie'] = cookie_str
    headers['Content-Type'] = 'text/markdown'
    new_url = hc.get("internal_url", "http://127.0.0.1:3030").rstrip('/') + "/new"
    r = requests.post(new_url, data=markdown_text.encode('utf-8'),
                      headers=headers, allow_redirects=False, timeout=10)

    location = r.headers.get('Location')
    if location:
        public_url = hc.get("public_url", "https://md.maifeipin.com").rstrip('/')
        if location.startswith("http"):
            import urllib.parse
            parsed = urllib.parse.urlparse(location)
            return public_url + parsed.path
        return public_url + location
    return ""

# ============================================================
#  RssAdapter API 调用
# ============================================================
def _call_url2md(url: str, screenshot: bool = False) -> dict:
    """调用 Mac 端 RssAdapter 的 /api/Url2Md（带 SQLite 缓存）"""
    # 1. 先查缓存（screenshot=True 跳过缓存，因为带截图的需求是即时的）
    if not screenshot:
        cached = _cache_get(url)
        if cached is not None:
            return cached

    import requests
    api_url = _get_rssadapter_url().rstrip('/') + "/api/Url2Md"
    try:
        resp = requests.post(api_url, json={
            "url": url,
            "screenshot": screenshot
        }, timeout=60)
        if resp.status_code == 200:
            result = resp.json()
        else:
            result = {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}", "url": url}
    except requests.exceptions.ConnectionError:
        result = {"success": False, "error": f"无法连接 RssAdapter ({api_url})，请确认 Mac 端服务已启动且 Tailscale 可达", "url": url}
    except Exception as e:
        result = {"success": False, "error": str(e), "url": url}

    # 2. 写缓存（不带截图的请求才缓存；截图响应有 base64 太大）
    if not screenshot:
        _cache_put(url, result)
    return result

def _call_url2md_batch(urls: list) -> list:
    """调用 Mac 端 RssAdapter 的 /api/Url2Md/batch"""
    import requests
    api_url = _get_rssadapter_url().rstrip('/') + "/api/Url2Md/batch"
    try:
        payload = [{"url": u, "screenshot": False} for u in urls]
        resp = requests.post(api_url, json=payload, timeout=120)
        if resp.status_code == 200:
            return resp.json()
        return [{"Success": False, "Error": f"HTTP {resp.status_code}"}]
    except Exception as e:
        return [{"Success": False, "Error": str(e)}]

# ============================================================
#  智能分发：判断是直接回复还是上传 HedgeDoc
# ============================================================
def _smart_deliver(result: dict) -> str:
    """根据内容长度和图片数量，决定是直接返回 MD 还是上传 HedgeDoc 返回链接"""
    if not result.get("success", result.get("Success")):
        return f"❌ 抓取失败: {result.get('Error', '未知错误')}"

    title = result.get("title", "无标题")
    markdown = result.get("markdown", "")
    image_count = result.get("imageCount", 0)
    source_url = result.get("url", "")

    # 判定阈值：超过 2500 字 或 包含图片 → 上传 HedgeDoc
    if len(markdown) > 2500 or image_count > 0:
        try:
            # 优先复用缓存里的 HedgeDoc 链接，避免重复上传新笔记
            hedgedoc_url = result.get('hedgedoc_url', '')
            if not hedgedoc_url:
                hedgedoc_url = _upload_to_hedgedoc(markdown)
                # 回写缓存里的 hedgedoc_url 字段（供下次命中复用）
                if hedgedoc_url and not result.get('_cached'):
                    _cache_put(source_url, result, hedgedoc_url=hedgedoc_url)
            if hedgedoc_url:
                summary = markdown[:800].replace('\n', ' ').strip()
                lines = [
                    f"📄 **{title}**",
                    f"",
                    f"> {summary}{'...' if len(markdown) > 800 else ''}",
                    f"",
                    f"📊 全文 {len(markdown)} 字 | 🖼 {image_count} 张图片",
                    f"🔗 [点击查看完整图文]({hedgedoc_url})",
                ]
                if source_url:
                    lines.append(f"📎 [原文链接]({source_url})")
                return "\n".join(lines)
        except Exception as e:
            # HedgeDoc 上传失败，降级为纯文本截断
            pass

    # 短文本直接回复
    if len(markdown) > 2500:
        return f"📄 **{title}**\n\n{markdown[:2400]}\n\n... (全文 {len(markdown)} 字，因平台限制已截断)"
    return f"📄 **{title}**\n\n{markdown}"

# ============================================================
#  对外暴露的 LLM Tool
# ============================================================
@skill(
    name='web_clip',
    description="""网页剪藏工具：抓取指定 URL 的网页内容，自动转换为 Markdown 格式并保存到 HedgeDoc。
支持微信公众号文章、网易新闻、知乎专栏等各类网页，能自动处理懒加载图片和反爬虫机制。
当内容超过 2500 字或包含图片时，自动上传到 HedgeDoc 并返回在线阅读链接。

使用场景：
- 用户给出具体 URL："帮我把这个链接转成笔记" → 直接传入 url
- 用户说批量操作："帮我把这几个链接都保存一下" → 传入逗号分隔的多个 URL
- 用户说模糊请求："帮我看看这篇文章讲了什么" + URL → 先调用 web_clip 获取内容，再自行总结

注意：如果用户提到"我关注的列表"、"我的订阅"等，请先调用 rss_today 获取文章列表和链接，再用本工具批量抓取。""",
    params={
        'urls': {
            'type': 'string',
            'description': '要抓取的 URL，多个 URL 用逗号分隔。例如："https://mp.weixin.qq.com/s/xxx" 或 "https://url1.com,https://url2.com"',
        },
        'screenshot': {
            'type': 'boolean',
            'description': '是否同时截取全页长图作为兜底（默认 false，当图片无法正常显示时可设为 true）',
            'default': False,
        }
    },
    tags=['web', 'tool', 'rss'],
)
def web_clip(urls: str, screenshot: bool = False) -> str:
    """抓取一个或多个 URL 并转为 Markdown"""
    # 解析 URL 列表
    url_list = [u.strip() for u in urls.split(',') if u.strip()]

    if not url_list:
        return "❌ 请提供至少一个有效的 URL"

    if len(url_list) > 10:
        return "❌ 单次最多支持 10 个 URL"

    # 单个 URL
    if len(url_list) == 1:
        result = _call_url2md(url_list[0], screenshot=screenshot)
        return _smart_deliver(result)

    # 批量 URL
    results = _call_url2md_batch(url_list)
    output_lines = [f"📋 **批量抓取结果** ({len(results)}/{len(url_list)} 完成)\n"]

    for i, result in enumerate(results, 1):
        delivery = _smart_deliver(result)
        output_lines.append(f"---\n### [{i}] {result.get('Title', url_list[i-1][:50])}\n{delivery}\n")

    return "\n".join(output_lines)
