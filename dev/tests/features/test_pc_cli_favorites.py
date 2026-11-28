"""PC Bots 收藏到知识库单测 (features.pc_cli.favorites)。"""

from __future__ import annotations

import os
import tempfile
import unittest

from lite_agent.features.pc_cli.favorites import PcCliFavoritesService, make_message_source_key
from lite_agent.features.pc_cli.repository import PcCliRepository


class _FakeMeiliClient:
    """内存模拟 Meilisearch personal_kb，验证实时同步、检索、删除与重建。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.docs = {}

    def upsert_document(self, doc):
        if self.fail:
            return False
        self.docs[doc["id"]] = dict(doc)
        return True

    def delete_document(self, doc_id):
        if self.fail:
            return False
        self.docs.pop(doc_id, None)
        return True

    def rebuild_documents(self, docs):
        if self.fail:
            return False
        self.docs = {d["id"]: dict(d) for d in docs}
        return True

    def search(self, query, *, bot_id=None, source_type=None, limit=30):
        if self.fail:
            return None
        q = query.lower()
        hits = []
        for d in self.docs.values():
            if bot_id and d.get("bot_id") != bot_id:
                continue
            if source_type and d.get("source_type") != source_type:
                continue
            hay = " ".join([d.get("title", ""), d.get("content", ""), d.get("note", ""), " ".join(d.get("tags", []))]).lower()
            if q in hay:
                hits.append({**d, "_formatted": {"content": d.get("content", "")[:80]}})
        return hits[:limit]


class TestPcCliFavorites(unittest.TestCase):
    def test_save_dedup_append_and_unfavorite_with_meili_sync(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "output_archive.db")
            meili = _FakeMeiliClient()
            svc = PcCliFavoritesService(db_path=db_path, meili_client=meili)

            # 1. 单条收藏（第 10 轮）
            item10 = {"turn_id": "t10", "seq_id": 100, "role": "assistant", "text": "根因是线程池 worker 阻塞退出"}
            ok, res1 = svc.save_favorite(
                bot_id="bot_1",
                bot_name="OpsBot",
                node_id="vps1",
                cwd="/app",
                title="线程池阻塞退出排障",
                content="根因是线程池 worker 阻塞退出",
                tags="排障, Python",
                items=[item10],
            )
            self.assertTrue(ok)
            self.assertFalse(res1["deduplicated"])
            self.assertTrue(res1["meili_synced"])
            fav_id = res1["favorite"]["id"]
            self.assertIn(fav_id, meili.docs)

            # 2. 重复收藏同一条（第 10 轮）必须幂等复用，不产生重复条目
            ok_dup, res_dup = svc.save_favorite(
                bot_id="bot_1",
                bot_name="OpsBot",
                content="根因是线程池 worker 阻塞退出",
                items=[item10],
            )
            self.assertTrue(ok_dup)
            self.assertTrue(res_dup["deduplicated"])
            self.assertEqual(res_dup["favorite"]["id"], fav_id)

            # 3. 第 12 轮增量追加到同一张卡片，并测试重复追加同一条被自动跳过
            item12_user = {"turn_id": "t12", "seq_id": 120, "role": "user", "text": "怎么彻底解决？"}
            item12_asst = {"turn_id": "t12", "seq_id": 121, "role": "assistant", "text": "在关闭阶段显式 shutdown(wait=False)"}
            ok_app, res_app = svc.save_favorite(
                bot_id="bot_1",
                content="**用户提问：**\n怎么彻底解决？\n\n在关闭阶段显式 shutdown(wait=False)",
                tags=["生命周期"],
                items=[item12_user, item12_asst],
                append_to_id=fav_id,
            )
            self.assertTrue(ok_app)
            self.assertTrue(res_app["appended"])
            self.assertIn("shutdown(wait=False)", res_app["favorite"]["content"])
            self.assertIn("生命周期", res_app["favorite"]["tags"])
            self.assertEqual(len(res_app["favorite"]["source_refs"]), 3)

            # 重复追加第 12 轮消息必须去重跳过
            ok_app2, res_app2 = svc.save_favorite(
                bot_id="bot_1",
                content="重复内容",
                items=[item12_asst],
                append_to_id=fav_id,
            )
            self.assertTrue(ok_app2)
            self.assertTrue(res_app2["deduplicated"])
            self.assertFalse(res_app2["appended"])

            # 已收藏 source_key 列表包含第 10 轮和第 12 轮
            keys = svc.list_collected_source_keys("bot_1")
            self.assertIn(make_message_source_key("bot_1", "t10", 100), keys)
            self.assertIn(make_message_source_key("bot_1", "t12", 121), keys)

            # 4. Meilisearch 关键词检索命中追加后的内容
            searched = svc.search_favorites(query="shutdown")
            self.assertEqual(searched["engine"], "meilisearch")
            self.assertEqual(len(searched["favorites"]), 1)
            self.assertEqual(searched["favorites"][0]["id"], fav_id)

            # 5. 取消收藏后，SQLite 与 Meilisearch 同步移除
            ok_del, res_del = svc.delete_favorite(fav_id)
            self.assertTrue(ok_del)
            self.assertTrue(res_del["meili_synced"])
            self.assertNotIn(fav_id, meili.docs)
            self.assertEqual(svc.search_favorites(query="shutdown")["favorites"], [])

    def test_favorite_survives_bot_hard_delete_and_supports_sqlite_fallback_and_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pc_db = os.path.join(tmp_dir, "pc_cli.db")
            archive_db = os.path.join(tmp_dir, "output_archive.db")
            repo = PcCliRepository(db_path=pc_db)
            try:
                bot = repo.create_bot(name="TempBot", node_id="mac", engine="codex", cwd="/tmp/demo")
                meili_down = _FakeMeiliClient(fail=True)
                svc = PcCliFavoritesService(db_path=archive_db, meili_client=meili_down)

                ok, res = svc.save_favorite(
                    bot_id=bot["bot_id"],
                    bot_name=bot["name"],
                    node_id=bot["node_id"],
                    cwd=bot["cwd"],
                    title="独立副本验证",
                    content="即便原 Bot 被硬删除，这条收藏和出处快照仍可读可搜",
                    items=[{"turn_id": "t1", "seq_id": 5, "role": "assistant", "text": "即便原 Bot 被硬删除"}],
                )
                self.assertTrue(ok)
                self.assertFalse(res["meili_synced"])

                # 硬删除原 Bot
                self.assertTrue(repo.delete_bot(bot["bot_id"]))
                self.assertIsNone(repo.get_bot(bot["bot_id"]))

                # Meilisearch 不可用时降级 SQLite 检索依然完好
                fallback = svc.search_favorites(query="硬删除")
                self.assertEqual(fallback["engine"], "sqlite")
                self.assertEqual(len(fallback["favorites"]), 1)
                self.assertEqual(fallback["favorites"][0]["bot_name"], "TempBot")
                self.assertEqual(fallback["favorites"][0]["cwd"], "/tmp/demo")

                # Meilisearch 恢复后可从 output_archive.db 完整重建
                meili_down.fail = False
                ok_rebuilt, rebuild_res = svc.rebuild_meili_index()
                self.assertTrue(ok_rebuilt)
                self.assertEqual(rebuild_res["rebuilt_count"], 1)
                recovered = svc.search_favorites(query="硬删除")
                self.assertEqual(recovered["engine"], "meilisearch")
                self.assertEqual(len(recovered["favorites"]), 1)
            finally:
                repo.close()


if __name__ == "__main__":
    unittest.main()
