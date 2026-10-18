"""
京东业务适配器单元测试 (test_jd_adapter.py).
基于 FakeDeviceDriver 验证 Section 15.2 与 B05 要求：
- 严格基于 XML 节点树与卡片容器边界提取，杜绝串卡与不可信文本回退。
- 损坏 XML 严格报告 extraction_failed，绝不脑补。
- 假旗舰店名防御（无真实徽标元素不赋予 official_flagship）。
- 物流否定语义优先判定（“不包邮”严格为 False，仅“运费险”不代表收费）。
- 销量解析保留“10万+”等完整原文。
"""

import pytest
from lite_agent.integrations.phone_app.worker.drivers.handsets import FakeDeviceDriver
from lite_agent.integrations.phone_app.worker.adapters.jd import JdAdapter


@pytest.mark.parametrize("operation", ["open_product", "read_product_metrics", "restore_ready"])
def test_driver_internal_type_error_is_not_retried(operation):
    """驱动内部异常不能触发第二次动作，也不能丢弃截止时间。"""
    import time

    class FailingDriver(FakeDeviceDriver):
        def tap(self, target, timeout=None, deadline=None):
            self.actions_log.append(("tap", deadline))
            raise TypeError("driver failed after issuing action")

        def dump_ui(self, format_type="table", timeout=None, deadline=None):
            self.actions_log.append(("dump_ui", deadline))
            raise TypeError("driver internal read failure")

    driver = FailingDriver()
    adapter = JdAdapter(driver)
    deadline = time.monotonic() + 1
    with pytest.raises(TypeError):
        if operation == "restore_ready":
            adapter.restore_ready(deadline=deadline)
        else:
            getattr(adapter, operation)("item", deadline=deadline)
    assert len(driver.actions_log) == 1
    assert driver.actions_log[0][1] == deadline


MOCK_REAL_XML = """<?xml version="1.0" encoding="utf-8"?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
    <node class="androidx.recyclerview.widget.RecyclerView" bounds="[0,200][1080,2200]">
      <!-- Card 1: 真实官方旗舰店与包邮 -->
      <node class="android.view.ViewGroup" bounds="[0,200][1080,600]">
        <node class="android.widget.TextView" text="全键热插拔 迈从G87 V2三模无线机械键盘 紫云轴" bounds="[300,210][1050,280]"/>
        <node class="android.widget.TextView" text="¥170.8" bounds="[300,320][450,370]"/>
        <node class="android.widget.TextView" text="已售50万+" bounds="[460,330][600,370]"/>
        <node class="android.widget.TextView" text="MCHOSE 迈从官方旗舰店" bounds="[300,400][700,440]"/>
        <node class="android.widget.TextView" resource-id="com.jd:id/badge_flagship" text="官方旗舰店" bounds="[710,405][780,435]"/>
        <node class="android.widget.TextView" text="包邮" bounds="[300,460][380,490]"/>
        <node class="android.widget.TextView" text="京东发货" bounds="[400,460][520,490]"/>
      </node>

      <!-- Card 2: 真实自营与明示不包邮 -->
      <node class="android.view.ViewGroup" bounds="[0,610][1080,1010]">
        <node class="android.widget.TextView" text="狼蛛S500游戏机械键盘 青轴" bounds="[300,620][1050,690]"/>
        <node class="android.widget.TextView" text="¥75.45" bounds="[300,730][450,780]"/>
        <node class="android.widget.TextView" text="已售6万+" bounds="[460,740][600,780]"/>
        <node class="android.widget.TextView" text="狼蛛外设京东自营旗舰店" bounds="[300,810][700,850]"/>
        <node class="android.widget.TextView" resource-id="com.jd:id/badge_self" text="自营" bounds="[710,815][780,845]"/>
        <node class="android.widget.TextView" text="不包邮" bounds="[300,870][380,900]"/>
      </node>
    </node>
  </node>
</hierarchy>
"""


def test_jd_search_extraction():
    fake_driver = FakeDeviceDriver(mock_ui_xml=MOCK_REAL_XML)
    adapter = JdAdapter(fake_driver)

    res = adapter.search("机械键盘", max_candidates=5)
    assert res["status"] == "succeeded"
    assert len(res["items"]) == 2

    item1 = res["items"][0]
    assert "迈从G87" in item1["title"]
    assert item1["raw_price_text"] == "¥170.8"
    assert item1["amount"] == 170.8
    assert "official_flagship" in item1["evidence"]["verified_badges"]
    assert item1["logistics_info"]["free_shipping"] is True
    assert item1["logistics_info"]["delivery_partner"] == "jd_delivery"
    assert item1["sales_info"]["raw_text"] == "已售50万+"

    item2 = res["items"][1]
    assert "狼蛛S500" in item2["title"]
    assert item2["raw_price_text"] == "¥75.45"
    assert "self_operated" in item2["evidence"]["verified_badges"]
    assert item2["logistics_info"]["free_shipping"] is False


def test_jd_malformed_xml_fails_without_fallback():
    """损坏或不完整的 XML 严禁调用文本 fallback 脑补，必须报告 extraction_failed (B05)。"""
    broken_xml = '<hierarchy><node text="综合推荐"><broken_xml_tag'
    fake_driver = FakeDeviceDriver(mock_ui_xml=broken_xml)
    adapter = JdAdapter(fake_driver)

    res = adapter.search("测试商品")
    assert res["status"] == "extraction_failed"
    assert len(res["items"]) == 0


