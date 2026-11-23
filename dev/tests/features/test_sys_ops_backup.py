"""Sys Ops 备份与云同步单测 (features.sys_ops.backup_service)。"""
import os
import tempfile
import unittest
import subprocess
from unittest.mock import patch, MagicMock
from lite_agent.features.sys_ops.backup_service import (
    _bdpan_sync_dir,
    _create_meili_dump,
    _clean_meili_dumps_after_archive,
    do_backup,
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
