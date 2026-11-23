"""
Mac 本地真实设备执行端 (lite_agent.integrations.phone_app.worker.worker).
严格遵循第 15.1 节与 R01 / R07 / B01 / B04 / B07 要求：
- 真实调用 HandsetsDriver 与 JdAdapter 操作连接的物理手机。
- 基于不可变模式分流：fixed 模式严禁执行任意脚本；custom_ai 模式仅执行获批受限 DSL。
- 实测分段计时 (prepare_ms, search_ms, details_ms, restore_ms, total_ms)，拒绝假冒估算。
- 设备离线或命令失败时如实报告。
"""

from __future__ import annotations
import time
import logging
import threading
from typing import Dict, Any, Optional, List, Tuple
from lite_agent.integrations.phone_app.worker.drivers.handsets import (
    HandsetsDriver,
    BaseDeviceDriver,
    classify_ready_error,
    format_ready_reason,
)
from lite_agent.integrations.phone_app.worker.adapters.jd import JdAdapter
from lite_agent.integrations.phone_app.worker.dsl_executor import DslExecutor

logger = logging.getLogger("integrations.phone_app.worker")


class MacWorkerClient:
    """Mac 端真实物理设备工作端。"""

    def __init__(
        self,
        device_id: str = "c68a3cf2",
        driver: Optional[BaseDeviceDriver] = None
    ):
        self.device_id = device_id
        self.driver = driver or HandsetsDriver(device_id=device_id)
        self.jd_adapter = JdAdapter(self.driver)
        self.dsl_executor = DslExecutor(self.driver)

    def check_online(self) -> Tuple[bool, str, str]:
        """返回 (是否就绪, 稳定错误码, 压缩后的原始原因)。

        C03: 离线原因必须可追溯到具体环节 (hs 缺失 / adb 未识别 / adb 未授权 /
        hs 守护进程未绑定 / 驱动启动失败)，不得只上报一个 offline。
        """
        ok, reason = self.driver.ensure_ready()
        if ok:
            return True, "", ""
        return False, classify_ready_error(reason), format_ready_reason(reason)

    def is_online(self) -> bool:
        return self.check_online()[0]

    def execute_task(
        self,
        task: Dict[str, Any],
        batch: Dict[str, Any],
        cancel_event: Optional[threading.Event] = None
    ) -> Dict[str, Any]:
        """在真实手机上执行任务并生成权威回执 (C03 & C04: 支持取消信号与单调时钟实测)。"""
        t_start = time.monotonic()
        aid = task["app_id"]
        query = batch["query"]
        mode = batch.get("mode", "fixed")
        budget = batch.get("budget", {}) or {}
        max_candidates = int(budget.get("max_candidates") if budget.get("max_candidates") is not None else 20)
        max_detail_visits = int(budget.get("max_detail_visits") if budget.get("max_detail_visits") is not None else 10)
        max_results = int(budget.get("max_results") if budget.get("max_results") is not None else 5)
        timeout_seconds = float(budget.get("total_timeout_seconds", 120.0))
        deadline = t_start + timeout_seconds

        items = []
        warnings = []
        actual_status = "succeeded"
        t_search_ms = 0
        t_details_ms = 0
        t_prep_start = 0.0
        t_prep_end = 0.0

        # 0. 准备前检查取消与时限 (D02)
        if cancel_event and cancel_event.is_set():
            actual_status = "cancelled"
            warnings.append("执行前收到任务取消信号")
        elif time.monotonic() >= deadline:
            actual_status = "timed_out"
            warnings.append("执行超出总时限预算 (DEADLINE_EXCEEDED)")

        # 1. 验证设备驱动在线状态
        if actual_status == "succeeded":
            t_prep_start = time.monotonic()
            ready, err = self.driver.ensure_ready()
            t_prep_end = time.monotonic()
            if not ready:
                raise RuntimeError(f"设备 '{self.device_id}' 未就绪或离线: {err}")
            if time.monotonic() >= deadline:
                actual_status = "timed_out"
                warnings.append("执行超出总时限预算 (DEADLINE_EXCEEDED)")

        # 2. 依据不可变模式严格路由 (B01)
        if actual_status == "succeeded":
            if cancel_event and cancel_event.is_set():
                actual_status = "cancelled"
                warnings.append("进入搜索阶段前收到取消信号")
            elif mode == "fixed":
                # 固定搜索模式：无论 scripts 字典是否存在，绝对禁止执行自定义脚本！
                t_search_start = time.monotonic()
                if aid == "jd":
                    res = self.jd_adapter.search(
                        query=query,
                        max_candidates=max_candidates,
                        deadline=deadline,
                        cancel_event=cancel_event
                    )
                    raw_st = res.get("status", "failed")
                    if raw_st in ("timeout", "timed_out") or time.monotonic() >= deadline:
                        actual_status = "timed_out"
                        warnings.append("执行超出总时限预算 (DEADLINE_EXCEEDED)")
                    elif raw_st == "cancelled" or (cancel_event and cancel_event.is_set()):
                        actual_status = "cancelled"
                        warnings.append("搜索阶段收到取消信号")
                    else:
                        actual_status = raw_st
                    items = res.get("items", [])
                else:
                    actual_status = "failed"
                    warnings.append(f"未支持的固定 App 适配器: '{aid}'")
                t_search_end = time.monotonic()
                t_search_ms = int((t_search_end - t_search_start) * 1000)

                # 3. 详情补充与真实采集 (C03 & C04: 预算上限、取消中断与 finally 导航复位)
                if max_detail_visits > 0 and aid == "jd" and items and actual_status == "succeeded":
                    t_det_start = time.monotonic()
                    for it in items[:max_detail_visits]:
                        if cancel_event and cancel_event.is_set():
                            warnings.append("收到取消信号，提前终止详情补充")
                            actual_status = "cancelled"
                            break
                        if time.monotonic() >= deadline:
                            warnings.append("执行超出总时限预算 (DEADLINE_EXCEEDED)")
                            break
                        title = it.get("title")
                        if title:
                            try:
                                if self.jd_adapter.open_product(title, deadline=deadline):
                                    metrics = self.jd_adapter.read_product_metrics(title, deadline=deadline)
                                    if metrics.get("positive_rate") is not None:
                                        it["positive_rate"] = metrics["positive_rate"]
                                    # 丰富物流与店铺事实 (F02: 严格合入正确嵌套位置 logistics_info 与 evidence)
                                    logistics = self.jd_adapter.read_logistics(title, deadline=deadline)
                                    if "logistics_info" not in it or not isinstance(it["logistics_info"], dict):
                                        it["logistics_info"] = {}
                                    if logistics:
                                        it["logistics_info"].update(logistics)
                                    if logistics.get("free_shipping") is not None:
                                        it["free_shipping"] = logistics["free_shipping"]
                                        it["logistics_info"]["free_shipping"] = logistics["free_shipping"]
                                    if logistics.get("delivery_partner"):
                                        it["delivery_partner"] = logistics["delivery_partner"]
                                        it["logistics_info"]["delivery_partner"] = logistics["delivery_partner"]

                                    shop_info = self.jd_adapter.read_shop_info(title, deadline=deadline)
                                    if "evidence" not in it or not isinstance(it["evidence"], dict):
                                        it["evidence"] = {}
                                    if "verified_badges" not in it["evidence"]:
                                        it["evidence"]["verified_badges"] = []
                                    if shop_info.get("verified_badges"):
                                        for b in shop_info["verified_badges"]:
                                            if b not in it["evidence"]["verified_badges"]:
                                                it["evidence"]["verified_badges"].append(b)
                                    if shop_info.get("shop_name"):
                                        it["shop_name"] = shop_info["shop_name"]
                                    if shop_info.get("shop_type"):
                                        it["shop_type"] = shop_info["shop_type"]
                                    it["verified_badges"] = list(it["evidence"]["verified_badges"])
                            except Exception as d_err:
                                warnings.append(f"详情补充提取异常 [{title[:10]}]: {d_err}")
                            finally:
                                # 严格保障每步详情导航复位回到搜索结果页 (C04 & F03)
                                if aid == "jd" and hasattr(self.jd_adapter, "restore_to_product_list"):
                                    self.jd_adapter.restore_to_product_list(deadline=deadline)
                                else:
                                    self.driver.go_back(deadline=deadline)
                                rem = deadline - time.monotonic()
                                if rem > 0:
                                    time.sleep(min(0.3, rem))

                    t_det_end = time.monotonic()
                    t_details_ms = int((t_det_end - t_det_start) * 1000)

            elif mode == "custom_ai":
                # 自定义 AI 模式：仅执行获批的 DSL 脚本
                scripts = batch.get("scripts", {})
                app_script = scripts.get(aid)
                if not app_script:
                    actual_status = "failed"
                    warnings.append(f"自定义模式 App '{aid}' 缺少获批脚本")
                else:
                    t_search_start = time.monotonic()
                    ok, extracted, err_msg = self.dsl_executor.execute_script(
                        app_script,
                        cancel_event=cancel_event,
                        deadline=deadline
                    )
                    t_search_end = time.monotonic()
                    t_search_ms = int((t_search_end - t_search_start) * 1000)
                    if not ok:
                        if cancel_event and cancel_event.is_set():
                            actual_status = "cancelled"
                        elif time.monotonic() >= deadline or "DEADLINE_EXCEEDED" in str(err_msg):
                            actual_status = "timed_out"
                            warnings.append("执行超出总时限预算 (DEADLINE_EXCEEDED)")
                        else:
                            actual_status = "failed"
                        warnings.append(f"DSL 脚本执行失败: {err_msg}")
                    else:
                        items = extracted
            else:
                actual_status = "failed"
                warnings.append(f"未知批次执行模式: '{mode}'")

        # 4. 复位回到就绪状态 (门禁检查，R07 & E03: 独立保护预算与安全截止时间)
        t_rst_start = time.monotonic()
        reset_ok = self.jd_adapter.restore_ready(timeout_sec=12.0, deadline=time.monotonic() + 12.0) if aid == "jd" else True
        t_rst_end = time.monotonic()
        reset_status = "ready" if reset_ok else "failed"
        t_restore_ms = int((t_rst_end - t_rst_start) * 1000)

        total_ms = int((time.monotonic() - t_start) * 1000)
        t_prepare_ms = int((t_prep_end - t_prep_start) * 1000) if t_prep_end >= t_prep_start else 0

        # E04: Worker 严格按 max_candidates 候选预算切片，严禁在筛选前按 max_results 截断导致遗漏后续匹配商品
        items = items[:max_candidates]

        # 终态单调时钟终检：若超时绝不可返回 succeeded！(D02)
        if time.monotonic() >= deadline and actual_status == "succeeded":
            actual_status = "timed_out"
            if "执行超出总时限预算 (DEADLINE_EXCEEDED)" not in warnings:
                warnings.append("执行超出总时限预算 (DEADLINE_EXCEEDED)")

        # 驱动版本真实提取，杜绝写死虚假事实 (D02)
        driver_version = getattr(self.driver, "version", getattr(self.driver, "DRIVER_VERSION", "handsets-1.0.0"))

        return {
            "schema_version": "1.0.0",
            "batch_id": task["batch_id"],
            "task_id": task["task_id"],
            "attempt_id": task["attempt_id"],
            "app_id": aid,
            "device_id": self.device_id,
            "status": actual_status,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "partial": True,
            "items": items,
            "warnings": warnings,
            "timings": {
                "prepare_ms": t_prepare_ms,
                "search_ms": t_search_ms,
                "details_ms": t_details_ms,
                "restore_ms": t_restore_ms,
                "total_ms": total_ms
            },
            "reset_status": reset_status,
            "metadata": {
                "driver_version": driver_version,
                "adapter_version": "1.0.0",
                "timing_clock": "monotonic"
            }
        }


