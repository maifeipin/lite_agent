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
    assert url == "https://md.example/abc123"
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
