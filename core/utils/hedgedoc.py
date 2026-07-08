# -*- coding: utf-8 -*-
import requests
import urllib.parse

def upload_to_hedgedoc(markdown_text: str, hc_config: dict) -> str:
    """上传 Markdown 文本至 HedgeDoc 并返回公网可访问 URL
    
    :param markdown_text: 待上传的完整内容
    :param hc_config: 从 config.json 中读取的 "hedgedoc" 配置字典
    :return: 成功返回公网 URL，失败返回空字符串 ""
    """
    if not hc_config or not hc_config.get("enabled"):
        return ""
        
    s = requests.Session()
    headers = {'X-Forwarded-Proto': 'https'}
    
    try:
        # 1. 登录以换取 Cookie
        login_url = hc_config.get("internal_url", "http://127.0.0.1:3030").rstrip('/') + "/login"
        r_login = s.post(
            login_url, 
            data={'email': hc_config.get("email"), 'password': hc_config.get("password")}, 
            headers=headers, 
            allow_redirects=False, 
            timeout=10
        )
        
        cookie_str = '; '.join([f'{k}={v}' for k, v in s.cookies.items()])
        if not cookie_str:
            print("⚠️ [HedgeDoc Uploader] HedgeDoc 登录未返回 Cookie，可能是账号密码错误或服务端异常")
        
        # 2. 发送创建新笔记请求
        headers['Cookie'] = cookie_str
        headers['Content-Type'] = 'text/markdown'
        new_url = hc_config.get("internal_url", "http://127.0.0.1:3030").rstrip('/') + "/new"
        r_new = s.post(
            new_url, 
            data=markdown_text.encode('utf-8'), 
            headers=headers, 
            allow_redirects=False, 
            timeout=10
        )
        
        location = r_new.headers.get('Location')
        if location:
            public_url = hc_config.get("public_url", "https://md.maifeipin.com").rstrip('/')
            if location.startswith("http"):
                # 如果返回了完整的内部 URL，替换域名协议为公网 URL 的相应部分
                parsed = urllib.parse.urlparse(location)
                return public_url + parsed.path
            else:
                return public_url + location
    except Exception as e:
        print(f"❌ [HedgeDoc Uploader] 发生异常，上传失败: {e}")
        
    return ""
