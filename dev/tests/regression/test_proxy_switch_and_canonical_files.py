"""
Codex Round 9 Review Probes and Long-Term Regression Tests.
覆盖:
1. Socks5 真实切换流程：保持 /etc/proxy/config.json、switch_naive_config.sh 特权隔离、helper 失败回滚与 DB 一致性；
2. 双向模块代理污染消除：patch 前后 canonical 服务对象身份不变、连续调用零自递归；
3. 盘点清单真实性门禁：全量盘点中的 canonical 文件与符号 100% 物理存在，杜绝虚假迁出声明。
"""

import os
import re
import unittest
from unittest.mock import patch, Mock
import pytest

from lite_agent.features.network_ops import socks5_service
from lite_agent.skills import ops_socks5
from lite_agent.features.content_ops import meili_sync_service
from lite_agent.skills import ops_meili_sync


def test_r9_1_socks5_switch_helper_failure_rolls_back_and_preserves_db(tmp_path):
    """R9-1: helper 执行失败时必须自动回滚，数据库 is_active 绝不提前或错误标记为 active。"""
    # 模拟测试数据库
    test_db = str(tmp_path / "test_socks5.db")
    with patch("lite_agent.features.network_ops.socks5_service.get_socks5_db_path", return_value=test_db), \
         patch("lite_agent.skills.ops_socks5.get_socks5_db_path", return_value=test_db):

        # 插入当前主节点 (ID 1) 与 待切换节点 (ID 2)
        n1 = socks5_service.add_socks5_proxy("192.0.2.1", "naive --proxy=https://u:p@192.0.2.1", "Node1", priority=1)
        n2 = socks5_service.add_socks5_proxy("192.0.2.2", "naive --proxy=https://u:p@192.0.2.2", "Node2", priority=2)
        socks5_service._update_db_is_active(n1)
        assert socks5_service.get_current_active_proxy()["id"] == n1

        # 模拟特权 helper 脚本返回非 0 失败码
        mock_proc = Mock(returncode=1, stdout="", stderr="Config check failed: port conflict")
        with patch("os.name", "posix"), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open(read_data='{"listen":"socks://127.0.0.1:18988"}')), \
             patch("subprocess.run", return_value=mock_proc):

            ok, msg = socks5_service.apply_active_proxy_to_vps1(n2)
            assert ok is False
            assert "恢复原服务" in msg or "失败" in msg

            # 验证 DB 状态：主节点依然是 n1，n2 绝未被设为 active
            current = socks5_service.get_current_active_proxy()
            assert current["id"] == n1
            assert current["id"] != n2


def test_r9_1_socks5_host_signature_and_param_order():
    """R9-1: 验证 test_socks5_host 保持原参数顺序 (host, runcmd="", port=None, timeout=3.0)。"""
    with patch("socket.getaddrinfo", side_effect=Exception("mocked network block")):
        # 按原位置参数调用: (host, runcmd)
        res = socks5_service.test_socks5_host("example.invalid", "naive --proxy=https://user:pass@example.invalid:8443")
        assert res["success"] is False
        assert res["port"] == 8443
        assert res["host"] == "example.invalid"


def test_r9_2_meili_and_socks5_patch_leaves_canonical_uncontaminated():
    """R9-2: 退出 patch 后，旧模块属性回退绝不污染 canonical 服务对象，且无自递归。"""
    # 1. 验证 Meili 同步服务不受 patch 污染
    orig_meili_fn = meili_sync_service.sync_meili
    with patch.object(ops_meili_sync, "sync_meili", return_value="mocked_meili_result"):
        assert ops_meili_sync.sync_meili() == "mocked_meili_result"

    # 退出 patch 后，canonical 函数对象必须完全恒等于原始函数对象
    assert meili_sync_service.sync_meili is orig_meili_fn
    assert ops_meili_sync.sync_meili is not meili_sync_service.sync_meili

    # 验证薄入口调用 canonical 服务，连续调用绝无无限自递归
    with patch.object(meili_sync_service, "sync_meili", return_value="canonical_success"):
        for _ in range(5):
            res = ops_meili_sync.sync_meili()
            assert res == "canonical_success"

    # 2. 验证 Socks5 服务不受 patch 污染
    orig_socks5_apply = socks5_service.apply_active_proxy_to_vps1
    with patch.object(ops_socks5, "apply_active_proxy_to_vps1", return_value=(True, "mocked_apply")):
        assert ops_socks5.apply_active_proxy_to_vps1(999) == (True, "mocked_apply")

    assert socks5_service.apply_active_proxy_to_vps1 is orig_socks5_apply


def test_r9_3_all_inventory_canonical_files_physically_exist():
    """R9-3: 全量盘点清单中的每一个 canonical 文件路径在仓库中必须 100% 真实物理存在。"""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    import json
    inventory_path = os.path.join(repo_root, "dev/tests/fixtures/contracts/canonical-files.json")
    with open(inventory_path, encoding="utf-8") as stream:
        paths = json.load(stream)["canonical_files"]
    missing_files = [path for path in paths if not os.path.isfile(os.path.join(repo_root, path))]
    assert not missing_files, f"Canonical files missing: {missing_files}"
    assert len(paths) > 50, f"Canonical coverage too small: {len(paths)}"


def test_worker_adapter_uses_only_explicit_dependency_injection():
    """旧模块属性变化不能隐式改写 worker 的依赖选择。"""
    with patch.object(ops_socks5, "get_socks5_proxies") as legacy_getter, \
         patch.object(socks5_service, "start_failover_worker") as start:
        ops_socks5.start_failover_worker()
        assert start.call_args.kwargs["get_proxies_fn"] is None
        explicit_getter = Mock(return_value=[])
        ops_socks5.start_failover_worker(get_proxies_fn=explicit_getter)
        assert start.call_args.kwargs["get_proxies_fn"] is explicit_getter
        legacy_getter.assert_not_called()
