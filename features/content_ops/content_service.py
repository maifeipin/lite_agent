"""
Content Ops 内容、博客、剪藏与搜索业务服务 (features.content_ops.content_service)。
整合 Halo 博客管理、网页剪藏、HedgeDoc 上传、静态网页抓取、RSS 抓取节点状态与互联网搜索。
"""
import os
import sys
import json
import time
import sqlite3
import hashlib
import threading
import urllib.request
import urllib.error
import urllib.parse
from html.parser import HTMLParser
from typing import Optional, List, Dict, Any

from core.config_loader import load_config
from core.utils.hedgedoc import upload_to_hedgedoc

# ============================================================
# 1. Halo 博客业务
# ============================================================
_cached_halo_token = None


def get_halo_config() -> Dict[str, Any]:
    return (load_config() or {}).get("halo", {})


def get_halo_token(config: Dict[str, Any]) -> str:
    global _cached_halo_token
    if _cached_halo_token:
        return _cached_halo_token

    url = config.get("url", "http://127.0.0.1:8090").rstrip("/")
    username = config.get("username", "admin")
    password = config.get("password", "")

    if not password:
        raise ValueError("缺少 Halo 密码配置，且没有有效的 Token，请在 config.json 中配置 halo.password")

    req = urllib.request.Request(
        f"{url}/api/admin/login",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode("utf-8"))
            if res.get("status") == 200:
                _cached_halo_token = res["data"]["access_token"]
                return _cached_halo_token
            else:
                raise ValueError(f"登录失败: {res.get('message')}")
    except Exception as e:
        raise ValueError(f"无法获取 Halo Token: {e}")


def halo_request(method: str, endpoint: str, payload: Optional[dict] = None) -> Any:
    global _cached_halo_token
    config = get_halo_config()
    url = config.get("url", "http://127.0.0.1:8090").rstrip("/")
    token = get_halo_token(config)

    headers = {
        "ADMIN-Authorization": token,
        "Content-Type": "application/json"
    }

    req_url = f"{url}{endpoint}"
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(req_url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _cached_halo_token = None
            token = get_halo_token(config)
            headers["ADMIN-Authorization"] = token
            req = urllib.request.Request(req_url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=15) as retry_response:
                return json.loads(retry_response.read().decode("utf-8"))
        raise e


def blog_list_articles(limit: int = 5) -> str:
    try:
        res = halo_request("GET", f"/api/admin/posts?page=0&size={limit}&sort=createTime,desc")
        if res.get("status") != 200:
            return f"获取失败: {res.get('message')}"

        posts = res.get("data", {}).get("content", [])
        if not posts:
            return "当前博客还没有任何文章。"

        out = ["=== 最近发布的博客文章 ==="]
        for p in posts:
            out.append(f"- [{p.get('id')}] {p.get('title')} ({p.get('status')}) - {p.get('createTime')}")
        return "\n".join(out)
    except Exception as e:
        return str(e)


def blog_publish_article(title: str, content: str, status: str = 'PUBLISHED') -> str:
    payload = {
        "title": title,
        "originalContent": content,
        "formatContent": "", # Halo usually parses Markdown itself, but passing empty formatContent is okay or we might need a parser.
        "status": status.upper(),
        "summary": content[:100] + "...",
        "allowComment": True,
        "keepMarkdown": True # Important for some Halo versions
    }

    try:
        res = halo_request("POST", "/api/admin/posts", payload)
        if res.get("status") == 200:
            post_id = res["data"]["id"]
            post_url = res["data"].get("fullPath", f"/archives/{post_id}")
            return f"✅ 文章《{title}》发布成功！\nID: {post_id}\n状态: {status}\n链接: {post_url}"
        else:
            return f"❌ 发布失败: {res.get('message')}"
    except Exception as e:
        return str(e)


def blog_export_articles(export_dir: str = '/root/blog_export', output_dir: Optional[str] = None) -> str:
    """批量导出博客所有文章为本地 Markdown 归档文件"""
    target_dir = output_dir if output_dir is not None else export_dir
    norm_dir = os.path.normpath(target_dir)
    if '..' in norm_dir.split(os.sep) or norm_dir.startswith(('/etc', '/bin', '/sbin', '/var/log', '/boot', '/dev', '/proc', '/sys')):
        return "❌ 拒绝访问: 非法的导出目录路径 (禁止导出到系统关键目录或向上跳跃)"

    try:
        res = halo_request("GET", "/api/admin/posts?page=0&size=1000")
        if res.get("status") != 200:
            return f"❌ 导出失败: {res.get('message')}"
        posts = res.get("data", {}).get("content", [])
        if not posts:
            return "没有任何文章可以导出。"

        os.makedirs(target_dir, exist_ok=True)
        count = 0
        for p in posts:
            title = p.get("title", "未命名").replace("/", "_").replace("\\", "_")
            content = p.get("originalContent", "")
            file_path = os.path.join(target_dir, f"{title}.md")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(f"---\n")
                f.write(f"title: {title}\n")
                f.write(f"date: {p.get('createTime')}\n")
                f.write(f"---\n\n")
                f.write(content)
            count += 1
        return f"✅ 成功导出 {count} 篇文章至本地目录: `{target_dir}`"
    except Exception as e:
        return f"❌ 导出异常: {e}"


# ============================================================
# 2. 网页剪藏与笔记上传 (Web Clipper & HedgeDoc)
# ============================================================
def _get_rssadapter_url() -> str:
    cfg = load_config() or {}
    return cfg.get("rssadapter", {}).get("url", "http://100.103.70.97:5216")


_CACHE_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data',
    'web_clips.db'
)
_cache_lock = threading.Lock()
_cache_inited = False

_TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'spss', 'spsnuid', 'spsdevid', 'spsvid', 'spsshare', 'spsts', 'spstoken',
    'fr', 'from', 'share_token', 'share_from',
    'wxshare_appkey', 'scene', 'srcid', 'mid',
    'gclid', 'fbclid', 'msclkid',
}


def _get_hedgedoc_config() -> dict:
    cfg = load_config() or {}
    return cfg.get("hedgedoc", {})


def _normalize_url(url: str) -> str:
    """规范化 URL：剥掉跟踪参数 + 排序剩余参数，保证同一资源命中同一缓存键"""
    try:
        p = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        kept = [(k, v) for k, v in qs if k.lower() not in _TRACKING_PARAMS]
        kept.sort()
        new_q = urllib.parse.urlencode(kept)
        return urllib.parse.urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, '', new_q, ''))
    except Exception:
        return url


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_CACHE_DB_PATH, timeout=10.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _cache_init():
    """建表（幂等）"""
    global _cache_inited
    if _cache_inited:
        return
    try:
        os.makedirs(os.path.dirname(_CACHE_DB_PATH), exist_ok=True)
        with _cache_lock:
            if _cache_inited:
                return
            with _connect() as conn:
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
    except Exception:
        pass


_TTL_SUCCESS = 7 * 24 * 3600
_TTL_FAILURE = 5 * 60


def _cache_get(url: str):
    """查缓存。命中且未过期则返回 dict（含 hedgedoc_url），否则 None"""
    _cache_init()
    key = _normalize_url(url)
    now = time.time()
    try:
        with _connect() as conn:
            row = conn.execute(
                'SELECT success, title, markdown, image_count, error, hedgedoc_url, created_at, expires_at '
                'FROM web_clips WHERE url_key = ?',
                (key,)
            ).fetchone()
        if not row:
            return None
        success, title, markdown, image_count, error, hedgedoc_url, created_at, expires_at = row
        if expires_at < now:
            return None
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
    except Exception:
        return None


