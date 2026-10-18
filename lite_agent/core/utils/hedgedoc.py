# -*- coding: utf-8 -*-
import re
from urllib.parse import quote

import requests

# PostgreSQL 的 text 类型不接受 0x00：抓取来的 HTML 转 Markdown 时常混入 NUL
# 等控制字符，直接入库会让 HedgeDoc 后端 500
# （invalid byte sequence for encoding "UTF8": 0x00）。
# 上传前统一清洗，保留换行与制表符。
_CONTROL_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _note_url(public_url: str, alias: str) -> str:
    """拼接笔记的公网访问地址。

    HedgeDoc v2 的笔记路由是 /n/{alias}；根路径 /{alias} 只是 v1 直链的
    兼容入口（frontend/src/app/(editor)/[id]/page.tsx 里 getNote 后判
    metadata.version !== 1 即 notFound），直接给 /{alias} 会得到
    "Page not found"。因此统一走 /n/。
    """
    return f"{public_url.rstrip('/')}/n/{alias}"


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


def _share_note_readonly(session, base_url: str, alias: str) -> bool:
    """把【这一篇】笔记开放为匿名只读，返回是否成功。

    HedgeDoc v2 的创建接口没有任何 share/public 参数：
    backend/src/api/private/notes/notes.controller.ts 的 createNote 只接
    @MarkdownBody() 的 Markdown 正文（createNamedNote 多一个 alias），
    笔记的 publiclyVisible 与 _EVERYONE/_LOGGED_IN 权限全部取自服务端
    全局默认（note.config.ts / note.service.ts 的 createNote）。
    所以“单篇可分享”只能在创建后对这篇的 metadata 调权限接口：
      PUT /api/private/notes/{alias}/metadata/permissions/groups/_EVERYONE
          {"canEdit": false}          -> 匿名组只读
      PUT /api/private/notes/{alias}/metadata/permissions/visibility
          {"publiclyVisible": true}   -> 允许未登录访问
    两步都只作用于当前 alias，不影响其他笔记，也不改服务端默认。
    注意 HD_NOTE_PERMISSIONS_MAX_GUEST_LEVEL 必须 >= read，否则组权限
    设置会被后端拒绝。
    """
    try:
        csrf = session.get(f"{base_url}/api/private/csrf/token", timeout=5).json().get("token")
        if not csrf:
            return False
        group = session.put(
            f"{base_url}/api/private/notes/{alias}/metadata/permissions/groups/_EVERYONE",
            json={"canEdit": False},
            headers={"csrf-token": csrf, "Content-Type": "application/json"}, timeout=10)
        if group.status_code not in (200, 201):
            return False
        vis = session.put(
            f"{base_url}/api/private/notes/{alias}/metadata/permissions/visibility",
            json={"publiclyVisible": True},
            headers={"csrf-token": csrf, "Content-Type": "application/json"}, timeout=10)
        if vis.status_code not in (200, 201, 204):
            return False
        # 复核：确认这篇笔记的权限确实变成“匿名只读 + 公开可见”
        check = session.get(
            f"{base_url}/api/private/notes/{alias}/metadata/permissions", timeout=10)
        if check.status_code != 200:
            return False
        data = check.json()
        groups = {g.get("groupName"): g.get("canEdit") for g in data.get("sharedToGroups", [])}
        return data.get("publiclyVisible") is True and groups.get("_EVERYONE") is False
    except Exception:
        return False


def _share_note_with_user(session, base_url: str, alias: str, username: str,
                          can_edit: bool = False) -> bool:
    """把单篇笔记共享给某个注册用户（默认只读），返回是否成功。

    PUT /api/private/notes/{alias}/metadata/permissions/users/{username}
    需要调用者是 owner（FULL）。用于"上传者不是最终管理者"的场景：
    笔记由 liteagent 上传，但人用 bot2 登录查看。
    """
    try:
        csrf = session.get(f"{base_url}/api/private/csrf/token", timeout=5).json().get("token")
        if not csrf or not username:
            return False
        resp = session.put(
            f"{base_url}/api/private/notes/{alias}/metadata/permissions/users/{quote(username, safe='')}",
            json={"canEdit": bool(can_edit)},
            headers={"csrf-token": csrf, "Content-Type": "application/json"}, timeout=10)
        if resp.status_code not in (200, 201):
            return False
        data = resp.json() if resp.content else {}
        return any((u.get("username") or "").lower() == username.lower()
                   for u in (data.get("sharedToUsers") or []))
    except Exception:
        return False