def test_jd_fake_flagship_and_logistics_semantics():
    """验证假冒旗舰店名防御与物流否定优先逻辑 (B05)。"""
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <hierarchy rotation="0">
      <node class="androidx.recyclerview.widget.RecyclerView" bounds="[0,0][1080,2000]">
        <!-- 假冒官方旗舰店：店名带字样但无真实独立的徽标节点 -->
        <node class="android.view.ViewGroup" bounds="[0,100][1080,500]">
          <node class="android.widget.TextView" text="山寨高仿机械键盘 特价清仓" bounds="[300,110][900,160]"/>
          <node class="android.widget.TextView" text="¥39.9" bounds="[300,200][400,250]"/>
          <node class="android.widget.TextView" text="假官方旗舰店专卖" bounds="[300,280][600,320]"/>
          <node class="android.widget.TextView" text="已售 10万+件" bounds="[300,340][500,380]"/>
          <!-- 仅赠送运费险，不代表包邮；未明示包邮时为 None -->
          <node class="android.widget.TextView" text="赠送运费险" bounds="[300,400][500,440]"/>
        </node>

        <!-- 收取明确运费 -->
        <node class="android.view.ViewGroup" bounds="[0,520][1080,920]">
          <node class="android.widget.TextView" text="大件机械键盘实木掌托" bounds="[300,530][900,580]"/>
          <node class="android.widget.TextView" text="¥29.0" bounds="[300,620][400,670]"/>
          <node class="android.widget.TextView" text="木艺生活店" bounds="[300,700][500,740]"/>
          <node class="android.widget.TextView" text="运费: ¥10" bounds="[300,760][450,800]"/>
        </node>
      </node>
    </hierarchy>
    """
    fake_driver = FakeDeviceDriver(mock_ui_xml=xml)
    adapter = JdAdapter(fake_driver)

    res = adapter.search("机械键盘")
    assert res["status"] == "succeeded"
    assert len(res["items"]) == 2

    fake_shop_item = res["items"][0]
    # 假旗舰店名不应被赋予 official_flagship 认证
    assert "official_flagship" not in fake_shop_item["evidence"]["verified_badges"]
    # 销量完整保留加号
    assert fake_shop_item["sales_info"]["raw_text"] == "已售 10万+件"
    # 仅赠运费险，未明示免运费，归为 None
    assert fake_shop_item["logistics_info"]["free_shipping"] is None

    freight_item = res["items"][1]
    # 明确有运费，free_shipping 必须为 False
    assert freight_item["logistics_info"]["free_shipping"] is False


def test_jd_search_empty_result():
    mock_ui = """<?xml version="1.0" encoding="utf-8"?>
    <hierarchy rotation="0">
      <node class="android.widget.TextView" text="抱歉，没有找到相关商品" bounds="[100,500][800,600]"/>
    </hierarchy>
    """
    fake_driver = FakeDeviceDriver(mock_ui_xml=mock_ui, mock_ui_table='抱歉，没有找到相关商品')
    adapter = JdAdapter(fake_driver)

    res = adapter.search("zzxxccvv998811")
    assert res["status"] == "empty_results"
    assert len(res["items"]) == 0


def test_jd_read_product_metrics_and_shop():
    mock_detail_ui = """
-    TextView              "98%好评"  200,800
-    TextView              "已售 20万+件"  200,900
-    TextView              "加入购物车"  800,2200
"""
    fake_driver = FakeDeviceDriver(mock_ui_table=mock_detail_ui)
    adapter = JdAdapter(fake_driver)

    metrics = adapter.read_product_metrics("mock_product_ref")
    assert metrics["positive_rate"] == 98.0
    assert "20万" in metrics["sales_raw"]

    mock_shop_ui = """
-    TextView              "罗技官方旗舰店"  400,200
-    TextView              "【官方旗舰店认证】"  600,200
-    TextView              "【京东自营】"  500,200
-    TextView              "全部商品"  500,1000
"""
    fake_driver.mock_ui_table = mock_shop_ui
    shop_info = adapter.read_shop_info("mock_shop_ref")
    assert "self_operated" in shop_info["verified_badges"]
    assert "official_flagship" in shop_info["verified_badges"]


def test_jd_read_logistics_and_restore():
    mock_logistics_ui = """
-    TextView              "京东发货&售后"  300,500
-    TextView              "免运费"  300,600
"""
    fake_driver = FakeDeviceDriver(mock_ui_table=mock_logistics_ui)
    adapter = JdAdapter(fake_driver)

    logistics = adapter.read_logistics("mock_ref")
    assert logistics["free_shipping"] is True
    assert logistics["delivery_partner"] == "jd_delivery"

    # 测试不包邮及运费险混淆
    mock_unfree_ui = """
-    TextView              "送至 北京市朝阳区"  300,500
-    TextView              "不包邮，运费 ¥15"  300,600
-    TextView              "赠运费险"  300,700
"""
    fake_driver.mock_ui_table = mock_unfree_ui
    logistics2 = adapter.read_logistics("mock_ref2")
    assert logistics2["free_shipping"] is False

    mock_home_ui = """
-    TextView              "首页"  108,2277
-    RelativeLayout        "搜索栏"  #b51  540,285
"""
    fake_driver.mock_ui_table = mock_home_ui
    ready = adapter.restore_ready()
    assert ready is True
