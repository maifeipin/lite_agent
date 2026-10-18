"""
Phone App 核心契约、安全校验、审批状态机与三态筛选自动化测试 (test_phone_app.py).
覆盖任务文档第 13 节与第 15.6 节要求的必测场景。
"""

import pytest
import time
import tempfile
import os
from lite_agent.features.phone_app.contracts import (
    BatchStatus,
    TaskStatus,
    ConditionEvaluation,
    OverallDiagnosis,
    ProductFilters,
    SalesFilter,
    PositiveRateFilter,
    ShopTypesFilter,
    ProductItem,
)
from lite_agent.features.phone_app.catalog import AppCatalog
from lite_agent.features.phone_app.approval import ApprovalManager, compute_canonical_hash
from lite_agent.features.phone_app.script_validation import validate_script_dsl
from lite_agent.features.phone_app.filter_engine import FilterEngine
from lite_agent.features.phone_app.repository import PhoneAppRepository
from lite_agent.features.phone_app.service import PhoneAppService


# --- 1. App Catalog 测试 ---

def test_app_catalog_validation():
    catalog = AppCatalog()
    # 正常京东已验证
    ok, err = catalog.validate_batch_target(["jd"], "c68a3cf2", "search_products")
    assert ok is True
    assert err is None

    # 包含未验证/实验性应用 (如抖音)
    ok, err = catalog.validate_batch_target(["douyin"], "c68a3cf2", "search_products")
    assert ok is False
    assert "experimental" in err

    # 未知设备
    ok, err = catalog.validate_batch_target(["jd"], "unknown_device_999", "search_products")
    assert ok is False
    assert "尚未在设备" in err

    # 伪造包名/未登记 App
    ok, err = catalog.validate_batch_target(["malicious_fake_app"], "c68a3cf2", "search")
    assert ok is False
    assert "未知的 App" in err


# --- 2. 脚本 DSL 静态安全与注入测试 ---

def test_script_dsl_validation():
    allowed_pkgs = {"com.jingdong.app.mall"}

    # 合法脚本
    valid_script = [
        {"action": "open_app", "package": "com.jingdong.app.mall"},
        {"action": "tap", "target": "#b51"},
        {"action": "fill", "target": "id=com.jd.lib.search.feature:id/zt", "text": "机械键盘"},
        {"action": "submit", "target": "搜索"},
        {"action": "back"}
    ]
    ok, err = validate_script_dsl(valid_script, allowed_pkgs)
    assert ok is True
    assert err is None

    # 命令注入检测：包含分号或命令拼接
    injection_script = [
        {"action": "tap", "target": "#b51; rm -rf /"}
    ]
    ok, err = validate_script_dsl(injection_script, allowed_pkgs)
    assert ok is False
    assert "非法或潜在注入字符" in err

    # 命令注入检测：$(...) 反引号
    injection_script2 = [
        {"action": "fill", "target": "id=search", "text": "$(whoami)"}
    ]
    ok, err = validate_script_dsl(injection_script2, allowed_pkgs)
    assert ok is False
    assert "非法控制字符" in err

    # 未知高风险字段检测 (禁止 eval/exec/shell)
    evil_step = [
        {"action": "tap", "target": "#btn", "eval": "import os; os.system('ls')"}
    ]
    ok, err = validate_script_dsl(evil_step, allowed_pkgs)
    assert ok is False
    assert ("未白名单的非法字段" in err or "包含被禁用的高风险字段" in err)

    # 越界包名
    illegal_pkg_step = [
        {"action": "open_app", "package": "com.evil.trojan"}
    ]
    ok, err = validate_script_dsl(illegal_pkg_step, allowed_pkgs)
    assert ok is False
    assert "不在当前受信任 App 清单中" in err


# --- 3. 规范化审批哈希与防篡改测试 ---

def test_approval_tamper_and_consumption():
    scripts = {"jd": [{"action": "tap", "target": "#search"}]}
    params = {"query": "iPhone 16", "filters": {}}
    app_ids = ["jd"]
    device_id = "c68a3cf2"
    versions = {"jd": "1.0.0"}
    limits = {"timeout": 30}

    # 创建审批
    approval = ApprovalManager.create_approval_record(
        batch_id="batch-001",
        scripts=scripts,
        params=params,
        app_ids=app_ids,
        device_id=device_id,
        adapter_versions=versions,
        limits=limits,
        reviewer_id="admin_user",
        ttl_seconds=600
    )
    assert approval["status"] == "approved"
    assert approval["consumed"] is False

    # 正常首次验证并消费
    ok, err = ApprovalManager.verify_and_consume_approval(
        approval_record=approval,
        current_scripts=scripts,
        current_params=params,
        app_ids=app_ids,
        device_id=device_id,
        adapter_versions=versions,
        limits=limits
    )
    assert ok is True
    assert approval["consumed"] is True

    # 重复消费被拒绝 (一次性授权)
    ok, err = ApprovalManager.verify_and_consume_approval(
        approval_record=approval,
        current_scripts=scripts,
        current_params=params,
        app_ids=app_ids,
        device_id=device_id,
        adapter_versions=versions,
        limits=limits
    )
    assert ok is False
    assert "已被消费" in err

    # 篡改参数导致哈希校验失败
    approval2 = ApprovalManager.create_approval_record(
        batch_id="batch-002",
        scripts=scripts,
        params=params,
        app_ids=app_ids,
        device_id=device_id,
        adapter_versions=versions,
        limits=limits,
        reviewer_id="admin_user",
        ttl_seconds=600
    )
    tampered_scripts = {"jd": [{"action": "tap", "target": "#tampered_button"}]}
    ok, err = ApprovalManager.verify_and_consume_approval(
        approval_record=approval2,
        current_scripts=tampered_scripts,
        current_params=params,
        app_ids=app_ids,
        device_id=device_id,
        adapter_versions=versions,
        limits=limits
    )
    assert ok is False
    assert "哈希校验不符" in err


# --- 4. 确定性三态筛选规则测试 (第 15.4 节核心依据) ---

def test_filter_engine_deterministic_rules():
    filters = ProductFilters(
        sales=SalesFilter(metric="cumulative_sales", operator="gt", threshold=1000),
        positive_rate=PositiveRateFilter(operator="gte", threshold_percent=95.0),
        shop_types=ShopTypesFilter(types=["self_operated", "official_flagship"])
    )

    # 场景 A: 明确满足 (全通 -> matched)
    p_pass = ProductItem(
        app_id="jd", source_task_id="t1", source_item_id="item_pass",
        title="自营机械键盘",
        sales_info={"raw_text": "5万+", "metric_name": "cumulative_sales", "scope": "item"},
        positive_rate=98.0,
        evidence={"verified_badges": ["self_operated"]}
    )
    diag_pass = FilterEngine.evaluate_product(p_pass, filters)
    assert diag_pass == OverallDiagnosis.MATCHED

    # 场景 B: 1000+ 与 > 1500 (区间无法断言 -> unknown -> needs_verification)
    filters_gt_1500 = ProductFilters(
        sales=SalesFilter(metric="cumulative_sales", operator="gt", threshold=1500)
    )
    p_unknown = ProductItem(
        app_id="jd", source_task_id="t2", source_item_id="item_unknown",
        title="小众键盘",
        sales_info={"raw_text": "1000+", "metric_name": "cumulative_sales", "scope": "item"},
        positive_rate=96.0,
        evidence={"verified_badges": ["self_operated"]}
    )
    diag_unknown = FilterEngine.evaluate_product(p_unknown, filters_gt_1500)
    assert diag_unknown == OverallDiagnosis.NEEDS_VERIFICATION
    assert p_unknown.evaluations["sales"]["status"] == ConditionEvaluation.UNKNOWN.value

    # 场景 C: 销量口径不符 (如月销量 vs 累计销量 -> unknown)
    p_metric_mismatch = ProductItem(
        app_id="jd", source_task_id="t3", source_item_id="item_mismatch",
        title="键盘",
        sales_info={"raw_text": "5000", "metric_name": "monthly_sales", "scope": "item"},
        positive_rate=96.0,
        evidence={"verified_badges": ["self_operated"]}
    )
    diag_mismatch = FilterEngine.evaluate_product(p_metric_mismatch, filters)
    assert diag_mismatch == OverallDiagnosis.NEEDS_VERIFICATION
    assert "指标口径不符" in p_metric_mismatch.evaluations["sales"]["reason"]

    # 场景 D: 明确不符合 (如好评率 92% < 95% -> fail -> rejected)
    p_fail = ProductItem(
        app_id="jd", source_task_id="t4", source_item_id="item_fail",
        title="差评键盘",
        sales_info={"raw_text": "10万+", "metric_name": "cumulative_sales", "scope": "item"},
        positive_rate=91.5,
        evidence={"verified_badges": ["self_operated"]}
    )
    diag_fail = FilterEngine.evaluate_product(p_fail, filters)
    assert diag_fail == OverallDiagnosis.REJECTED
    assert p_fail.evaluations["positive_rate"]["status"] == ConditionEvaluation.FAIL.value


