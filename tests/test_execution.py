"""
Phase 1a — 执行协议单元测试

覆盖 core/execution.py 中 ExecutionContext、ExecutionResult、SkillPolicy
和枚举的不可变性、语义正确性和 JSON 稳定性。
"""

import json
import pytest
from types import MappingProxyType

from core.execution import (
    ActorType,
    ExecutionSource,
    ExecutionContext,
    ExecutionResult,
    SkillPolicy,
)


# ============================================================
#  枚举稳定性
# ============================================================

class TestEnumStability:
    """枚举值在 JSON / 账本中必须稳定，不受 auto() 顺序影响。"""

    def test_actor_type_values(self):
        assert ActorType.ADMIN == "admin"
        assert ActorType.USER == "user"
        assert ActorType.GUEST == "guest"
        assert ActorType.WORKER == "worker"
        assert ActorType.SYSTEM == "system"
        assert ActorType.CRON == "cron"

    def test_execution_source_values(self):
        assert ExecutionSource.DIRECT == "direct"
        assert ExecutionSource.ORCHESTRATOR == "orchestrator"
        assert ExecutionSource.STREAM == "stream"
        assert ExecutionSource.CRON == "cron"
        assert ExecutionSource.API == "api"
        assert ExecutionSource.LEGACY == "legacy"

    def test_actor_type_json_roundtrip(self):
        for at in (ActorType.ADMIN, ActorType.CRON, ActorType.GUEST):
            assert json.loads(json.dumps(at)) == at.value

    def test_execution_source_json_roundtrip(self):
        for es in (ExecutionSource.LEGACY, ExecutionSource.API, ExecutionSource.CRON):
            assert json.loads(json.dumps(es)) == es.value

    def test_actor_type_is_str(self):
        assert isinstance(ActorType.ADMIN, str)
        assert isinstance(ActorType.CRON, str)

    def test_execution_source_is_str(self):
        assert isinstance(ExecutionSource.LEGACY, str)
        assert isinstance(ExecutionSource.API, str)


# ============================================================
#  ExecutionContext
# ============================================================

class TestExecutionContext:
    """权限边界、预算字段与不可变性。"""

    # ---- 权限 ----

    def test_none_means_unrestricted(self):
        ctx = ExecutionContext(actor_id="u1")
        assert ctx.is_unrestricted
        assert ctx.has_tool_access("any_tool")
        assert ctx.tool_set is None

    def test_empty_tuple_means_nothing_allowed(self):
        ctx = ExecutionContext(actor_id="u1", allowed_tools=())
        assert not ctx.is_unrestricted
        assert not ctx.has_tool_access("any_tool")
        assert ctx.tool_set == frozenset()
        assert ctx.allowed_tools == frozenset()

    def test_nonempty_allowlist(self):
        ctx = ExecutionContext(actor_id="u1", allowed_tools=["echo", "read"])
        assert ctx.has_tool_access("echo")
        assert ctx.has_tool_access("read")
        assert not ctx.has_tool_access("write")
        assert ctx.tool_set == frozenset({"echo", "read"})

    def test_allowed_tools_is_frozenset(self):
        ctx = ExecutionContext(actor_id="u1", allowed_tools=["echo"])
        assert isinstance(ctx.allowed_tools, frozenset)
        assert ctx.allowed_tools == frozenset({"echo"})

    # ---- 不可变性 ----

    def test_cannot_mutate_original_list(self):
        """传入 list 后修改原始 list 不影响 Context 权限。"""
        tools = ["echo", "read"]
        ctx = ExecutionContext(actor_id="u1", allowed_tools=tools)
        tools.append("write")
        assert not ctx.has_tool_access("write")
        assert ctx.allowed_tools == frozenset({"echo", "read"})

    def test_cannot_mutate_original_set(self):
        """传入 set 后修改原始 set 不影响 Context 权限。"""
        tools = {"echo", "read"}
        ctx = ExecutionContext(actor_id="u1", allowed_tools=tools)
        tools.add("write")
        assert not ctx.has_tool_access("write")

    def test_frozen_dataclass_prevents_setattr(self):
        ctx = ExecutionContext(actor_id="u1")
        with pytest.raises(Exception):
            ctx.actor_id = "hacked"

    # ---- 预算字段 ----

    def test_budget_defaults(self):
        ctx = ExecutionContext(actor_id="u1")
        assert ctx.max_steps == 8
        assert ctx.max_output_tokens == 2048
        assert ctx.token_budget is None

    def test_budget_custom(self):
        ctx = ExecutionContext(actor_id="u1", max_steps=3,
                               max_output_tokens=512, token_budget=10000)
        assert ctx.max_steps == 3
        assert ctx.max_output_tokens == 512
        assert ctx.token_budget == 10000

    def test_token_budget_none_means_unlimited(self):
        ctx = ExecutionContext(actor_id="u1", token_budget=None)
        assert ctx.token_budget is None

    # ---- Actor / Source ----

    def test_guest_actor_type(self):
        ctx = ExecutionContext(actor_id="u1", actor_type=ActorType.GUEST)
        assert ctx.is_guest
        assert not ctx.is_dry_run

    def test_legacy_source(self):
        ctx = ExecutionContext(actor_id="u1", source=ExecutionSource.LEGACY)
        assert ctx.source == ExecutionSource.LEGACY
        assert ctx.source == "legacy"

    def test_dry_run_flag(self):
        ctx = ExecutionContext(actor_id="u1", is_dry_run=True)
        assert ctx.is_dry_run


