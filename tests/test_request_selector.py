"""
RequestSelector Phase 0 验收测试
设计文档: docs/request_selector_design.md (v7) 第 7 节验收标准 #1-#20 中模块级可测项。
代码能力在 Phase 0 一次交付；Phase 1/2 只切换 Shadow/Enabled 环境变量。

隔离策略: 使用 StubEngine 桩（只实现 get_all_names / get_guest_schemas 两个公开接口），
不触发 skills/ 目录真实模块导入。
"""

import os

import pytest

from core.request_selector import (
    DOMAIN_MAP,
    MissMarkStreamFilter,
    MISS_SIGNALS,
    TOOLSET_MISS_MARK,
    SelectionResult,
    RequestSelector,
    detect_miss,
    strip_miss_mark,
)

AGENT_PY = os.path.join(os.path.dirname(__file__), "..", "agent.py")


def _all_domain_tools():
    """DOMAIN_MAP 引用的全部工具名（= 完整映射场景下的注册技能集）。"""
    names = []
    for cfg in DOMAIN_MAP.values():
        names += cfg.get("default_tools", [])
        names += cfg.get("explicit_intent_tools", [])
    return sorted(set(names))


class StubEngine:
    """最小 SkillEngine 桩: 只实现 selector 用到的两个公开接口。"""

    def __init__(self, names=None, guest_names=None):
        self._names = set(names if names is not None else _all_domain_tools())
        self._guest = set(guest_names if guest_names is not None else self._names)

    def get_all_names(self):
        return set(self._names)

    def get_guest_schemas(self):
        return [{"function": {"name": n}} for n in sorted(self._guest)]

    def get_all_schemas(self):
        return [{"function": {"name": n}} for n in sorted(self._names)]

    def get_schemas_by_names(self, names):
        wanted = set(names)
        return [schema for schema in self.get_all_schemas()
                if schema["function"]["name"] in wanted]


@pytest.fixture
def selector():
    """完整映射场景: DOMAIN_MAP 引用与注册技能一一对应，无警告。"""
    return RequestSelector(StubEngine())


# ---------------------------------------------------------------------------
# 验收 #1/#2: 启动校验
# ---------------------------------------------------------------------------

class TestStartupValidation:

    def test_warns_on_invalid_refs(self, capsys):
        """#1: DOMAIN_MAP 引用不存在的技能时输出明确警告。"""
        names = set(_all_domain_tools()) - {"todo_done"}
        RequestSelector(StubEngine(names=names))
        out = capsys.readouterr().out
        assert "引用了不存在的技能" in out
        assert "todo" in out and "todo_done" in out

    def test_strict_mode_raises(self, monkeypatch):
        """#1: LITE_AGENT_STRICT_SELECTOR=1 时映射错误直接抛 RuntimeError。"""
        monkeypatch.setenv("LITE_AGENT_STRICT_SELECTOR", "1")
        names = set(_all_domain_tools()) - {"mail_reprocess"}
        with pytest.raises(RuntimeError, match="映射错误"):
            RequestSelector(StubEngine(names=names))

    def test_lists_unmapped_skills(self, capsys):
        """#2: 启动时输出未映射技能列表。"""
        names = set(_all_domain_tools()) | {"ops_rss_node"}
        RequestSelector(StubEngine(names=names))
        out = capsys.readouterr().out
        assert "未映射技能" in out
        assert "ops_rss_node" in out

    def test_no_warning_on_complete_mapping(self, capsys):
        sel = RequestSelector(StubEngine())
        out = capsys.readouterr().out
        assert "引用了不存在的技能" not in out
        assert "未映射技能" not in out
        assert sel._miss_count == 0


# ---------------------------------------------------------------------------
# 验收 #3: selector 仅在 Agent.__init__ 初始化一次（源码级断言，行为集成属 Phase 1）
# ---------------------------------------------------------------------------

class TestAgentInitOnce:

    def test_selector_instantiated_once_in_agent_init(self):
        with open(AGENT_PY, encoding="utf-8") as f:
            src = f.read()
        assert "self.request_selector = RequestSelector(self.skill_engine)" in src
        assert src.count("= RequestSelector(") == 1