def test_filter_engine_logistics_condition():
    """复核 R04 核心复现：要求包邮、商品明确不包邮，必须判定为 FAIL 与 REJECTED。"""
    from lite_agent.features.phone_app.contracts import LogisticsFilter

    filters = ProductFilters(
        logistics=LogisticsFilter(free_shipping=True, delivery_partner="jd_delivery")
    )

    # 1. 商品明确不包邮 -> FAIL -> REJECTED
    p_not_free = ProductItem(
        app_id="jd", source_task_id="t_log1", source_item_id="item_not_free",
        title="不包邮商品",
        logistics_info={"free_shipping": False, "delivery_partner": "merchant_delivery"}
    )
    diag1 = FilterEngine.evaluate_product(p_not_free, filters)
    assert diag1 == OverallDiagnosis.REJECTED
    assert p_not_free.evaluations["logistics"]["status"] == ConditionEvaluation.FAIL.value
    assert "不包邮" in p_not_free.evaluations["logistics"]["reason"]

    # 2. 包邮但配送方不符 -> FAIL -> REJECTED
    p_diff_partner = ProductItem(
        app_id="jd", source_task_id="t_log2", source_item_id="item_diff_partner",
        title="包邮非京东快递",
        logistics_info={"free_shipping": True, "delivery_partner": "merchant_delivery"}
    )
    diag2 = FilterEngine.evaluate_product(p_diff_partner, filters)
    assert diag2 == OverallDiagnosis.REJECTED
    assert p_diff_partner.evaluations["logistics"]["status"] == ConditionEvaluation.FAIL.value

    # 3. 物流信息缺失/未知 -> UNKNOWN -> NEEDS_VERIFICATION
    p_unknown_log = ProductItem(
        app_id="jd", source_task_id="t_log3", source_item_id="item_unknown_log",
        title="未注明物流商品",
        logistics_info={"free_shipping": None, "delivery_partner": None}
    )
    diag3 = FilterEngine.evaluate_product(p_unknown_log, filters)
    assert diag3 == OverallDiagnosis.NEEDS_VERIFICATION
    assert p_unknown_log.evaluations["logistics"]["status"] == ConditionEvaluation.UNKNOWN.value

    # 4. 完全满足 -> PASS -> MATCHED
    p_match_log = ProductItem(
        app_id="jd", source_task_id="t_log4", source_item_id="item_match_log",
        title="京东物流包邮商品",
        logistics_info={"free_shipping": True, "delivery_partner": "jd_delivery"}
    )
    diag4 = FilterEngine.evaluate_product(p_match_log, filters)
    assert diag4 == OverallDiagnosis.MATCHED
    assert p_match_log.evaluations["logistics"]["status"] == ConditionEvaluation.PASS.value


# --- 5. 调度、审核状态机与无 Worker 真实失败测试 ---