def _transfer_note_owner(session, base_url: str, alias: str, username: str) -> bool:
    """把笔记所有者转给另一个注册用户，返回是否成功。

    HedgeDoc v2 里只有 owner 拿到 PermissionLevel.FULL，而"公开分享/权限设置"
    这些操作全部要求 FULL（notes.controller.ts 的 visibility / users / groups
    路由都是 @RequirePermission(PermissionLevel.FULL)）。也就是说：想让某人在
    网页上手动切换公开分享，他必须是 owner，光有读写共享是不够的。

    PUT /api/private/notes/{alias}/metadata/permissions/owner  {"owner": "..."}
    仅有只读/读写共享无法调用本接口，所以必须在创建者仍是 owner 时调用，
    且要放在所有权限设置（收紧/共享）之后。
    """
    try:
        csrf = session.get(f"{base_url}/api/private/csrf/token", timeout=5).json().get("token")
        if not csrf or not username:
            return False
        resp = session.put(
            f"{base_url}/api/private/notes/{alias}/metadata/permissions/owner",
            json={"owner": username},
            headers={"csrf-token": csrf, "Content-Type": "application/json"}, timeout=10)
        if resp.status_code not in (200, 201):
            return False
        data = resp.json() if resp.content else {}
        return (data.get("owner") or "").lower() == username.lower()
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


def upload_to_hedgedoc(markdown_text: str, hc_config: dict, public: bool = False,
                       owner: str = "", share_readers=None) -> str:
    """上传 Markdown 文本至 HedgeDoc 并返回笔记 URL

    仅支持 HedgeDoc v2（Fastify/NestJS 后端，含 CSRF token 与 /api/private/notes）。
    创建接口没有 share/public 参数，权限必须创建后按【单篇】设置：
      - public=False（默认）：收紧为仅 owner 可见，用于账单/邮件/文章存档；
        链接不再匿名可开，需要分享时由 owner 在网页里手动切换公开分享；
      - public=True：把这一篇开放为匿名只读（_EVERYONE=read + publiclyVisible）；
      - owner：创建后把所有者转给该账号。只有 owner 有 FULL 权限，才能在网页上
        改权限/切换公开分享；转交放在所有权限设置之后执行；
      - share_readers：额外共享只读的账号列表（上传者不是查看者的场景）。
    v1 无权限接口，已拒绝静默降级。

    :param markdown_text: 待上传的完整内容
    :param hc_config: 从 config.json 中读取的 "hedgedoc" 配置字典
    :param public: 是否为这一篇单独开放匿名只读
    :param owner: 上传后把笔记所有者转给谁（留空则用 hc_config["owner"]）
    :param share_readers: 额外共享只读的账号（留空则用 hc_config["share_readers"]）
    :return: 成功返回笔记 URL（/n/{alias}），失败返回空字符串 ""
    """
    if not hc_config or not hc_config.get("enabled"):
        return ""

    owner = owner or (hc_config.get("owner") or "")
    if share_readers is None:
        share_readers = hc_config.get("share_readers") or []

    markdown_text = _CONTROL_CHARS_RE.sub("", markdown_text or "")

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
                                if public:
                                    # 单篇开放匿名只读；失败则退回仅本人可见（不删笔记）
                                    if not _share_note_readonly(s, target_url, primary_alias):
                                        _restrict_note_to_owner(s, target_url, primary_alias)
                                        print("⚠️ [HedgeDoc Uploader] 本篇公开设置失败，"
                                              f"已退回仅本人可见：{_note_url(public_url, primary_alias)}")
                                elif not _restrict_note_to_owner(s, target_url, primary_alias):
                                    # 收紧失败 = 内容可能处于非 owner-only 状态，删除止损
                                    if _delete_note(s, target_url, primary_alias):
                                        print("❌ [HedgeDoc Uploader] 无法收紧为仅本人可见，已删除笔记并放弃上传")
                                    else:
                                        print(f"❌ [HedgeDoc Uploader] 无法收紧为仅本人可见，且删除未确认："
                                              f"笔记 {primary_alias} 可能残留在 {target_url}，需人工核查清理")
                                    return ""

                                # 额外共享只读（必须在转移 owner 之前，调用者还是 owner）
                                for reader in share_readers:
                                    if not _share_note_with_user(s, target_url, primary_alias, reader):
                                        print("⚠️ [HedgeDoc Uploader] 共享只读失败："
                                              f"{reader} @ {_note_url(public_url, primary_alias)}")

                                # 转移 owner 放最后：转完原账号就没有 FULL 权限了
                                if owner and not _transfer_note_owner(s, target_url, primary_alias, owner):
                                    if _share_note_with_user(s, target_url, primary_alias, owner):
                                        print(f"⚠️ [HedgeDoc Uploader] 所有者转给 {owner} 失败，"
                                              "已改为共享只读（该账号无法在界面切换公开分享）")
                                    else:
                                        print(f"⚠️ [HedgeDoc Uploader] 所有者转给 {owner} 失败，"
                                              "且共享只读也失败，仅上传账号可见")

                                return _note_url(public_url, primary_alias)
        except Exception:
            pass

    # v1 回退已禁用：v1 没有权限收紧接口，/new 建出的笔记默认公开，
    # 与"仅登录账号可见"的上传契约冲突。v2 不可用/登录失败时如实报错，
    # 不得静默降级产出无法核验权限的链接。
    print("❌ [HedgeDoc Uploader] 上传失败: v2 鉴权或笔记创建未成功；"
          "v1 回退无法保证仅本人可见，已拒绝降级")
    return ""
