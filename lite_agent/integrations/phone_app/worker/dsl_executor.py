"""
受限设备脚本 (JSON DSL) 安全解释执行器 (lite_agent.integrations.phone_app.worker.dsl_executor).
严格遵循第 7 节、第 8 节与 R06 / B07 要求：
- 逐项解释执行获批白名单动作：open_app, tap, fill, submit, wait_for, extract, screenshot, back
- 产物路径由执行层内部安全生成，严禁脚本指定外部写入路径。
- submit 兜底、back、screenshot 等操作失败必须如实传播错误，严禁静默吞掉。
- extract 产出明确的结构化商品字典，无有效商品时报错。
"""

from __future__ import annotations
import os
import time
import re
import uuid
import logging
import tempfile
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Optional, Tuple
from lite_agent.integrations.phone_app.worker.drivers.handsets import BaseDeviceDriver

logger = logging.getLogger("integrations.phone_app.worker.dsl_executor")


class DslExecutionError(Exception):
    pass


class DslExecutor:
    """受限设备 DSL 解释执行器。"""

    def __init__(self, driver: BaseDeviceDriver):
        self.driver = driver

    def execute_script(
        self,
        steps: List[Dict[str, Any]],
        cancel_event: Optional[Any] = None,
        deadline: Optional[float] = None
    ) -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
        """逐步解释执行 DSL 脚本 (D02: 支持总时限与取消信号)。返回 (ok, extracted_records, error_message)。"""
        extracted_data: List[Dict[str, Any]] = []

        for idx, step in enumerate(steps, 1):
            if cancel_event and cancel_event.is_set():
                return False, extracted_data, "执行被中途取消"
            if deadline is not None and time.monotonic() >= deadline:
                return False, extracted_data, "执行超出总时限预算 (DEADLINE_EXCEEDED)"

            action = step.get("action")
            logger.info(f"执行步骤 [{idx}/{len(steps)}]: {action} -> {step}")

            try:
                if action == "open_app":
                    pkg = step["package"]
                    ok = self.driver.open_app(pkg)
                    if not ok:
                        return False, extracted_data, f"步骤 #{idx} open_app '{pkg}' 失败"
                    time.sleep(1.0)

                elif action == "tap":
                    target = step["target"]
                    ok = self.driver.tap(target)
                    if not ok:
                        return False, extracted_data, f"步骤 #{idx} tap '{target}' 失败"
                    time.sleep(0.5)

                elif action == "fill":
                    target = step["target"]
                    text = step["text"]
                    ok = self.driver.fill(target, text)
                    if not ok:
                        return False, extracted_data, f"步骤 #{idx} fill '{target}' 失败"
                    time.sleep(0.3)

                elif action == "submit":
                    ok = self.driver.submit()
                    if not ok:
                        target = step.get("target", "搜索")
                        ok = self.driver.tap(target)
                        if not ok:
                            return False, extracted_data, f"步骤 #{idx} submit 提交失败且兜底点击 '{target}' 失败"
                    time.sleep(0.8)

                elif action == "wait_for":
                    target = step["target"]
                    timeout = step.get("timeout", 5.0)
                    ok = self.driver.wait_for(target, timeout_seconds=timeout)
                    if not ok:
                        return False, extracted_data, f"步骤 #{idx} wait_for '{target}' 超时"

                elif action == "extract":
                    fields = step.get("fields", ["title", "price"])
                    raw_xml = ""
                    try:
                        raw_xml = self.driver.dump_ui("xml")
                    except Exception as dump_err:
                        return False, extracted_data, f"步骤 #{idx} extract 读取 XML 失败: {dump_err}"

                    if not raw_xml or "<node" not in raw_xml:
                        return False, extracted_data, f"步骤 #{idx} extract 失败: 未能获取到合规 XML UI 树 (严格禁止不可信文本回退)"

                    parsed, err_msg = self._extract_products_from_xml(raw_xml, fields)
                    if not parsed:
                        return False, extracted_data, f"步骤 #{idx} extract 结构化容器提取失败: {err_msg or '未识别到合规商品容器节点'} (严格禁止不可信文本回退)"
                    extracted_data.extend(parsed)

                elif action == "screenshot":
                    # 产物路径由执行层内部受控生成，严禁脚本指定外部路径 (B07 & E02)
                    artifact_dir = os.environ.get("PHONE_APP_ARTIFACT_DIR") or tempfile.gettempdir()
                    safe_path = os.path.join(artifact_dir, f"phone_app_artifact_{int(time.time() * 1000)}_{idx}.png")
                    ok = self.driver.take_screenshot(safe_path)
                    if not ok:
                        return False, extracted_data, f"步骤 #{idx} screenshot 截图操作失败"

                elif action == "back":
                    ok = self.driver.go_back()
                    if not ok:
                        return False, extracted_data, f"步骤 #{idx} back 返回操作失败"
                    time.sleep(0.4)

                else:
                    return False, extracted_data, f"步骤 #{idx} 未知动作: {action}"

            except Exception as e:
                logger.error(f"步骤 #{idx} 执行异常: {e}")
                return False, extracted_data, str(e)

        return True, extracted_data, None

    def _extract_products_from_xml(self, raw_xml: str, fields: List[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """从 XML UI 树提取商品，保证卡片字段在同一容器节点内绑定，杜绝跨卡片串字段 (E01: 容器级强绑定)。"""
        if not raw_xml:
            return [], "XML 内容为空"

        # 过滤 Android XML 转储中的控制字符 (如 \x00-\x08, \x0b-\x0c, \x0e-\x1f)
        raw_xml = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw_xml)
        try:
            root = ET.fromstring(raw_xml)
        except Exception as parse_err:
            return [], f"XML 语法解析失败: {parse_err}"

        cards = []
        for node in root.iter("node"):
            cls_name = node.attrib.get("class", "")
            if "RecyclerView" in cls_name:
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

        if not cards:
            return [], "未找到符合规范的商品列表容器或卡片节点"

        results = []
        seen_titles = set()
        for idx, card in enumerate(cards, 1):
            title = None
            price_text = None
            amount = None
            for elem in card.iter("node"):
                text = elem.attrib.get("text", "").replace("\u200b", "").strip()
                if not price_text and re.match(r"^¥\d+(\.\d+)?", text):
                    price_text = text
                    try:
                        amount = float(text.replace("¥", ""))
                    except ValueError:
                        pass
                if not title and len(text) > 7:
                    # 过滤广告与通用导航干扰词 (E01)
                    if (not text.startswith("¥") and "商品" not in text and "排行榜" not in text and
                        "进店" not in text and "广告" not in text and "赞助" not in text and
                        "已选中" not in text and "未选中" not in text and "点击" not in text):
                        title = text

            if title and price_text and title not in seen_titles:
                seen_titles.add(title)
                results.append({
                    "id": f"dsl_xml_{idx}_{card.attrib.get('bounds', '')[-8:-1].replace(',', '_')}",
                    "title": title,
                    "raw_price_text": price_text,
                    "amount": amount,
                    "price_type": "标价",
                    "sales_info": {},
                    "positive_rate": None,
                    "logistics_info": {},
                    "evidence": {"source": "dsl_extract_container_xml"}
                })

        if not results:
            return [], "容器卡片中未能提取到合规的商品标题与价格"

        return results, None
