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
    """调用 Mac 端 RssAdapter 的 /api/Url2Md"""
    import requests
    api_url = _get_rssadapter_url().rstrip('/') + "/api/Url2Md"
    try:
        resp = requests.post(api_url, json={
            "url": url,
            "screenshot": screenshot
        }, timeout=60)
        if resp.status_code == 200:
            return resp.json()
        return {"Success": False, "Error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except requests.exceptions.ConnectionError:
        return {"Success": False, "Error": f"无法连接 RssAdapter ({api_url})，请确认 Mac 端服务已启动且 Tailscale 可达"}
    except Exception as e:
        return {"Success": False, "Error": str(e)}

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
    if not result.get("Success"):
        return f"❌ 抓取失败: {result.get('Error', '未知错误')}"

    title = result.get("Title", "无标题")
    markdown = result.get("Markdown", "")
    image_count = result.get("ImageCount", 0)
    source_url = result.get("Url", "")

    # 判定阈值：超过 2500 字 或 包含图片 → 上传 HedgeDoc
    if len(markdown) > 2500 or image_count > 0:
        try:
            hedgedoc_url = _upload_to_hedgedoc(markdown)
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