class FakeWorkerClient:
    """显式测试桩客户端 (仅用于单元/集成测试注入，回执与警告中明确标记 TEST_INJECTED_FAKE)。"""

    def __init__(
        self,
        device_id: str = "c68a3cf2",
        is_online: bool = True,
        items: Optional[List[Dict[str, Any]]] = None,
        status: str = "succeeded",
        reset_status: str = "ready"
    ):
        self.device_id = device_id
        self._is_online = is_online
        self._items = items if items is not None else [
            {
                "id": "mock_p1",
                "title": "自营机械键盘 87键",
                "raw_price_text": "¥199.0",
                "amount": 199.0,
                "price_type": "标价",
                "shop_name": "京东自营官方旗舰店",
                "sales_info": {"raw_text": "5万+", "metric_name": "cumulative_sales", "scope": "item"},
                "positive_rate": 98.0,
                "logistics_info": {"free_shipping": True, "delivery_partner": "jd_delivery"},
                "evidence": {"verified_badges": ["self_operated", "official_flagship"]}
            },
            {
                "id": "mock_p2",
                "title": "第三方机械键盘 104键",
                "raw_price_text": "¥89.0",
                "amount": 89.0,
                "price_type": "标价",
                "shop_name": "某外设专营店",
                "sales_info": {"raw_text": "1000+", "metric_name": "cumulative_sales", "scope": "item"},
                "positive_rate": 94.0,
                "logistics_info": {"free_shipping": False, "delivery_partner": "merchant_delivery"},
                "evidence": {"verified_badges": []}
            }
        ]
        self._status = status
        self._reset_status = reset_status

    def check_online(self) -> Tuple[bool, str, str]:
        """Fake 模式同样披露离线原因，保持与真机同构的上报契约。"""
        if self._is_online:
            return True, "", ""
        return False, "unknown", format_ready_reason("Fake: 设备离线")

    def is_online(self) -> bool:
        return self._is_online

    def execute_task(
        self,
        task: Dict[str, Any],
        batch: Dict[str, Any],
        cancel_event: Optional[Any] = None
    ) -> Dict[str, Any]:
        self.last_task = task
        self.last_batch = batch
        if not self._is_online:
            raise RuntimeError(f"Fake: 设备 '{self.device_id}' 离线")
        st = "cancelled" if (cancel_event and cancel_event.is_set()) else self._status
        return {
            "schema_version": "1.0.0",
            "batch_id": task["batch_id"],
            "task_id": task["task_id"],
            "attempt_id": task["attempt_id"],
            "app_id": task["app_id"],
            "device_id": self.device_id,
            "status": st,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "partial": False,
            "items": self._items,
            "warnings": ["TEST_INJECTED_FAKE: 显式测试桩数据，非物理设备产出"],
            "timings": {
                "prepare_ms": 10,
                "search_ms": 20,
                "details_ms": 10,
                "restore_ms": 10,
                "total_ms": 50
            },
            "reset_status": self._reset_status,
            "metadata": {
                "driver_version": "handsets-1.0.0",
                "adapter_version": "1.0.0",
                "timing_clock": "monotonic"
            }
        }
