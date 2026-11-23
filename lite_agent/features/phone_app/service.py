"""
Phone App 核心调度服务 (features.phone_app.service).
负责批次全生命周期管理、设备独占互斥锁、审批状态机校验、子任务调度与多维度汇总。
严格遵循 phone-app-execution-development-20261018.md 规范及第一轮复核整改要求：
- 严格禁止默认模拟成功兜底冒充真机 (R01)。
- 严格状态机与原子 CAS 消费授权，禁止未审核或空脚本执行，禁止重复执行 (R02)。
- 严格筛选条件交集验证与三态判定 (R04)。
- 真实 Attempt ID 与复位状态就绪门禁 (R07)。
"""

from __future__ import annotations
import uuid
import time
import threading
import logging
from typing import Dict, Any, List, Optional, Tuple, Callable
from lite_agent.features.phone_app.contracts import (
    BatchStatus,
    TaskStatus,
    ProductFilters,
    SearchBudget,
    ProductItem,
    OverallDiagnosis,
    RECEIPT_STATUS_MAP,
)
from lite_agent.features.phone_app.catalog import AppCatalog
from lite_agent.features.phone_app.repository import PhoneAppRepository
from lite_agent.features.phone_app.approval import ApprovalManager, compute_canonical_hash
from lite_agent.features.phone_app.script_validation import validate_script_dsl
from lite_agent.features.phone_app.filter_engine import FilterEngine
from lite_agent.features.phone_app.worker_hub import WorkerHub

logger = logging.getLogger("features.phone_app.service")


