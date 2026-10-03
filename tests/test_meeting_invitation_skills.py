import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from core.meeting_channel_commands import handle
from core.review_meeting_api import ReviewAPIError
from skills import ops_review_meeting as skills
from scripts import review_meeting as room


class ChannelInvitationTests(unittest.TestCase):
    def test_wechat_invite_feishu_claim_and_review(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'room.db'
            cfg = {'channels': {'wechat': {'admin_wxid': 'owner'},
                               'feishu': {'admin_open_id': 'ou_owner'},
                               'api': {'review_meeting_base_url': 'https://example.com'}}}
            def command(channel, text, guest=False):
                return handle(cfg, SimpleNamespace(text=text, channel=channel,
                    user_id='ou_owner' if channel == 'feishu' else 'owner', is_guest=guest, channel_payload={'chat_type': 'p2p'}), path)
            created = json.loads(command('wechat', '/meeting create Test | Review code'))
            mid = created['id']
            invite = json.loads(command('wechat', f'/meeting invite {mid} feishu-reviewer'))
            url = invite['invite_url']
            self.assertIn('拒绝', command('feishu', '/meeting join '+url, guest=True))
            with patch.dict(os.environ, {'REVIEW_MEETING_DB': str(path)}):
                with self.assertRaises(ValueError):
                    skills.ops_review_meeting_join(mid, 'feishu-reviewer')
                with self.assertRaises(ValueError):
                    skills.ops_review_meeting_join('000000000000', invite_url=url)
                joined = json.loads(skills.ops_review_meeting_join(mid, participant='wrong',
                    invite_url=f'[{url}]({url})'))
                self.assertEqual(joined['participant'], 'feishu-reviewer')
                self.assertNotIn('session_token', joined)
                with self.assertRaises(ReviewAPIError):
                    skills.ops_review_meeting_join(mid, invite_url=url)
                result = json.loads(skills.ops_review_meeting_submit(mid, joined['participant'],
                    'revise', 'A substantive review with evidence.'))
                self.assertGreater(result['event_seq'], 0)
            self.assertIn('参会：feishu-reviewer', command('wechat', f'/meeting status {mid}'))
            self.assertIn('feishu-reviewer：revise（事件', command('wechat', f'/meeting status {mid}'))
            with room.connect(path) as db:
                self.assertEqual(room.snapshot(db, mid)['missing'], [])
                self.assertTrue(room.check_chain(room.events(db, mid), mid)['valid'])
            invite2 = json.loads(command('wechat', f'/meeting invite {mid} another'))
            self.assertIn('已入会', command('feishu', '/meeting join '+invite2['invite_url']))