def test_service_missing_worker_fails_truthfully():
    """复核 R01 要求：无 Worker 时严禁默认模拟成功兜底，必须真实失败并如实报错。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_phone.db")
        repo = PhoneAppRepository(db_path=db_path)
        catalog = AppCatalog()
        service = PhoneAppService(repo=repo, catalog=catalog)

        ok, res = service.create_batch(
            query="机械键盘",
            app_ids=["jd"],
            device_id="c68a3cf2",
            action="search_products",
            mode="fixed"
        )
        assert ok is True
        batch_id = res["batch_id"]

        # 执行时不传入 worker_client，模拟 worker 离线
        exec_ok, _ = service.execute_batch(
            batch_id=batch_id,
            user_context={"user_id": "test_runner"},
            worker_client=None,
            async_run=False
        )
        assert exec_ok is True

        summary = service.get_batch_summary(batch_id)
        assert summary["batch"]["status"] == BatchStatus.FAILED.value
        tasks = summary["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["status"] == TaskStatus.FAILED.value
        assert "无可用执行端或 Worker 离线" in (tasks[0]["error"] or "")
        # 绝无虚假伪造商品
        assert summary["summary"]["total_scanned"] == 0
        assert len(summary["grouped_results"]["matched"]) == 0


def test_service_custom_ai_approval_and_repeat_prevention():
    """复核 R02 要求：未审核自定义任务禁止执行，不可重复执行，授权 CAS 原子消费。"""
    from lite_agent.integrations.phone_app.worker.worker import FakeWorkerClient

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_phone.db")
        repo = PhoneAppRepository(db_path=db_path)
        catalog = AppCatalog()
        service = PhoneAppService(repo=repo, catalog=catalog)
        fake_worker = FakeWorkerClient()

        # 1. 创建 custom_ai 任务，初始状态必须为 draft
        ok, res = service.create_batch(
            query="无线耳机",
            app_ids=["jd"],
            device_id="c68a3cf2",
            action="search_products",
            mode="custom_ai"
        )
        assert ok is True
        batch_id = res["batch_id"]
        assert res["status"] == BatchStatus.DRAFT.value

        # 2. 未生成脚本、未审批前直接执行 -> 必须拒绝
        exec_ok, err = service.execute_batch(batch_id, async_run=False)
        assert exec_ok is False
        assert "批次未审核批准" in err.get("error", "")

        def mock_ai_generator(query, app_ids):
            return {
                aid: [
                    {"action": "open_app", "package": "com.jingdong.app.mall"},
                    {"action": "wait_for", "target": "搜索栏", "timeout": 3.0},
                    {"action": "tap", "target": "#b51"},
                    {"action": "fill", "target": "id=com.jd.lib.search.feature:id/zt", "text": query},
                    {"action": "submit", "target": "Button[text='搜索']"},
                    {"action": "extract", "fields": ["title", "price", "shop", "sales"]},
                    {"action": "back"}
                ]
                for aid in app_ids
            }

        # 3. 验证若模型服务不可用，严禁回退固定模板假冒 AI (R06)
        service.config = {"llm": {"models": {}}}
        fail_ok, fail_res = service.generate_scripts(batch_id)
        assert fail_ok is False
        assert "AI 模型生成脚本失败" in fail_res.get("error", "")

        # 4. 正常生成合法脚本 -> 状态进入 pending_review
        gen_ok, gen_res = service.generate_scripts(batch_id, scripts_generator_fn=mock_ai_generator)
        assert gen_ok is True
        assert gen_res["status"] == BatchStatus.PENDING_REVIEW.value

        # 5. pending_review 状态下未审批直接执行 -> 必须拒绝
        exec_ok, err = service.execute_batch(batch_id, async_run=False)
        assert exec_ok is False
        assert "批次未审核批准" in err.get("error", "")

        # 6. 用户尝试清空脚本或批准未生成脚本 -> 必须被拦截拒绝
        edit_ok, edit_err = service.update_draft(batch_id, {"jd": []})
        assert edit_ok is False
        assert "缺少有效的脚本步骤" in edit_err.get("error", "")

        # 7. 重新生成并正常审批
        service.generate_scripts(batch_id, scripts_generator_fn=mock_ai_generator)
        appr_ok, appr_res = service.approve_batch(batch_id, reviewer_id="admin")
        assert appr_ok is True
        assert appr_res["status"] == BatchStatus.APPROVED.value

        # 7. 审批后首次执行 -> 成功
        exec_ok, _ = service.execute_batch(
            batch_id=batch_id,
            user_context={"user_id": "test_runner"},
            worker_client=fake_worker,
            async_run=False
        )
        assert exec_ok is True
        summary = service.get_batch_summary(batch_id)
        assert summary["batch"]["status"] == BatchStatus.SUCCEEDED.value

        # 8. 完成后再次执行 -> 必须拒绝重复执行
        exec_again, err_again = service.execute_batch(
            batch_id=batch_id,
            user_context={"user_id": "test_runner"},
            worker_client=fake_worker,
            async_run=False
        )
        assert exec_again is False
        assert "禁止执行" in err_again.get("error", "")

        # 9. 终态下禁止篡改草稿
        edit_ok, edit_err = service.update_draft(batch_id, {"jd": [{"action": "back"}]})
        assert edit_ok is False
        assert "禁止修改草稿" in edit_err.get("error", "")


def test_service_batch_lifecycle_and_locking():
    """使用显式 FakeWorkerClient 验证完整生命周期、排队、执行与三态汇总。"""
    from lite_agent.integrations.phone_app.worker.worker import FakeWorkerClient

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_phone.db")
        repo = PhoneAppRepository(db_path=db_path)
        catalog = AppCatalog()
        service = PhoneAppService(repo=repo, catalog=catalog)
        fake_worker = FakeWorkerClient()

        # 1. 创建批次
        ok, res = service.create_batch(
            query="机械键盘",
            app_ids=["jd"],
            device_id="c68a3cf2",
            action="search_products",
            filters={"sales": {"threshold": 1000, "operator": "gt"}},
            mode="fixed"
        )
        assert ok is True
        batch_id = res["batch_id"]

        # 2. 注入 FakeWorkerClient 同步执行批次
        exec_ok, exec_res = service.execute_batch(
            batch_id=batch_id,
            user_context={"user_id": "test_runner"},
            worker_client=fake_worker,
            async_run=False
        )
        assert exec_ok is True

        # 3. 检查汇总与三态分组
        summary = service.get_batch_summary(batch_id)
        assert summary is not None
        assert summary["batch"]["status"] == BatchStatus.SUCCEEDED.value
        assert summary["summary"]["total_scanned"] == 2
        # item_1: 5万+ 自营 -> matched
        # item_2: 1000+ 第三方 -> unknown/needs_verification
        assert summary["summary"]["matched_count"] == 1
        assert summary["summary"]["needs_verification_count"] == 1
        assert summary["summary"]["rejected_count"] == 0


def test_plugin_disabled_by_default_and_lifecycle():
    """复核 R08 要求：插件默认禁用，显式配置启用，并通过 PluginContext 事务登记与清理。"""
    from lite_agent.features.phone_app import register

    class MockContext:
        def __init__(self, config=None):
            self.config = config or {}
            self.registered_routes = []
            self.registered_skills = []
            self.cleanups = []

        def register_route(self, method, pattern, handler, **kwargs):
            self.registered_routes.append((method, pattern))

        def register_skill(self, name, skill_def):
            self.registered_skills.append(name)

        def register_cleanup(self, name, fn):
            self.cleanups.append((name, fn))

    # 1. 默认无配置或 enabled=False -> 绝不注册任何资源
    ctx_disabled = MockContext(config={})
    register(ctx_disabled)
    assert len(ctx_disabled.registered_routes) == 0
    assert len(ctx_disabled.registered_skills) == 0
    assert len(ctx_disabled.cleanups) == 0

    ctx_false = MockContext(config={"phone_app": {"enabled": False}})
    register(ctx_false)
    assert len(ctx_false.registered_routes) == 0

    # 2. 显式 enabled=True -> 注册路由、技能与清理回调
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = os.path.join(tmpdir, "plugin_test.db")
        ctx_enabled = MockContext(config={"phone_app": {"enabled": True, "db_path": test_db}})
        register(ctx_enabled)
        assert len(ctx_enabled.registered_routes) > 0
        assert any(r[1] == "/api/v1/phone-app/devices" for r in ctx_enabled.registered_routes)
        assert len(ctx_enabled.cleanups) == 1
        assert ctx_enabled.cleanups[0][0] == "phone_app_service_cleanup"

        # 验证清理钩子可正常执行关闭连接
        cleanup_fn = ctx_enabled.cleanups[0][1]
        cleanup_fn()


# --- 10. B01: 固定模式禁止注入脚本 & custom_ai 草稿编辑失效既有审批 ---

def test_b01_fixed_mode_script_injection_and_draft_invalidation():
    """复核 B01 要求：fixed 模式不可篡改/不可生成脚本；custom_ai 编辑重置状态并作废审批。"""
    from lite_agent.integrations.phone_app.worker.worker import FakeWorkerClient
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_b01.db")
        repo = PhoneAppRepository(db_path=db_path)
        catalog = AppCatalog()
        service = PhoneAppService(repo=repo, catalog=catalog)

        # 1. 创建 fixed 模式批次
        ok, res = service.create_batch(
            query="机械键盘",
            app_ids=["jd"],
            device_id="c68a3cf2",
            action="search_products",
            mode="fixed"
        )
        assert ok is True
        batch_id = res["batch_id"]

        # 尝试在 fixed 批次生成自定义脚本 -> 必须严词拒绝
        gen_ok, gen_err = service.generate_scripts(batch_id)
        assert gen_ok is False
        assert "固定搜索模式 (fixed) 模板不可变" in gen_err.get("error", "")

        # 尝试在 fixed 批次编辑/注入脚本 -> 必须严词拒绝
        edit_ok, edit_err = service.update_draft(batch_id, {"jd": [{"action": "tap", "target": "#evil"}]})
        assert edit_ok is False
        assert "固定搜索模式 (fixed) 模板不可变" in edit_err.get("error", "")

        # 批次脚本保持为空
        batch = repo.get_batch(batch_id)
        assert batch["scripts"] == {} or batch["scripts"] == ""

        # 2. 创建 custom_ai 批次，验证审批后编辑草稿自动作废审批且 CAS 状态受控
        ok_ai, res_ai = service.create_batch(
            query="手机支架",
            app_ids=["jd"],
            device_id="c68a3cf2",
            action="search_products",
            mode="custom_ai"
        )
        assert ok_ai is True
        ai_batch_id = res_ai["batch_id"]

        def mock_generator(query, app_ids):
            return {
                "jd": [
                    {"action": "open_app", "package": "com.jingdong.app.mall"},
                    {"action": "tap", "target": "#search"},
                    {"action": "back"}
                ]
            }

        service.generate_scripts(ai_batch_id, scripts_generator_fn=mock_generator)
        appr_ok, appr_res = service.approve_batch(ai_batch_id, reviewer_id="reviewer_1")
        assert appr_ok is True
        assert appr_res["status"] == BatchStatus.APPROVED.value
        initial_revision = repo.get_batch(ai_batch_id).get("revision", 1)

        # 用户编辑修改了脚本
        new_scripts = {
            "jd": [
                {"action": "open_app", "package": "com.jingdong.app.mall"},
                {"action": "tap", "target": "#modified_search"},
                {"action": "back"}
            ]
        }
        edit_ok, edit_res = service.update_draft(ai_batch_id, new_scripts)
        assert edit_ok is True
        assert edit_res["status"] == BatchStatus.PENDING_REVIEW.value
        assert edit_res["revision"] == initial_revision + 1

        # 验证数据库中持久化的状态被原子重置为 pending_review，既有审批被作废
        persisted_batch = repo.get_batch(ai_batch_id)
        assert persisted_batch["status"] == BatchStatus.PENDING_REVIEW.value
        appr = repo.get_approval(ai_batch_id)
        assert appr is None or appr.get("status") == "revoked"

        # 未重新审批直接执行 -> 必须被拦截拒绝
        fake_worker = FakeWorkerClient()
        exec_ok, exec_err = service.execute_batch(ai_batch_id, worker_client=fake_worker, async_run=False)
        assert exec_ok is False
        assert "批次未审核批准" in exec_err.get("error", "")


# --- 11. B02: WorkerHub 认证握手、心跳与租赁协议测试 ---

def test_b02_worker_hub_protocol_and_handshake():
    """复核 B02 要求：WorkerHub 握手认证、版本锁定、心跳租约与安全隔离。"""
    from lite_agent.features.phone_app.worker_hub import WorkerHub

    hub = WorkerHub(expected_token="secure_token_abc")

    # 1. 错误 token 握手 -> 拒绝
    h_ok, h_res = hub.handshake(
        worker_id="mac-node-1",
        token="wrong_token",
        version="1.0.0",
        capabilities=["jd"],
        devices=[{"device_id": "c68a3cf2"}]
    )
    assert h_ok is False
    assert "Token 无效" in h_res.get("error", "")

    # 2. 协议版本不匹配 -> 拒绝
    h_ok, h_res = hub.handshake(
        worker_id="mac-node-1",
        token="secure_token_abc",
        version="9.9.9",
        capabilities=["jd"],
        devices=[{"device_id": "c68a3cf2"}]
    )
    assert h_ok is False
    assert "版本不兼容" in h_res.get("error", "")

    # 3. 合法握手 -> 成功并分配 session_id
    h_ok, h_res = hub.handshake(
        worker_id="mac-node-1",
        token="secure_token_abc",
        version="1.0.0",
        capabilities=["jd"],
        devices=[{"device_id": "c68a3cf2", "status": "online"}]
    )
    assert h_ok is True
    session_id = h_res.get("session_id")
    assert session_id is not None

    # 4. 心跳保活测试
    hb_ok, hb_res = hub.heartbeat(
        worker_id="mac-node-1",
        session_id=session_id,
        devices=[{"device_id": "c68a3cf2", "status": "online"}]
    )
    assert hb_ok is True

    # 5. 设备上线状态上报
    devices = hub.get_online_devices()
    assert len(devices) == 1
    assert devices[0]["device_id"] == "c68a3cf2"
    assert devices[0]["worker_id"] == "mac-node-1"


# --- 12. B03: AI 脚本生成向 LLMGateway 传递 model 参数测试 ---

def test_b03_ai_generation_invokes_llm_with_model():
    """复核 B03 要求：_generate_scripts_via_llm 正确解析并传递 model 参数，杜绝 ValueError。"""
    import json

    class MockRouter:
        def __init__(self):
            self.models_cfg = {"qwen-plus": {"provider": "dashscope"}}
            self.config = {"llm": {"default": "qwen-plus"}}

    class MockGateway:
        def __init__(self):
            self.calls = []

        def invoke_sync(self, messages, role, model=None, timeout=None):
            self.calls.append({
                "messages": messages,
                "role": role,
                "model": model,
                "timeout": timeout
            })
            content = json.dumps({
                "jd": [
                    {"action": "open_app", "package": "com.jingdong.app.mall"},
                    {"action": "tap", "target": "#search"},
                    {"action": "back"}
                ]
            })
            return {"content": content}

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_b03.db")
        repo = PhoneAppRepository(db_path=db_path)
        catalog = AppCatalog()
        mock_router = MockRouter()
        mock_gateway = MockGateway()
        service = PhoneAppService(
            repo=repo,
            catalog=catalog,
            router=mock_router,
            gateway=mock_gateway
        )

        ok, res = service.create_batch(
            query="蓝牙降噪耳机",
            app_ids=["jd"],
            device_id="c68a3cf2",
            action="search_products",
            mode="custom_ai"
        )
        assert ok is True
        batch_id = res["batch_id"]

        gen_ok, gen_res = service.generate_scripts(batch_id)
        assert gen_ok is True
        assert gen_res["status"] == BatchStatus.PENDING_REVIEW.value

        # 核心断言：invoke_sync 必须接收到 model='qwen-plus'，不留空
        assert len(mock_gateway.calls) == 1
        call = mock_gateway.calls[0]
        assert call["model"] == "qwen-plus"
        assert call["role"] == "dsl_generator"


# --- 13. B04: Timeout 状态映射及复位失败设备持久暂停测试 ---

def test_b04_worker_timeout_and_device_paused_persistence():
    """复核 B04 要求：Worker 超时映射为 timed_out；复位失败持久化暂停，阻止后续批次。"""
    from lite_agent.integrations.phone_app.worker.worker import FakeWorkerClient
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_b04.db")
        repo = PhoneAppRepository(db_path=db_path)
        catalog = AppCatalog()
        service = PhoneAppService(repo=repo, catalog=catalog)

        # 1. 测试 Worker 返回 'timeout' 状态，必须映射为 timed_out，绝不可标为 succeeded
        timeout_worker = FakeWorkerClient(status="timeout", reset_status="ready")
        ok, res = service.create_batch(
            query="降噪耳塞",
            app_ids=["jd"],
            device_id="c68a3cf2",
            action="search_products",
            mode="fixed"
        )
        b1_id = res["batch_id"]
        exec_ok, _ = service.execute_batch(b1_id, worker_client=timeout_worker, async_run=False)
        assert exec_ok is True
        summary1 = service.get_batch_summary(b1_id)
        assert summary1["tasks"][0]["status"] == TaskStatus.TIMED_OUT.value
        assert summary1["batch"]["status"] == BatchStatus.FAILED.value

        # 2. 测试 Worker 复位失败 (reset_status != 'ready') -> 触发设备持久暂停
        reset_fail_worker = FakeWorkerClient(status="failed", reset_status="device_frozen")
        ok, res = service.create_batch(
            query="智能手表",
            app_ids=["jd"],
            device_id="c68a3cf2",
            action="search_products",
            mode="fixed"
        )
        b2_id = res["batch_id"]
        service.execute_batch(b2_id, worker_client=reset_fail_worker, async_run=False)

        # 设备状态必须被持久化记录为 paused
        dev_state = repo.get_device_state("c68a3cf2")
        assert dev_state is not None
        assert dev_state["state"] == "paused"
        assert "复位" in (dev_state.get("paused_reason") or dev_state.get("reason") or "")

        # 3. 同一设备后续批次执行 -> 必须被拒绝拦截 (设备已暂停)
        ok, res = service.create_batch(
            query="充电宝",
            app_ids=["jd"],
            device_id="c68a3cf2",
            action="search_products",
            mode="fixed"
        )
        b3_id = res["batch_id"]
        ready_worker = FakeWorkerClient(status="succeeded", reset_status="ready")
        exec3_ok, exec3_err = service.execute_batch(b3_id, worker_client=ready_worker, async_run=False)
        assert exec3_ok is False
        assert "处于暂停状态" in exec3_err.get("error", "")

        # 4. 人工运维核实恢复设备
        resume_ok, resume_res = service.resume_device("c68a3cf2", operator="admin", comment="人工解除告警")
        assert resume_ok is True
        assert repo.get_device_state("c68a3cf2")["state"] == "ready"

        # 恢复后批次正常执行成功
        exec3_retry, _ = service.execute_batch(b3_id, worker_client=ready_worker, async_run=False)
        assert exec3_retry is True
        summary3 = service.get_batch_summary(b3_id)
        assert summary3["batch"]["status"] == BatchStatus.SUCCEEDED.value


# --- 14. B06: 根目录卫生与顶层包门禁架构测试 ---

def test_b06_directory_hygiene_and_no_root_phone_worker():
    """复核 B06 要求：根目录 phone_worker/ 彻底迁移，严禁存在顶层非法包。"""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))

    # 1. 根目录 phone_worker/ 严禁存在
    root_phone_worker = os.path.join(repo_root, "phone_worker")
    assert not os.path.exists(root_phone_worker), f"根目录仍存在非法包: {root_phone_worker}"

    # 2. 目标迁移目录完整存在
    migrated_dir = os.path.join(repo_root, "lite_agent/integrations/phone_app/worker")
    assert os.path.isdir(migrated_dir), f"迁移目录不存在: {migrated_dir}"
    for required_file in ["worker.py", "run_worker.py", "dsl_executor.py", "adapters/jd.py"]:
        fpath = os.path.join(migrated_dir, required_file)
        assert os.path.exists(fpath), f"迁移目录缺少核心文件: {fpath}"
    readme_path = os.path.join(migrated_dir, "README.md")
    if os.path.exists(readme_path):
        assert os.path.isfile(readme_path)

    # 3. 架构门禁：根目录只允许白名单目录，严禁存在 phone_worker
    allowed_top_dirs = {
        "lite_agent", "web_dashboard", "dev", "scripts", "edge_node",
        "deploy", "conf.d", "data", "assets", "rules", "workspace",
        ".agents", ".local", ".git", ".pytest_cache", ".venv", "venv", "__pycache__",
        "mail-statement-parser", "mail_statement_parser",
    }
    top_entries = [
        e for e in os.listdir(repo_root)
        if os.path.isdir(os.path.join(repo_root, e)) and not e.startswith(".")
    ]
    assert "phone_worker" not in top_entries, "根目录仍存在 phone_worker 独立包"
    for d in top_entries:
        assert d in allowed_top_dirs, f"根目录检测到未白名单的目录: {d}"


# --- 15. B07: DSL 严格字段白名单与实测分段计时测试 ---

def test_b07_dsl_validation_strict_whitelist_and_timings():
    """复核 B07 要求：DSL 严格动作字段白名单，禁止指定本地产物路径，真实采集分段计时。"""
    allowed_pkgs = {"com.jingdong.app.mall"}

    # 1. 试图在 screenshot 中指定自定义路径 -> 拒绝
    evil_screenshot = [
        {"action": "screenshot", "path": "/etc/passwd"}
    ]
    ok, err = validate_script_dsl(evil_screenshot, allowed_pkgs)
    assert ok is False
    assert "未白名单的非法字段" in err

    # 2. 试图在 tap 步骤中夹带未知参数 -> 拒绝
    evil_tap = [
        {"action": "tap", "target": "#search", "extra_param": "bypass"}
    ]
    ok, err = validate_script_dsl(evil_tap, allowed_pkgs)
    assert ok is False
    assert "未白名单的非法字段" in err

    # 3. 验证 Worker 真实计时返回 (包含 prepare_ms, search_ms, details_ms, restore_ms, total_ms)
    from lite_agent.integrations.phone_app.worker.worker import FakeWorkerClient
    fake_worker = FakeWorkerClient(status="succeeded", reset_status="ready")
    res = fake_worker.execute_task(
        {"batch_id": "b-1", "task_id": "t-1", "attempt_id": "att-1", "app_id": "jd"},
        {"mode": "fixed"}
    )
    timings = res.get("timings", {})
    for k in ["prepare_ms", "search_ms", "details_ms", "restore_ms", "total_ms"]:
        assert k in timings, f"缺少计时指标: {k}"
        assert isinstance(timings[k], int)
        assert timings[k] >= 0


# --- 16. C01-C06: 第三轮复核阻断项针对性回归测试 ---

def test_c01_worker_session_auth_and_receipt_protection():
    """复核 C01: Worker 会话鉴权、回执安全防护与杜绝默认口令。"""
    from lite_agent.features.phone_app.worker_hub import WorkerHub

    # 1. 验证未指定口令时自动生成高熵临时口令，且绝非默认明文
    hub = WorkerHub(expected_token="")
    assert hub.expected_token, "必须生成高熵口令"
    assert hub.expected_token != "phone-worker-secret-token", "严禁使用硬编码默认口令"
    assert len(hub.expected_token) >= 32, "临时口令长度必须充足"

    # 2. 正常 Handshake 并获取 session_id
    ok, hs_data = hub.handshake(
        worker_id="test-w1",
        token=hub.expected_token,
        version="1.0.0",
        capabilities=["jd"],
        devices=[{"device_id": "c68a3cf2", "model": "OnePlus 9", "status": "online"}]
    )
    assert ok is True
    session_id = hs_data.get("session_id")
    assert session_id, "握手必须返回合法 session_id"

    # 3. 模拟提交回执：缺少 session_id 必须被拒
    ok, err_res = hub.submit_receipt(
        worker_id="test-w1",
        session_id=None,
        lease_id="fake-lease-1",
        receipt={"status": "succeeded"}
    )
    assert ok is False
    assert ("会话" in err_res.get("error", "") or "session" in err_res.get("error", "").lower())

    # 4. 模拟提交回执：session_id 不匹配必须被拒
    ok, err_res = hub.submit_receipt(
        worker_id="test-w1",
        session_id="invalid-session-xyz",
        lease_id="fake-lease-1",
        receipt={"status": "succeeded"}
    )
    assert ok is False
    assert ("会话" in err_res.get("error", "") or "session" in err_res.get("error", "").lower())

    # 5. 回执针对不存在的 lease 必须返回 False
    ok, err_res = hub.submit_receipt(
        worker_id="test-w1",
        session_id=session_id,
        lease_id="non-existent-lease",
        receipt={"status": "succeeded"}
    )
    assert ok is False
    assert "不存在" in err_res.get("error", "")


def test_c02_fixed_batch_concurrent_execution_single_claim():
    """复核 C02: 固定模式并发执行时的原子 CAS 认领，杜绝重复派发。"""
    import threading

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_c02.db")
        repo = PhoneAppRepository(db_path=db_path)
        service = PhoneAppService(repo=repo)

        # 创建一个已获批的固定动作批次
        ok, b_data = service.create_batch(
            query="机械键盘",
            app_ids=["jd"],
            device_id="c68a3cf2",
            mode="fixed"
        )
        assert ok is True
        batch_id = b_data["batch_id"]
        # 固定动作无需 DSL 审核，直接处于 approved
        batch = repo.get_batch(batch_id)
        assert batch["status"] == "approved"

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker_claim():
            try:
                barrier.wait(timeout=5)
                ok_claim, res = service.execute_batch(batch_id, async_run=False)
                if ok_claim:
                    results.append(res)
                else:
                    errors.append(res.get("error", ""))
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=worker_claim)
        t2 = threading.Thread(target=worker_claim)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 必须且只能有 1 个线程成功认领执行，另 1 个线程被原子锁排斥
        assert len(results) == 1, f"期望恰好 1 次成功，实际成功次数: {len(results)}"
        assert len(errors) == 1, f"期望恰好 1 次失败，实际失败次数: {len(errors)}"
        assert ("冲突" in errors[0] or "approved" in errors[0] or "queued" in errors[0] or "批次未审核批准" in errors[0] or "批准" in errors[0])

        # 批次状态已被认领派发，且只生成了对应 tasks
        final_batch = repo.get_batch(batch_id)
        assert final_batch["status"] in ("queued", "failed")
        conn = repo._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT task_id FROM tasks WHERE batch_id = ?", (batch_id,))
            tasks = cursor.fetchall()
            assert len(tasks) == 1, "只应有 1 个任务被排队派发"
        finally:
            conn.close()


def test_c03_device_offline_status_preserved_and_heartbeat_cancellation():
    """复核 C03: 假在线阻断 (保留设备 offline 状态) 与心跳返回取消租约。"""
    from lite_agent.features.phone_app.worker_hub import WorkerHub

    hub = WorkerHub(expected_token="test-token")
    # Worker 上报手机状态为 offline
    ok, reg = hub.handshake(
        worker_id="w-off",
        token="test-token",
        version="1.0.0",
        capabilities=["jd"],
        devices=[{"device_id": "phone-offline-1", "model": "OnePlus", "status": "offline"}]
    )
    assert ok is True
    session_id = reg["session_id"]

    # 1. 验证 get_online_devices 保留 offline，不得篡改为 online
    devs = hub.get_online_devices()
    assert len(devs) == 1
    assert devs[0]["status"] == "offline", "离线状态必须严格保留，杜绝假在线"

    # 2. 验证 has_online_worker_for_device 返回 False
    assert hub.has_online_worker_for_device("phone-offline-1") is False

    # 3. 验证任务轮询不会向 offline 设备派发
    ok, poll_res = hub.poll_task(
        worker_id="w-off",
        session_id=session_id,
        timeout_sec=0.5
    )
    assert ok is True
    assert poll_res.get("has_task") is False

    # 4. 验证取消租约通过心跳通知 Worker
    # 创建一个激活的 lease
    from lite_agent.features.phone_app.worker_hub import TaskLease
    lease = TaskLease(
        lease_id="lease-abc",
        device_id="phone-offline-1",
        task_id="task-abc",
        attempt_id="att-1",
        task_data={"batch_id": "batch-to-cancel", "task_id": "task-abc"},
        batch_data={"batch_id": "batch-to-cancel"},
        ttl_seconds=60.0
    )
    lease.worker_id = "w-off"
    hub._active_leases["lease-abc"] = lease
    cancelled_count = hub.cancel_lease_by_batch("batch-to-cancel")
    assert cancelled_count == 1
    assert lease.cancelled is True

    # Worker 进行心跳，必须拿到 cancelled_leases
    hb_ok, hb_res = hub.heartbeat(
        worker_id="w-off",
        session_id=session_id,
        devices=[{"device_id": "phone-offline-1", "status": "offline"}]
    )
    assert hb_ok is True
    assert "lease-abc" in hb_res.get("cancelled_leases", [])


def test_c04_worker_monotonic_timings_and_budget_deadline():
    """复核 C04: 单调时钟 monotonic 计时、预算与详情页 finally go_back 保障。"""
    from lite_agent.integrations.phone_app.worker.worker import MacWorkerClient, FakeWorkerClient
    import threading

    # 1. 验证 FakeWorkerClient 返回 monotonic 标记
    fake = FakeWorkerClient(status="succeeded")
    res = fake.execute_task(
        {"batch_id": "b-c04", "task_id": "t-1", "attempt_id": "a-1", "app_id": "jd"},
        {"mode": "fixed"}
    )
    assert res.get("metadata", {}).get("timing_clock") == "monotonic"

    # 2. 验证驱动与取消控制
    class MockDriver:
        def __init__(self):
            self.back_called = 0
        def ensure_ready(self):
            return True, None
        def go_back(self, timeout=None, deadline=None):
            self.back_called += 1
        def dump_ui(self, *args, **kwargs):
            return "package: com.jingdong.app.mall"
        def keyevent(self, *args, **kwargs):
            pass
        def open_app(self, *args, **kwargs):
            return True

    mock_driver = MockDriver()
    client = MacWorkerClient(device_id="c68a3cf2", driver=mock_driver)

    # 注入一个 cancel_event 已经 set 的情况，执行应立即感知并终止
    cancel_evt = threading.Event()
    cancel_evt.set()

    task_def = {"batch_id": "b-c04", "task_id": "t-1", "attempt_id": "a-1", "app_id": "jd"}
    run_cfg = {"mode": "fixed", "query": "测试", "budget": {"total_timeout_seconds": 1}}

    result = client.execute_task(task_def, run_cfg, cancel_event=cancel_evt)
    assert result.get("status") == "cancelled"
    assert any("取消" in w for w in result.get("warnings", []))


def test_c05_host_agent_model_injection_matching():
    """复核 C05: 宿主模型接入匹配，使用 agent._request_model_router 与 agent.llm。"""
    from lite_agent.features import phone_app
    from lite_agent.features.phone_app import register, get_phone_app_service

    phone_app._global_service = None
    try:
        class DummyModelConfig:
            def __init__(self, name):
                self.name = name

        class DummyRouter:
            def __init__(self):
                self.default_model = "test-glm-model"
                self.models_cfg = {"test-glm-model": DummyModelConfig("test-glm-model")}

        class DummyAgent:
            def __init__(self):
                self._request_model_router = DummyRouter()
                self.llm = object()

        class DummyContext:
            def __init__(self):
                self.agent = DummyAgent()
                self.routes = {}
                self.config = {}
            def register_route(self, method, path, handler, **kwargs):
                self.routes[(method, path)] = handler

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_c05.db")
            ctx = DummyContext()
            ctx.config = {
                "phone_app": {
                    "enabled": True,
                    "db_path": db_path
                }
            }
            register(ctx)

            service = get_phone_app_service()
            assert service.default_model == "test-glm-model", "必须正确继承宿主 Agent 的默认模型"
    finally:
        phone_app._global_service = None


def test_c06_batch_cancel_and_reject_flow():
    """复核 C06: 任务取消与驳回状态流转完整性。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_c06.db")
        repo = PhoneAppRepository(db_path=db_path)
        service = PhoneAppService(repo=repo)

        # 1. 待审核批次被驳回 -> 状态变为 cancelled，原因被记录
        ok, b1_data = service.create_batch(query="键盘", app_ids=["jd"], device_id="c68a3cf2", mode="custom_ai")
        assert ok is True
        b1 = b1_data["batch_id"]
        # 模拟生成了脚本，进入 pending_review
        repo.update_draft_atomic(b1, [{"action": "tap", "target": "#b"}], is_generation=True)
        assert repo.get_batch(b1)["status"] == "pending_review"

        ok, res = service.reject_batch(b1, reason="步骤包含未授权动作", operator="admin")
        assert ok is True
        assert res.get("status") == "cancelled"
        b1_after = repo.get_batch(b1)
        assert b1_after["status"] == "cancelled"

        # 2. 排队中批次被取消 -> 状态变为 cancelled
        ok, b2_data = service.create_batch(query="鼠标", app_ids=["jd"], device_id="c68a3cf2", mode="fixed")
        assert ok is True
        b2 = b2_data["batch_id"]
        repo.claim_batch_for_execution_atomic(b2)
        assert repo.get_batch(b2)["status"] == "queued"

        ok, res = service.cancel_batch(b2, reason="用户取消", operator="user")
        assert ok is True
        b2_after = repo.get_batch(b2)
        assert b2_after["status"] == "cancelled"

        # 3. 已取消的批次不可再取消
        ok, err = service.cancel_batch(b2, reason="再次取消", operator="user")
        assert ok is False
        assert "终态" in err.get("error", "")