class PhoneAppService:
    """Phone App 调度核心服务。"""

    # 全局设备互斥锁：同手机串行，跨手机可并行
    _device_locks: Dict[str, threading.Lock] = {}
    _device_locks_guard = threading.Lock()

    def __init__(
        self,
        repo: Optional[PhoneAppRepository] = None,
        catalog: Optional[AppCatalog] = None,
        db_path: Optional[str] = None,
        worker_client: Optional[Any] = None,
        enable_real_worker: bool = False,
        router: Optional[Any] = None,
        gateway: Optional[Any] = None,
        config: Optional[Dict[str, Any]] = None,
        worker_hub: Optional[WorkerHub] = None,
        expected_token: Optional[str] = None,
        default_model: Optional[str] = None,
    ):
        self.repo = repo or PhoneAppRepository(db_path=db_path)
        self.catalog = catalog or AppCatalog()
        self.worker_client = worker_client
        self.enable_real_worker = enable_real_worker
        self.router = router
        self.gateway = gateway
        self.config = config or {}
        self.worker_hub = worker_hub or WorkerHub(expected_token=expected_token)
        self.default_model = default_model
        self._active_threads: List[threading.Thread] = []

    def close(self):
        """显式清理仓储、连接与 WorkerHub 通道。"""
        if self.worker_hub:
            self.worker_hub.close()
        if self.repo:
            self.repo.close()

    def resume_device(self, device_id: str, operator: str = "admin", comment: str = "") -> Tuple[bool, Dict[str, Any]]:
        """恢复处于暂停状态的设备。"""
        if self.worker_hub and self.worker_hub.has_active_lease_for_device(device_id):
            return False, {
                "error": f"设备 '{device_id}' 仍有在途未确认停止的任务租约，禁止直接恢复 (D01: Unresolved active lease on device)"
            }
        reason = f"人工恢复已确认 by {operator}" + (f": {comment}" if comment else "")
        self.repo.set_device_state(device_id, "ready", reason=reason)
        return True, {"device_id": device_id, "state": "ready", "reason": reason}

    def get_devices(self) -> List[Dict[str, Any]]:
        """获取真实连接与受控的设备列表 (B02: 优先从中枢 WorkerHub 聚合的在线 Worker 获取)。"""
        # 1. 优先从中枢 WorkerHub 读取已建立真实通信通道的在线 Worker 上报的设备
        if self.worker_hub:
            online_devs = self.worker_hub.get_online_devices()
            if online_devs:
                return online_devs

        # 2. 如果开启了本机直连 worker，允许尝试读取本机 adb (主要用于本地单测或开发模式)
        if self.enable_real_worker:
            import subprocess
            adb_serials = set()
            try:
                res = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=2.0)
                for line in res.stdout.strip().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "device":
                        adb_serials.add(parts[0])
            except Exception:
                pass

            trusted_devices = [
                {"device_id": "c68a3cf2", "model": "OnePlus 9 (LE2110)", "android_version": 14, "validated": True}
            ]
            devices = []
            for dev in trusted_devices:
                did = dev["device_id"]
                if did in adb_serials:
                    devices.append({
                        **dev,
                        "status": "online",
                        "error_code": "",
                        "offline_reason": "",
                        "status_text": "🟢 本机ADB在线已就绪"
                    })
                else:
                    devices.append({
                        **dev,
                        "status": "offline",
                        "error_code": "adb_no_device",
                        "offline_reason": "本机 adb devices 未列出该设备",
                        "status_text": "🔴 本机 ADB 未识别到设备"
                    })
            return devices

        # 3. 默认情况下，无在线 Worker 即报告离线
        return [
            {
                "device_id": "c68a3cf2",
                "model": "OnePlus 9 (LE2110)",
                "android_version": 14,
                "validated": True,
                "status": "offline",
                "error_code": "worker_offline",
                "offline_reason": "VPS1 ↔ Mac 链路未建立 (无 Worker 心跳上报)",
                "status_text": "🔴 Worker 离线未连接 (VPS1 ↔ Mac 链路未建立)"
            }
        ]

    def _generate_scripts_via_llm(self, query: str, app_ids: List[str]) -> Tuple[bool, Dict[str, Any]]:
        """调用宿主大模型服务生成逐 App 受限 DSL 脚本，严禁模板假冒 (B03)。"""
        import json
        app_contexts = []
        allowed_pkgs = set()
        for aid in app_ids:
            app = self.catalog.get_app(aid)
            if app:
                app_contexts.append(f"- App '{aid}': 名称={app.display_name}, 包名={app.package_name}")
                allowed_pkgs.add(app.package_name)

        sys_prompt = (
            "你是一个受限移动自动化脚本专家。根据用户的搜索目标与目标 App 列表，生成符合白名单 DSL 的 JSON 执行脚本。\n"
            "允许的动作类型仅限：open_app, tap, fill, submit, wait_for, extract, screenshot, back。\n"
            "严禁包含 eval, exec, shell, 或拼接非法字符 (; | ` $)。\n"
            "必须输出纯 JSON 格式：{\"<app_id>\": [ {\"action\": ...}, ... ]}。"
        )
        user_prompt = f"用户搜索目标: {query}\n目标 App:\n" + "\n".join(app_contexts)

        try:
            import os
            from lite_agent.core.llm_gateway import LLMGateway
            from lite_agent.core.model_router import ModelRouter

            router = self.router
            gateway = self.gateway
            if router is None or gateway is None:
                cfg = self.config or {}
                if not cfg:
                    return False, {"error": "AI 模型生成脚本失败: 宿主未提供 ModelRouter/LLMGateway 实例且无有效配置"}
                router = router or ModelRouter(cfg)
                gateway = gateway or LLMGateway(router=router)

            # 获取有效配置的模型名称 (C05: 严格按宿主配置选择模型，不依赖 router.config 假属性)
            selected_model = self.default_model
            if not selected_model and isinstance(self.config, dict):
                selected_model = (
                    self.config.get("phone_app", {}).get("model") or
                    self.config.get("llm", {}).get("default")
                )

            if hasattr(router, "models_cfg") and router.models_cfg:
                if not selected_model or selected_model not in router.models_cfg:
                    selected_model = next(iter(router.models_cfg.keys()))

            if not selected_model:
                return False, {"error": "AI 模型生成脚本失败: 宿主未配置可用的大模型 (No model configured in ModelRouter)"}

            # 宿主模型网关匹配 (D03):
            # 真实 Agent 初始化的 LLMGateway 没有 router (self.router is None)。
            # 直接传 model 会触发 ValueError: model or invoker is required。
            # 正确规范：从宿主 Router 取得 invoker 与 provider，显式传入 gateway.invoke_sync，
            # 这样既避免了直接修改共享网关的 router 产生串扰，又能通过宿主 ledger 准确记账。
            invoker = None
            provider = ""
            if router is not None:
                if hasattr(router, "get_invoker"):
                    invoker = router.get_invoker(selected_model, timeout=30.0)
                if hasattr(router, "get_driver"):
                    provider = router.get_driver(selected_model) or ""
                elif hasattr(invoker, "provider"):
                    provider = getattr(invoker, "provider", "")

            invoke_kwargs = {
                "role": "dsl_generator",
                "model": selected_model,
                "timeout": 30.0,
            }
            if invoker is not None:
                invoke_kwargs["invoker"] = invoker
            if provider:
                invoke_kwargs["provider"] = provider

            res = gateway.invoke_sync(
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                **invoke_kwargs
            )
            content = res.get("content", "").strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            scripts = json.loads(content)
            if not isinstance(scripts, dict):
                return False, {"error": "模型返回的脚本必须是 JSON Object"}

            for aid in app_ids:
                if not scripts.get(aid):
                    return False, {"error": f"模型未生成 App '{aid}' 的脚本"}
                ok, err = validate_script_dsl(scripts[aid], allowed_pkgs)
                if not ok:
                    return False, {"error": f"App '{aid}' DSL 校验未通过: {err}"}

            return True, scripts
        except Exception as e:
            logger.warning(f"AI 生成脚本失败: {e}")
            return False, {"error": f"AI 模型生成脚本失败: {e}"}

    @classmethod
    def _get_device_lock(cls, device_id: str) -> threading.Lock:
        with cls._device_locks_guard:
            if device_id not in cls._device_locks:
                cls._device_locks[device_id] = threading.Lock()
            return cls._device_locks[device_id]

    def create_batch(
        self,
        query: str,
        app_ids: List[str],
        device_id: str,
        action: str = "search_products",
        filters: Optional[Dict[str, Any]] = None,
        budget: Optional[Dict[str, Any]] = None,
        mode: str = "fixed",  # fixed (预审核) | custom_ai (AI生成脚本)
    ) -> Tuple[bool, Dict[str, Any]]:
        """创建搜索或脚本批次，包含设备、应用及筛选条件联合校验。"""
        if mode not in ("fixed", "custom_ai"):
            return False, {"error": f"未知的执行模式 '{mode}'，仅支持 fixed 或 custom_ai"}

        # 1. 目录联合有效性校验 (包含 requested_filters 校验，R04)
        valid, err = self.catalog.validate_batch_target(
            app_ids=app_ids,
            device_id=device_id,
            action=action,
            requested_filters=filters
        )
        if not valid:
            return False, {"error": err}

        # 预算参数 schema 校验 (D02)
        if budget:
            from lite_agent.features.phone_app.contracts import SearchBudget
            try:
                b_obj = SearchBudget(**budget)
                b_ok, b_err = b_obj.validate()
                if not b_ok:
                    return False, {"error": f"预算配置不合法: {b_err}"}
            except Exception as b_e:
                return False, {"error": f"预算参数结构无效: {b_e}"}

        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        initial_status = BatchStatus.APPROVED.value if mode == "fixed" else BatchStatus.DRAFT.value

        # 2. 持久化 Batch (显式持久化 mode 字段，杜绝推断，R02)
        self.repo.create_batch(
            batch_id=batch_id,
            mode=mode,
            action=action,
            query=query,
            device_id=device_id,
            app_ids=app_ids,
            status=initial_status,
            filters=filters,
            budget=budget,
            scripts={},
        )

        # 3. 为每个 App 创建独立子任务
        for aid in app_ids:
            task_id = f"task-{uuid.uuid4().hex[:12]}"
            attempt_id = f"att-1"
            app_meta = self.catalog.get_app(aid)
            adapter_ver = app_meta.adapter_version if app_meta else "1.0.0"
            self.repo.create_task(
                task_id=task_id,
                batch_id=batch_id,
                app_id=aid,
                device_id=device_id,
                status=initial_status,
                attempt_id=attempt_id,
                adapter_version=adapter_ver,
            )

        self.repo.append_event(batch_id, "batch_created", {
            "mode": mode, "query": query, "app_ids": app_ids, "device_id": device_id
        })

        return True, {"batch_id": batch_id, "status": initial_status, "mode": mode}

    def generate_scripts(
        self,
        batch_id: str,
        scripts_generator_fn: Optional[Callable[[str, List[str]], Dict[str, Any]]] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """为 AI 模式生成受限 DSL 步骤，并通过静态安全检查。"""
        batch = self.repo.get_batch(batch_id)
        if not batch:
            return False, {"error": "批次不存在"}

        # 状态检查：运行中或终态禁止重新生成
        if batch["status"] in ("running", "queued", "succeeded", "failed", "partial_failed", "cancelled"):
            return False, {"error": f"批次处于 '{batch['status']}' 状态，禁止重新生成脚本"}

        # 固定模式严禁生成自定义脚本 (B01)
        if batch.get("mode") == "fixed":
            return False, {"error": "固定搜索模式 (fixed) 模板不可变，禁止生成自定义脚本"}

        app_ids = batch["app_ids"]
        allowed_pkgs = {self.catalog.get_app(a).package_name for a in app_ids if self.catalog.get_app(a)}

        # 若提供了真实的 AI 脚本生成器则调用，未提供时尝试通过宿主大模型生成，严禁模板假冒 (R06)
        if scripts_generator_fn:
            try:
                generated_scripts = scripts_generator_fn(batch["query"], app_ids)
            except Exception as e:
                return False, {"error": f"AI 模型生成脚本失败: {e}"}
        else:
            ok, res = self._generate_scripts_via_llm(batch["query"], app_ids)
            if not ok:
                return False, res
            generated_scripts = res

        # 静态安全校验
        for aid, steps in generated_scripts.items():
            ok, err = validate_script_dsl(steps, allowed_pkgs)
            if not ok:
                return False, {"error": f"App '{aid}' 生成脚本静态检查未通过: {err}"}

        # 保存草稿，使用原子事务递增版本并重置审批 (C02: 杜绝直接写 scripts 绕过事务)
        ok, err, new_rev = self.repo.update_draft_atomic(
            batch_id,
            generated_scripts,
            is_generation=True
        )
        if not ok:
            return False, {"error": err}
        self.repo.append_event(batch_id, "scripts_generated", {"app_count": len(generated_scripts), "revision": new_rev})

        return True, {"batch_id": batch_id, "status": BatchStatus.PENDING_REVIEW.value, "scripts": generated_scripts, "revision": new_rev}

    def update_draft(
        self,
        batch_id: str,
        scripts: Dict[str, Any],
        expected_revision: Optional[int] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """用户编辑草稿，严格校验并使既有审批失效 (B01 & C02)。"""
        batch = self.repo.get_batch(batch_id)
        if not batch:
            return False, {"error": "批次不存在"}

        # 固定模式严禁修改自定义脚本 (B01)
        if batch.get("mode") == "fixed":
            return False, {"error": "固定搜索模式 (fixed) 模板不可变，禁止注入或修改自定义脚本"}

        if batch["status"] in ("running", "queued", "succeeded", "failed", "partial_failed", "cancelled"):
            return False, {"error": f"批次处于 '{batch['status']}' 状态，禁止修改草稿"}

        app_ids = batch["app_ids"]
        allowed_pkgs = {self.catalog.get_app(a).package_name for a in app_ids if self.catalog.get_app(a)}

        if not scripts:
            return False, {"error": "提交的脚本字典不能为空"}

        for aid in app_ids:
            steps = scripts.get(aid)
            if not steps:
                return False, {"error": f"目标 App '{aid}' 缺少有效的脚本步骤"}
            ok, err = validate_script_dsl(steps, allowed_pkgs)
            if not ok:
                return False, {"error": f"App '{aid}' 脚本安全校验失败: {err}"}

        # 原子更新草稿，同时递增版本号并使既有审批失效 (B01 & C02: 支持 expected_revision 乐观锁)
        ok, err, new_rev = self.repo.update_draft_atomic(
            batch_id, scripts, expected_revision=expected_revision
        )
        if not ok:
            return False, {"error": err}

        return True, {"batch_id": batch_id, "status": BatchStatus.PENDING_REVIEW.value, "revision": new_rev}

    def approve_batch(
        self,
        batch_id: str,
        reviewer_id: str
    ) -> Tuple[bool, Dict[str, Any]]:
        """管理员审核批准，写入规范化哈希快照。"""
        batch = self.repo.get_batch(batch_id)
        if not batch:
            return False, {"error": "批次不存在"}

        # 若批次已处于 approved 状态（例如固定搜索模式或已通过审批），幂等返回成功
        if batch["status"] == BatchStatus.APPROVED.value:
            approval_rec = self.repo.get_approval(batch_id)
            canonical_hash = approval_rec.get("canonical_hash", "") if approval_rec else ""
            return True, {
                "batch_id": batch_id,
                "status": BatchStatus.APPROVED.value,
                "hash": canonical_hash,
                "message": "批次已处于审批通过状态",
            }

        if batch["status"] not in (BatchStatus.PENDING_REVIEW.value, BatchStatus.DRAFT.value):
            return False, {"error": f"当前状态不可审批: {batch['status']}"}

        # 自定义模式必须存在完整脚本才可批准 (R02)
        if batch.get("mode") == "custom_ai":
            scripts = batch.get("scripts") or {}
            for aid in batch["app_ids"]:
                if not scripts.get(aid):
                    return False, {"error": f"App '{aid}' 脚本步骤为空，禁止批准未完整编写的脚本"}

        adapter_versions = {
            aid: (self.catalog.get_app(aid).adapter_version if self.catalog.get_app(aid) else "1.0.0")
            for aid in batch["app_ids"]
        }

        approval_rec = ApprovalManager.create_approval_record(
            batch_id=batch_id,
            scripts=batch["scripts"],
            params={"query": batch["query"], "filters": batch["filters"]},
            app_ids=batch["app_ids"],
            device_id=batch["device_id"],
            adapter_versions=adapter_versions,
            limits=batch["budget"],
            reviewer_id=reviewer_id,
            ttl_seconds=1800,
        )

        self.repo.save_approval(approval_rec)
        self.repo.update_batch_status(batch_id, BatchStatus.APPROVED.value)
        self.repo.append_event(batch_id, "batch_approved", {"reviewer_id": reviewer_id, "hash": approval_rec["canonical_hash"]})

        return True, {"batch_id": batch_id, "status": BatchStatus.APPROVED.value, "hash": approval_rec["canonical_hash"]}

    def cancel_batch(
        self,
        batch_id: str,
        operator: str = "admin",
        reason: str = "用户取消"
    ) -> Tuple[bool, Dict[str, Any]]:
        """取消批次任务并终止关联租约 (C03)。"""
        batch = self.repo.get_batch(batch_id)
        if not batch:
            return False, {"error": "批次不存在"}

        # 中枢租约级取消
        if self.worker_hub:
            self.worker_hub.cancel_lease_by_batch(batch_id)

        ok, err = self.repo.cancel_batch_atomic(batch_id, reason=reason, operator=operator)
        if not ok:
            return False, {"error": err}

        return True, {"batch_id": batch_id, "status": BatchStatus.CANCELLED.value, "reason": reason}

    def reject_batch(
        self,
        batch_id: str,
        operator: str = "admin",
        reason: str = "审批人驳回"
    ) -> Tuple[bool, Dict[str, Any]]:
        """驳回待审核批次并终止流程 (C06)。"""
        batch = self.repo.get_batch(batch_id)
        if not batch:
            return False, {"error": "批次不存在"}

        if batch["status"] != BatchStatus.PENDING_REVIEW.value:
            return False, {"error": f"当前状态不可驳回: '{batch['status']}'，仅限 pending_review 批次"}

        ok, err = self.repo.cancel_batch_atomic(batch_id, reason=reason, operator=operator)
        if not ok:
            return False, {"error": err}

        return True, {"batch_id": batch_id, "status": BatchStatus.CANCELLED.value, "reason": reason}

    def execute_batch(
        self,
        batch_id: str,
        user_context: Optional[Dict[str, Any]] = None,
        worker_client: Optional[Any] = None,
        async_run: bool = True,
        expected_revision: Optional[int] = None
    ) -> Tuple[bool, Dict[str, Any]]:
        """执行批次任务：严格校验状态机 -> 验证并原子消费审批令牌 -> 原子锁定领取 -> 派发执行。"""
        batch = self.repo.get_batch(batch_id)
        if not batch:
            return False, {"error": "批次不存在"}

        user_ctx = user_context or {"user_id": "admin"}

        # 严格检查批次当前状态：必须为 APPROVED，终态或运行中严禁重复执行 (R02)
        if batch["status"] != BatchStatus.APPROVED.value:
            return False, {"error": f"批次未审核批准 (当前状态: '{batch['status']}'), 禁止执行"}

        # 设备持久化状态检查门禁 (B04: 设备已暂停时禁止排队或派发执行)
        device_id = batch["device_id"]
        dev_state = self.repo.get_device_state(device_id)
        if dev_state and dev_state.get("state") in ("paused", "needs_human_action"):
            reason = dev_state.get("paused_reason", "设备处于暂停状态")
            return False, {"error": f"设备 '{device_id}' 处于暂停状态 ({reason})，请在人工核实后先恢复设备"}

        is_custom_ai = (batch.get("mode") == "custom_ai")
        if is_custom_ai:
            # 严格检查完整 App 脚本集合
            scripts = batch.get("scripts") or {}
            for aid in batch["app_ids"]:
                if not scripts.get(aid):
                    return False, {"error": f"App '{aid}' 脚本步骤为空，禁止执行未完整编写的自定义任务"}

            approval_rec = self.repo.get_approval(batch_id)
            if not approval_rec:
                return False, {"error": "未找到批次审批记录，禁止执行"}

            adapter_versions = {
                aid: (self.catalog.get_app(aid).adapter_version if self.catalog.get_app(aid) else "1.0.0")
                for aid in batch["app_ids"]
            }

            # 重新计算期望哈希，防范审批后数据库直接篡改
            expected_hash = compute_canonical_hash(
                scripts=batch["scripts"],
                params={"query": batch["query"], "filters": batch["filters"]},
                app_ids=batch["app_ids"],
                device_id=batch["device_id"],
                adapter_versions=adapter_versions,
                limits=batch["budget"],
                expires_at=approval_rec["expires_at"],
            )

            # 原子 CAS 消费授权 (单条 SQL 判定并更新 consumed=1，R02)
            ok, err = self.repo.consume_approval_atomic(batch_id, expected_hash)
            if not ok:
                return False, {"error": f"授权消费失败: {err}"}

        # 原子领取批次执行权并流转至 QUEUED (C02: 单条排他事务 CAS 校验 status == 'approved' 与 revision)
        ok, err, claimed_batch = self.repo.claim_batch_for_execution_atomic(
            batch_id, expected_revision=expected_revision
        )
        if not ok:
            return False, {"error": err}

        self.repo.append_event(batch_id, "execution_queued", {"by": user_ctx.get("user_id", "admin")})

        def _run():
            self._execute_batch_internal(batch_id, worker_client)

        if async_run:
            t = threading.Thread(target=_run, daemon=True)
            self._active_threads.append(t)
            t.start()
        else:
            _run()

        return True, {"batch_id": batch_id, "status": BatchStatus.QUEUED.value}

    def _execute_batch_internal(self, batch_id: str, worker_client: Optional[Any] = None):
        batch = self.repo.get_batch(batch_id)
        if not batch:
            return

        # 排队派发前核验批次状态：已取消则立刻返回，严禁进入执行 (D01)
        if batch.get("status") in ("cancelled", "cancel_requested"):
            logger.info(f"批次 '{batch_id}' 已处于取消状态 ({batch['status']})，终止排队派发 (D01)")
            return

        device_id = batch["device_id"]
        lock = self._get_device_lock(device_id)

        # 严格获取设备独占锁，确保同一手机上的任务串行执行 (R07)
        with lock:
            # 锁等待结束后立即重新事务核验批次状态 (D01)
            fresh_batch = self.repo.get_batch(batch_id)
            if not fresh_batch or fresh_batch.get("status") in ("cancelled", "cancel_requested"):
                logger.info(f"批次 '{batch_id}' 在等待锁期间已被取消，终止执行 (D01)")
                return

            # 设备持久化状态检查门禁 (B04)
            dev_state = self.repo.get_device_state(device_id)
            if dev_state and dev_state.get("state") in ("paused", "needs_human_action"):
                reason = dev_state.get("paused_reason", "设备处于暂停状态")
                logger.error(f"设备 '{device_id}' 处于暂停状态 ({reason})，拦截任务派发")
                self.repo.update_batch_status(batch_id, BatchStatus.FAILED.value)
                self.repo.update_tasks_status_for_batch(batch_id, TaskStatus.FAILED.value)
                self.repo.append_event(batch_id, "execution_blocked", {
                    "reason": f"设备处于暂停状态: {reason}，需要人工恢复或重置"
                })
                return

            ok, err = self.repo.update_batch_status(batch_id, BatchStatus.RUNNING.value)
            if not ok:
                logger.info(f"批次 '{batch_id}' 无法转为 running ({err})，终止执行")
                return

            tasks = self.repo.list_tasks_for_batch(batch_id)
            success_count = 0
            failed_count = 0

            for task in tasks:
                # 检查最新批次状态：若批次已被请求取消或已取消，中止后续子任务派发 (D01)
                cur_batch = self.repo.get_batch(batch_id)
                if not cur_batch or cur_batch.get("status") in ("cancelled", "cancel_requested"):
                    logger.info(f"批次 '{batch_id}' 已被取消，停止派发后续子任务 (D01)")
                    break

                task_id = task["task_id"]
                aid = task["app_id"]
                # 为每次执行生成独立的 attempt_id (R07)
                attempt_id = f"att-{int(time.time() * 1000)}"

                # 事务核验并流转任务至 running，若失败 (如任务已被取消) 则绝对禁止调用 Worker (D01)
                started = self.repo.start_task_attempt(task_id, attempt_id)
                if not started:
                    logger.warning(f"子任务 '{task_id}' start_task_attempt 失败 (状态已被取消或非法)，终止调用 Worker (D01)")
                    continue

                self.repo.append_event(batch_id, "task_started", {"app_id": aid, "attempt_id": attempt_id}, task_id=task_id)

                # 将带有最新 attempt_id 的任务传递给 Worker (B04)
                current_task = dict(task, attempt_id=attempt_id)

                # 派发调用 Worker 前最终核验批次状态 (D01)
                cur_batch = self.repo.get_batch(batch_id)
                if not cur_batch or cur_batch.get("status") in ("cancelled", "cancel_requested"):
                    logger.info(f"批次 '{batch_id}' 已被取消，放弃调用 Worker 执行端 (D01)")
                    break

                try:
                    # 确定执行端 Worker (B02 & B06)
                    actual_worker = worker_client or self.worker_client
                    if actual_worker is None and self.enable_real_worker:
                        try:
                            from lite_agent.integrations.phone_app.worker.worker import MacWorkerClient
                            actual_worker = MacWorkerClient(device_id=device_id)
                        except Exception as w_err:
                            logger.warning(f"无法初始化 MacWorkerClient: {w_err}")
                            actual_worker = None

                    if actual_worker is not None:
                        if hasattr(actual_worker, "is_online") and not actual_worker.is_online():
                            raise RuntimeError(f"设备 '{device_id}' 未连接或离线，禁止冒充真机结果 (Device Offline)")
                        receipt = actual_worker.execute_task(current_task, batch)
                    elif self.worker_hub and self.worker_hub.has_online_worker_for_device(device_id):
                        budget = batch.get("budget", {}) or {}
                        timeout_sec = float(budget.get("total_timeout_seconds", 180))
                        receipt = self.worker_hub.dispatch_task_sync(device_id, current_task, batch, timeout_seconds=timeout_sec)
                    else:
                        raise RuntimeError("无可用执行端或 Worker 离线，禁止冒充真机结果 (Worker Offline)")

                    # 检查回执实际状态，规范化映射，未定义状态视为协议错误 (B04)
                    raw_status = receipt.get("status", "")
                    status_enum = RECEIPT_STATUS_MAP.get(raw_status)
                    if status_enum is None:
                        actual_task_status = TaskStatus.FAILED.value
                        receipt["warnings"] = receipt.get("warnings", []) + [
                            f"执行端返回未知状态 '{raw_status}'，判定为协议错误 (PROTOCOL_ERROR)"
                        ]
                    else:
                        actual_task_status = status_enum.value

                    # 校验回执 attempt_id 一致性 (B04)
                    receipt_att = receipt.get("attempt_id")
                    if receipt_att != attempt_id:
                        actual_task_status = TaskStatus.FAILED.value
                        receipt["warnings"] = receipt.get("warnings", []) + [
                            f"回执 attempt_id '{receipt_att}' 与派发 '{attempt_id}' 不匹配，判定为无效过期回执"
                        ]

                    # 复位就绪门禁与设备持久化暂停 (R07 & B04)
                    reset_status = receipt.get("reset_status", "unverified")
                    if reset_status != "ready":
                        actual_task_status = TaskStatus.NEEDS_HUMAN_ACTION.value
                        self.repo.set_device_state(
                            device_id, "paused",
                            reason="设备复位检查未通过，已暂停后续任务派发",
                            attempt_id=attempt_id
                        )
                        receipt["warnings"] = receipt.get("warnings", []) + ["设备复位检查未通过，设备已持久化置为暂停状态"]

                    # 对提取的商品应用确定性三态筛选
                    filters_raw = batch.get("filters")
                    filters_obj = None
                    if filters_raw:
                        from lite_agent.features.phone_app.contracts import (
                            ProductFilters, SalesFilter, PositiveRateFilter, ShopTypesFilter, LogisticsFilter
                        )
                        filters_obj = ProductFilters()
                        if "sales" in filters_raw:
                            filters_obj.sales = SalesFilter(**filters_raw["sales"])
                        if "positive_rate" in filters_raw:
                            filters_obj.positive_rate = PositiveRateFilter(**filters_raw["positive_rate"])
                        if "shop_types" in filters_raw:
                            st_val = filters_raw["shop_types"]
                            if isinstance(st_val, list):
                                filters_obj.shop_types = ShopTypesFilter(types=st_val)
                            elif isinstance(st_val, dict):
                                filters_obj.shop_types = ShopTypesFilter(**st_val)
                        if "logistics" in filters_raw:
                            filters_obj.logistics = LogisticsFilter(**filters_raw["logistics"])

                    processed_items = []
                    for item_dict in receipt.get("items", []):
                        # F02: 保证物流与资质信息无论来自 item_dict.logistics_info 还是顶层字段，均合入正确契约位置
                        logistics_info = dict(item_dict.get("logistics_info") or {})
                        if item_dict.get("free_shipping") is not None:
                            logistics_info.setdefault("free_shipping", item_dict["free_shipping"])
                        if item_dict.get("delivery_partner"):
                            logistics_info.setdefault("delivery_partner", item_dict["delivery_partner"])

                        evidence = dict(item_dict.get("evidence") or {})
                        v_badges = list(evidence.get("verified_badges") or [])
                        if item_dict.get("verified_badges"):
                            for b in item_dict["verified_badges"]:
                                if b not in v_badges:
                                    v_badges.append(b)
                        evidence["verified_badges"] = v_badges

                        prod = ProductItem(
                            app_id=aid,
                            source_task_id=task_id,
                            source_item_id=item_dict.get("id", str(uuid.uuid4())[:8]),
                            title=item_dict.get("title", ""),
                            raw_price_text=item_dict.get("raw_price_text"),
                            amount=item_dict.get("amount"),
                            price_type=item_dict.get("price_type", "标价"),
                            shop_name=item_dict.get("shop_name"),
                            shop_type=item_dict.get("shop_type"),
                            sales_info=item_dict.get("sales_info", {}),
                            positive_rate=item_dict.get("positive_rate"),
                            logistics_info=logistics_info,
                            evidence=evidence,
                        )
                        FilterEngine.evaluate_product(prod, filters_obj)
                        processed_items.append(prod.to_dict())

                    receipt["items"] = processed_items
                    self.repo.save_task_receipt(task_id, actual_task_status, attempt_id, receipt)
                    self.repo.append_event(batch_id, "task_finished", {"app_id": aid, "status": actual_task_status}, task_id=task_id)

                    # 若批次处于 cancel_requested 或任务回执为 cancelled，确认终态流转 (D01)
                    cur_b = self.repo.get_batch(batch_id)
                    if (cur_b and cur_b.get("status") == "cancel_requested") or actual_task_status == TaskStatus.CANCELLED.value:
                        self.repo.finalize_cancellation_atomic(batch_id)

                    if actual_task_status == TaskStatus.SUCCEEDED.value:
                        success_count += 1
                    else:
                        failed_count += 1

                    # 若设备复位失败，暂停该设备后续派发 (R07)
                    if reset_status != "ready":
                        break

                except Exception as e:
                    self.repo.save_task_receipt(task_id, TaskStatus.FAILED.value, attempt_id, {}, error=str(e))
                    self.repo.append_event(batch_id, "task_failed", {"app_id": aid, "error": str(e)}, task_id=task_id)
                    failed_count += 1

            # 汇总批次状态 (D01: 若批次已取消或处于取消请求，确认终态流转，绝不覆盖为 succeeded/failed)
            cur_b = self.repo.get_batch(batch_id)
            if cur_b and cur_b.get("status") in ("cancelled", "cancel_requested"):
                self.repo.finalize_cancellation_atomic(batch_id)
                self.repo.append_event(batch_id, "batch_cancelled_finalized", {"status": "cancelled"})
                return

            if failed_count == 0 and success_count > 0:
                final_batch_status = BatchStatus.SUCCEEDED.value
            elif success_count > 0:
                final_batch_status = BatchStatus.PARTIAL_FAILED.value
            else:
                final_batch_status = BatchStatus.FAILED.value

            self.repo.update_batch_status(batch_id, final_batch_status)
            self.repo.append_event(batch_id, "batch_finished", {
                "status": final_batch_status, "success_count": success_count, "failed_count": failed_count
            })

    def get_batch_summary(self, batch_id: str) -> Optional[Dict[str, Any]]:
        """获取批次详情与三态分组汇总。"""
        batch = self.repo.get_batch(batch_id)
        if not batch:
            return None

        tasks = self.repo.list_tasks_for_batch(batch_id)
        matched_items = []
        needs_verification_items = []
        rejected_items = []

        total_scanned = 0
        for t in tasks:
            receipt = t.get("receipt") or {}
            for item in receipt.get("items", []):
                total_scanned += 1
                diag = item.get("diagnosis")
                if diag == OverallDiagnosis.MATCHED.value:
                    matched_items.append(item)
                elif diag == OverallDiagnosis.REJECTED.value:
                    rejected_items.append(item)
                else:
                    needs_verification_items.append(item)

        budget = batch.get("budget") or {}
        max_results = int(budget.get("max_results", 10)) if isinstance(budget, dict) else 10
        if max_results > 0:
            matched_items = matched_items[:max_results]

        return {
            "batch": batch,
            "tasks": tasks,
            "test_mode": any(
                any(str(w).startswith("TEST_INJECTED_FAKE")
                    for w in (t.get("receipt") or {}).get("warnings", []))
                for t in tasks
            ),
            "summary": {
                "total_scanned": total_scanned,
                "matched_count": len(matched_items),
                "needs_verification_count": len(needs_verification_items),
                "rejected_count": len(rejected_items),
            },
            "grouped_results": {
                "matched": matched_items,
                "needs_verification": needs_verification_items,
                "rejected": rejected_items,
            }
        }
