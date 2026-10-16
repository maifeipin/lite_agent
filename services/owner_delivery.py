"""Deliver a response to the first configured owner channel with a receipt."""

import logging

logger = logging.getLogger(__name__)
CHANNEL_PRIORITY = ('feishu', 'wecom', 'wechat', 'dingtalk', 'telegram')


def resolve_admin_uid(name, channels_config):
    config = channels_config.get(name, {}) or {}
    fields = {
        'feishu': 'admin_open_id', 'wecom': 'admin_userid',
        'wechat': 'admin_wxid', 'telegram': 'admin_chat_id',
        'dingtalk': 'admin_staff_id',
    }
    value = config.get(fields.get(name, ''), '')
    if name == 'dingtalk':
        value = value or config.get('admin_userid', '')
    return value


def send_owner_response(channels, channels_config, response):
    for name in CHANNEL_PRIORITY:
        channel = next((c for c in channels if getattr(c, 'name', '') == name), None)
        recipient = resolve_admin_uid(name, channels_config)
        if channel is None or not recipient or not hasattr(channel, 'send_to'):
            continue
        try:
            if channel.send_to(recipient, response):
                return True
        except Exception as exc:
            logger.warning('Owner delivery failed on %s (%s)', name, type(exc).__name__)
    return False
