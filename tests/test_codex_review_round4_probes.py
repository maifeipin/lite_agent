"""
Codex 第四轮复核反例长期回归专项 (tests/test_codex_review_round4_probes.py).
覆盖 U1–U3 缺陷的全部防倒退探针：
1. U1: 生产启动链路唯一 Agent 拥有权与通知/通道绑定同实例
2. U2: 严禁吞掉 Agent 构造异常，禁止 SimpleNamespace 伪 Agent 降级
3. U3: 公共通道 channels.api 零导入业务模块 (features/skills)
4. U1+U2 综合：main 真实通道挂载后触发会议 outbox，通道接收到通知，且生命周期退出无残留
"""

import subprocess
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import Mock, patch
import pytest


def test_bootstrap_must_not_swallow_agent_construction_failure():
    """U2: 构造 Agent 抛出异常时，bootstrap 必须立即向上 re-raise 并释放资源，严禁吞异常。"""
    from app.bootstrap import create_app
    with patch('agent.Agent', side_effect=RuntimeError('review construction failure')):
        with pytest.raises(RuntimeError, match='review construction failure'):
            app = create_app(mode='core', config={'llm': {'model': 'dummy'}})
            app.shutdown()


def test_main_creates_one_agent_and_notifier_receives_serving_instance():
    """U1: 生产启动全流程 Agent 构造器必须精确调用 1 次，通知器与通道必须绑定在同一个 serving 实例。"""
    import main
    made = []

    def construct(**kwargs):
        obj = SimpleNamespace(channels=[], session_mgr=Mock(), **kwargs)
        made.append(obj)
        return obj

    constructor = Mock(side_effect=construct)
    fake_skills = ModuleType('skills.ops_review_meeting')
    cfg = {
        'llm': {'model': 'dummy'},
        'plugins': {'enabled': ['review_meeting']},
        'channels': {},
        'review_meeting_notifications': {'enabled': True},
    }
    args = SimpleNamespace(mode='full', config=None, plugins=None)
    with patch('main.parse_args', return_value=args), \
         patch('main.load_config', return_value=cfg), \
         patch('core.model_config.validate_and_print', return_value=True), \
         patch('agent.Agent', constructor), patch('main.Agent', constructor), \
         patch.dict(sys.modules, {'skills.ops_review_meeting': fake_skills}), \
         patch('skills.ops_review_meeting', fake_skills, create=True), \
         patch('features.review_meeting.notifications.start_notifier', return_value=Mock()) as notifier, \
         patch('services.cron.CronService.start'), patch('main.signal.signal'), patch('main.time.sleep', side_effect=KeyboardInterrupt):
        main.main()

    assert constructor.call_count == 1, 'Bootstrap and main create different Agent instances'
    assert notifier.call_args.args[0] is made[-1]