def _cache_put(url: str, result: dict, hedgedoc_url: str = ''):
    """写缓存"""
    _cache_init()
    key = _normalize_url(url)
    now = time.time()
    success = bool(result.get('success', result.get('Success')))
    ttl = _TTL_SUCCESS if success else _TTL_FAILURE
    try:
        with _connect() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO web_clips '
                '(url_key, original_url, success, title, markdown, image_count, error, hedgedoc_url, created_at, expires_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    key, url, 1 if success else 0,
                    result.get('title') or result.get('Title') or '',
                    result.get('markdown') or result.get('Markdown') or result.get('content') or '',
                    int(result.get('imageCount') or result.get('ImageCount') or 0),
                    result.get('error') or result.get('Error'),
                    hedgedoc_url,
                    now, now + ttl,
                )
            )
            conn.execute('DELETE FROM web_clips WHERE expires_at < ?', (now,))
    except Exception:
        pass


def _upload_to_hedgedoc(markdown_text: str) -> str:
    """上传 Markdown 到 HedgeDoc，返回公网链接（复用通用工具包）"""
    hc = _get_hedgedoc_config()
    if not hc.get("enabled"):
        return ""
    return upload_to_hedgedoc(markdown_text, hc) or ""


def _call_url2md(url: str, screenshot: bool = False) -> dict:
    """调用 RssAdapter 的 /api/Url2Md（带 SQLite 缓存）"""
    if not screenshot:
        cached = _cache_get(url)
        if cached is not None:
            return cached

    api_url = _get_rssadapter_url().rstrip('/') + "/api/Url2Md"
    req_data = json.dumps({"url": url, "screenshot": screenshot}).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=req_data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        result = {"success": False, "error": f"无法连接 RssAdapter ({api_url}): {e}", "url": url}
    except Exception as e:
        result = {"success": False, "error": str(e), "url": url}

    if not screenshot:
        _cache_put(url, result)
    return result


def _call_url2md_batch(urls: list) -> list:
    """调用 RssAdapter 的 /api/Url2Md/batch"""
    api_url = _get_rssadapter_url().rstrip('/') + "/api/Url2Md/batch"
    payload = [{"url": u, "screenshot": False} for u in urls]
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=req_data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if isinstance(res, list):
                return res
            return [res]
    except Exception as e:
        return [{"Success": False, "Error": str(e), "url": u} for u in urls]


def _smart_deliver(result: dict, force_hedgedoc: bool = False, auto_upload: bool = True) -> str:
    """根据内容长度和图片数量，决定是直接返回 MD 还是上传 HedgeDoc 返回链接。

    返回的字符串是最终答复——LLM 应原样转发给用户，不要二次包装、概括或虚构事实。
    每种返回格式都包含明确的元信息行（字数 / 图片数 / 是否有 HedgeDoc 链接），
    避免 LLM 看到纯文本时编造"已上传"等不存在的事实。
    """
    if not result.get("success", result.get("Success")):
        err_msg = result.get('error') or result.get('Error') or '未知错误'
        return f"❌ 抓取失败: {err_msg}"

    title = result.get("title") or result.get("Title") or "无标题"
    markdown = result.get("markdown") or result.get("Markdown") or result.get("content") or ""
    image_count = int(result.get("imageCount") or result.get("ImageCount") or 0)
    source_url = result.get("url") or result.get("Url") or ""

    should_upload = auto_upload and (force_hedgedoc or len(markdown) > 2500 or image_count > 0)
    if should_upload:
        try:
            hedgedoc_url = result.get('hedgedoc_url', '')
            if not hedgedoc_url:
                hedgedoc_url = _upload_to_hedgedoc(markdown)
                if hedgedoc_url and source_url:
                    _cache_put(source_url, result, hedgedoc_url=hedgedoc_url)
            if hedgedoc_url:
                summary = markdown[:800].replace('\n', ' ').strip()
                lines = [
                    f"📄 **{title}**",
                    f"",
                    f"> {summary}{'...' if len(markdown) > 800 else ''}",
                    f"",
                    f"📊 全文 {len(markdown)} 字 | 🖼 {image_count} 张图片",
                    f"🔗 [点击查看完整图文（需登录 HedgeDoc 账号）]({hedgedoc_url})",
                ]
                if source_url:
                    lines.append(f"📎 [原文链接]({source_url})")
                return "\n".join(lines)
        except Exception:
            pass

    meta_line = f"📊 全文 {len(markdown)} 字（未达上传阈值，仅返回纯文本）"
    if source_url:
        meta_line += f" | 📎 [原文链接]({source_url})"

    if len(markdown) > 2500:
        return (
            f"📄 **{title}**\n\n"
            f"{markdown[:2400]}\n\n"
            f"... (全文 {len(markdown)} 字，HedgeDoc 上传失败已截断)\n\n"
            f"{meta_line}"
        )
    return f"📄 **{title}**\n\n{markdown}\n\n---\n{meta_line}"