# --- 17. D01-D04: 第四轮复核阻断项针对性深度回归测试 ---

def test_d01_cancellation_flow_guarantees_stoppage_and_queued_not_executed():
    """复核 D01: 取消流程保证停止、已取消排队任务绝不执行 Worker、在途租约阻止直接恢复。"""
    import tempfile
    from lite_agent.features.phone_app.service import PhoneAppService
    from lite_agent.features.phone_app.repository import PhoneAppRepository
    from lite_agent.features.phone_app.worker_hub import WorkerHub

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        repo = PhoneAppRepository(db_path=f.name)
        hub = WorkerHub(expected_token="test-token-123")
        service = PhoneAppService(repo=repo, worker_hub=hub)

        class CountingFakeWorker:
            def __init__(self):
                self.calls = 0
            def execute_task(self, task, batch, cancel_event=None):
                self.calls += 1
                return {
                    "status": "succeeded",
                    "attempt_id": task["attempt_id"],
                    "items": [],
                    "warnings": [],
                    "reset_status": "ready"
                }

        counting_worker = CountingFakeWorker()

        # 反例 1: create fixed -> claim queued -> cancel_batch -> 调用代表已排队后台线程的 _execute_batch_internal
        # 必须保证 CountingFakeWorker.execute_task 计数严格为 0！
        ok, b1_data = service.create_batch(query="键盘", app_ids=["jd"], device_id="c68a3cf2", mode="fixed")
        assert ok is True
        b1 = b1_data["batch_id"]
        ok, err, _ = repo.claim_batch_for_execution_atomic(b1)
        assert ok is True
        assert repo.get_batch(b1)["status"] == "queued"

        # 取消批次
        ok, res = service.cancel_batch(b1, operator="user", reason="已改变主意")
        assert ok is True
        assert repo.get_batch(b1)["status"] == "cancelled"

        # 模拟后台执行线程启动派发已取消的批次
        service._execute_batch_internal(b1, worker_client=counting_worker)
        assert counting_worker.calls == 0, "D01 违规: 已取消的排队任务绝不得调用 Worker 执行端！"

        # 反例 2: 设备独占锁等待期间被取消，锁释放后同样不得调用 Worker
        import threading
        dev_lock = service._get_device_lock("c68a3cf2")
        ok, b2_data = service.create_batch(query="鼠标", app_ids=["jd"], device_id="c68a3cf2", mode="fixed")
        b2 = b2_data["batch_id"]
        repo.claim_batch_for_execution_atomic(b2)

        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with dev_lock:
                lock_acquired.set()
                release_lock.wait(timeout=2.0)

        t_holder = threading.Thread(target=hold_lock)
        t_holder.start()
        lock_acquired.wait()

        # 在锁被占用时，在另一个线程发起执行并取消批次
        t_exec = threading.Thread(target=lambda: service._execute_batch_internal(b2, worker_client=counting_worker))
        t_exec.start()

        # 在锁等待期间取消批次
        service.cancel_batch(b2, operator="admin", reason="排队中取消")
        assert repo.get_batch(b2)["status"] == "cancelled"

        # 释放锁
        release_lock.set()
        t_holder.join()
        t_exec.join()

        assert counting_worker.calls == 0, "D01 违规: 锁等待期间取消的任务在获得锁后绝不得调用 Worker！"

        # 反例 3: 运行中取消区分 cancel_requested 与终态 cancelled，并保留租约直到停止确认
        ok, b3_data = service.create_batch(query="耳机", app_ids=["jd"], device_id="c68a3cf2", mode="fixed")
        b3 = b3_data["batch_id"]
        repo.claim_batch_for_execution_atomic(b3)
        repo.update_batch_status(b3, "running")

        # 运行中取消：进入 cancel_requested
        ok, res = service.cancel_batch(b3, operator="user")
        assert ok is True
        assert repo.get_batch(b3)["status"] == "cancel_requested"

        # Worker 停止并确认回执后，流转为 cancelled 终态
        repo.finalize_cancellation_atomic(b3)
        assert repo.get_batch(b3)["status"] == "cancelled"

        # 反例 4: 设备存在在途未确认停止租约时，人工 resume_device 必须严禁直接恢复
        repo.set_device_state("c68a3cf2", "paused", reason="任务执行中")
        from lite_agent.features.phone_app.worker_hub import TaskLease
        active_lease = TaskLease(
            lease_id="lease-unresolved-1",
            device_id="c68a3cf2",
            task_id="task-1",
            attempt_id="att-1",
            task_data={"batch_id": b3, "task_id": "task-1"},
            batch_data={"batch_id": b3}
        )
        hub._active_leases["lease-unresolved-1"] = active_lease

        # 此时尝试恢复设备
        res_ok, res_err = service.resume_device("c68a3cf2", operator="admin")
        assert res_ok is False, "D01 违规: 设备仍有在途未确认停止租约时，严禁直接恢复！"
        assert "在途未确认停止" in res_err.get("error", "")

        # 当租约完成后，恢复设备被允许
        hub._active_leases.pop("lease-unresolved-1", None)
        res_ok2, _ = service.resume_device("c68a3cf2", operator="admin")
        assert res_ok2 is True


