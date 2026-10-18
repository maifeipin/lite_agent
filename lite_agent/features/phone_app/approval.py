"""
AI 脚本审批流与服务端规范化哈希鉴权 (features.phone_app.approval).
严格遵循第 7 节：
- 规范化绑定哈希计算 (覆盖脚本、参数、目标App集合、设备ID、适配器版本、执行限制与过期时间)。
- 单次授权消费与竞争防范。
- 严禁客户端篡改、严禁 AI/Worker 越权审批。
"""

from __future__ import annotations
import json
import hashlib
import time
from typing import Dict, Any, Optional, Tuple


def compute_canonical_hash(
    scripts: Dict[str, Any],
    params: Dict[str, Any],
    app_ids: list,
    device_id: str,
    adapter_versions: Dict[str, str],
    limits: Dict[str, Any],
    expires_at: int,
) -> str:
    """计算规范化的服务端不可伪造哈希。"""
    payload = {
        "scripts": scripts,
        "params": params,
        "app_ids": sorted(list(app_ids)),
        "device_id": device_id,
        "adapter_versions": adapter_versions,
        "limits": limits,
        "expires_at": expires_at,
    }
    # 强制排序键，无空白分隔符
    canonical_json = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


class ApprovalManager:
    """审批状态与授权令牌校验器。"""

    @staticmethod
    def create_approval_record(
        batch_id: str,
        scripts: Dict[str, Any],
        params: Dict[str, Any],
        app_ids: list,
        device_id: str,
        adapter_versions: Dict[str, str],
        limits: Dict[str, Any],
        reviewer_id: str,
        ttl_seconds: int = 1800,
    ) -> Dict[str, Any]:
        """由合法审核者创建单次审批记录。"""
        now = int(time.time())
        expires_at = now + ttl_seconds
        canonical_hash = compute_canonical_hash(
            scripts=scripts,
            params=params,
            app_ids=app_ids,
            device_id=device_id,
            adapter_versions=adapter_versions,
            limits=limits,
            expires_at=expires_at,
        )

        return {
            "batch_id": batch_id,
            "status": "approved",
            "reviewer_id": reviewer_id,
            "approved_at": now,
            "expires_at": expires_at,
            "canonical_hash": canonical_hash,
            "consumed": False,
            "consumed_at": None,
            "scripts_snapshot": scripts,
            "params_snapshot": params,
        }

    @staticmethod
    def verify_and_consume_approval(
        approval_record: Dict[str, Any],
        current_scripts: Dict[str, Any],
        current_params: Dict[str, Any],
        app_ids: list,
        device_id: str,
        adapter_versions: Dict[str, str],
        limits: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """在执行前严格验证授权哈希与有效性，并标记消费。"""
        if not approval_record:
            return False, "未找到该批次的审批记录，禁止执行"

        if approval_record.get("status") != "approved":
            return False, f"审批状态不为 approved (当前: {approval_record.get('status')})"

        if approval_record.get("consumed"):
            return False, "该审批授权已被消费（一次性授权），禁止重复执行"

        now = int(time.time())
        expires_at = approval_record.get("expires_at", 0)
        if now > expires_at:
            return False, f"审批授权已过期 ({now} > {expires_at})，请重新发起审核"

        # 重新根据当前状态计算哈希，杜绝审核后修改脚本或参数
        expected_hash = compute_canonical_hash(
            scripts=current_scripts,
            params=current_params,
            app_ids=app_ids,
            device_id=device_id,
            adapter_versions=adapter_versions,
            limits=limits,
            expires_at=expires_at,
        )

        stored_hash = approval_record.get("canonical_hash")
        if stored_hash != expected_hash:
            return False, "脚本或参数在审批后被修改，哈希校验不符，审批已失效！"

        # 消费授权
        approval_record["consumed"] = True
        approval_record["consumed_at"] = now
        return True, None