# ---------------------------------------------------------------------------
# 验收 #4: Guest 安全
# ---------------------------------------------------------------------------

class TestGuestFilter:

    def test_guest_result_is_guest_subset(self):
        """#4: guest 请求不返回非 guest_ok 工具（子集关系断言）。"""
        # 构造 guest 白名单 = 各域 default_tools（不含任何显式工具）
        guest = set()
        for cfg in DOMAIN_MAP.values():
            guest.update(cfg.get("default_tools", []))
        sel = RequestSelector(StubEngine(guest_names=guest))

        result = sel.select("标记这个待办为完成", is_guest=True)
        assert result.names
        assert set(result.names) <= guest
        assert "todo_done" not in result.names

    def test_non_guest_gets_action_tool(self, selector):
        result = selector.select("标记这个待办为完成", is_guest=False)
        assert "todo_done" in result.names


# ---------------------------------------------------------------------------
# 验收 #5/#6/#7: 只读模式与默认/显式分层
# ---------------------------------------------------------------------------

class TestReadOnlyMode:

    def test_view_todo_defaults_only(self, selector):
        """#5: '查看待办' 只返回 todo_list/todo_get，且 read_only_mode=True。"""
        result = selector.select("查看待办")
        assert result.names == ["todo_list", "todo_get"]
        assert result.read_only_mode is True
        assert result.domains == ["todo"]
        assert result.confidence == "high"

    def test_mark_done_injects_action(self, selector):
        """#6: '标记这个待办为完成' 返回默认工具 + todo_done，read_only_mode=False。"""
        result = selector.select("标记这个待办为完成")
        assert "todo_done" in result.names
        assert "todo_list" in result.names and "todo_get" in result.names
        assert result.read_only_mode is False

    def test_routes_return_only_matched_action(self, selector):
        """#16: '标记完成' 只注入 todo_done，而非全部 9 个 todo 写工具。"""
        result = selector.select("标记这个待办为完成")
        explicit_given = set(result.names) - set(DOMAIN_MAP["todo"]["default_tools"])
        assert explicit_given == {"todo_done"}

    def test_read_only_strict_rule_multi_domain(self, selector):
        """#7: 任一 domain 命中 explicit_patterns 即 read_only_mode=False。"""
        both_default = selector.select("查看待办和账单")
        assert both_default.read_only_mode is True

        with_explicit = selector.select("查账单并同步到网盘")
        assert with_explicit.read_only_mode is False


# ---------------------------------------------------------------------------
# 验收 #8: v5 归类修正生效
# ---------------------------------------------------------------------------

class TestExplicitClassification:

    def test_blog_export(self, selector):
        result = selector.select("导出博客文章")
        assert "blog_export_articles" in result.names
        assert result.read_only_mode is False

    def test_mail_fetch_summaries_is_explicit(self, selector):
        result = selector.select("抓取邮件摘要")
        assert "mail_fetch_summaries" in result.names
        assert result.read_only_mode is False

    def test_todo_push_brief_is_explicit(self, selector):
        result = selector.select("推送待办简报")
        assert "todo_push_brief" in result.names
        assert result.read_only_mode is False


# ---------------------------------------------------------------------------
# 验收 #9/#18: 多领域合并与超限回退
# ---------------------------------------------------------------------------

