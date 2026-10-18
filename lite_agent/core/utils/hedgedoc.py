# -*- coding: utf-8 -*-
import requests


def _restrict_note_to_owner(session, base_url: str, alias: str) -> bool:
    """把 v2 新笔记收紧为仅 owner 可见。

    v2 默认组权限取决于部署配置（HD_NOTE_PERMISSIONS_DEFAULT_* 可能允许
    _EVERYONE=read / _LOGGED_IN=write），直接返回链接可能把账单等内容
    暴露给非 owner。必须移除特殊组权限并关闭 explore 公开可见性；
    任何一步失败都视为收紧失败，调用方应删除笔记。在默认即 owner-only
    的部署上，本调用是幂等保险。
    """
    try:
        csrf = session.get(f"{base_url}/api/private/csrf/token", timeout=5).json().get("token")
        if not csrf:
            return False
        for group in ("_EVERYONE", "_LOGGED_IN"):
            # DELETE 不能带 application/json 空体（NestJS 400），只带 csrf 头
            resp = session.delete(
                f"{base_url}/api/private/notes/{alias}/metadata/permissions/groups/{group}",
                headers={"csrf-token": csrf}, timeout=10)
            if resp.status_code not in (200, 201, 204, 404):
                return False
        vis = session.put(
            f"{base_url}/api/private/notes/{alias}/metadata/permissions/visibility",
            json={"publiclyVisible": False},
            headers={"csrf-token": csrf, "Content-Type": "application/json"}, timeout=10)
        return vis.status_code in (200, 201, 204)
    except Exception:
        return False


def _delete_note(session, base_url: str, alias: str) -> bool:
    """尽力删除笔记（keepMedia=true 仅解除媒体关联），用于收紧权限失败后的止损。

    返回是否确认删除（204/200 成功，404 视为已不存在）；其余状态/异常返回 False，
    调用方必须如实上报残留风险，不得在未确认时宣称已删除。
    """
    try:
        csrf = session.get(f"{base_url}/api/private/csrf/token", timeout=5).json().get("token")
        resp = session.request(
            "DELETE", f"{base_url}/api/private/notes/{alias}",
            json={"keepMedia": True},
            headers={"csrf-token": csrf, "Content-Type": "application/json"},
            timeout=10)
        return resp.status_code in (200, 201, 204, 404)
    except Exception:
        return False


def upload_to_hedgedoc(markdown_text: str, hc_config: dict) -> str:
    """上传 Markdown 文本至 HedgeDoc 并返回笔记 URL

    仅支持 HedgeDoc v2（Fastify/NestJS 后端，含 CSRF token 与 /api/private/notes）。
    v2 上传后强制收紧为仅登录 owner 可见；v1 无权限收紧接口，已拒绝静默降级。

    :param markdown_text: 待上传的完整内容
    :param hc_config: 从 config.json 中读取的 "hedgedoc" 配置字典
    :return: 成功返回仅本人可见的笔记 URL，失败返回空字符串 ""
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
                                if not _restrict_note_to_owner(s, target_url, primary_alias):
                                    # 收紧失败 = 内容可能处于非 owner-only 状态，删除止损
                                    if _delete_note(s, target_url, primary_alias):
                                        print("❌ [HedgeDoc Uploader] 无法收紧为仅本人可见，已删除笔记并放弃上传")
                                    else:
                                        print(f"❌ [HedgeDoc Uploader] 无法收紧为仅本人可见，且删除未确认："
                                              f"笔记 {primary_alias} 可能残留在 {target_url}，需人工核查清理")
                                    return ""
                                return f"{public_url}/{primary_alias}"
        except Exception:
            pass

    # v1 回退已禁用：v1 没有权限收紧接口，/new 建出的笔记默认公开，
    # 与"仅登录账号可见"的上传契约冲突。v2 不可用/登录失败时如实报错，
    # 不得静默降级产出无法核验权限的链接。
    print("❌ [HedgeDoc Uploader] 上传失败: v2 鉴权或笔记创建未成功；"
          "v1 回退无法保证仅本人可见，已拒绝降级")
    return ""
