import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.request_selector import DOMAIN_MAP, RequestSelector
from core.skill_engine import SkillEngine, _skill_registry


def entry(name, *, guest=False, tags=()):
    return {
        "schema": {"function": {"name": name, "description": f"{name} description"}},
        "policy": SimpleNamespace(guest_ok=guest),
        "tags": list(tags),
    }


class StubEngine:
    def __init__(self):
        self.names = {name for cfg in DOMAIN_MAP.values()
                      for name in cfg["default_tools"] + cfg["explicit_intent_tools"]}

    def get_all_names(self):
        return self.names

    def get_guest_schemas(self):
        return []


class ReviewToolCatalogueTests(unittest.TestCase):
    def test_catalogue_is_live_filtered_and_paginated(self):
        data = {
            "ops_review_meeting_show": entry("ops_review_meeting_show", tags=("review",)),
            "ops_review_meeting_submit": entry("ops_review_meeting_submit", tags=("review",)),
            "public_status": entry("public_status", guest=True),
        }
        engine = SkillEngine.__new__(SkillEngine)
        with patch.dict(_skill_registry, data, clear=True):
            first = engine.list_skills_page(query="review", page=1, page_size=1)
            second = engine.list_skills_page(query="review", page=2, page_size=1)
            guest = engine.list_skills_page(is_guest=True)
            self.assertIn("第 1/2 页", first)
            self.assertIn("ops_review_meeting_show", first)
            self.assertIn("ops_review_meeting_submit", second)
            self.assertIn("public_status", guest)
            self.assertNotIn("ops_review_meeting", guest)

    def test_selector_distinguishes_review_room_and_model_scoring(self):
        selector = RequestSelector(StubEngine())
        read = selector.select("查看会审室状态")
        self.assertEqual(read.names, ["ops_review_meeting_status", "ops_review_meeting_show"])
        self.assertTrue(read.read_only_mode)
        write = selector.select("在会审室提交正式意见")
        self.assertIn("ops_review_meeting_submit", write.names)
        self.assertNotIn("ops_review_meeting_join", write.names)
        self.assertNotIn("ops_decision", write.names)
        self.assertFalse(write.read_only_mode)
        self.assertEqual(selector.select("加入会审室", is_guest=True).names, [])
        self.assertEqual(selector.select("请用评判委员会评估方案").names, ["ops_decision"])

    def test_invitation_exposes_join_without_granting_approval(self):
        selector = RequestSelector(StubEngine())
        selected = selector.select('会议操作 https://example.com/agent/api/v1/review-meetings/0123456789ab/invite#invite=token')
        self.assertIn('ops_review_meeting_join', selected.names)
        self.assertFalse(selected.read_only_mode)
        self.assertNotIn('ops_review_meeting_submit', selected.names)
        self.assertEqual(selector.select('评审会议 #invite=token', is_guest=True).names, [])


if __name__ == "__main__":
    unittest.main()