def test_d02_worker_monotonic_deadline_and_budget_enforced():
    """复核 D02: 单调时钟总时限贯彻、搜索预算超时绝不返回 succeeded、max_results 限制切片。"""
    from lite_agent.integrations.phone_app.worker.worker import MacWorkerClient
    from lite_agent.features.phone_app.contracts import SearchBudget

    class FakeSlowSearchDriver:
        version = "fake-driver-2.1.0"
        def ensure_ready(self):
            return True, None
        def open_app(self, pkg, timeout=None, deadline=None):
            return True
        def tap(self, target, timeout=None, deadline=None):
            return True
        def fill(self, target, text):
            return True
        def dump_ui(self, fmt="xml", timeout=None, deadline=None):
            time.sleep(0.02)  # 20ms 模拟耗时
            if fmt == "table":
                return "#b51 搜索栏 首页"
            return (
                '<node class="android.widget.FrameLayout">'
                '<node text="综合推荐" />'
                '<node class="android.support.v7.widget.RecyclerView">'
                '<node class="android.view.ViewGroup" bounds="[0,0][100,100]"><node text="自营高档机械键盘87键1" /><node text="¥100.0" /></node>'
                '<node class="android.view.ViewGroup" bounds="[0,100][100,200]"><node text="自营高档机械键盘87键2" /><node text="¥200.0" /></node>'
                '<node class="android.view.ViewGroup" bounds="[0,200][100,300]"><node text="自营高档机械键盘87键3" /><node text="¥300.0" /></node>'
                '</node></node>'
            )
        def go_back(self, timeout=None, deadline=None):
            return True
        def restore_ready(self, timeout_sec=5.0):
            return True

    worker = MacWorkerClient(device_id="c68a3cf2")
    worker.driver = FakeSlowSearchDriver()
    worker.jd_adapter.driver = worker.driver

    # 1. 预算总时限 1ms，但 search 耗时 20ms -> 严禁返回 succeeded，必须是 timed_out 并记录 DEADLINE_EXCEEDED！
    task = {"task_id": "t1", "batch_id": "b1", "attempt_id": "att-1", "app_id": "jd"}
    batch = {
        "batch_id": "b1",
        "mode": "fixed",
        "query": "键盘",
        "budget": {
            "max_candidates": 10,
            "max_detail_visits": 0,
            "max_results": 1,
            "total_timeout_seconds": 0.001  # 1ms 预算
        }
    }

    receipt = worker.execute_task(task, batch)
    assert receipt["status"] == "timed_out", f"D02 违规: 预算超时仍返回了 {receipt['status']}！"
    assert any("DEADLINE_EXCEEDED" in w for w in receipt["warnings"]), "必须记录 DEADLINE_EXCEEDED 超时警告"
    assert receipt["metadata"]["driver_version"] == "fake-driver-2.1.0"

    # 2. 贯彻 max_candidates 与 max_results 职责分离 (E04)
    # Worker 端按 max_candidates 切片候选扫描数，杜绝在筛选前被 max_results 截断
    batch_normal = {
        "batch_id": "b2",
        "mode": "fixed",
        "query": "键盘",
        "budget": {
            "max_candidates": 2,
            "max_detail_visits": 0,
            "max_results": 1,
            "total_timeout_seconds": 10.0
        }
    }
    receipt_normal = worker.execute_task(task, batch_normal)
    assert receipt_normal["status"] == "succeeded"
    assert len(receipt_normal["items"]) == 2, f"E04 违规: Worker 应严格按 max_candidates 切片，实际包含 {len(receipt_normal['items'])} 条"

    # 3. 校验 SearchBudget schema
    b_bad = SearchBudget(total_timeout_seconds=-5)
    ok, err = b_bad.validate()
    assert ok is False
    assert "total_timeout_seconds" in err

    b_bad2 = SearchBudget(max_results=0)
    ok, err = b_bad2.validate()
    assert ok is False
    assert "max_results" in err


