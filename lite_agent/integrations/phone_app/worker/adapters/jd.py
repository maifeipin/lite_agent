"""
京东 App 业务适配器 (phone_worker.adapters.jd).
严格遵循任务文档第 15.2 节与第一轮复核整改要求 (R03)：
- 严格基于 XML 节点树与 RecyclerView 直接子容器提取商品卡片，杜绝扁平顺序拼接与串卡。
- 严禁任何从标题/店名的主观脑补推断（删除硬编码98%、删除默认自营/普通店铺/merchant默认值）。
- 字段未知时必须显式返回 None，并附带明确的 availability 与证据来源。
"""

from __future__ import annotations
import time
import re
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional, Tuple
from lite_agent.integrations.phone_app.worker.drivers.handsets import BaseDeviceDriver


class JdAdapter:
    """京东 App (com.jingdong.app.mall) 业务适配器。"""

    PACKAGE_NAME = "com.jingdong.app.mall"

    def __init__(self, driver: BaseDeviceDriver):
        self.driver = driver

    # --- 1. search ---
    def search(
        self,
        query: str,
        max_candidates: int = 20,
        deadline: Optional[float] = None,
        cancel_event: Optional[Any] = None
    ) -> Dict[str, Any]:
        """执行首页搜索并基于真实容器边界提取候选卡片 (D02: 支持总时限与取消信号)。"""
        if cancel_event and cancel_event.is_set():
            return {"status": "cancelled", "items": [], "error": "搜索前已收到取消信号"}
        if deadline is not None and time.monotonic() >= deadline:
            return {"status": "timeout", "items": [], "error": "搜索前超出总时限预算 (DEADLINE_EXCEEDED)"}

        # 1. 确保进入搜索输入状态：若当前不在首页，先复位到首页 (E03)
        ui_check = self.driver.dump_ui("table", timeout=1.5, deadline=deadline)
        if not ("#b51" in ui_check or ("扫啊扫" in ui_check and "首页" in ui_check)):
            self.restore_ready(timeout_sec=10.0, deadline=deadline)

        tapped = self.driver.tap("#b51", deadline=deadline)
        if not tapped:
            tapped = self.driver.tap((540, 285), deadline=deadline)
        time.sleep(0.5)

        # 2. 中文原子注入
        filled = self.driver.fill("id=com.jd.lib.search.feature:id/zt", query)
        if not filled:
            filled = self.driver.fill("id=com.jd.lib.search.feature:id/zs", query)
        if not filled:
            filled = self.driver.fill("class=android.widget.EditText", query)
        time.sleep(0.4)

        # 3. 提交搜索 (优先直接触发输入框 IME ACTION_SEARCH，后备点击搜索按钮)
        submitted = self.driver.submit() if hasattr(self.driver, "submit") else False
        if not submitted:
            tapped_search = self.driver.tap("搜索 按钮")
            if not tapped_search:
                self.driver.tap("搜索")





        # 4. 轮询等待结果页呈现并提取卡片
        loaded = False
        empty_result = False
        raw_xml = ""
        items = []

        for _ in range(8):
            if cancel_event and cancel_event.is_set():
                return {"status": "cancelled", "items": [], "error": "等待结果时收到取消信号"}
            if deadline is not None and time.monotonic() >= deadline:
                return {"status": "timeout", "items": [], "error": "等待结果时超出总时限预算 (DEADLINE_EXCEEDED)"}
            time.sleep(0.7)
            raw_xml = self.driver.dump_ui("xml")
            items = self._parse_items_from_xml(raw_xml)
            empty_result = bool("没有找到" in raw_xml or "抱歉" in raw_xml or "暂无相关商品" in raw_xml or "换个关键词" in raw_xml)
            has_markers = bool("综合推荐" in raw_xml or "筛选商品" in raw_xml or "官方直降" in raw_xml or "到手价" in raw_xml or "已售" in raw_xml or "显示模式" in raw_xml)
            if items:
                loaded = True
                break
            elif empty_result:
                loaded = True
                break
            elif has_markers:
                loaded = True
                # 标记检测到容器，但卡片可能仍在渲染，继续轮询

        if not loaded:
            return {
                "status": "timeout",
                "error": "未在规定时间内检测到搜索结果容器",
                "items": []
            }

        if empty_result:
            return {
                "status": "empty_results",
                "items": [],
                "coverage": {"is_recommended": False, "raw_total": 0}
            }

        status = "succeeded" if items else "extraction_failed"

        return {
            "status": status,
            "items": items[:max_candidates],
            "coverage": {
                "is_recommended": False,
                "raw_total": len(items)
            }
        }


    def _parse_items_from_xml(self, raw_xml: str) -> List[Dict[str, Any]]:
        """从 XML UI 树中提取商品卡片，保证每个卡片所有字段均在同一容器节点内绑定 (B05)。"""
        if not raw_xml:
            return []

        # 过滤 Android XML 转储中的控制字符 (如 \x00-\x08, \x0b-\x0c, \x0e-\x1f)
        raw_xml = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw_xml)

        try:
            root = ET.fromstring(raw_xml)
        except Exception:
            # 严格禁止不可信回退，解析失败直接返回空列表
            return []

        # 寻找包含商品列表的 RecyclerView 或容器节点
        cards = []
        for node in root.iter("node"):
            cls_name = node.attrib.get("class", "")
            if "RecyclerView" in cls_name:
                # 检查该 RecyclerView 是否包含商品价格，避免命中顶部的筛选栏/分类栏等辅助 RecyclerView
                has_price = any(re.match(r"^¥\d+", n.attrib.get("text", "").replace("\u200b", "").strip()) for n in node.iter("node"))
                if has_price:
                    for child in node:
                        if child.attrib.get("bounds"):
                            cards.append(child)
                    break

        if not cards:
            for node in root.iter("node"):
                texts = [n.attrib.get("text", "").replace("\u200b", "").strip() for n in node.iter("node")]
                prices = [t for t in texts if re.match(r"^¥\d+(\.\d+)?", t)]
                if len(prices) == 1 and any(len(t) > 7 for t in texts):
                    cards.append(node)

        results = []
        seen_titles = set()

        for idx, card in enumerate(cards, 1):
            title = None
            price_text = None
            amount = None
            sales_text = None
            sales_metric = None
            shop_name = None
            badges = []
            free_shipping = None
            delivery_partner = None

            card_elements = list(card.iter("node"))

            for elem in card_elements:
                text = elem.attrib.get("text", "").replace("\u200b", "").strip()
                desc = elem.attrib.get("content-desc", "").replace("\u200b", "").strip()
                combined = f"{text} {desc}".strip()
                res_id = elem.attrib.get("resource-id", "")

                # 识别价格 (如 ¥170.8)
                if not price_text and re.match(r"^¥\d+(\.\d+)?", text):
                    price_text = text
                    try:
                        amount = float(text.replace("¥", ""))
                    except ValueError:
                        pass

                # 识别店铺
                if not shop_name and ("旗舰店" in text or "专营店" in text or "自营" in text or "小店" in text):
                    if "榜" not in text and "加购物车" not in text and len(text) < 30 and "立减" not in text and "到手" not in text:
                        shop_name = text

                # 识别销量口径及完整原文 (B05: 完整保留如 '10万+'、'5000+条' 及下界)
                if not sales_text:
                    m_sold = re.search(r"已售\s*(\d+(?:\.\d+)?(?:\s*万)?(?:\s*\+)?(?:\s*件)?)", combined)
                    if m_sold:
                        sales_text = m_sold.group(0).strip()
                        sales_metric = "cumulative_sales"
                    else:
                        m_pay = re.search(r"(\d+(?:\.\d+)?(?:\s*万)?(?:\s*\+)?(?:\s*人)?)\s*付款", combined)
                        if m_pay:
                            sales_text = m_pay.group(0).strip()
                            sales_metric = "payment_count"

                # 识别官方认证徽标 (B05: 必须有明确独立的徽标元素证据，严禁按店名推断)
                is_badge_element = (
                    "badge" in res_id.lower() or
                    "tag" in res_id.lower() or
                    (len(text) <= 6 and (text in ("自营", "官方旗舰店", "旗舰店"))) or
                    (len(desc) <= 6 and (desc in ("自营", "官方旗舰店", "旗舰店"))) or
                    (desc.startswith("自营,") or desc.startswith("广告自营,") or ",自营," in desc) or
                    (desc.startswith("官方旗舰店,") or ",官方旗舰店," in desc)
                )
                if is_badge_element:
                    if (text == "自营" or desc == "自营" or desc.startswith("自营,") or desc.startswith("广告自营,") or ",自营," in desc) and "self_operated" not in badges:
                        badges.append("self_operated")
                    if (text == "官方旗舰店" or desc == "官方旗舰店" or desc.startswith("官方旗舰店,") or ",官方旗舰店," in desc) and "official_flagship" not in badges:
                        badges.append("official_flagship")

                # 识别物流事实 (B05: 优先判定明确否定，防止“不包邮”命中“包邮”，运费险不等于收取运费)
                cleaned_for_logistics = re.sub(r"运费险", "", combined)
                has_negative_shipping = (
                    "不包邮" in combined or
                    "不免邮" in combined or
                    "有运费" in combined or
                    bool(re.search(r"运费\s*[:：]?\s*¥?\s*\d+", cleaned_for_logistics))
                )
                has_positive_shipping = (
                    ("免运费" in combined or "包邮" in combined) and
                    not has_negative_shipping
                )
                if has_negative_shipping:
                    free_shipping = False
                elif has_positive_shipping:
                    free_shipping = True

                if "京东物流" in combined or "京东发货" in combined or "明日达" in combined:
                    delivery_partner = "jd_delivery"

                # 识别商品标题
                if not title and len(text) > 7:
                    if (not text.startswith("¥") and "商品" not in text and "排行榜" not in text and
                        "已选中" not in text and "未选中" not in text and "点击" not in text and
                        "旗舰店" not in text and "专营店" not in text):
                        title = text

            if title and price_text and title not in seen_titles:
                seen_titles.add(title)
                results.append({
                    "id": f"jd_{idx}_{card.attrib.get('bounds', '')[-8:-1].replace(',', '_')}",
                    "title": title,
                    "raw_price_text": price_text,
                    "amount": amount,
                    "price_type": "标价",
                    "shop_name": shop_name,
                    "sales_info": {
                        "raw_text": sales_text,
                        "metric_name": sales_metric,
                        "scope": "item"
                    },
                    "positive_rate": None,  # 列表页无好评率明确展示，严禁硬编码
                    "logistics_info": {
                        "free_shipping": free_shipping,
                        "delivery_partner": delivery_partner,
                        "evidence": f"卡片提取: free_shipping={free_shipping}, partner={delivery_partner}"
                    },
                    "evidence": {
                        "verified_badges": badges,
                        "sales": sales_text,
                        "shop": shop_name or "未注明店铺",
                        "bounds": card.attrib.get("bounds", "")
                    }
                })

        return results

    # --- 2. open_product ---
    def open_product(self, product_ref: str, deadline: Optional[float] = None) -> bool:
        """打开商品详情页 (E03 & F03: 遵守截止时限)。"""
        if deadline is not None and time.monotonic() >= deadline:
            return False
        tapped = self.driver.tap(product_ref, deadline=deadline)
        if not tapped:
            return False
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                return False
            time.sleep(min(1.2, rem))
        else:
            time.sleep(1.2)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        ui = self.driver.dump_ui("table", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return "加入购物车" in ui or "立即购买" in ui or "商品介绍" in ui

    # --- 3. read_product_metrics ---
    def read_product_metrics(self, product_ref: str, deadline: Optional[float] = None) -> Dict[str, Any]:
        """读取详情页好评率与销量口径 (E03 & F03: 遵守截止时限)。"""
        if deadline is not None and time.monotonic() >= deadline:
            return {}
        ui = self.driver.dump_ui("table", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return {}
        rate = None
        m_rate = re.search(r"(\d+(\.\d+)?)%\s*好评", ui)
        if m_rate:
            rate = float(m_rate.group(1))

        sales_text = None
        m_sales = re.search(r"(已售|热卖|已买)\s*(\d+(\.\d+)?(万|\+)?条?)", ui)
        if m_sales:
            sales_text = m_sales.group(0)

        return {
            "positive_rate": rate,
            "sales_raw": sales_text,
            "evidence": f"详情页提取: rate={rate}%, sales={sales_text}"
        }

    # --- 4. open_shop ---
    def open_shop(self, shop_ref: str, deadline: Optional[float] = None) -> bool:
        """打开店铺页 (E03 & F03: 遵守截止时限)。"""
        if deadline is not None and time.monotonic() >= deadline:
            return False
        tapped = self.driver.tap("进店", deadline=deadline)
        if not tapped:
            tapped = self.driver.tap(shop_ref, deadline=deadline)
        if not tapped:
            return False
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                return False
            time.sleep(min(1.0, rem))
        else:
            time.sleep(1.0)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        ui = self.driver.dump_ui("table", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        return "全部商品" in ui or "关注" in ui or "店铺首页" in ui

    # --- 5. read_shop_info ---
    def read_shop_info(self, shop_ref: str, deadline: Optional[float] = None) -> Dict[str, Any]:
        """读取店铺页官方认证徽标 (E03 & F03: 遵守截止时限)。"""
        if deadline is not None and time.monotonic() >= deadline:
            return {}
        ui = self.driver.dump_ui("table", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return {}
        badges = []
        # 严格依据店铺资质明确认证标签，杜绝店名脑补 (B05)
        if re.search(r"【\s*官方旗舰店\s*】|\[\s*官方旗舰店\s*\]|\b官方旗舰店认证\b", ui):
            badges.append("official_flagship")
        if re.search(r"【\s*自营\s*】|\[\s*自营\s*\]|\b京东自营\b", ui):
            badges.append("self_operated")

        return {
            "verified_badges": badges,
            "evidence": f"店铺页徽标读取: {badges}"
        }

    # --- 6. read_logistics ---
    def read_logistics(
        self,
        product_ref: str,
        destination_region: Optional[str] = None,
        deadline: Optional[float] = None
    ) -> Dict[str, Any]:
        """读取商品物流与配送方事实 (E03 & F03: 遵守截止时限)。"""
        if deadline is not None and time.monotonic() >= deadline:
            return {}
        ui = self.driver.dump_ui("table", deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            return {}
        cleaned_ui = re.sub(r"运费险", "", ui)
        has_negative = (
            "不包邮" in ui or
            "不免邮" in ui or
            "有运费" in ui or
            bool(re.search(r"运费\s*[:：]?\s*¥?\s*\d+", cleaned_ui))
        )
        has_positive = ("免运费" in ui or "包邮" in ui) and not has_negative
        if has_negative:
            is_free_shipping = False
        elif has_positive:
            is_free_shipping = True
        else:
            is_free_shipping = None

        is_jd = "jd_delivery" if ("京东物流" in ui or "京东发货" in ui) else None

        return {
            "free_shipping": is_free_shipping,
            "delivery_partner": is_jd,
            "destination_region": destination_region or "当前默认地区",
            "evidence": f"物流页提取: free_shipping={is_free_shipping}, partner={is_jd}"
        }

    def restore_to_product_list(self, deadline: Optional[float] = None) -> bool:
        """从商品详情或店铺页返回至搜索结果列表 (C04: 详情返回隔离)。"""
        for _ in range(3):
            if deadline is not None and time.monotonic() >= deadline:
                return False
            ui = self.driver.dump_ui("table", deadline=deadline)
            if "综合推荐" in ui or "显示模式" in ui or ("筛选" in ui and "销量" in ui):
                return True
            if "返回" in ui:
                self.driver.tap("返回", deadline=deadline)
            else:
                self.driver.go_back(deadline=deadline)
            time.sleep(0.4)
        return False

    # --- 7. restore_ready ---
    def restore_ready(self, timeout_sec: float = 12.0, deadline: Optional[float] = None) -> bool:
        """复位返回首页就绪状态 (E03 & F03: 严格受限于独立安全预算与截止时间，杜绝超时后判定为就绪)。"""
        eff_deadline = time.monotonic() + timeout_sec
        if deadline is not None:
            eff_deadline = min(eff_deadline, deadline)

        for _ in range(5):
            if time.monotonic() >= eff_deadline:
                return False
            # 传入 eff_deadline 约束底层 dump_ui 阻塞
            ui = self.driver.dump_ui("table", deadline=eff_deadline)
            # F03 核心防御：dump_ui 执行后立即复核 deadline！若已超时绝不可判定为 ready！
            if time.monotonic() >= eff_deadline:
                return False
            if "#b51" in ui or ("扫啊扫" in ui and "首页" in ui):
                return True

            if time.monotonic() >= eff_deadline:
                return False
            # 优先点击界面的“返回”控件，防止软键盘拦截物理返回键
            if "返回" in ui:
                self.driver.tap("返回", deadline=eff_deadline)
            else:
                self.driver.go_back(deadline=eff_deadline)
            rem = eff_deadline - time.monotonic()
            if rem <= 0:
                return False
            time.sleep(min(0.4, rem))

        if time.monotonic() >= eff_deadline:
            return False

        self.driver.open_app(self.PACKAGE_NAME, deadline=eff_deadline)
        rem = eff_deadline - time.monotonic()
        if rem <= 0:
            return False
        time.sleep(min(1.5, rem))

        if time.monotonic() >= eff_deadline:
            return False

        ui = self.driver.dump_ui("table", deadline=eff_deadline)
        if time.monotonic() >= eff_deadline:
            return False
        return "#b51" in ui or ("扫啊扫" in ui and "首页" in ui)