class TestMultiDomain:

    def test_billing_backup_within_limit(self, selector):
        """#9: '查账单并同步到网盘' 返回 billing+backup 工具且 <= 15 个。"""
        result = selector.select("查账单并同步到网盘")
        assert set(result.domains) == {"billing", "backup"}
        assert len(result.names) <= 15
        for name in DOMAIN_MAP["billing"]["default_tools"]:
            assert name in result.names
        # backup 无 explicit_routes，explicit_patterns 命中"同步" -> 全量兜底入 other_explicit
        for name in DOMAIN_MAP["backup"]["explicit_intent_tools"]:
            assert name in result.names

    def test_mandatory_over_limit_falls_back_to_full(self, selector):
        """#18: mail+billing+system+security 同时命中，mandatory(23) > 15 -> names=None。"""
        result = selector.select("查邮件账单和系统日志并做安全巡检")
        assert result.names is None
        assert result.confidence == "low"
        assert "回退全量" in result.reason

    def test_cross_domain_action_not_lost(self, selector):
        """#16: '抓取邮件并同步到网盘' 动作工具与 backup 显式工具均在结果内。"""
        result = selector.select("抓取邮件并同步到网盘")
        assert "mail_fetch_summaries" in result.names
        assert "ops_backup_data" in result.names
        assert "ops_backup_cloud" in result.names
        assert result.names[0] == "mail_fetch_summaries"  # 动作工具排头部


# ---------------------------------------------------------------------------
# 验收 #10/#11/#17/#20: 多轮上下文继承与确认守卫
# ---------------------------------------------------------------------------

class TestHistoryInheritance:

    def test_continue_inherits_domain(self, selector):
        """#10: '继续' 继承最近 user 消息的领域。"""
        history = [{"role": "user", "content": "查看待办"}]
        result = selector.select("继续", history=history)
        assert result.domains == ["todo"]
        assert result.names == ["todo_list", "todo_get"]
        assert result.confidence == "medium"

    def test_tool_calls_infer_domain(self, selector):
        """#10 旁路: 上轮 assistant 有 tool_calls 时反推 domain。"""
        history = [
            {"role": "user", "content": "查看待办"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "todo_list"}}]},
        ]
        result = selector.select("再看看", history=history)
        assert result.domains == ["todo"]

    def test_write_intent_inherit_confirmed(self, selector):
        """#11: 发布博客 -> 确认问句 -> '确认' 继承写意图，publish 被注入。"""
        history = [
            {"role": "user", "content": "发布这篇博客"},
            {"role": "assistant", "content": "确认发布吗？"},
        ]
        result = selector.select("确认", history=history)
        assert result.domains == ["blog"]
        assert "blog_publish_article" in result.names
        assert result.read_only_mode is False

    def test_write_intent_rejected_on_thanks(self, selector):
        """#11 对照: '谢谢' 非短确认词，守卫拒绝，不注入 publish。"""
        history = [
            {"role": "user", "content": "发布这篇博客"},
            {"role": "assistant", "content": "确认发布吗？"},
        ]
        result = selector.select("谢谢", history=history)
        assert result.domains == ["blog"]
        assert "blog_publish_article" not in result.names
        assert result.read_only_mode is True

    def test_guard_rejects_plain_statement(self, selector):
        """#17: '已确认发布完成' 非短确认词，不触发写意图继承。"""
        history = [
            {"role": "user", "content": "发布这篇博客"},
            {"role": "assistant", "content": "确认发布吗？"},
        ]
        result = selector.select("已确认发布完成", history=history)
        assert "blog_publish_article" not in result.names
        assert result.read_only_mode is True

    def test_guard_rejects_non_confirm_question(self, selector):
        """#17: 助手消息非确认请求（'还需要查看结果吗？'）不触发继承。"""
        history = [
            {"role": "user", "content": "发布这篇博客"},
            {"role": "assistant", "content": "还需要查看结果吗？"},
        ]
        result = selector.select("确认", history=history)
        assert "blog_publish_article" not in result.names

    def test_guard_cross_domain_reject(self, selector):
        """#20: 继承 mail domain 时，'确认发布吗？' + '确认' 不触发写意图继承。"""
        history = [
            {"role": "user", "content": "抓取邮件摘要"},
            {"role": "assistant", "content": "确认发布吗？"},
        ]
        result = selector.select("确认", history=history)
        assert result.domains == ["mail"]
        assert "mail_fetch_summaries" not in result.names
        assert result.read_only_mode is True

    def test_guard_same_phrase_blog_allows(self, selector):
        """#20 对照: 同句式下继承 blog domain 正常触发。"""
        history = [
            {"role": "user", "content": "发布这篇博客"},
            {"role": "assistant", "content": "确认发布吗？"},
        ]
        result = selector.select("确认", history=history)
        assert "blog_publish_article" in result.names