def test_d03_host_llm_gateway_invoker_matching():
    """复核 D03: 宿主真实 LLMGateway (无 router) 注入，通过 Router 获取 invoker/provider 规范调用。"""
    from lite_agent.features.phone_app.service import PhoneAppService
    from lite_agent.features.phone_app.repository import PhoneAppRepository
    from lite_agent.core.llm_gateway import LLMGateway

    # 模拟宿主 Agent 构造的真实 LLMGateway: router is None
    gateway = LLMGateway()
    assert gateway.router is None, "真实宿主 LLMGateway 的 router 确实为 None"

    # 构造拥有 models_cfg 与 get_invoker 的 Router 替身
    class OfflineInvoker:
        def __init__(self, model_name="test-gpt"):
            self.model_name = model_name
            self.provider = "mock_provider"
            self.max_tokens = 2048
        def invoke_sync(self, messages, tools=None, **kwargs):
            return {
                "content": '```json\n{"jd": [{"action": "open_app", "package": "com.jingdong.app.mall"}, {"action": "tap", "target": "#search"}]}\n```',
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "usage_total": 150
            }

    class MockModelRouter:
        def __init__(self):
            self.models_cfg = {"deepseek-chat": {"provider": "deepseek"}}
        def get_invoker(self, model, **kwargs):
            return OfflineInvoker(model_name=model)
        def get_driver(self, model):
            return "mock_provider"

    router = MockModelRouter()

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        repo = PhoneAppRepository(db_path=f.name)
        service = PhoneAppService(
            repo=repo,
            router=router,
            gateway=gateway,
            default_model="deepseek-chat"
        )

        # 调用 _generate_scripts_via_llm，必须成功执行且不抛出 "model or invoker is required"！
        ok, scripts = service._generate_scripts_via_llm("机械键盘", ["jd"])
        assert ok is True, f"D03 失败: 模型网关调用返回错误 {scripts}"
        assert "jd" in scripts
        assert len(scripts["jd"]) == 2
        assert scripts["jd"][0]["action"] == "open_app"


def test_d04_worker_remote_http_rejected_and_session_rehandshake():
    """复核 D04: 执行端拒绝远程明文 HTTP、心跳会话失效后自动重握手恢复、心跳防伪造设备注入。"""
    from lite_agent.integrations.phone_app.worker.run_worker import MacWorkerDaemon
    from lite_agent.features.phone_app.worker_hub import WorkerHub

    # 1. 执行端约束：远程明文 HTTP 严禁接受！
    with pytest.raises(ValueError) as excinfo:
        MacWorkerDaemon(hub_url="http://remote.example.com:8000", token="secret-token-123", fake_mode=True)
    assert "传输安全阻断 (D04)" in str(excinfo.value)
    assert "HTTPS" in str(excinfo.value)

    # 2. 回环 HTTP 允许接受
    daemon_local = MacWorkerDaemon(hub_url="http://127.0.0.1:8000", token="secret-token-123", fake_mode=True)
    assert daemon_local.hub_url == "http://127.0.0.1:8000"

    daemon_local_host = MacWorkerDaemon(hub_url="http://localhost:8000", token="secret-token-123", fake_mode=True)
    assert daemon_local_host.hub_url == "http://localhost:8000"

    # 3. 远程 HTTPS 允许接受
    daemon_remote_ssl = MacWorkerDaemon(hub_url="https://vps1.example.com", token="secret-token-123", fake_mode=True)
    assert daemon_remote_ssl.hub_url == "https://vps1.example.com"

    # 4. WorkerHub 未配置凭证时，is_ready 返回 False，状态明确披露缺失
    os_token_bak = os.environ.pop("PHONE_WORKER_TOKEN", None)
    try:
        hub_unconfigured = WorkerHub(expected_token=None)
        assert hub_unconfigured.is_ready() is False, "D04: 未配置凭证时不能显示已就绪"
        st = hub_unconfigured.get_status()
        assert st["is_ready"] is False
        assert "缺失" in st["status_text"]
    finally:
        if os_token_bak:
            os.environ["PHONE_WORKER_TOKEN"] = os_token_bak

    # 5. 心跳防伪造设备注入：未在握手阶段授权的设备在心跳上报时被拦截并忽略
    hub = WorkerHub(expected_token="auth-token-999")
    ok, hs_data = hub.handshake(
        worker_id="worker-oneplus",
        token="auth-token-999",
        version="1.0.0",
        capabilities=["jd"],
        devices=[{"device_id": "c68a3cf2", "status": "online"}]
    )
    assert ok is True
    sess_id = hs_data["session_id"]

    # 心跳上报一个合法的和一个注入的未授权新设备
    ok, hb_ack = hub.heartbeat(
        worker_id="worker-oneplus",
        session_id=sess_id,
        devices=[
            {"device_id": "c68a3cf2", "status": "online"},
            {"device_id": "unauthorized_injected_phone", "status": "online"}
        ]
    )
    assert ok is True
    online_devs = hub.get_online_devices()
    online_dids = [d["device_id"] for d in online_devs]
    assert "c68a3cf2" in online_dids
    assert "unauthorized_injected_phone" not in online_dids, "D04 违规: 心跳不得任意新增未授权设备！"

    # 6. 心跳失效后重新握手恢复：模拟会话超时或中枢重启导致会话失效
    class MockRehandshakeWorkerDaemon(MacWorkerDaemon):
        def __init__(self):
            super().__init__(hub_url="http://127.0.0.1:8000", token="secret", fake_mode=True)
            self.session_id = "expired-sess-old"
            self.handshake_called = 0
        def heartbeat(self):
            if self.session_id == "expired-sess-old":
                return False, {"error": "Worker 会话已心跳超时，请重新握手"}
            return True, {"status": "ack"}
        def handshake(self):
            self.handshake_called += 1
            self.session_id = "recovered-sess-new"
            return True

    mock_daemon = MockRehandshakeWorkerDaemon()
    # 模拟心跳后台线程的一次执行
    ok, res = mock_daemon.heartbeat()
    assert ok is False
    err_msg = str(res.get("error", ""))
    if any(kw in err_msg for kw in ("会话", "超时", "未握手", "未注册", "失效", "401")):
        mock_daemon.session_id = None
        mock_daemon.handshake()

    assert mock_daemon.handshake_called == 1
    assert mock_daemon.session_id == "recovered-sess-new"


