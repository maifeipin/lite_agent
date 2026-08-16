"""Regression tests for the shared outbound notification priority."""

from core.alerts import _CHANNEL_PRIORITY, _resolve_admin_uid


def test_alert_priority_matches_channel_fallback_contract():
    assert _CHANNEL_PRIORITY == ['feishu', 'wecom', 'wechat', 'dingtalk', 'telegram']


def test_each_priority_channel_uses_its_own_admin_identifier():
    channels = {
        'feishu': {'admin_open_id': 'feishu-admin'},
        'wecom': {'admin_userid': 'wecom-admin'},
        'wechat': {'admin_wxid': 'wechat-admin'},
        'dingtalk': {'admin_staff_id': 'dingtalk-admin'},
        'telegram': {'admin_chat_id': 'telegram-admin'},
    }
    assert [_resolve_admin_uid(name, channels) for name in _CHANNEL_PRIORITY] == [
        'feishu-admin', 'wecom-admin', 'wechat-admin', 'dingtalk-admin', 'telegram-admin',
    ]
