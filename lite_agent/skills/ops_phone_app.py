"""
Phone App 业务技能入口 (skills.ops_phone_app).
薄适配层，委托至 features.phone_app.service。
提供标准 @skill 接入，支持参数类型约束、side_effect 标记与批次引用摘要返回。
"""

from __future__ import annotations
import json
from typing import List, Dict, Any, Optional
from lite_agent.core.skill_engine import skill
from lite_agent.features.phone_app import get_phone_app_service


@skill(
    name="mobile_product_search",
    description="在真实手机 App (首选京东) 上执行多条件商品筛选 (销量、好评率、店铺资质等)。",
    params={
        "query": {"type": "string", "description": "商品搜索关键词，如'机械键盘'"},
        "app_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "目标 App 标识列表，如 ['jd']",
            "default": ["jd"]
        },
        "device_id": {
            "type": "string",
            "description": "受控手机序列号，如 'c68a3cf2'",
            "default": "c68a3cf2"
        },
        "filters": {
            "type": "object",
            "description": "筛选条件：可含 sales (销量)、positive_rate (好评率)、shop_types (店铺类型)",
            "default": {}
        },
        "budget": {
            "type": "object",
            "description": "搜索预算限制：max_candidates, max_results, total_timeout_seconds",
            "default": {}
        }
    },
    tags=["phone", "e_commerce", "mobile"],
    side_effect=True,  # 操作真实物理设备，具有副作用
)
def mobile_product_search(
    query: str,
    app_ids: Optional[List[str]] = None,
    device_id: str = "c68a3cf2",
    filters: Optional[Dict[str, Any]] = None,
    budget: Optional[Dict[str, Any]] = None
) -> str:
    """提交手机商品搜索与筛选任务并返回批次跟踪摘要。"""
    service = get_phone_app_service()
    target_apps = app_ids or ["jd"]

    ok, res = service.create_batch(
        query=query,
        app_ids=target_apps,
        device_id=device_id,
        action="search_products",
        filters=filters or {},
        budget=budget or {},
        mode="fixed"
    )

    if not ok:
        return f"❌ 任务创建失败: {res.get('error')}"

    batch_id = res["batch_id"]

    # 触发异步执行
    exec_ok, exec_res = service.execute_batch(
        batch_id=batch_id,
        user_context={"user_id": "skill_agent"},
        async_run=True
    )

    if not exec_ok:
        return f"❌ 任务排队派发失败: {exec_res.get('error')}"

    return (
        f"✅ 手机商品筛选任务已成功派发排队！\n"
        f"- 批次编号 (Batch ID): {batch_id}\n"
        f"- 目标设备: {device_id}\n"
        f"- 目标平台: {', '.join(target_apps)}\n"
        f"- 搜索词: {query}\n"
        f"- 当前状态: 排队中 (queued)\n"
        f"提示：可调用 mobile_task_status(batch_id='{batch_id}') 查询实时进度与结果摘要。"
    )


@skill(
    name="mobile_task_status",
    description="查询 Phone App 批次任务的执行状态、进度与三态结果摘要 (符合/待核实/不符合)。",
    params={
        "batch_id": {"type": "string", "description": "批次 ID，如 'batch-xxxx'"}
    },
    tags=["phone", "e_commerce", "mobile"],
    side_effect=False,
)
def mobile_task_status(batch_id: str) -> str:
    """查询批次状态与三态判定结果摘要。"""
    service = get_phone_app_service()
    summary = service.get_batch_summary(batch_id)

    if not summary:
        return f"❌ 未找到批次 '{batch_id}' 的记录。"

    batch = summary["batch"]
    status = batch.get("status", "unknown")
    counts = summary.get("summary", {})

    output = [
        f"📱 Phone App 批次状态报告 [{batch_id}]",
        f"- 搜索词: {batch.get('query')}",
        f"- 执行状态: {status}",
        f"- 扫描候选总数: {counts.get('total_scanned', 0)} 条",
        f"- 判定分布:",
        f"  * ✅ 符合条件 (matched): {counts.get('matched_count', 0)} 条",
        f"  * 🔍 待核实 (needs_verification): {counts.get('needs_verification_count', 0)} 条",
        f"  * ❌ 不符合 (rejected): {counts.get('rejected_count', 0)} 条",
    ]

    # 列出部分符合条件的商品示例
    matched = summary.get("grouped_results", {}).get("matched", [])
    if matched:
        output.append("\n【符合条件商品精选】:")
        for idx, item in enumerate(matched[:3], 1):
            p = item.get("raw_price_text") or f"¥{item.get('amount')}"
            output.append(f"  {idx}. {item.get('title')} | 标价: {p} | 店铺: {item.get('shop_name')}")

    # 列出待核实说明
    needs_ver = summary.get("grouped_results", {}).get("needs_verification", [])
    if needs_ver:
        output.append(f"\n【待核实项提醒】: 有 {len(needs_ver)} 条候选存在待核实项 (如销量区间待确认或资质徽标未获取)。")

    return "\n".join(output)