# ============================================================
#  ExecutionResult
# ============================================================

class TestExecutionResult:
    """工厂方法、错误码、序列化与不可变性。"""

    # ---- 工厂方法 ----

    def test_success(self):
        r = ExecutionResult.success("echo", {"text": "hi"}, "echo: hi",
                                     data={"len": 7},
                                     side_effects=["stdout"],
                                     tool_call_id="call_1")
        assert r.ok
        assert r.output == "echo: hi"
        assert r.tool_args["text"] == "hi"
        assert r.data["len"] == 7
        assert r.side_effects_performed == ("stdout",)
        assert r.tool_call_id == "call_1"
        assert r.error_code == ""

    def test_error(self):
        r = ExecutionResult.error("echo", {"text": "hi"},
                                   error_code="TIMEOUT", error_msg="超时",
                                   retryable=True, tool_call_id="call_x")
        assert not r.ok
        assert r.output == "超时"
        assert r.error_code == "TIMEOUT"
        assert r.retryable is True
        assert r.tool_call_id == "call_x"

    def test_unknown_skill(self):
        r = ExecutionResult.unknown_skill("bad", "{}", tool_call_id="tc1")
        assert not r.ok
        assert r.error_code == "UNKNOWN_SKILL"
        assert "未知技能: bad" in r.output
        assert r.retryable is False
        assert r.tool_call_id == "tc1"

    def test_invalid_args(self):
        r = ExecutionResult.invalid_args("echo", "{bad", "JSONDecodeError")
        assert not r.ok
        assert r.error_code == "INVALID_ARGS"
        assert "参数解析失败" in r.output
        assert r.retryable is False

    def test_skill_error(self):
        r = ExecutionResult.skill_error("echo", {"text": "x"},
                                         error="Division by zero",
                                         retryable=True,
                                         side_effects=["db_write"],
                                         tool_call_id="tc2")
        assert not r.ok
        assert r.error_code == "SKILL_ERROR"
        assert r.retryable is True
        assert r.side_effects_performed == ("db_write",)
        assert r.tool_call_id == "tc2"

    def test_skill_error_default_retryable(self):
        r = ExecutionResult.skill_error("echo", {}, "err")
        assert r.retryable is False
        assert r.side_effects_performed == ()

    # ---- 序列化 ----

    def test_to_legacy_string(self):
        r = ExecutionResult.success("echo", {}, "result")
        assert r.to_legacy_string() == "result"

    def test_to_legacy_string_error(self):
        r = ExecutionResult.unknown_skill("bad", "{}")
        assert "未知技能" in r.to_legacy_string()
        assert r.to_legacy_string().startswith("❌ ")

    def test_to_legacy_string_success_no_prefix(self):
        r = ExecutionResult.success("echo", {}, "result")
        assert r.to_legacy_string() == "result"
        assert not r.to_legacy_string().startswith("❌")

    def test_to_model_message(self):
        r = ExecutionResult.success("echo", {"text": "hi"}, "echo: hi",
                                     tool_call_id="call_1")
        msg = r.to_model_message()
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_1"
        assert msg["name"] == "echo"
        assert msg["content"] == "echo: hi"

    def test_to_model_message_override_call_id(self):
        r = ExecutionResult.success("echo", {}, "ok", tool_call_id="call_1")
        msg = r.to_model_message("call_override")
        assert msg["tool_call_id"] == "call_override"

    def test_to_model_message_fallback_to_instance(self):
        r = ExecutionResult.success("echo", {}, "ok", tool_call_id="call_1")
        msg = r.to_model_message()
        assert msg["tool_call_id"] == "call_1"

    # ---- 不可变性 ----

    def test_tool_args_is_mapping_proxy(self):
        r = ExecutionResult.success("echo", {"text": "hi"}, "ok")
        assert isinstance(r.tool_args, MappingProxyType)
        with pytest.raises(TypeError):
            r.tool_args["text"] = "hacked"

    def test_data_is_mapping_proxy(self):
        r = ExecutionResult.success("echo", {}, "ok", data={"key": "val"})
        assert isinstance(r.data, MappingProxyType)
        with pytest.raises(TypeError):
            r.data["key"] = "hacked"

    def test_side_effects_is_tuple(self):
        r = ExecutionResult.success("echo", {}, side_effects=["a", "b"])
        assert isinstance(r.side_effects_performed, tuple)
        assert r.side_effects_performed == ("a", "b")

    def test_factory_copies_original_dict(self):
        """传入原始 dict 后修改原始 dict 不影响 Result。"""
        args = {"text": "hi"}
        r = ExecutionResult.success("echo", args)
        args["text"] = "hacked"
        assert r.tool_args["text"] == "hi"

    def test_factory_deep_copies_nested_dict(self):
        """修改原始嵌套参数后，Result.tool_args 不变化。"""
        args = {"filter": {"level": "info"}}
        r = ExecutionResult.success("echo", args)
        args["filter"]["level"] = "hacked"
        assert r.tool_args["filter"]["level"] == "info"

    def test_factory_deep_copies_nested_list(self):
        """修改原始嵌套列表后，Result.tool_args 不变化。"""
        args = {"tags": ["a", "b"]}
        r = ExecutionResult.success("echo", args)
        args["tags"].append("c")
        assert r.tool_args["tags"] == ("a", "b")

    def test_cannot_mutate_nested_list_in_result(self):
        """嵌套列表已被冻结为 tuple，不可修改。"""
        r = ExecutionResult.success("echo", {"tags": ["a", "b"]})
        nested = r.tool_args["tags"]
        assert isinstance(nested, tuple)
        with pytest.raises(Exception):
            nested.append("c")  # type: ignore

    def test_cannot_mutate_nested_mapping_in_result(self):
        """嵌套映射已被冻结为 MappingProxyType，不可修改。"""
        r = ExecutionResult.success("echo", {"filter": {"level": "info"}})
        nested = r.tool_args["filter"]
        assert isinstance(nested, MappingProxyType)
        with pytest.raises(TypeError):
            nested["level"] = "hacked"  # type: ignore

    def test_data_deep_frozen(self):
        """data 字段中的嵌套结构也被深度冻结。"""
        r = ExecutionResult.success("echo", {},
                                     data={"items": [{"id": 1}, {"id": 2}]})
        items = r.data["items"]
        assert isinstance(items, tuple)
        assert isinstance(items[0], MappingProxyType)
        with pytest.raises(TypeError):
            items[0]["id"] = 99  # type: ignore

    def test_frozen_dataclass_prevents_setattr(self):
        r = ExecutionResult.success("echo", {}, "ok")
        with pytest.raises(Exception):
            r.ok = False