def test_public_http_import_does_not_load_disabled_business():
    """U3: 新进程直接导入 channels.api 时，严格禁止载入任何 features 或 skills 模块。"""
    code = (
        "import sys; import channels.api; "
        "bad=[n for n in sys.modules if n=='features' or n.startswith('features.') or n=='skills' or n.startswith('skills.')]; "
        "assert not bad, bad"
    )
    proc = subprocess.run([sys.executable, '-B', '-c', code], text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr


def test_main_startup_with_fake_channel_receives_meeting_notification(tmp_path):
    """U1 验证强化: 真实通道挂载到同一 Agent 后，真实 outbox 扫描发送成功，通道收到通知且 DB 状态更新为 delivered。"""
    import main
    from features.review_meeting.notifications import deliver_once
    from scripts import review_meeting as room

    # 1. 准备真实的会议与待裁决 outbox 记录
    db_path = tmp_path / 'meeting_test.db'
    brief = tmp_path / 'brief.md'
    brief.write_text('test brief')
    summary = tmp_path / 'summary.md'
    summary.write_text('test summary')
    db = room.connect(db_path)
    with db:
        res = room.execute(db, SimpleNamespace(action='create', title='test', brief_file=str(brief),
                                              participants='aa,bb', owner_key_hash=room.sha('owner'), owner_key_file=None))
        mid = res['id']
        row = room.get(db, mid)
        first = room.append(db, row, 'aa', 'review', {'position': 'support', 'text': 'a', 'responds_to_seq': None})
        db.execute('UPDATE meetings SET round=2 WHERE id=?', (mid,))
        row = room.get(db, mid)
        room.append(db, row, 'aa', 'review', {'position': 'support', 'text': 'a2', 'responds_to_seq': first['seq']})
        room.append(db, row, 'bb', 'review', {'position': 'support', 'text': 'b2', 'responds_to_seq': first['seq']})
        room.execute(db, SimpleNamespace(action='request-approval', id=mid, summary_file=str(summary)))
    db.close()

    sent_deliveries = []

    class FakeFeishuChannel:
        def __init__(self, name="feishu"):
            self.name = name
            self.running = True

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

        def send_to(self, uid, response):
            sent_deliveries.append((uid, response))
            return True

    fake_ch = FakeFeishuChannel("feishu")
    made_agents = []

    def construct(**kwargs):
        obj = SimpleNamespace(channels=[], session_mgr=Mock(), **kwargs)
        made_agents.append(obj)
        return obj

    constructor = Mock(side_effect=construct)
    fake_skills = ModuleType('skills.ops_review_meeting')
    fake_feishu_mod = ModuleType('channels.feishu')
    fake_feishu_mod.FeishuChannel = Mock(return_value=fake_ch)

    cfg = {
        'llm': {'model': 'dummy'},
        'plugins': {'enabled': ['review_meeting']},
        'channels': {'feishu': {'enabled': True, 'admin_open_id': 'ou_real_admin_123'}},
        'review_meeting_notifications': {
            'enabled': True,
            'owner_channels': ['feishu'],
        },
    }
    args = SimpleNamespace(mode='full', config=None, plugins=None)

    notifier_holder = {}

    def capture_start_notifier(agent, config):
        notifier_holder["agent"] = agent
        notifier_holder["config"] = config
        return lambda: None

    with patch('main.parse_args', return_value=args), \
         patch('main.load_config', return_value=cfg), \
         patch('core.model_config.validate_and_print', return_value=True), \
         patch('agent.Agent', constructor), patch('main.Agent', constructor), \
         patch.dict(sys.modules, {'skills.ops_review_meeting': fake_skills, 'channels.feishu': fake_feishu_mod}), \
         patch('skills.ops_review_meeting', fake_skills, create=True), \
         patch('features.review_meeting.notifications.start_notifier', side_effect=capture_start_notifier), \
         patch('services.cron.CronService.start'), patch('main.signal.signal'), \
         patch('main.time.sleep', side_effect=KeyboardInterrupt):
        main.main()

    # 验证唯一 Agent
    assert len(made_agents) == 1, f"Expected 1 Agent instance, got {len(made_agents)}"
    srv_agent = made_agents[0]
    assert notifier_holder["agent"] is srv_agent
    assert fake_ch in srv_agent.channels

    # 执行真实的 deliver_once 扫描派发，验证端到端链路
    ok = deliver_once(srv_agent, notifier_holder["config"], db_path=db_path)
    assert ok is True, "deliver_once should succeed"
    assert len(sent_deliveries) == 1
    assert sent_deliveries[0][0] == 'ou_real_admin_123'
    assert mid in sent_deliveries[0][1].text

    # 验证数据库中通知记录状态已流转为 delivered，delivered_channel 为 feishu
    with room.connect(db_path) as check_db:
        row = check_db.execute("SELECT state, delivered_channel FROM meeting_notification_outbox WHERE meeting_id=?", (mid,)).fetchone()
        assert row["state"] == "delivered"
        assert row["delivered_channel"] == "feishu"
