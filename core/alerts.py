"""Edge 告警统一出口: 复用 agent.channels 推送到 IM, 带 dedup。

设计:
- IM 密钥只在中心 (agent.channels 已实例化), 不下发边缘。
- 永不 raise — 告警失败不得影响业务 (报告 ACK / skill 执行)。
- 去重复用 session_mgr.is_message_processed, 时间窗进 key (仿 wecom.py:132)。

依赖的 Agent 公开属性 (agent.py:114-155):
- agent.channels: list[BaseChannel], 由 main.py 注入
- agent.session_mgr: SessionManager, is_message_processed(key) 可用
(config 存在 self._config 私有属性, 不直接读; channels 配置走 load_config())
"""
from agent import AgentResponse
from core.config_loader import load_config

# 通道优先级: 谁启用谁先, 逐个 try 直到成功
_CHANNEL_PRIORITY = ['feishu', 'wecom', 'dingtalk', 'telegram']


def _resolve_admin_uid(ch_name: str, channels_cfg: dict) -> str:
    """按通道解析管理员 uid (镜像 main.py:62-68, 但修正每通道字段)。"""
    c = channels_cfg.get(ch_name, {})
    if ch_name == 'feishu':    return c.get('admin_open_id', '')
    if ch_name == 'wecom':     return c.get('admin_userid', '')
    if ch_name == 'telegram':  return c.get('admin_chat_id', '')
    if ch_name == 'dingtalk':  return c.get('admin_chat_id', '') or c.get('admin_open_id', '')
    return ''


def push_alert(agent, text: str, title: str = '🛡️ Edge 告警',
               color: str = 'red', dedup_key: str = None) -> bool:
    """推送告警到第一个可用的 IM 通道。返回是否推送成功。

    - dedup_key: 若提供, 先查 session_mgr 去重; 命中则跳过 (返回 True, 视为已处理)。
    - 任何异常吞掉, 只 print, 不 raise。
    """
    try:
        if not text or not text.strip():
            return False

        # 去重: 时间窗进 key 由调用方负责构造 (见 _evaluate_edge_alert)
        if dedup_key:
            if agent.session_mgr.is_message_processed(f"edge_alert:{dedup_key}"):
                return True  # 本窗口已推过

        channels_cfg = (load_config() or {}).get('channels', {})

        for ch_name in _CHANNEL_PRIORITY:
            ch = next((c for c in getattr(agent, 'channels', []) if getattr(c, 'name', '') == ch_name), None)
            if not ch or not hasattr(ch, 'send_to'):
                continue
            uid = _resolve_admin_uid(ch_name, channels_cfg)
            if not uid:
                continue
            try:
                ok = ch.send_to(uid, AgentResponse(text, title=title, color=color))
                if ok:
                    return True
            except Exception as e:
                print(f"  ⚠️ [alerts] {ch_name} 推送失败: {e}")
                continue
        print(f"  ⚠️ [alerts] 无可用通道推送告警: {text[:60]}")
        return False
    except Exception as e:
        print(f"  ⚠️ [alerts] push_alert 异常: {e}")
        return False