def web_clip(
    urls: str = "",
    screenshot: bool = False,
    force_hedgedoc: bool = False,
    url: Optional[str] = None,
    output_format: str = "markdown",
    auto_upload: bool = True
) -> str:
    """抓取一个或多个 URL 并转为 Markdown"""
    raw_urls = urls or url or ""
    url_list = [u.strip() for u in raw_urls.split(',') if u.strip()]
    if not url_list:
        return "❌ 请提供至少一个有效的 URL"
    if len(url_list) > 3:
        return "❌ 单次最多支持 3 个 URL"

    if len(url_list) == 1:
        result = _call_url2md(url_list[0], screenshot=screenshot)
        return _smart_deliver(result, force_hedgedoc=force_hedgedoc, auto_upload=auto_upload)

    results = _call_url2md_batch(url_list)
    output_lines = [f"📋 **批量抓取结果** ({len(results)}/{len(url_list)} 完成)\n"]
    for i, res in enumerate(results, 1):
        delivery = _smart_deliver(res, force_hedgedoc=force_hedgedoc, auto_upload=auto_upload)
        t = res.get('Title') or res.get('title') or (url_list[i - 1][:50] if i - 1 < len(url_list) else f"URL {i}")
        output_lines.append(f"---\n### [{i}] {t}\n{delivery}\n")
    return "\n".join(output_lines)


def upload_markdown_to_hedgedoc(content: str, title: str = "Untitled Note") -> str:
    """上传任意 Markdown 文本到 HedgeDoc"""
    cfg = load_config() or {}
    hc = cfg.get("hedgedoc", {})
    if not hc.get("enabled"):
        return "❌ HedgeDoc 服务未在 config.json 中启用。"
    try:
        url = upload_to_hedgedoc(content, hc)
        if url:
            return f"✅ 已成功上传到 HedgeDoc:\n👉 [点击查看笔记]({url})"
        return "❌ 上传 HedgeDoc 失败，未返回有效链接。"
    except Exception as e:
        return f"❌ 上传 HedgeDoc 异常: {e}"