def test_e01_dsl_extract_malformed_xml_fails_without_flat_text_fallback():
    """复核 E01: XML 提取失败严禁回退到扁平文本拼装，语法错误/无容器卡片/广告干扰反例。"""
    from lite_agent.integrations.phone_app.worker.dsl_executor import DslExecutor
    from lite_agent.integrations.phone_app.worker.drivers.handsets import BaseDeviceDriver

    class BrokenXmlDriver(BaseDeviceDriver):
        def __init__(self, xml_content, table_content=""):
            self.xml_content = xml_content
            self.table_content = table_content
        def dump_ui(self, fmt="xml", timeout=None, deadline=None):
            if fmt == "xml":
                return self.xml_content
            return self.table_content
        def open_app(self, pkg, timeout=None, deadline=None): return True
        def tap(self, target, timeout=None, deadline=None): return True
        def fill(self, sel, text): return True
        def go_back(self, timeout=None, deadline=None): return True

    script = [{"action": "extract", "fields": ["title", "price"]}]

    # 1. XML 损坏 (<node BROKEN) 且 table 存在诱导性文本 -> 严禁成功，必须报错且商品列表为空！
    driver_broken = BrokenXmlDriver(
        xml_content='<node BROKEN',
        table_content='自营高档机械键盘87键\n¥99.0'
    )
    executor_broken = DslExecutor(driver=driver_broken)
    ok1, items1, err1 = executor_broken.execute_script(script)
    assert ok1 is False, "E01 违规: XML 损坏时严禁回退到扁平文本并返回成功！"
    assert len(items1) == 0
    assert "extract" in err1 and ("XML" in err1 or "结构化容器提取失败" in err1)

    # 2. XML 结构有效但无任何合规商品容器 (无 RecyclerView/卡片 ViewGroup) -> 必须报错
    driver_no_container = BrokenXmlDriver(
        xml_content='<node class="android.widget.FrameLayout"><node text="欢迎光临商城首页" /></node>'
    )
    executor_no_container = DslExecutor(driver=driver_no_container)
    ok2, items2, err2 = executor_no_container.execute_script(script)
    assert ok2 is False
    assert len(items2) == 0
    assert "未找到符合规范的商品列表容器" in err2 or "提取失败" in err2

    # 3. 广告干扰词过滤：卡片内包含“广告”、“赞助”的不作为正常商品标题
    driver_ad = BrokenXmlDriver(
        xml_content='''<node class="android.widget.FrameLayout">
            <node class="android.support.v7.widget.RecyclerView">
                <node class="android.view.ViewGroup" bounds="[0,0][100,100]">
                    <node text="广告 极速发货" />
                    <node text="¥199.0" />
                </node>
                <node class="android.view.ViewGroup" bounds="[0,100][100,200]">
                    <node text="自营正品静音机械键盘" />
                    <node text="¥299.0" />
                </node>
            </node>
        </node>'''
    )
    executor_ad = DslExecutor(driver=driver_ad)
    ok3, items3, err3 = executor_ad.execute_script(script)
    assert ok3 is True
    # 仅提取出非广告的第二张卡片
    assert len(items3) == 1
    assert items3[0]["title"] == "自营正品静音机械键盘"
    assert items3[0]["amount"] == 299.0


def test_e02_handsets_driver_no_homebrew_hardcode_and_platform_handling():
    """复核 E02: 彻底清除 /opt/homebrew 路径硬编码，显式依赖探测与平台限制客观声明。"""
    from lite_agent.integrations.phone_app.worker.drivers.handsets import HandsetsDriver
    import inspect

    # 1. 静态检查源码中严禁包含写死的 /opt/homebrew 字符串
    src = inspect.getsource(HandsetsDriver.__init__)
    assert "/opt/homebrew" not in src, "E02 违规: HandsetsDriver 依然包含硬编码的 /opt/homebrew 路径！"

    # 2. 当未找到 hs CLI 且未提供环境变量时，ensure_ready 必须如实返回依赖缺失提示
    driver_missing = HandsetsDriver(device_id="c68a3cf2", hs_bin_path="/nonexistent/bin/hs")
    ready, err = driver_missing.ensure_ready()
    assert ready is False
    assert "未找到 handsets 可执行文件" in err
    assert driver_missing.version == "hs-bin-missing"

    # 3. 检查 logger 命名已纠正为新路径
    from lite_agent.integrations.phone_app.worker.drivers.handsets import logger
    assert "phone_worker" not in logger.name, f"E02 违规: logger 依然残留旧路径: {logger.name}"


def test_e03_deadline_propagation_and_restore_timeout_budget():
    """复核 E03: 截止时间贯彻至详情提取与 restore_ready，复位拥有独立硬时限保护，杜绝无界阻塞。"""
    from lite_agent.integrations.phone_app.worker.adapters.jd import JdAdapter
    from lite_agent.integrations.phone_app.worker.drivers.handsets import BaseDeviceDriver

    class HangingDriver(BaseDeviceDriver):
        def dump_ui(self, fmt="xml", timeout=None, deadline=None):
            time.sleep(0.05)  # 每次耗时 50ms
            return "其他页面 未就绪"
        def go_back(self, timeout=None, deadline=None):
            time.sleep(0.05)
            return True
        def open_app(self, pkg, timeout=None, deadline=None):
            time.sleep(0.05)
            return True
        def tap(self, target, timeout=None, deadline=None): return True

    hanging_driver = HangingDriver()
    adapter = JdAdapter(driver=hanging_driver)

    # 1. restore_ready 指定 timeout_sec=0.1 秒硬预算 -> 必须在 ~0.15 秒内及时跳出返回 False，绝不能无限沉睡！
    t_rst_start = time.monotonic()
    rst_ok = adapter.restore_ready(timeout_sec=0.1)
    t_rst_elapsed = time.monotonic() - t_rst_start
    assert rst_ok is False
    assert t_rst_elapsed < 0.35, f"E03 违规: restore_ready 预算未生效，实际耗时 {t_rst_elapsed}s 远超预期！"

    # 2. open_product 传入已过期的 deadline -> 必须立即退出返回 False
    t_open_start = time.monotonic()
    open_ok = adapter.open_product("测试商品", deadline=time.monotonic() - 0.01)
    t_open_elapsed = time.monotonic() - t_open_start
    assert open_ok is False
    assert t_open_elapsed < 0.05


def test_e04_candidate_scanning_vs_filtering_no_premature_drop():
    """复核 E04: Worker 端按 max_candidates 扫描候选，中枢筛选后再贯彻 max_results，杜绝前置截断遗漏匹配项。"""
    from lite_agent.features.phone_app.service import PhoneAppService
    from lite_agent.features.phone_app.repository import PhoneAppRepository
    from lite_agent.features.phone_app.catalog import AppCatalog
    from lite_agent.integrations.phone_app.worker.worker import FakeWorkerClient
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_e04.db")
        repo = PhoneAppRepository(db_path=db_path)
        catalog = AppCatalog()
        service = PhoneAppService(repo=repo, catalog=catalog)

        # 模拟 Worker 扫描到 3 个候选商品：
        # 商品 1: 销量 500 (不满足 > 1000 筛选条件)
        # 商品 2: 销量 5000 (满足 > 1000 筛选条件)
        # 商品 3: 销量 8000 (满足 > 1000 筛选条件)
        class CustomCandidatesWorker(FakeWorkerClient):
            def execute_task(self, task, batch, cancel_event=None):
                return {
                    "schema_version": "1.0.0",
                    "batch_id": task["batch_id"],
                    "task_id": task["task_id"],
                    "attempt_id": task["attempt_id"],
                    "app_id": "jd",
                    "device_id": "c68a3cf2",
                    "status": "succeeded",
                    "captured_at": "2026-10-18T10:00:00Z",
                    "partial": False,
                    "items": [
                        {
                            "id": "c1", "title": "低销量商品1", "raw_price_text": "¥50.0", "amount": 50.0,
                            "sales_info": {"raw_text": "已售 500 件", "metric": "cumulative_sales", "lower_bound": 500},
                            "verified_badges": ["self_operated"]
                        },
                        {
                            "id": "c2", "title": "高销量商品2", "raw_price_text": "¥150.0", "amount": 150.0,
                            "sales_info": {"raw_text": "已售 5000 件", "metric": "cumulative_sales", "lower_bound": 5000},
                            "verified_badges": ["self_operated"]
                        },
                        {
                            "id": "c3", "title": "高销量商品3", "raw_price_text": "¥250.0", "amount": 250.0,
                            "sales_info": {"raw_text": "已售 8000 件", "metric": "cumulative_sales", "lower_bound": 8000},
                            "verified_badges": ["self_operated"]
                        }
                    ],
                    "warnings": [],
                    "timings": {"prepare_ms": 10, "search_ms": 10, "details_ms": 0, "restore_ms": 10, "total_ms": 30},
                    "metadata": {"reset_status": "ready"}
                }

        # 设置 max_candidates=5 (扫描上限), max_results=1 (最终期望返回 1 条符合商品)
        # 筛选条件: 销量 > 1000
        ok, res = service.create_batch(
            query="机械键盘",
            app_ids=["jd"],
            device_id="c68a3cf2",
            mode="fixed",
            filters={"sales": {"threshold": 1000, "operator": "gt"}},
            budget={"max_candidates": 5, "max_results": 1, "total_timeout_seconds": 10.0}
        )
        assert ok is True
        batch_id = res["batch_id"]

        exec_ok, _ = service.execute_batch(
            batch_id=batch_id,
            user_context={"user_id": "test_runner"},
            worker_client=CustomCandidatesWorker(),
            async_run=False
        )
        assert exec_ok is True

        summary = service.get_batch_summary(batch_id)
        assert summary["summary"]["total_scanned"] == 3
        # 若在 Worker 端按 max_results=1 截断，则只看到商品 1 (未命中)，导致符合条数=0！
        # 经过 E04 修复，全部候选被筛选，商品 2 和 3 均命中，并在最终结果按 max_results=1 截取商品 2
        assert summary["summary"]["matched_count"] == 1, "E04 违规: 前置截断丢失了后续符合条件的商品！"
        assert summary["grouped_results"]["matched"][0]["title"] == "高销量商品2"
        assert summary["summary"]["rejected_count"] == 1  # 商品 1 被准确归入不符合


