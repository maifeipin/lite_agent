import sys, os, json, time, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
# Store cached token globally to avoid re-login on every request
_cached_token = None

def _get_halo_config():
    try:
        import config_loader
        return config_loader.load_config().get("halo", {})
    except Exception:
        return {}

def _get_halo_token(config):
    global _cached_token
    # If we have a cached token, try to use it first
    # However, since tokens expire, we might need to refresh if API calls fail.
    # For now, return the cached one. If you want full auto-refresh, we can do it on 401.
    if _cached_token:
        return _cached_token
        
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
                _cached_token = res["data"]["access_token"]
                return _cached_token
            else:
                raise ValueError(f"登录失败: {res.get('message')}")
    except Exception as e:
        raise ValueError(f"无法获取 Halo Token: {e}")

def _halo_request(method, endpoint, payload=None):
    global _cached_token
    config = _get_halo_config()
    url = config.get("url", "http://127.0.0.1:8090").rstrip("/")
    
    token = _get_halo_token(config)
    
    headers = {
        "ADMIN-Authorization": token,
        "Content-Type": "application/json"
    }
    
    req_url = f"{url}{endpoint}"
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(req_url, data=data, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # If token is invalid (401), clear cache and try once more if password is configured
        if e.code == 401 and _cached_token:
            _cached_token = None
            if config.get("password"):
                return _halo_request(method, endpoint, payload)
        raise ValueError(f"API 请求失败: {e.code} - {e.read().decode('utf-8')}")
    except Exception as e:
        raise ValueError(f"API 请求异常: {e}")

@skill(
    name='blog_list_articles',
    description='获取 Halo 博客最近发布的文章列表',
    params={
        'limit': {
            'type': 'integer',
            'description': '返回的文章数量，默认 5 篇',
            'default': 5
        }
    }
)
def blog_list_articles(limit: int = 5) -> str:
    try:
        res = _halo_request("GET", f"/api/admin/posts?page=0&size={limit}&sort=createTime,desc")
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


@skill(
    name='blog_publish_article',
    description='发布 Markdown 文章到 Halo 博客。⚠️注意：当你决定要发布文章时，请直接并且立即调用此技能，绝不允许先回复确认文字或告知用户“准备发布”，必须直接执行调用！',
    params={
        'title': {
            'type': 'string',
            'description': '文章标题'
        },
        'content': {
            'type': 'string',
            'description': '文章的 Markdown 正文内容'
        },
        'status': {
            'type': 'string',
            'description': '状态，PUBLISHED 或 DRAFT',
            'default': 'PUBLISHED'
        }
    }
)
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
        res = _halo_request("POST", "/api/admin/posts", payload)
        if res.get("status") == 200:
            post_id = res["data"]["id"]
            post_url = res["data"].get("fullPath", f"/archives/{post_id}")
            return f"✅ 文章《{title}》发布成功！\nID: {post_id}\n状态: {status}\n链接: {post_url}"
        else:
            return f"❌ 发布失败: {res.get('message')}"
    except Exception as e:
        return str(e)


@skill(
    name='blog_export_articles',
    description='将 Halo 博客中的所有文章导出为 Markdown 文件到本地目录',
    params={
        'export_dir': {
            'type': 'string',
            'description': '导出目录路径，默认为 /root/blog_export',
            'default': '/root/blog_export'
        }
    }
)
def blog_export_articles(export_dir: str = '/root/blog_export') -> str:
    try:
        res = _halo_request("GET", "/api/admin/posts?page=0&size=1000")
        if res.get("status") != 200:
            return f"获取文章列表失败: {res.get('message')}"
            
        posts = res.get("data", {}).get("content", [])
        if not posts:
            return "没有任何文章可以导出。"
            
        import os
        os.makedirs(export_dir, exist_ok=True)
        
        count = 0
        for p in posts:
            title = p.get("title", "未命名").replace("/", "_").replace("\\", "_")
            content = p.get("originalContent", "")
            
            # Write file
            file_path = os.path.join(export_dir, f"{title}.md")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(f"---\n")
                f.write(f"title: {title}\n")
                f.write(f"date: {p.get('createTime')}\n")
                f.write(f"---\n\n")
                f.write(content)
            count += 1
            
        return f"✅ 成功导出 {count} 篇文章到目录 {export_dir}"
    except Exception as e:
        return str(e)
