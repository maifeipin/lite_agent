# -*- coding: utf-8 -*-
import requests
import urllib.parse

def upload_to_hedgedoc(markdown_text: str, hc_config: dict) -> str:
    """上传 Markdown 文本至 HedgeDoc 并返回公网可访问 URL
    
    兼容 HedgeDoc v2（Fastify/NestJS 后端，含 CSRF token 与 /api/private/notes）
    与 HedgeDoc v1（旧版经典端点 /login 与 /new）。
    
    :param markdown_text: 待上传的完整内容
    :param hc_config: 从 config.json 中读取的 "hedgedoc" 配置字典
    :return: 成功返回公网 URL，失败返回空字符串 ""
    """
    if not hc_config or not hc_config.get("enabled"):
        return ""
        
    s = requests.Session()
    public_url = hc_config.get("public_url", "https://md.maifeipin.com").rstrip('/')
    base_url = hc_config.get("internal_url", "http://127.0.0.1:3031").rstrip('/')
    # HedgeDoc v2 后端通常在 3031 端口；如果配置了 3030，尝试连接 3031 或原地址
    candidate_urls = [base_url]
    if ":3030" in base_url:
        candidate_urls.insert(0, base_url.replace(":3030", ":3031"))
    
    email = hc_config.get("email", "")
    password = hc_config.get("password", "")
    
    # 优先尝试 HedgeDoc v2 API
    for target_url in candidate_urls:
        try:
            # 1. 获取 CSRF Token
            csrf_resp = s.get(f"{target_url}/api/private/csrf/token", timeout=5)
            if csrf_resp.status_code == 200:
                csrf_token = csrf_resp.json().get("token")
                if csrf_token:
                    headers = {
                        "csrf-token": csrf_token,
                        "Content-Type": "application/json"
                    }
                    # 2. 登录 v2
                    login_resp = s.post(
                        f"{target_url}/api/private/auth/local/login",
                        json={"username": email, "password": password},
                        headers=headers,
                        timeout=10
                    )
                    if login_resp.status_code in (200, 201):
                        # 3. 创建笔记 v2
                        note_headers = {
                            "csrf-token": csrf_token,
                            "Content-Type": "text/markdown"
                        }
                        note_resp = s.post(
                            f"{target_url}/api/private/notes",
                            data=markdown_text.encode("utf-8"),
                            headers=note_headers,
                            timeout=10
                        )
                        if note_resp.status_code in (200, 201):
                            note_data = note_resp.json()
                            primary_alias = note_data.get("metadata", {}).get("primaryAlias")
                            if primary_alias:
                                return f"{public_url}/{primary_alias}"
        except Exception:
            pass

    # 回退尝试 HedgeDoc v1 API
    headers = {'X-Forwarded-Proto': 'https'}
    for target_url in candidate_urls:
        try:
            login_url = f"{target_url}/login"
            r_login = s.post(
                login_url, 
                data={'email': email, 'password': password}, 
                headers=headers, 
                allow_redirects=False, 
                timeout=10
            )
            cookie_str = '; '.join([f'{k}={v}' for k, v in s.cookies.items()])
            headers['Cookie'] = cookie_str
            headers['Content-Type'] = 'text/markdown'
            new_url = f"{target_url}/new"
            r_new = s.post(
                new_url, 
                data=markdown_text.encode('utf-8'), 
                headers=headers, 
                allow_redirects=False, 
                timeout=10
            )
            location = r_new.headers.get('Location')
            if location:
                if location.startswith("http"):
                    parsed = urllib.parse.urlparse(location)
                    return public_url + parsed.path
                else:
                    return public_url + location
        except Exception:
            pass
            
    print("❌ [HedgeDoc Uploader] 上传失败: 未能完成 HedgeDoc 鉴权或笔记创建")
    return ""
