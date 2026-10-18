"""
商品与店铺确定性多条件筛选与三态判定引擎 (features.phone_app.filter_engine).
严格遵循第 15.3 与 15.4 节：
- 事实区间语义 (如 1000+ 与 gt 1500 判定为 unknown)。
- 口径严格隔离 (月销、累计销量、付款人数、评价数不互通)。
- 资质真实徽标验证 (店名带自营/旗舰不等于资质，无正面证据为 unknown)。
- 三态输出：matched (全通), rejected (任一明确fail), needs_verification (无fail含unknown)。
"""

from __future__ import annotations
import re
from typing import Dict, Any, List, Optional, Tuple
from lite_agent.features.phone_app.contracts import (
    ProductFilters,
    ProductItem,
    ConditionResult,
    ConditionEvaluation,
    OverallDiagnosis,
)


def parse_chinese_number_lower_bound(raw_str: str) -> Tuple[Optional[int], bool]:
    """解析如 '1000+', '50万+', '6万+', '2000' 的下界数值与是否有加号。"""
    if not raw_str:
        return None, False

    clean = raw_str.replace(",", "").replace("，", "").strip()
    is_plus = "+" in clean or "＋" in clean or "多" in clean

    m_wan = re.search(r"(\d+(\.\d+)?)\s*万", clean)
    if m_wan:
        val = int(float(m_wan.group(1)) * 10000)
        return val, is_plus

    m_num = re.search(r"(\d+)", clean)
    if m_num:
        val = int(m_num.group(1))
        return val, is_plus

    return None, False


