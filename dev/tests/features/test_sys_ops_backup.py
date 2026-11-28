"""Sys Ops 备份与云同步单测 (features.sys_ops.backup_service)。"""
import json
import os
import sqlite3
import tempfile
import unittest
import subprocess
from datetime import datetime as real_datetime
from unittest.mock import patch, MagicMock
from lite_agent.features.sys_ops.backup_service import (
    _bdpan_sync_dir,
    _create_meili_dump,
    _clean_meili_dumps_after_archive,
    _is_excluded_core_entry,
    _snapshot_sqlite_db,
    _stage_core_backup_tree,
    _write_upload_status,
    do_backup,
    do_backup_and_sync,
)


class TestSysOpsBackup(unittest.TestCase):
    def test_bdpan_sync_dir_retries_on_failure(self):
        """测试单个文件上传在遇到网络抖动失败时具备重试机制并在重试中成功"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fpath = os.path.join(tmp_dir, "data_test.zip")
            with open(fpath, "w") as f:
                f.write("content")

            upload_calls = 0
            def fake_subprocess_run(cmd, **kwargs):
                nonlocal upload_calls
                mock_res = MagicMock()
                if "mkdir" in cmd:
                    mock_res.returncode = 0
                    return mock_res
                if "upload" in cmd:
                    upload_calls += 1
                    # upload 第一次失败（模拟504网关超时），第二次重试成功
                    if upload_calls == 1:
                        mock_res.returncode = 1
                        mock_res.stderr = "HTTP 504 Gateway Timeout"
                        mock_res.stdout = ""
                    else:
                        mock_res.returncode = 0
                        mock_res.stderr = ""
                        mock_res.stdout = ""
                return mock_res

            with patch("subprocess.run", side_effect=fake_subprocess_run), \
                 patch("time.sleep", return_value=None), \
                 patch("shutil.which", return_value="/fake/bdpan"), \
                 patch("lite_agent.features.sys_ops.backup_service._BDPAN_SYNC_LOG", os.path.join(tmp_dir, "bdpan.log")):
                success, detail = _bdpan_sync_dir(tmp_dir)
                self.assertTrue(success)
                self.assertIn("上传 1 个", detail)
                # 包含 1 次失败 + 1 次重试成功，共 2 次 upload 调用
                self.assertEqual(upload_calls, 2)

    def test_create_meili_dump_locates_by_dump_uid_even_if_newer_dump_exists(self):
        """测试按 details.dumpUid 精准定位，即使目录中存在更新的无关/手工 dump 也不误选、不误删"""
        with tempfile.TemporaryDirectory() as tmp_dumps_dir:
            my_dump = os.path.join(tmp_dumps_dir, "20261109-100000000.dump")
            newer_manual = os.path.join(tmp_dumps_dir, "manual_backup.dump")
            newer_other = os.path.join(tmp_dumps_dir, "20261109-120000000.dump")
            for p in (my_dump, newer_manual, newer_other):
                with open(p, "w") as f:
                    f.write("content")

            mock_resp_task = MagicMock()
            mock_resp_task.read.return_value = b'{"status": "succeeded", "details": {"dumpUid": "20261109-100000000"}}'
            mock_resp_task.__enter__.return_value = mock_resp_task

            mock_resp_trigger = MagicMock()
            mock_resp_trigger.read.return_value = b'{"taskUid": 123}'
            mock_resp_trigger.__enter__.return_value = mock_resp_trigger

            def fake_urlopen(req, **kwargs):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "/dumps" in url:
                    return mock_resp_trigger
                return mock_resp_task

            orig_exists = os.path.exists
            orig_isfile = os.path.isfile
            def fake_exists(p):
                if p == "/home/liteagent/meilisearch/meili_data/dumps":
                    return True
                return orig_exists(p)

            def fake_isfile(p):
                if p == "/home/liteagent/meilisearch/meili_data/dumps/20261109-100000000.dump":
                    return True
                return orig_isfile(p)

            with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("time.sleep", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service.load_config", return_value={"meilisearch": {"url": "http://127.0.0.1:7700", "master_key": "k"}}), \
                 patch("lite_agent.features.sys_ops.backup_service.os.path.exists", side_effect=fake_exists), \
                 patch("lite_agent.features.sys_ops.backup_service.os.path.isfile", side_effect=fake_isfile):
                with patch("os.remove") as mock_remove:
                    result = _create_meili_dump()
                    self.assertEqual(result, f"/home/liteagent/meilisearch/meili_data/dumps/20261109-100000000.dump")
                    # 创建阶段绝对不提前删除任何 dump
                    mock_remove.assert_not_called()

    def test_create_meili_dump_returns_none_when_dump_uid_missing_or_file_absent(self):
        """测试当 dumpUid 缺失或文件未生成时，坚决不使用 mtime 兜底选择任意最新 dump，直接返回 None 保留现场"""
        with tempfile.TemporaryDirectory() as tmp_dumps_dir:
            existing_dump = os.path.join(tmp_dumps_dir, "existing_manual.dump")
            with open(existing_dump, "w") as f:
                f.write("manual dump")

            # 场景 A: details.dumpUid 缺失
            mock_resp_task_no_uid = MagicMock()
            mock_resp_task_no_uid.read.return_value = b'{"status": "succeeded", "details": {}}'
            mock_resp_task_no_uid.__enter__.return_value = mock_resp_task_no_uid

            mock_resp_trigger = MagicMock()
            mock_resp_trigger.read.return_value = b'{"taskUid": 123}'
            mock_resp_trigger.__enter__.return_value = mock_resp_trigger

            def fake_urlopen_no_uid(req, **kwargs):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "/dumps" in url:
                    return mock_resp_trigger
                return mock_resp_task_no_uid

            orig_exists = os.path.exists
            with patch("urllib.request.urlopen", side_effect=fake_urlopen_no_uid), \
                 patch("time.sleep", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service.load_config", return_value={"meilisearch": {"url": "http://127.0.0.1:7700", "master_key": "k"}}), \
                 patch("lite_agent.features.sys_ops.backup_service.os.path.exists", side_effect=lambda p: True if p == "/home/liteagent/meilisearch/meili_data/dumps" else orig_exists(p)):
                result = _create_meili_dump()
                # 缺失 dumpUid 时绝不猜选 existing_manual.dump，必须返回 None
                self.assertIsNone(result)

            # 场景 B: details.dumpUid 存在但对应文件未找到
            mock_resp_task_file_missing = MagicMock()
            mock_resp_task_file_missing.read.return_value = b'{"status": "succeeded", "details": {"dumpUid": "missing-uuid"}}'
            mock_resp_task_file_missing.__enter__.return_value = mock_resp_task_file_missing

            def fake_urlopen_file_missing(req, **kwargs):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                if "/dumps" in url:
                    return mock_resp_trigger
                return mock_resp_task_file_missing

            with patch("urllib.request.urlopen", side_effect=fake_urlopen_file_missing), \
                 patch("time.sleep", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service.load_config", return_value={"meilisearch": {"url": "http://127.0.0.1:7700", "master_key": "k"}}), \
                 patch("lite_agent.features.sys_ops.backup_service.os.path.exists", side_effect=lambda p: True if p == "/home/liteagent/meilisearch/meili_data/dumps" else orig_exists(p)), \
                 patch("lite_agent.features.sys_ops.backup_service.os.path.isfile", return_value=False):
                result = _create_meili_dump()
                # 文件未找到时绝不猜选，必须返回 None
                self.assertIsNone(result)

    def test_clean_meili_dumps_after_archive_only_deletes_current_dump(self):
        """测试归档后清理：仅清理确认由本服务成功归档的当前文件，绝对不触碰其他任何文件"""
        with tempfile.TemporaryDirectory() as tmp_dumps_dir:
            cur_dump = os.path.join(tmp_dumps_dir, "20261109-100000000.dump")
            manual_dump = os.path.join(tmp_dumps_dir, "manual_backup.dump")
            other_api_dump = os.path.join(tmp_dumps_dir, "20261101-030000000.dump")

            for p in (cur_dump, manual_dump, other_api_dump):
                with open(p, "w") as f:
                    f.write("dump")

            _clean_meili_dumps_after_archive(cur_dump)

            # 1. 只有当前已归档 dump 被删除
            self.assertFalse(os.path.exists(cur_dump))
            # 2. 其它任何 dump 坚决不被误删
            self.assertTrue(os.path.exists(manual_dump))
            self.assertTrue(os.path.exists(other_api_dump))

    def test_do_backup_preserves_dump_when_zip_fails(self):
        """测试 ZIP 归档失败时仍保留恢复文件，清单标记失败，不调用清理"""
        with tempfile.TemporaryDirectory() as tmp_backup_dir, \
             tempfile.TemporaryDirectory() as tmp_data_dir:

            fake_dump_file = os.path.join(tmp_data_dir, "20261109-100000000.dump")
            with open(fake_dump_file, "w") as f:
                f.write("raw dump data for recovery")

            def fake_subprocess_run(cmd, **kwargs):
                # 模拟 zip 命令抛出异常，如磁盘满或被杀死
                if "zip" in cmd and "meilisearch" in str(cmd):
                    raise subprocess.CalledProcessError(1, cmd, stderr="Zip failed: disk full")
                mock_res = MagicMock()
                mock_res.returncode = 0
                return mock_res

            with patch("lite_agent.features.sys_ops.backup_service._create_meili_dump", return_value=fake_dump_file), \
                 patch("lite_agent.features.sys_ops.backup_service._clean_meili_dumps_after_archive") as mock_clean, \
                 patch("lite_agent.features.sys_ops.backup_service.subprocess.run", side_effect=fake_subprocess_run), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_vaultwarden", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_mongodb", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_halo", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_hedgedoc", return_value=[]), \
                 patch("lite_agent.features.sys_ops.backup_service._BASE_DIR", tmp_backup_dir):

                res = do_backup()
                # 清单不能被标记为 OK
                self.assertNotIn("meilisearch: ✅ OK", res)
                # 原始 dump 文件必须完整保留以供灾难恢复
                self.assertTrue(os.path.exists(fake_dump_file))
                # 归档后清理逻辑绝对不能被调用
                mock_clean.assert_not_called()

    def test_snapshot_sqlite_db_merges_wal_and_passes_quick_check(self):
        """WAL 模式库经在线快照后应合并为可独立恢复的单文件 .db 并通过 quick_check"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            src_db = os.path.join(tmp_dir, "pc_cli.db")
            dst_db = os.path.join(tmp_dir, "staged", "pc_cli.db")

            conn = sqlite3.connect(src_db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
            conn.execute("INSERT INTO items (val) VALUES ('wal_committed_row')")
            conn.commit()
            # 故意保持连接打开使 WAL 尚未自动 checkpoint，模拟线上热库
            try:
                res = _snapshot_sqlite_db(src_db, dst_db, busy_timeout_ms=5000, budget_sec=30.0, max_attempts=2)
            finally:
                conn.close()

            self.assertEqual(res["status"], "verified")
            self.assertIsNotNone(res["snapshot_time"])
            self.assertGreater(res["size_bytes"], 0)
            self.assertTrue(os.path.isfile(dst_db))
            self.assertFalse(os.path.exists(dst_db + "-wal"))
            self.assertFalse(os.path.exists(dst_db + "-shm"))

            # 在隔离副本上独立打开验证数据完整
            check_conn = sqlite3.connect(dst_db)
            rows = check_conn.execute("SELECT val FROM items").fetchall()
            check_conn.close()
            self.assertEqual(rows, [("wal_committed_row",)])

    def test_snapshot_sqlite_db_fails_without_hot_copy_on_corrupt_or_budget_exceeded(self):
        """损坏库或超过总预算时必须失败且不残留半成品文件、不降级热拷贝"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_db = os.path.join(tmp_dir, "corrupt.db")
            with open(bad_db, "wb") as f:
                f.write(b"not a valid sqlite database header" * 20)
            dst_bad = os.path.join(tmp_dir, "staged", "corrupt.db")
            res_bad = _snapshot_sqlite_db(bad_db, dst_bad, budget_sec=5.0, max_attempts=2)
            self.assertEqual(res_bad["status"], "failed")
            self.assertFalse(os.path.exists(dst_bad))

            # 模拟单库总预算耗尽（monotonic 跨过 deadline）
            ok_db = os.path.join(tmp_dir, "ok.db")
            c = sqlite3.connect(ok_db)
            c.execute("CREATE TABLE t (x TEXT)")
            c.execute("INSERT INTO t VALUES ('hello')")
            c.commit()
            c.close()
            dst_timeout = os.path.join(tmp_dir, "staged", "ok.db")
            ticks = iter([100.0, 100.0, 200.0, 200.0, 200.0])
            with patch("lite_agent.features.sys_ops.backup_service.time.monotonic", side_effect=lambda: next(ticks, 200.0)):
                res_to = _snapshot_sqlite_db(ok_db, dst_timeout, budget_sec=10.0, max_attempts=2)
            self.assertEqual(res_to["status"], "failed")
            self.assertIn("budget", res_to["error"])
            self.assertFalse(os.path.exists(dst_timeout))

    def test_stage_core_backup_tree_excludes_sidecars_preserves_pre_and_tracks_degraded(self):
        """暂存树排除 WAL/SHM/journal/lock，保留 .pre-*，单库失败时继续备份其他库并记录上次已验证时间"""
        with tempfile.TemporaryDirectory() as tmp_root:
            data_dir = os.path.join(tmp_root, "data")
            billing_dir = os.path.join(tmp_root, "billing")
            tree_dir = os.path.join(tmp_root, "staged")
            os.makedirs(data_dir, exist_ok=True)
            os.makedirs(billing_dir, exist_ok=True)
            os.makedirs(tree_dir, exist_ok=True)

            # 健康库(WAL) + WAL/SHM 伴随文件 + 孤立 journal + 锁文件 + 0 字节空库 + .pre-* 迁移备份 + 损坏库
            good_db = os.path.join(data_dir, "pc_cli.db")
            c = sqlite3.connect(good_db)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE t (id INTEGER)")
            c.execute("INSERT INTO t VALUES (1)")
            c.commit()
            c.close()

            for excluded in ("orphan.db-wal", "orphan.db-shm", "orphan.db-journal", "todos.db.schema.lock"):
                with open(os.path.join(data_dir, excluded), "wb") as f:
                    f.write(b"runtime-sidecar")
            with open(os.path.join(data_dir, "agent.db"), "wb"):
                pass
            with open(os.path.join(data_dir, "todos.db.pre-e1-20261012"), "wb") as f:
                f.write(b"pre-migration-copy")
            with open(os.path.join(data_dir, "broken.db"), "wb") as f:
                f.write(b"corrupt-bytes" * 30)

            self.assertTrue(_is_excluded_core_entry("pc_cli.db-wal"))
            self.assertTrue(_is_excluded_core_entry("personal.md.lock"))
            self.assertFalse(_is_excluded_core_entry("todos.db.pre-e1-20261012"))

            prev = {"data/broken.db": {"status": "verified", "last_verified_at": "2026-11-27 03:00:00"}}
            has_content, snapshots, failed_dbs, non_db = _stage_core_backup_tree(
                base_dir=tmp_root,
                billing_dir=billing_dir,
                tree_dir=tree_dir,
                prev_sqlite_snapshots=prev,
            )

            self.assertTrue(has_content)
            self.assertEqual(snapshots["data/pc_cli.db"]["status"], "verified")
            self.assertEqual(snapshots["data/agent.db"]["status"], "skipped_empty")
            self.assertEqual(snapshots["data/broken.db"]["status"], "failed")
            self.assertEqual(snapshots["data/broken.db"]["last_verified_at"], "2026-11-27 03:00:00")
            self.assertEqual(failed_dbs, ["data/broken.db"])
            self.assertIn("data/todos.db.pre-e1-20261012", non_db)
            self.assertTrue(os.path.isfile(os.path.join(tree_dir, "data", "pc_cli.db")))
            self.assertFalse(os.path.exists(os.path.join(tree_dir, "data", "broken.db")))
            for excluded in ("orphan.db-wal", "orphan.db-shm", "orphan.db-journal", "todos.db.schema.lock"):
                self.assertFalse(os.path.exists(os.path.join(tree_dir, "data", excluded)))

    def test_do_backup_hedgedoc_compose_only_not_ok_and_splits_db_uploads_status(self):
        """HedgeDoc 仅拿到 docker-compose.yml 不得标 OK；db 与 uploads 分别记录状态与上次成功时间"""
        with tempfile.TemporaryDirectory() as tmp_root:
            data_dir = os.path.join(tmp_root, "data")
            os.makedirs(data_dir, exist_ok=True)
            status_file = os.path.join(data_dir, "backup_status.json")
            with open(status_file, "w", encoding="utf-8") as sf:
                json.dump(
                    {"hedgedoc_status": {"db": {"status": "OK", "last_ok_time": "2026-11-27 03:00:00"},
                                         "uploads": {"status": "OK", "last_ok_time": "2026-11-23 03:00:00"}}},
                    sf,
                )

            hd_tmp = os.path.join(tmp_root, "hedgedoc_backup_case1")
            os.makedirs(hd_tmp, exist_ok=True)
            compose_only = os.path.join(hd_tmp, "docker-compose.yml")
            with open(compose_only, "w", encoding="utf-8") as f:
                f.write("version: '3'\n")

            def fake_run(cmd, **kwargs):
                if isinstance(cmd, list) and cmd and cmd[0] == "zip":
                    zip_out = cmd[2]
                    with open(zip_out, "wb") as zf:
                        zf.write(b"PK\x05\x06" + b"\x00" * 18)
                res = MagicMock()
                res.returncode = 0
                res.stdout = ""
                return res

            # Case 1: 仅返回 docker-compose.yml（无 hedgedoc_db.sql）-> 严禁标 OK
            with patch("lite_agent.features.sys_ops.backup_service._BASE_DIR", tmp_root), \
                 patch("lite_agent.features.sys_ops.backup_service._BILLING_DIR", os.path.join(tmp_root, "no_billing")), \
                 patch("lite_agent.features.sys_ops.backup_service.subprocess.run", side_effect=fake_run), \
                 patch("lite_agent.features.sys_ops.backup_service._create_meili_dump", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_vaultwarden", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_mongodb", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_halo", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_hedgedoc", return_value=[compose_only]):
                report1 = do_backup()

            self.assertNotIn("hedgedoc: ✅ OK", report1)
            self.assertIn("hedgedoc_db: ⚠️ Failed", report1)
            saved1 = json.loads(open(status_file, "r", encoding="utf-8").read())
            self.assertTrue(saved1["backup_checklist"]["hedgedoc_db"].startswith("Failed"))
            self.assertEqual(saved1["hedgedoc_status"]["db"]["last_ok_time"], "2026-11-27 03:00:00")
            self.assertEqual(saved1["hedgedoc_status"]["uploads"]["last_ok_time"], "2026-11-23 03:00:00")

            # Case 2: 非周一且成功拿到非空 hedgedoc_db.sql -> hedgedoc_db OK，hedgedoc_uploads Skipped 并保留周一时间
            hd_tmp2 = os.path.join(tmp_root, "hedgedoc_backup_case2")
            os.makedirs(hd_tmp2, exist_ok=True)
            db_sql = os.path.join(hd_tmp2, "hedgedoc_db.sql")
            with open(db_sql, "w", encoding="utf-8") as f:
                f.write("-- pg_dump content\n")

            class FakeTuesday(real_datetime):
                @classmethod
                def today(cls):
                    return cls(2026, 11, 24, 3, 0, 0)  # Tuesday

            with patch("lite_agent.features.sys_ops.backup_service._BASE_DIR", tmp_root), \
                 patch("lite_agent.features.sys_ops.backup_service._BILLING_DIR", os.path.join(tmp_root, "no_billing")), \
                 patch("lite_agent.features.sys_ops.backup_service.subprocess.run", side_effect=fake_run), \
                 patch("lite_agent.features.sys_ops.backup_service.datetime", FakeTuesday), \
                 patch("lite_agent.features.sys_ops.backup_service._create_meili_dump", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_vaultwarden", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_mongodb", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_halo", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_hedgedoc", return_value=[db_sql]) as mock_hd:
                report2 = do_backup()

            mock_hd.assert_called_once_with(include_uploads=False)
            self.assertIn("hedgedoc_db: ✅ OK", report2)
            self.assertIn("hedgedoc_uploads: ✅ Skipped (scheduled Monday)", report2)
            self.assertTrue(report2.startswith("⚠️"))
            saved2 = json.loads(open(status_file, "r", encoding="utf-8").read())
            self.assertEqual(saved2["hedgedoc_status"]["db"]["status"], "OK")
            self.assertEqual(saved2["hedgedoc_status"]["uploads"]["status"], "Skipped (scheduled Monday)")
            self.assertEqual(saved2["hedgedoc_status"]["uploads"]["last_ok_time"], "2026-11-23 03:00:00")

    def test_do_backup_preserves_previous_archive_on_zip_failure_and_degraded_core(self):
        """P1 回归：ZIP 失败不删旧归档、不推进 last_verified_at；降级成功时保留失败库仍依赖的上一份归档"""
        with tempfile.TemporaryDirectory() as tmp_root:
            data_dir = os.path.join(tmp_root, "data")
            backup_dir = os.path.join(tmp_root, "backup")
            os.makedirs(data_dir, exist_ok=True)
            os.makedirs(backup_dir, exist_ok=True)

            old_archive_name = "data_20261127_030000.zip"
            old_archive_path = os.path.join(backup_dir, old_archive_name)
            with open(old_archive_path, "wb") as f:
                f.write(b"previous-good-archive")

            good_db = os.path.join(data_dir, "pc_cli.db")
            c = sqlite3.connect(good_db)
            c.execute("CREATE TABLE t (id INTEGER)")
            c.execute("INSERT INTO t VALUES (1)")
            c.commit()
            c.close()

            bad_db = os.path.join(data_dir, "broken.db")
            with open(bad_db, "wb") as f:
                f.write(b"not-a-db" * 20)

            status_file = os.path.join(data_dir, "backup_status.json")
            with open(status_file, "w", encoding="utf-8") as sf:
                json.dump(
                    {
                        "sqlite_snapshots": {
                            "data/pc_cli.db": {
                                "status": "verified",
                                "last_verified_at": "2026-11-27 03:00:00",
                                "last_verified_archive": old_archive_name,
                            },
                            "data/broken.db": {
                                "status": "verified",
                                "last_verified_at": "2026-11-27 03:00:00",
                                "last_verified_archive": old_archive_name,
                            },
                        }
                    },
                    sf,
                )

            # 1) data zip 打包失败：旧归档必须保留，pc_cli.db 的 last_verified_at 绝不能提前推进
            def fail_data_zip(cmd, **kwargs):
                if isinstance(cmd, list) and cmd and cmd[0] == "zip" and "data_" in cmd[2]:
                    raise subprocess.CalledProcessError(1, cmd, stderr="disk full")
                res = MagicMock()
                res.returncode = 0
                res.stdout = ""
                return res

            with patch("lite_agent.features.sys_ops.backup_service._BASE_DIR", tmp_root), \
                 patch("lite_agent.features.sys_ops.backup_service._BILLING_DIR", os.path.join(tmp_root, "no_billing")), \
                 patch("lite_agent.features.sys_ops.backup_service.subprocess.run", side_effect=fail_data_zip), \
                 patch("lite_agent.features.sys_ops.backup_service._create_meili_dump", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_vaultwarden", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_mongodb", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_halo", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_hedgedoc", return_value=[]):
                do_backup()

            self.assertTrue(os.path.exists(old_archive_path), "old archive must NOT be deleted when new zip fails")
            saved_fail = json.loads(open(status_file, "r", encoding="utf-8").read())
            self.assertEqual(saved_fail["sqlite_snapshots"]["data/pc_cli.db"]["last_verified_at"], "2026-11-27 03:00:00")
            self.assertEqual(saved_fail["sqlite_snapshots"]["data/pc_cli.db"]["last_verified_archive"], old_archive_name)

            # 2) data zip 打包成功但处于降级态（broken.db 失败）：pc_cli.db 推进到新归档，old_archive 因被 broken.db 依赖必须继续保留
            def ok_zip(cmd, **kwargs):
                if isinstance(cmd, list) and cmd and cmd[0] == "zip":
                    with open(cmd[2], "wb") as zf:
                        zf.write(b"PK\x05\x06" + b"\x00" * 18)
                res = MagicMock()
                res.returncode = 0
                res.stdout = ""
                return res

            with patch("lite_agent.features.sys_ops.backup_service._BASE_DIR", tmp_root), \
                 patch("lite_agent.features.sys_ops.backup_service._BILLING_DIR", os.path.join(tmp_root, "no_billing")), \
                 patch("lite_agent.features.sys_ops.backup_service.subprocess.run", side_effect=ok_zip), \
                 patch("lite_agent.features.sys_ops.backup_service._create_meili_dump", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_vaultwarden", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_mongodb", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_halo", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_hedgedoc", return_value=[]):
                report_deg = do_backup()

            self.assertTrue(report_deg.startswith("⚠️"), f"missing/degraded items must trigger warning header: {report_deg}")
            self.assertTrue(os.path.exists(old_archive_path), "previous archive still referenced by failed DB must be retained")
            saved_deg = json.loads(open(status_file, "r", encoding="utf-8").read())
            self.assertNotEqual(saved_deg["sqlite_snapshots"]["data/pc_cli.db"]["last_verified_archive"], old_archive_name)
            self.assertEqual(saved_deg["sqlite_snapshots"]["data/broken.db"]["last_verified_archive"], old_archive_name)
            self.assertEqual(saved_deg["sqlite_snapshots"]["data/broken.db"]["last_verified_at"], "2026-11-27 03:00:00")

    def test_snapshot_sqlite_db_enforces_budget_during_quick_check(self):
        """P2 回归：quick_check 阶段超预算也必须被判定失败，不得返回 verified"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ok_db = os.path.join(tmp_dir, "ok.db")
            c = sqlite3.connect(ok_db)
            c.execute("CREATE TABLE t (x TEXT)")
            c.execute("INSERT INTO t VALUES ('hello')")
            c.commit()
            c.close()
            dst_db = os.path.join(tmp_dir, "staged", "ok.db")
            # 1: deadline=100+10, 2: attempt check, 3: backup progress, 4: before quick_check, 5+: during/after quick_check -> 200.0 超时
            ticks = iter([100.0, 100.0, 101.0, 102.0, 200.0, 200.0, 200.0])
            with patch("lite_agent.features.sys_ops.backup_service.time.monotonic", side_effect=lambda: next(ticks, 200.0)):
                res = _snapshot_sqlite_db(ok_db, dst_db, budget_sec=10.0, max_attempts=1)
            self.assertEqual(res["status"], "failed")
            self.assertFalse(os.path.exists(dst_db))

    def test_do_backup_reconciles_missing_previously_tracked_db_and_prunes_only_after_status_persist(self):
        """P1 回归：本轮未发现的既有库标为 missing 并保留其归档引用；状态原子持久化失败时不清理任何旧归档"""
        with tempfile.TemporaryDirectory() as tmp_root:
            data_dir = os.path.join(tmp_root, "data")
            backup_dir = os.path.join(tmp_root, "backup")
            os.makedirs(data_dir, exist_ok=True)
            os.makedirs(backup_dir, exist_ok=True)

            old_archive_name = "data_20261127_030000.zip"
            old_archive_path = os.path.join(backup_dir, old_archive_name)
            with open(old_archive_path, "wb") as f:
                f.write(b"contains-statements-db")

            good_db = os.path.join(data_dir, "pc_cli.db")
            c = sqlite3.connect(good_db)
            c.execute("CREATE TABLE t (id INTEGER)")
            c.execute("INSERT INTO t VALUES (1)")
            c.commit()
            c.close()

            status_file = os.path.join(data_dir, "backup_status.json")
            with open(status_file, "w", encoding="utf-8") as sf:
                json.dump(
                    {
                        "sqlite_snapshots": {
                            "data/pc_cli.db": {
                                "status": "verified",
                                "last_verified_at": "2026-11-27 03:00:00",
                                "last_verified_archive": old_archive_name,
                            },
                            "statements.db": {
                                "status": "verified",
                                "last_verified_at": "2026-11-27 03:00:00",
                                "last_verified_archive": old_archive_name,
                            },
                        }
                    },
                    sf,
                )

            def ok_zip(cmd, **kwargs):
                if isinstance(cmd, list) and cmd and cmd[0] == "zip":
                    with open(cmd[2], "wb") as zf:
                        zf.write(b"PK\x05\x06" + b"\x00" * 18)
                res = MagicMock()
                res.returncode = 0
                res.stdout = ""
                return res

            # 1) statements.db 所在目录未挂载（本轮未发现）-> 必须标为 missing、Core 降级、且保留 old_archive_path
            with patch("lite_agent.features.sys_ops.backup_service._BASE_DIR", tmp_root), \
                 patch("lite_agent.features.sys_ops.backup_service._BILLING_DIR", os.path.join(tmp_root, "unmounted_billing")), \
                 patch("lite_agent.features.sys_ops.backup_service.subprocess.run", side_effect=ok_zip), \
                 patch("lite_agent.features.sys_ops.backup_service._create_meili_dump", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_vaultwarden", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_mongodb", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_halo", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_hedgedoc", return_value=[]):
                report = do_backup()

            self.assertIn("Degraded", report)
            self.assertTrue(os.path.exists(old_archive_path), "old archive referenced by missing statements.db must NOT be pruned")
            saved = json.loads(open(status_file, "r", encoding="utf-8").read())
            self.assertEqual(saved["sqlite_snapshots"]["statements.db"]["status"], "missing")
            self.assertEqual(saved["sqlite_snapshots"]["statements.db"]["last_verified_archive"], old_archive_name)
            self.assertEqual(saved["sqlite_snapshots"]["statements.db"]["last_verified_at"], "2026-11-27 03:00:00")

            # 2) 若 backup_status.json 原子替换前崩溃（os.replace 抛异常），任何旧归档都不得提前被删
            old_rsslite = os.path.join(backup_dir, "rsslite_20261127_030000.zip")
            with open(old_rsslite, "wb") as f:
                f.write(b"old-rsslite")
            fake_mongo_dir = os.path.join(tmp_root, "mongo_tmp")
            os.makedirs(fake_mongo_dir, exist_ok=True)
            fake_mongo_file = os.path.join(fake_mongo_dir, "rsslite.gz")
            with open(fake_mongo_file, "wb") as f:
                f.write(b"new-rsslite")

            with patch("lite_agent.features.sys_ops.backup_service._BASE_DIR", tmp_root), \
                 patch("lite_agent.features.sys_ops.backup_service._BILLING_DIR", os.path.join(tmp_root, "unmounted_billing")), \
                 patch("lite_agent.features.sys_ops.backup_service.subprocess.run", side_effect=ok_zip), \
                 patch("lite_agent.features.sys_ops.backup_service._create_meili_dump", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_vaultwarden", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_mongodb", return_value=fake_mongo_file), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_halo", return_value=None), \
                 patch("lite_agent.features.sys_ops.backup_service._backup_hedgedoc", return_value=[]), \
                 patch("lite_agent.features.sys_ops.backup_service.os.replace", side_effect=OSError("simulated crash before status publish")):
                with self.assertRaises(OSError):
                    do_backup()

            self.assertTrue(os.path.exists(old_rsslite), "old archive must NOT be pruned if status persistence fails")

    def test_upload_status_isolated_from_backup_status_and_guarded_by_batch_id(self):
        """P1 回归：上传状态独立写入 backup_upload_status.json，绝不修改 backup_status.json，且旧批次回调不覆盖新批次"""
        with tempfile.TemporaryDirectory() as tmp_root:
            data_dir = os.path.join(tmp_root, "data")
            os.makedirs(data_dir, exist_ok=True)
            status_file = os.path.join(data_dir, "backup_status.json")
            upload_status_file = os.path.join(data_dir, "backup_upload_status.json")

            sentinel_manifest = {
                "backup_batch_id": "20261128_030000",
                "sqlite_snapshots": {"statements.db": {"status": "missing", "last_verified_archive": "data_old.zip"}},
            }
            with open(status_file, "w", encoding="utf-8") as sf:
                json.dump(sentinel_manifest, sf)
            before_raw = open(status_file, "r", encoding="utf-8").read()

            with patch("lite_agent.features.sys_ops.backup_service._BASE_DIR", tmp_root), \
                 patch("lite_agent.features.sys_ops.backup_service._BDPAN_SYNC_LOG", os.path.join(data_dir, "bdpan.log")), \
                 patch("lite_agent.features.sys_ops.backup_service.do_backup", return_value="⚠️ 备份完成（存在降级、缺失或失败项）"), \
                 patch("lite_agent.features.sys_ops.backup_service.subprocess.Popen") as mock_popen:
                res = do_backup_and_sync()

            self.assertIn("后台上传进行中", res)
            mock_popen.assert_called_once()
            # backup_status.json 必须一字未改
            self.assertEqual(open(status_file, "r", encoding="utf-8").read(), before_raw)
            # 独立上传状态文件记录了对应批次的 uploading
            up1 = json.loads(open(upload_status_file, "r", encoding="utf-8").read())
            self.assertEqual(up1["backup_batch_id"], "20261128_030000")
            self.assertEqual(up1["status"], "uploading")

            # 新批次 20261129_030000 开始上传后，旧批次 20261128_030000 的迟到完成回调必须被丢弃
            self.assertTrue(_write_upload_status(upload_status_file, "20261129_030000", status="uploading"))
            self.assertFalse(
                _write_upload_status(
                    upload_status_file,
                    "20261128_030000",
                    status="failed",
                    error_message="stale failure",
                    completion_time="2026-11-29 03:01:00",
                )
            )
            up2 = json.loads(open(upload_status_file, "r", encoding="utf-8").read())
            self.assertEqual(up2["backup_batch_id"], "20261129_030000")
            self.assertEqual(up2["status"], "uploading")

            # 当前新批次 20261129_030000 完成回调正常写入
            self.assertTrue(
                _write_upload_status(
                    upload_status_file,
                    "20261129_030000",
                    status="success",
                    completion_time="2026-11-29 03:05:00",
                )
            )
            up3 = json.loads(open(upload_status_file, "r", encoding="utf-8").read())
            self.assertEqual(up3["status"], "success")
            self.assertEqual(up3["completion_time"], "2026-11-29 03:05:00")
