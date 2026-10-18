"""v2 笔记默认 _EVERYONE=read：上传必须收紧为仅 owner 可见，失败须删除止损。"""
from unittest.mock import MagicMock, patch

from lite_agent.core.utils.hedgedoc import upload_to_hedgedoc

CONFIG = {
    "enabled": True,
    "internal_url": "http://hedge.internal:3031",
    "public_url": "https://md.example",
    "email": "bot@example",
    "password": "secret",
}


def _resp(status, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    return r


def _session(create_status=201, delete_status=200, vis_status=200):
    session = MagicMock()
    session.get.return_value = _resp(200, {"token": "csrf"})
    session.post.side_effect = [
        _resp(200),  # login
        _resp(create_status, {"metadata": {"primaryAlias": "abc123"}}),  # create note
    ]
    session.delete.return_value = _resp(delete_status)
    session.put.return_value = _resp(vis_status)
    return session


def test_upload_restricts_note_to_owner_before_returning_url():
    session = _session()
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        url = upload_to_hedgedoc("# 账单", CONFIG)
    assert url == "https://md.example/n/abc123"
    deleted_groups = [c.args[0] for c in session.delete.call_args_list]
    assert any("_EVERYONE" in u for u in deleted_groups)
    assert any("_LOGGED_IN" in u for u in deleted_groups)
    vis = session.put.call_args
    assert "visibility" in vis.args[0]
    assert vis.kwargs["json"] == {"publiclyVisible": False}
    session.request.assert_not_called()


def test_upload_deletes_note_and_fails_when_restriction_fails():
    session = _session(delete_status=500)
    session.request.return_value = _resp(204)  # 清理删除成功
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        url = upload_to_hedgedoc("# 账单", CONFIG)
    assert url == ""
    cleanup = [c for c in session.request.call_args_list if c.args[0] == "DELETE"]
    assert cleanup, "收紧失败必须删除笔记止损"
    assert "abc123" in cleanup[0].args[1]


def test_cleanup_success_reports_deleted(capsys):
    session = _session(delete_status=500)
    session.request.return_value = _resp(204)
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        upload_to_hedgedoc("# 账单", CONFIG)
    assert "已删除笔记并放弃上传" in capsys.readouterr().out


def test_cleanup_unconfirmed_is_reported_as_residue(capsys):
    """R4: 删除未确认时不得宣称已删除，必须留下可人工核查的残留信息。"""
    session = _session(delete_status=500)
    session.request.return_value = _resp(503)
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        url = upload_to_hedgedoc("# 账单", CONFIG)
    assert url == ""
    out = capsys.readouterr().out
    assert "可能残留" in out and "abc123" in out
    assert "已删除笔记" not in out


def test_cleanup_exception_is_reported_as_residue(capsys):
    session = _session(delete_status=500)
    session.request.side_effect = TimeoutError("slow")
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        assert upload_to_hedgedoc("# 账单", CONFIG) == ""
    assert "可能残留" in capsys.readouterr().out


def test_v1_fallback_refused_when_v2_unavailable(capsys):
    """R3: v2 不可用时不得静默降级 v1 产出无法核验权限的公开链接。"""
    session = MagicMock()
    session.get.side_effect = ConnectionError("down")
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        url = upload_to_hedgedoc("# 账单", CONFIG)
    assert url == ""
    assert not any("/new" in str(c) for c in session.post.call_args_list)
    assert "拒绝降级" in capsys.readouterr().out


def _public_session(verify_public=True):
    """public=True 分支：put 全部成功，回读的权限 DTO 可配置。"""
    session = MagicMock()

    def _get(url, **kwargs):
        if "csrf" in url:
            return _resp(200, {"token": "csrf"})
        return _resp(200, {
            "publiclyVisible": verify_public,
            "sharedToGroups": [{"groupName": "_EVERYONE", "canEdit": False}],
        })

    session.get.side_effect = _get
    session.post.side_effect = [
        _resp(200),  # login
        _resp(201, {"metadata": {"primaryAlias": "abc123"}}),  # create note
    ]
    session.delete.return_value = _resp(200)
    session.put.return_value = _resp(200, {})
    return session


def test_public_upload_opens_only_this_note_for_anonymous_read():
    """public=True：给本篇 _EVERYONE=read + publiclyVisible，返回 /n/ 链接。"""
    session = _public_session()
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        url = upload_to_hedgedoc("# 文章", CONFIG, public=True)
    assert url == "https://md.example/n/abc123"
    puts = [c.args[0] for c in session.put.call_args_list]
    assert any("_EVERYONE" in u for u in puts)
    assert any("visibility" in u for u in puts)
    # 公开分支不得再删除组权限（否则链接会退化成仅本人可见）
    session.delete.assert_not_called()
    session.request.assert_not_called()


def test_public_failure_falls_back_to_owner_only(capsys):
    """设置公开失败时退回 owner-only，并如实提示，不删笔记。"""
    session = _public_session(verify_public=False)
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        url = upload_to_hedgedoc("# 文章", CONFIG, public=True)
    assert url == "https://md.example/n/abc123"
    assert session.delete.called, "退回 owner-only 需要清掉已加的组权限"
    assert "本篇公开设置失败" in capsys.readouterr().out
    session.request.assert_not_called()


def test_nul_control_chars_are_stripped_before_upload():
    """HedgeDoc 后端 PostgreSQL 不接受 0x00，上传前必须清掉控制字符。"""
    session = _public_session()
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        upload_to_hedgedoc("a\x00b", CONFIG)
    create_call = [c for c in session.post.call_args_list if c.args[0].endswith("/notes")][0]
    assert create_call.kwargs["data"] == b"ab"


def _owner_session(owner_ok=True, share_ok=True):
    """私有上传 + owner 转移：put 按 URL 分流，能配置失败。"""
    from urllib.parse import unquote

    session = MagicMock()

    def _get(url, **kwargs):
        if "csrf" in url:
            return _resp(200, {"token": "csrf"})
        return _resp(200, {"publiclyVisible": False, "sharedToGroups": []})

    def _put(url, **kwargs):
        if url.endswith("/permissions/owner"):
            if not owner_ok:
                return _resp(500, {})
            return _resp(200, {"owner": (kwargs.get("json") or {}).get("owner")})
        if "/permissions/users/" in url:
            if not share_ok:
                return _resp(500, {})
            username = unquote(url.rsplit("/", 1)[-1])
            return _resp(200, {"sharedToUsers": [{"username": username, "canEdit": False}]})
        return _resp(200, {"publiclyVisible": False})

    session.get.side_effect = _get
    session.put.side_effect = _put
    session.post.side_effect = [
        _resp(200),  # login
        _resp(201, {"metadata": {"primaryAlias": "abc123"}}),  # create note
    ]
    session.delete.return_value = _resp(200)
    return session


def test_owner_is_transferred_after_note_is_locked_down():
    """配置 hedgedoc.owner 时：先收紧/共享，最后才转 owner（转完就没 FULL 了）。"""
    session = _owner_session()
    cfg = dict(CONFIG, owner="bot2@bot.com")
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        url = upload_to_hedgedoc("# 文章", cfg)
    assert url == "https://md.example/n/abc123"
    puts = [c.args[0] for c in session.put.call_args_list]
    assert puts[-1].endswith("/permissions/owner")
    assert session.put.call_args.kwargs["json"] == {"owner": "bot2@bot.com"}
    vis_idx = next(i for i, u in enumerate(puts) if "visibility" in u)
    assert vis_idx < len(puts) - 1, "必须先把笔记设成私有，再转 owner"
    # 私有分支仍要清掉 _EVERYONE/_LOGGED_IN
    deleted = [c.args[0] for c in session.delete.call_args_list]
    assert any("_EVERYONE" in u for u in deleted)


def test_owner_transfer_failure_falls_back_to_readonly_share(capsys):
    """转 owner 失败时退化为共享只读，并说明该账号无法在界面切换公开。"""
    session = _owner_session(owner_ok=False)
    cfg = dict(CONFIG, owner="bot2@bot.com")
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        url = upload_to_hedgedoc("# 文章", cfg)
    assert url == "https://md.example/n/abc123"
    assert any("/permissions/users/" in c.args[0] for c in session.put.call_args_list)
    assert "无法在界面切换公开分享" in capsys.readouterr().out


def test_share_readers_granted_before_owner_transfer():
    """额外只读账号必须在转 owner 之前授权（之后原账号已无 FULL）。"""
    session = _owner_session()
    cfg = dict(CONFIG, owner="bot2@bot.com", share_readers=["reader@example.com"])
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        upload_to_hedgedoc("# 文章", cfg)
    puts = [c.args[0] for c in session.put.call_args_list]
    reader_idx = next(i for i, u in enumerate(puts) if "/permissions/users/" in u)
    owner_idx = next(i for i, u in enumerate(puts) if u.endswith("/permissions/owner"))
    assert reader_idx < owner_idx


def test_upload_without_owner_config_keeps_ownership():
    """没配 owner 时不做转移，保持原行为（仅收紧为 owner-only）。"""
    session = _owner_session()
    with patch("lite_agent.core.utils.hedgedoc.requests") as rq:
        rq.Session.return_value = session
        upload_to_hedgedoc("# 账单", CONFIG)
    assert not any(c.args[0].endswith("/permissions/owner") for c in session.put.call_args_list)