class FilterEngine:
    """确定性多维度商品筛选器。"""

    @classmethod
    def evaluate_sales(
        cls,
        sales_filter: Any,
        sales_data: Dict[str, Any]
    ) -> ConditionResult:
        metric = getattr(sales_filter, "metric", "cumulative_sales")
        operator = getattr(sales_filter, "operator", "gt")
        threshold = getattr(sales_filter, "threshold", 1000)

        # 1. 检查口径是否对齐
        data_metric = sales_data.get("metric_name") or sales_data.get("metric")
        raw_text = sales_data.get("raw_text")
        evidence = sales_data.get("evidence", raw_text or "无销量数据")
        scope = sales_data.get("scope", "item")

        if not raw_text or data_metric is None:
            return ConditionResult(
                status=ConditionEvaluation.UNKNOWN,
                metric_name=metric,
                target_expression=f"{operator} {threshold}",
                raw_text=raw_text,
                parsed_value=None,
                scope=scope,
                evidence=evidence,
                reason="页面未展示销量或口径未知",
            )

        if data_metric != metric:
            return ConditionResult(
                status=ConditionEvaluation.UNKNOWN,
                metric_name=metric,
                target_expression=f"{operator} {threshold}",
                raw_text=raw_text,
                parsed_value=None,
                scope=scope,
                evidence=evidence,
                reason=f"指标口径不符: 期望 '{metric}'，实际页面为 '{data_metric}'，不自动互认",
            )

        # 2. 区间语义解析
        lower_bound, has_plus = parse_chinese_number_lower_bound(raw_text)
        if lower_bound is None:
            return ConditionResult(
                status=ConditionEvaluation.UNKNOWN,
                metric_name=metric,
                target_expression=f"{operator} {threshold}",
                raw_text=raw_text,
                parsed_value=None,
                scope=scope,
                evidence=evidence,
                reason=f"无法解析数值: '{raw_text}'",
            )

        # 3. 严格语义判定
        # Case A: operator == "gt"
        if operator == "gt":
            if has_plus:
                # 如 1000+ 与 > 1000：因为 + 表示多于1000，但未知确切为1001还是更大
                # 依据 15.4 节：“对严格大于 1000 的判断也不能只凭下界 1000 判通过，应为 unknown”
                if lower_bound == threshold:
                    return ConditionResult(
                        status=ConditionEvaluation.UNKNOWN,
                        metric_name=metric,
                        target_expression=f"> {threshold}",
                        raw_text=raw_text,
                        parsed_value=f"[{lower_bound}, +inf)",
                        scope=scope,
                        evidence=evidence,
                        reason=f"展示值为 '{raw_text}'，下界刚好等于阈值 {threshold}，无法断言严格大于，需进一步核实",
                    )
                elif lower_bound > threshold:
                    # 如 2万+ > 1000: 必然大于
                    return ConditionResult(
                        status=ConditionEvaluation.PASS,
                        metric_name=metric,
                        target_expression=f"> {threshold}",
                        raw_text=raw_text,
                        parsed_value=f"[{lower_bound}, +inf)",
                        scope=scope,
                        evidence=evidence,
                        reason=f"下界 {lower_bound} 明确大于阈值 {threshold}",
                    )
                else:
                    # 如 1000+ 与 > 1500:
                    # 依据 15.4 节：“大于 1500 不能据 1000+ 判断通过或不通过，应为 unknown”
                    return ConditionResult(
                        status=ConditionEvaluation.UNKNOWN,
                        metric_name=metric,
                        target_expression=f"> {threshold}",
                        raw_text=raw_text,
                        parsed_value=f"[{lower_bound}, +inf)",
                        scope=scope,
                        evidence=evidence,
                        reason=f"展示值为 '{raw_text}'，其实际数值可能在 {lower_bound} 至更高，无法断定是否达到 {threshold}",
                    )
            else:
                # 精确数值
                if lower_bound > threshold:
                    return ConditionResult(
                        status=ConditionEvaluation.PASS,
                        metric_name=metric,
                        target_expression=f"> {threshold}",
                        raw_text=raw_text,
                        parsed_value=lower_bound,
                        scope=scope,
                        evidence=evidence,
                        reason=f"实测值 {lower_bound} > 阈值 {threshold}",
                    )
                else:
                    return ConditionResult(
                        status=ConditionEvaluation.FAIL,
                        metric_name=metric,
                        target_expression=f"> {threshold}",
                        raw_text=raw_text,
                        parsed_value=lower_bound,
                        scope=scope,
                        evidence=evidence,
                        reason=f"实测值 {lower_bound} 不大于阈值 {threshold}",
                    )

        # Case B: operator == "gte"
        elif operator == "gte":
            if lower_bound >= threshold:
                return ConditionResult(
                    status=ConditionEvaluation.PASS,
                    metric_name=metric,
                    target_expression=f">= {threshold}",
                    raw_text=raw_text,
                    parsed_value=lower_bound,
                    scope=scope,
                    evidence=evidence,
                    reason=f"下界或实测值 {lower_bound} >= 阈值 {threshold}",
                )
            else:
                if has_plus:
                    return ConditionResult(
                        status=ConditionEvaluation.UNKNOWN,
                        metric_name=metric,
                        target_expression=f">= {threshold}",
                        raw_text=raw_text,
                        parsed_value=f"[{lower_bound}, +inf)",
                        scope=scope,
                        evidence=evidence,
                        reason=f"展示值为 '{raw_text}'，无法断定是否达到 {threshold}",
                    )
                return ConditionResult(
                    status=ConditionEvaluation.FAIL,
                    metric_name=metric,
                    target_expression=f">= {threshold}",
                    raw_text=raw_text,
                    parsed_value=lower_bound,
                    scope=scope,
                    evidence=evidence,
                    reason=f"实测值 {lower_bound} < 阈值 {threshold}",
                )

        return ConditionResult(
            status=ConditionEvaluation.UNKNOWN,
            metric_name=metric,
            target_expression=f"{operator} {threshold}",
            raw_text=raw_text,
            parsed_value=lower_bound,
            scope=scope,
            evidence=evidence,
            reason=f"暂不支持的操作符 '{operator}'",
        )

    @classmethod
    def evaluate_positive_rate(
        cls,
        rate_filter: Any,
        positive_rate: Optional[float],
        evidence: str
    ) -> ConditionResult:
        threshold = getattr(rate_filter, "threshold_percent", 95.0)
        operator = getattr(rate_filter, "operator", "gt")

        if positive_rate is None:
            return ConditionResult(
                status=ConditionEvaluation.UNKNOWN,
                metric_name="positive_rate",
                target_expression=f"{operator} {threshold}%",
                raw_text=None,
                parsed_value=None,
                scope="item",
                evidence=evidence or "页面未展示好评率",
                reason="商品详情或列表未展示明确好评率，禁止以星级脑补",
            )

        if operator == "gt":
            passed = positive_rate > threshold
        elif operator == "gte":
            passed = positive_rate >= threshold
        else:
            passed = positive_rate >= threshold

        return ConditionResult(
            status=ConditionEvaluation.PASS if passed else ConditionEvaluation.FAIL,
            metric_name="positive_rate",
            target_expression=f"{operator} {threshold}%",
            raw_text=f"{positive_rate}%",
            parsed_value=positive_rate,
            scope="item",
            evidence=evidence or f"提取到好评率 {positive_rate}%",
            reason=f"实测好评率 {positive_rate}% {'满足' if passed else '不满足'} {operator} {threshold}%",
        )

    @classmethod
    def evaluate_shop_types(
        cls,
        shop_filter: Any,
        shop_info: Dict[str, Any]
    ) -> ConditionResult:
        allowed_types = getattr(shop_filter, "types", ["self_operated", "official_flagship"])
        verified_badges = shop_info.get("verified_badges", [])
        shop_name = shop_info.get("shop_name", "未知店铺")
        evidence = shop_info.get("evidence", f"店铺: {shop_name}")

        # 依据 15.4 节：店铺名称中含“自营”“旗舰店”不等于官方资质，必须来自已验证徽标/资质页面
        is_self_op = "self_operated" in verified_badges
        is_official = "official_flagship" in verified_badges
        is_flagship = "flagship" in verified_badges

        actual_types = []
        if is_self_op:
            actual_types.append("self_operated")
        if is_official:
            actual_types.append("official_flagship")
        elif is_flagship:
            actual_types.append("flagship")

        # 匹配任一允许的店铺类型 (OR 逻辑)
        if any(t in allowed_types for t in actual_types):
            return ConditionResult(
                status=ConditionEvaluation.PASS,
                metric_name="shop_type",
                target_expression=f"in {allowed_types}",
                raw_text=",".join(verified_badges) if verified_badges else None,
                parsed_value=actual_types,
                scope="shop",
                evidence=evidence,
                reason=f"店铺资质徽标核实通过 (匹配类型: {actual_types})",
            )

        # 若资质信息未提取到或页面无资质徽标
        if not verified_badges:
            # 依据 15.4 节：只有正面证据缺失不能自动判定为非自营 -> unknown
            return ConditionResult(
                status=ConditionEvaluation.UNKNOWN,
                metric_name="shop_type",
                target_expression=f"in {allowed_types}",
                raw_text=shop_name,
                parsed_value=[],
                scope="shop",
                evidence=evidence,
                reason=f"店铺 '{shop_name}' 尚未获取到官方认证徽标证据，待进入店铺详情核实",
            )

        return ConditionResult(
            status=ConditionEvaluation.FAIL,
            metric_name="shop_type",
            target_expression=f"in {allowed_types}",
            raw_text=",".join(verified_badges),
            parsed_value=actual_types,
            scope="shop",
            evidence=evidence,
            reason=f"已核实店铺资质不属于所要求的类型 ({actual_types} not in {allowed_types})",
        )

    @classmethod
    def evaluate_logistics(
        cls,
        logistics_filter: Any,
        logistics_data: Dict[str, Any]
    ) -> ConditionResult:
        req_free_shipping = getattr(logistics_filter, "free_shipping", None)
        req_partner = getattr(logistics_filter, "delivery_partner", None)
        actual_free = logistics_data.get("free_shipping")
        actual_partner = logistics_data.get("delivery_partner")
        evidence = logistics_data.get("evidence", "未提取到物流详细上下文")

        # 1. 包邮条件核对
        if req_free_shipping is True:
            if actual_free is False:
                return ConditionResult(
                    status=ConditionEvaluation.FAIL,
                    metric_name="free_shipping",
                    target_expression="== True",
                    raw_text="明确不包邮/有运费",
                    parsed_value=False,
                    scope="item",
                    evidence=evidence,
                    reason="商品页面标注不包邮或需运费，与要求包邮冲突",
                )
            elif actual_free is None:
                return ConditionResult(
                    status=ConditionEvaluation.UNKNOWN,
                    metric_name="free_shipping",
                    target_expression="== True",
                    raw_text=None,
                    parsed_value=None,
                    scope="item",
                    evidence=evidence,
                    reason="页面未展示包邮信息，待进入详情页核实运费规则",
                )

        # 2. 履约方/配送方核对 (如京东物流)
        if req_partner:
            if actual_partner is None:
                return ConditionResult(
                    status=ConditionEvaluation.UNKNOWN,
                    metric_name="delivery_partner",
                    target_expression=f"== '{req_partner}'",
                    raw_text=None,
                    parsed_value=None,
                    scope="item",
                    evidence=evidence,
                    reason=f"页面未展示配送承运方，待进入详情页核对是否为 '{req_partner}'",
                )
            elif actual_partner != req_partner:
                return ConditionResult(
                    status=ConditionEvaluation.FAIL,
                    metric_name="delivery_partner",
                    target_expression=f"== '{req_partner}'",
                    raw_text=str(actual_partner),
                    parsed_value=actual_partner,
                    scope="item",
                    evidence=evidence,
                    reason=f"实际配送方 '{actual_partner}' 与要求的 '{req_partner}' 不符",
                )

        return ConditionResult(
            status=ConditionEvaluation.PASS,
            metric_name="logistics",
            target_expression="matched",
            raw_text="包邮/满足配送要求",
            parsed_value={"free_shipping": actual_free, "delivery_partner": actual_partner},
            scope="item",
            evidence=evidence,
            reason="物流条件核验通过",
        )

    @classmethod
    def evaluate_product(
        cls,
        product: ProductItem,
        filters: Optional[ProductFilters]
    ) -> OverallDiagnosis:
        """对单个商品执行全量已配置条件的确定性判定。"""
        if not filters:
            product.diagnosis = OverallDiagnosis.MATCHED
            return OverallDiagnosis.MATCHED

        evals: Dict[str, Dict[str, Any]] = {}

        # 1. 销量评估
        if filters.sales:
            res_sales = cls.evaluate_sales(filters.sales, product.sales_info)
            evals["sales"] = res_sales.to_dict()

        # 2. 好评率评估
        if filters.positive_rate:
            res_rate = cls.evaluate_positive_rate(
                filters.positive_rate,
                product.positive_rate,
                product.evidence.get("positive_rate", "")
            )
            evals["positive_rate"] = res_rate.to_dict()

        # 3. 店铺类型评估
        if filters.shop_types:
            shop_data = {
                "shop_name": product.shop_name,
                "verified_badges": product.evidence.get("verified_badges", []),
                "evidence": product.evidence.get("shop", product.shop_name or "")
            }
            res_shop = cls.evaluate_shop_types(filters.shop_types, shop_data)
            evals["shop_types"] = res_shop.to_dict()

        # 4. 物流评估 (R04)
        if filters.logistics and (filters.logistics.free_shipping is not None or filters.logistics.delivery_partner is not None):
            res_logistics = cls.evaluate_logistics(filters.logistics, product.logistics_info)
            evals["logistics"] = res_logistics.to_dict()

        product.evaluations = evals

        # 综合判定逻辑：
        # - 所有条件 pass -> matched
        # - 至少一个 fail -> rejected
        # - 无 fail，但包含 unknown -> needs_verification
        statuses = [v["status"] for v in evals.values()]
        if any(s == ConditionEvaluation.FAIL.value for s in statuses):
            diagnosis = OverallDiagnosis.REJECTED
        elif any(s == ConditionEvaluation.UNKNOWN.value for s in statuses):
            diagnosis = OverallDiagnosis.NEEDS_VERIFICATION
        else:
            diagnosis = OverallDiagnosis.MATCHED

        product.diagnosis = diagnosis
        return diagnosis