def ops_web_fetch(url: str, use_proxy: bool = True) -> str:
    """抓取网页 HTML 内容（仅适合静态/简单页面）。先直连，失败走 SOCKS5 代理"""
    import subprocess, re

    if not url.startswith(('http://', 'https://')):
        return "❌ 拒绝访问: url 必须以 http:// 或 https:// 开头"

    def try_fetch(proxy: str = '') -> tuple:
        cmd = ['curl', '-sL', '-m', '15', '--max-filesize', '500000',
               '-H', 'User-Agent: Mozilla/5.0 (compatible; LiteAgent/1.0)']
        if proxy:
            cmd.extend(['-x', proxy])
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return r.stdout, ''
        except subprocess.TimeoutExpired:
            return '', '请求超时'
        except Exception as e:
            return '', str(e)

    html, err = try_fetch()
    if not html and use_proxy:
        html, err2 = try_fetch('socks5://127.0.0.1:18988')
        if not html:
            return f'❌ 抓取失败: 直连={err or "无内容"}, 代理={err2 or "无内容"}'

    if not html or len(html) < 100:
        return f'❌ 页面内容过短或无内容 ({len(html)} 字符)'

    html = html[:50000]

    title = ''
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
    if m:
        title = re.sub(r'<[^>]+>', '', m.group(1)).strip()

    for tag in ['script', 'style', 'nav', 'header', 'footer', 'aside']:
        html = re.sub(f'<{tag}[^>]*>.*?</{tag}>', '', html, flags=re.I | re.S)

    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    text = re.sub(r'</(p|div|h\d|li|tr)>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = '\n'.join(line.strip() for line in text.split('\n') if line.strip())

    if len(text) > 3000:
        text = text[:3000] + '\n\n... (内容过长已截断)'

    if title:
        text = f'📌 {title}\n\n{text}'

    return text


# ============================================================
# 3. RSS 节点状态
# ============================================================
def get_rss_node_status(hours_threshold: int = 72) -> str:
    """检查 RSS 节点的更新状态"""
    try:
        from features.content_ops.rss_service import get_rss_db
    except ImportError:
        return "❌ 无法加载数据库配置"

    c, db_name = get_rss_db()
    db = c[db_name]

    from datetime import datetime, timedelta
    threshold_date = datetime.now() - timedelta(hours=hours_threshold)
    nodes = list(db['RssNode'].find({'isEnable': 1}))

    outdated_nodes = []
    for node in nodes:
        sitename = node.get('sitename', '未知节点')
        last_update = node.get('lastupdate')
        dt = None
        if isinstance(last_update, datetime):
            dt = last_update
        elif isinstance(last_update, str):
            try:
                time_str = last_update[:19].replace('T', ' ')
                dt = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
        if dt and dt < threshold_date:
            outdated_nodes.append(f"- **{sitename}**: 最后更新于 {dt.strftime('%Y-%m-%d %H:%M')}")
        elif not dt:
            outdated_nodes.append(f"- **{sitename}**: 暂无更新记录或时间格式异常")

    c.close()
    if outdated_nodes:
        return f"\n⚠️ **注意：以下 RSS 节点超过 {hours_threshold} 小时未更新：**\n" + "\n".join(outdated_nodes)
    return f"\n✅ **所有启用的 RSS 节点都在 {hours_threshold} 小时内正常更新。**"


# ============================================================
# 4. 全网搜索 (Web Search)
# ============================================================
class DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.current_result = None
        self.in_a = False
        self.in_snippet = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        class_name = attrs_dict.get('class', '')
        if tag == 'div' and 'result__body' in class_name:
            self.current_result = {'title': '', 'link': '', 'snippet': ''}
            self.results.append(self.current_result)
        elif tag == 'a' and self.current_result is not None:
            if 'result__a' in class_name:
                self.in_a = True
                href = attrs_dict.get('href', '')
                if 'uddg=' in href:
                    raw_url = href.split('uddg=')[1].split('&')[0]
                    self.current_result['link'] = urllib.parse.unquote(raw_url)
                else:
                    self.current_result['link'] = href
        elif tag in ('a', 'div') and self.current_result is not None:
            if 'result__snippet' in class_name:
                self.in_snippet = True

    def handle_endtag(self, tag):
        if tag == 'a':
            self.in_a = False
        elif tag in ('a', 'div'):
            self.in_snippet = False

    def handle_data(self, data):
        if self.current_result is not None:
            if self.in_a:
                self.current_result['title'] += data
            elif self.in_snippet:
                self.current_result['snippet'] += data


def web_search(query: str, max_results: int = 5) -> str:
    """通过 DuckDuckGo Lite 网页执行无广告搜索"""
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
        parser = DDGParser()
        parser.feed(html)
        results = [r for r in parser.results if r.get('link')][:max_results]
        if not results:
            return f"未找到关于 '{query}' 的搜索结果。"
        lines = [f"🔍 **关于 '{query}' 的搜索结果 (Top {len(results)}):**\n"]
        for idx, r in enumerate(results, 1):
            lines.append(f"{idx}. [{r['title'].strip()}]({r['link']})\n   {r['snippet'].strip()}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"❌ 搜索失败: {e}"


# 兼容符号
ops_rss_node_status = get_rss_node_status
hedgedoc_upload = upload_markdown_to_hedgedoc