# ============================================================
#  SkillPolicy
# ============================================================

class TestSkillPolicy:
    """三态副作用、dry-run 语义与不可变性。"""

    # ---- 副作用三态 ----

    def test_side_effect_default_is_none(self):
        sp = SkillPolicy()
        assert sp.side_effect is None
        assert sp.effective_side_effect is True  # 保守处理
        assert not sp.can_dry_run

    def test_side_effect_false(self):
        sp = SkillPolicy(side_effect=False)
        assert sp.side_effect is False
        assert sp.effective_side_effect is False
        assert not sp.can_dry_run  # 还需要 supports_dry_run

    def test_side_effect_none_unknown(self):
        """None 表示副作用未知，保守处理为有副作用。"""
        sp = SkillPolicy(side_effect=None)
        assert sp.side_effect is None
        assert sp.effective_side_effect is True  # 保守
        assert not sp.can_dry_run

    # ---- dry-run 语义 ----

    def test_can_dry_run_side_effect_false_and_supports(self):
        sp = SkillPolicy(side_effect=False, supports_dry_run=True)
        assert sp.can_dry_run is True

    def test_can_dry_run_side_effect_true_and_supports(self):
        """写操作（side_effect=True）是最需要预演的，不应排除。"""
        sp = SkillPolicy(side_effect=True, supports_dry_run=True)
        assert sp.can_dry_run is True

    def test_cannot_dry_run_without_supports_flag(self):
        sp = SkillPolicy(side_effect=False, supports_dry_run=False)
        assert sp.can_dry_run is False

    def test_cannot_dry_run_unknown_side_effect(self):
        """副作用未知（None）时，即使 supports_dry_run 也不可 dry-run。"""
        sp = SkillPolicy(side_effect=None, supports_dry_run=True)
        assert sp.can_dry_run is False

    def test_cannot_dry_run_unknown_without_supports(self):
        sp = SkillPolicy(side_effect=None, supports_dry_run=False)
        assert sp.can_dry_run is False

    # ---- 不可变性 ----

    def test_guard_keywords_is_tuple(self):
        sp = SkillPolicy(guard_keywords=["kw1", "kw2"])
        assert isinstance(sp.guard_keywords, tuple)
        assert sp.guard_keywords == ("kw1", "kw2")

    def test_cannot_mutate_original_guard_keywords(self):
        kws = ["kw1"]
        sp = SkillPolicy(guard_keywords=kws)
        kws.append("kw2")
        assert sp.guard_keywords == ("kw1",)

    def test_frozen_dataclass_prevents_setattr(self):
        sp = SkillPolicy()
        with pytest.raises(Exception):
            sp.side_effect = False