# ---------------------------------------------------------------------------
# 验收 #19: mail 路由互斥与多匹配
# ---------------------------------------------------------------------------

class TestMailRouteMutex:

    def test_only_fetch_exclusive(self, selector):
        """#19: '只抓取邮件' 只注入 mail_fetch_only。"""
        result = selector.select("只抓取邮件")
        assert "mail_fetch_only" in result.names
        assert "mail_fetch_summaries" not in result.names

    def test_fetch_summaries_exclusive(self, selector):
        """#19: '抓取邮件摘要' 只注入 mail_fetch_summaries。"""
        result = selector.select("抓取邮件摘要")
        assert "mail_fetch_summaries" in result.names
        assert "mail_fetch_only" not in result.names

    def test_orthogonal_multi_match(self, selector):
        """#19: '抓取邮件摘要并回填正文' 同时注入两个正交动作工具。"""
        result = selector.select("抓取邮件摘要并回填正文")
        assert "mail_fetch_summaries" in result.names
        assert "backfill_bodies" in result.names


# ---------------------------------------------------------------------------
# 验收 #12/#13: 漏选检测
# ---------------------------------------------------------------------------

class TestMissDetection:

    def test_record_miss_increments_and_logs_no_user_text(self, selector, capsys):
        """#12: record_miss 只含 domain/names，不含用户原文。"""
        result = selector.select("查看待办")
        selector.record_miss(result)
        selector.record_miss(result)
        out = capsys.readouterr().out
        assert selector._miss_count == 2
        assert "selection_miss #2" in out
        assert "domains=['todo']" in out

    def test_mark_hit_counts_even_with_tool_calls(self):
        """#13: 含 [TOOLSET_MISS] 标记即计数（无论是否有 tool_calls）。"""
        result = SelectionResult(names=["todo_list"], domains=["todo"],
                                 confidence="high", reason="test")
        assert detect_miss(result, f"{TOOLSET_MISS_MARK} 缺少工具", had_tool_calls=True) is True

    def test_signal_requires_no_tool_calls(self):
        """#13: 仅中文降级信号时要求整轮无 tool_calls。"""
        result = SelectionResult(names=["todo_list"], domains=["todo"],
                                 confidence="high", reason="test")
        assert detect_miss(result, MISS_SIGNALS[0], had_tool_calls=False) is True
        assert detect_miss(result, MISS_SIGNALS[0], had_tool_calls=True) is False

    def test_full_fallback_and_empty_not_counted(self):
        """#13: 全量兜底 (None) 与空集 ([]) 不计数。"""
        full = SelectionResult(names=None, domains=[], confidence="none", reason="test")
        empty = SelectionResult(names=[], domains=[], confidence="none", reason="test")
        assert detect_miss(full, TOOLSET_MISS_MARK, had_tool_calls=False) is False
        assert detect_miss(empty, TOOLSET_MISS_MARK, had_tool_calls=False) is False
        assert detect_miss(None, TOOLSET_MISS_MARK, had_tool_calls=False) is False

    def test_strip_mark_unconditional(self):
        """#13: 标记无条件剥离，不出现在最终回复。"""
        assert strip_miss_mark(f"好的{TOOLSET_MISS_MARK}，如下") == "好的，如下"
        assert strip_miss_mark("无标记回复") == "无标记回复"

    def test_stream_filter_handles_split_marker(self):
        stream_filter = MissMarkStreamFilter()
        chunks = ["开头[TOOL", "SET_MI", "SS]结尾"]
        visible = "".join(stream_filter.feed(chunk) for chunk in chunks)
        visible += stream_filter.flush()
        assert visible == "开头结尾"

    def test_stream_filter_flushes_partial_non_marker(self):
        stream_filter = MissMarkStreamFilter()
        visible = stream_filter.feed("普通文本[TOOL") + stream_filter.flush()
        assert visible == "普通文本[TOOL"