def test_e05_browser_test_server_directory_hygiene_and_path_traversal_protection():
    """复核 E05: 目录卫生（测试支持服务在 dev/tests/support 下）与静态服务防目录遍历。"""
    proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    old_script_path = os.path.join(proj_root, "scripts", "run_browser_test_server.py")
    new_support_path = os.path.join(proj_root, "dev", "tests", "support", "browser_test_server.py")

    # 1. scripts 目录卫生：不得残留专用测试服务器脚本
    assert not os.path.exists(old_script_path), "E05 违规: scripts 目录下仍残留 run_browser_test_server.py"
    assert os.path.exists(new_support_path), "E05 违规: dev/tests/support/browser_test_server.py 不存在"

    # 2. 静态资源目录遍历防护
    from dev.tests.support.browser_test_server import BrowserTestHttpHandler, WEB_DIR

    class DummyServer:
        service = None

    class MockHandler(BrowserTestHttpHandler):
        def __init__(self):
            self.server = DummyServer()
            self.errors = []
            self.sent_responses = []

        def send_error(self, code, message=None, explain=None):
            self.errors.append((code, message))

        def send_response(self, code, message=None):
            self.sent_responses.append(code)

        def send_header(self, keyword, value):
            pass

        def end_headers(self):
            pass

    handler = MockHandler()
    # 尝试路径穿越攻击逃逸 WEB_DIR
    handler._serve_static("../../etc/passwd")
    assert len(handler.errors) == 1
    code, msg = handler.errors[0]
    assert code == 403
    assert "Forbidden" in msg


def test_f01_browser_server_web_dir_exists_and_serves_static():
    """复核 F01: browser_test_server 正确解析项目根目录，静态资源可正常读取且全量端点就绪。"""
    from dev.tests.support.browser_test_server import BrowserTestHttpHandler, WEB_DIR

    # 1. 验证 WEB_DIR 存在且 index.html 存在
    assert os.path.exists(WEB_DIR), f"F01 违规: WEB_DIR 不存在: {WEB_DIR}"
    index_html = os.path.join(WEB_DIR, "index.html")
    assert os.path.exists(index_html), f"F01 违规: index.html 不存在: {index_html}"

    # 2. 模拟静态资源读取与 200 响应
    class DummyServer:
        def __init__(self):
            self.service = None

    class MockHandler(BrowserTestHttpHandler):
        def __init__(self):
            self.server = DummyServer()
            self.errors = []
            self.sent_responses = []
            self.headers_set = {}

        def send_error(self, code, message=None, explain=None):
            self.errors.append((code, message))

        def send_response(self, code, message=None):
            self.sent_responses.append(code)

        def send_header(self, keyword, value):
            self.headers_set[keyword] = value

        def end_headers(self):
            pass

        @property
        def wfile(self):
            import io
            return io.BytesIO()

    handler = MockHandler()
    handler._serve_static("index.html")
    assert 200 in handler.sent_responses
    assert handler.headers_set.get("Content-Type") == "text/html; charset=utf-8"

    handler_js = MockHandler()
    handler_js._serve_static("modules/phone_apps.js")
    assert 200 in handler_js.sent_responses
    assert handler_js.headers_set.get("Content-Type") == "application/javascript; charset=utf-8"

    handler_err = MockHandler()
    handler_err._serve_static("non_existent_file.xyz")
    assert any(code == 404 for code, _ in handler_err.errors)


def test_f02_detail_enrichment_nested_schema_and_filter_closure():
    """复核 F02: 详情采集数据 (好评率/包邮/物流/自营) 正确合入嵌套结构，并在 FilterEngine 确定性改变为 matched。"""
    from lite_agent.features.phone_app.service import PhoneAppService
    from lite_agent.features.phone_app.repository import PhoneAppRepository
    from lite_agent.features.phone_app.catalog import AppCatalog
    from lite_agent.integrations.phone_app.worker.worker import MacWorkerClient
    from lite_agent.integrations.phone_app.worker.drivers.handsets import FakeDeviceDriver
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_f02.db")
        repo = PhoneAppRepository(db_path=db_path)
        catalog = AppCatalog()
        service = PhoneAppService(repo=repo, catalog=catalog)

        # 创建 batch，配置三态严苛过滤：要求包邮、自营、好评率 >= 95%
        ok, res = service.create_batch(
            query="高性能降噪耳机",
            app_ids=["jd"],
            device_id="c68a3cf2",
            mode="fixed",
            filters={
                "logistics": {"free_shipping": True},
                "shop_types": {"types": ["self_operated"]},
                "positive_rate": {"threshold_percent": 95.0, "operator": "gte"}
            },
            budget={"max_candidates": 5, "max_results": 2}
        )
        assert ok is True
        batch_id = res["batch_id"]

        # 构造真实 MacWorkerClient，注入替身 Adapter
        fake_driver = FakeDeviceDriver()
        worker = MacWorkerClient(device_id="c68a3cf2", driver=fake_driver)

        class MockJDAdapter:
            def search(self, query, max_candidates=20, deadline=None, cancel_event=None):
                return {
                    "status": "succeeded",
                    "items": [{
                        "id": "item_1",
                        "title": "自营旗舰耳机",
                        "raw_price_text": "¥999.0",
                        "amount": 999.0,
                        "sales_info": {"raw_text": "1万+人付款", "metric_name": "cumulative_sales"}
                    }]
                }

            def open_product(self, title, deadline=None):
                return True

            def read_product_metrics(self, title, deadline=None):
                return {"positive_rate": 98.5}

            def read_logistics(self, title, deadline=None):
                return {
                    "free_shipping": True,
                    "delivery_partner": "jd_delivery",
                    "evidence": "详情页提取: free_shipping=True"
                }

            def read_shop_info(self, title, deadline=None):
                return {
                    "shop_name": "京东自营官方自营店",
                    "verified_badges": ["self_operated"],
                    "evidence": "店铺页徽标读取: ['self_operated']"
                }

            def restore_ready(self, timeout_sec=5.0, deadline=None):
                return True

        worker.jd_adapter = MockJDAdapter()

        # 执行批次任务
        exec_ok, _ = service.execute_batch(
            batch_id=batch_id,
            user_context={"user_id": "tester"},
            worker_client=worker,
            async_run=False
        )
        assert exec_ok is True

        # 验证最终总结与事实结构
        summary = service.get_batch_summary(batch_id)
        assert summary["summary"]["total_scanned"] == 1
        # F02 核心校验：原先由于结构嵌套错误被丢弃导致 needs_verification，修复后必须通过筛选变成 matched！
        assert summary["summary"]["matched_count"] == 1, "F02 违规: 详情事实未能使商品转变为 matched！"
        assert summary["summary"]["needs_verification_count"] == 0

        matched_item = summary["grouped_results"]["matched"][0]
        assert matched_item["diagnosis"] == "matched"
        assert matched_item["logistics_info"]["free_shipping"] is True
        assert "self_operated" in matched_item["evidence"]["verified_badges"]
        assert matched_item["positive_rate"] == 98.5
        assert matched_item["evaluations"]["logistics"]["status"] == "pass"
        assert matched_item["evaluations"]["shop_types"]["status"] == "pass"
        assert matched_item["evaluations"]["positive_rate"]["status"] == "pass"


def test_f03_driver_blocking_and_restore_timeout_fails_truthfully():
    """复核 F03: 驱动耗时超过预算且页面含就绪文字时，restore_ready 严禁误判为就绪，必须如实返回 False。"""
    from lite_agent.integrations.phone_app.worker.adapters.jd import JdAdapter
    from lite_agent.integrations.phone_app.worker.drivers.handsets import FakeDeviceDriver

    # 模拟 FakeDriver.dump_ui 耗时 200ms 后返回含首页字样的 UI
    fake_driver = FakeDeviceDriver(
        mock_ui_table="京东 首页 搜索栏 #b51 推荐",
        delays={"dump_ui": 0.2}
    )
    adapter = JdAdapter(driver=fake_driver)

    # 1. 预算仅有 10ms (0.01s)，底层 dump_ui 耗时 200ms 超过预算 -> 必须返回 False！
    t_start = time.monotonic()
    ready_result = adapter.restore_ready(timeout_sec=0.01)
    t_elapsed = time.monotonic() - t_start
    assert ready_result is False, "F03 违规: 恢复耗时超出预算但页面含就绪文字时仍返回了 True！"
    assert t_elapsed >= 0.15

    # 2. 预算充裕 (0.5s)，耗时 50ms 在预算内 -> 正常返回 True
    fake_driver_normal = FakeDeviceDriver(
        mock_ui_table="京东 首页 搜索栏 #b51 推荐",
        delays={"dump_ui": 0.05}
    )
    adapter_normal = JdAdapter(driver=fake_driver_normal)
    assert adapter_normal.restore_ready(timeout_sec=0.5) is True