# ---------------------------------------------------------------------------
# 验收 #14: Agent feature flag 三分支
# ---------------------------------------------------------------------------

class TestAgentFeatureFlags:

    @staticmethod
    def _agent_and_message(text="opaque-user-text"):
        from agent import Agent, IncomingMessage

        agent = Agent.__new__(Agent)
        agent.skill_engine = StubEngine()
        agent.request_selector = RequestSelector(agent.skill_engine)
        msg = IncomingMessage(
            channel="test", user_id="u1", chat_id="c1",
            message_id="m1", text=text,
        )
        return agent, msg

    def test_shadow_and_default_inject_identical_schemas(self, monkeypatch, capsys):
        agent, msg = self._agent_and_message()
        monkeypatch.delenv("LITE_AGENT_SELECTOR_ENABLED", raising=False)
        monkeypatch.delenv("LITE_AGENT_SELECTOR_SHADOW", raising=False)
        default_tools, default_names, default_result = agent._select_request_tools(msg, [])

        monkeypatch.setenv("LITE_AGENT_SELECTOR_SHADOW", "1")
        shadow_tools, shadow_names, shadow_result = agent._select_request_tools(msg, [])
        output = capsys.readouterr().out

        assert shadow_tools == default_tools
        assert shadow_names is default_names is None
        assert shadow_result is default_result is None
        assert "Selector-Shadow" in output
        assert msg.text not in output

    def test_enabled_applies_all_three_states(self, monkeypatch):
        agent, _ = self._agent_and_message()
        monkeypatch.setenv("LITE_AGENT_SELECTOR_ENABLED", "1")
        monkeypatch.setenv("LITE_AGENT_SELECTOR_SHADOW", "1")  # ENABLED 优先

        from agent import IncomingMessage
        subset_msg = IncomingMessage("test", "u1", "c1", "m1", "查看待办")
        subset_tools, subset_names, subset_result = agent._select_request_tools(subset_msg, [])
        assert subset_names == ["todo_list", "todo_get"]
        assert [s["function"]["name"] for s in subset_tools] == ["todo_get", "todo_list"]
        assert subset_result.names == subset_names

        full_msg = IncomingMessage("test", "u1", "c1", "m2", "今天天气怎么样")
        full_tools, full_names, full_result = agent._select_request_tools(full_msg, [])
        assert full_names is None
        assert full_tools == agent.skill_engine.get_all_schemas()
        assert full_result.names is None

        empty_agent, empty_msg = self._agent_and_message("查看待办")
        empty_msg.is_guest = True
        empty_agent.skill_engine._guest = set()
        empty_tools, empty_names, empty_result = empty_agent._select_request_tools(empty_msg, [])
        assert empty_tools == []
        assert empty_names == []
        assert empty_result.names == []

    def test_selector_exception_falls_back_to_full(self, monkeypatch, capsys):
        agent, msg = self._agent_and_message("查看待办")
        monkeypatch.setenv("LITE_AGENT_SELECTOR_ENABLED", "1")

        def fail(**_kwargs):
            raise ValueError("sensitive details must not be logged")

        agent.request_selector.select = fail
        tools, names, result = agent._select_request_tools(msg, [])
        output = capsys.readouterr().out
        assert tools == agent.skill_engine.get_all_schemas()
        assert names is None and result is None
        assert "ValueError" in output
        assert "sensitive details" not in output


# ---------------------------------------------------------------------------
# 三态边界
# ---------------------------------------------------------------------------

class TestTriState:

    def test_no_hit_returns_none(self, selector):
        """无关键词命中 -> None（不确定，回退全量）。"""
        result = selector.select("今天天气怎么样")
        assert result.names is None
        assert result.confidence == "none"
        assert result.domains == []

    def test_guest_filter_can_empty_names(self):
        """guest 过滤后子集可为空（不含任何 guest_ok 工具时）。"""
        sel = RequestSelector(StubEngine(guest_names=set()))
        result = sel.select("查看待办", is_guest=True)
        assert result.names == []
