"""
RequestSelector — 主路径动态工具选择 (Phase 0 纯函数实现)

设计文档: docs/request_selector_design.md (v7, 架构批准 2026-08-14)
实例化位置: Agent.__init__ 中构造一次 (设计 3.5)，启动校验随构造完成；
_stream_ai_loop 复用同一实例，不再每次请求 new。

三态返回 (SelectionResult.names):
  []    -> 明确闲聊/问答，纯文本回复
  None  -> 不确定，调用侧回退全量
  [...] -> 明确领域子集（排序: 动作工具 > 默认工具 > 其他显式候选）

v7 核心保证:
  - mandatory (动作+默认) 超 max_tools 时回退全量，不静默裁剪默认工具
  - other_explicit 超剩余配额时同样回退全量
  - selector 自身异常由 agent.py 调用侧 except 分支兜底全量 (设计 3.7)

Python 3.9 兼容: 禁止裸写 list[str] / X | None (PEP 585/604 均为 3.10+)，
统一使用 typing.List / typing.Optional。
"""

import os
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 漏选检测信号 (设计 3.3；接入 agent.py DONE 分支属 Phase 1/2)
# ---------------------------------------------------------------------------

TOOLSET_MISS_MARK = "[TOOLSET_MISS]"   # 主信号: system prompt 指示模型输出

MISS_SIGNALS = [                        # 降级兜底: 模型未按指示输出标记时匹配
    "当前可用工具无法",
    "没有合适的工具",
    "我无法通过现有工具",
    "需要切换到更通用的对话模式",
]

# 写意图继承守卫: 短确认词白名单 (设计 3.4, v6 P2-1 条件 1)
CONFIRM_WORDS = {"确认", "是", "好的", "继续", "执行吧", "行", "可以", "嗯", "ok", "OK", "确定"}


def strip_miss_mark(content: str) -> str:
    """无条件剥离漏选标记，防止异常路径向用户显示内部标记 (设计 3.3, v6 P2-2)。"""
    return content.replace(TOOLSET_MISS_MARK, "").strip()


class MissMarkStreamFilter:
    """Remove the internal miss marker even when it is split across stream chunks."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, chunk: str) -> str:
        text = (self._pending + (chunk or "")).replace(TOOLSET_MISS_MARK, "")
        keep = 0
        max_prefix = min(len(text), len(TOOLSET_MISS_MARK) - 1)
        for size in range(max_prefix, 0, -1):
            if text.endswith(TOOLSET_MISS_MARK[:size]):
                keep = size
                break
        if keep:
            self._pending = text[-keep:]
            return text[:-keep]
        self._pending = ""
        return text

    def flush(self) -> str:
        text = self._pending.replace(TOOLSET_MISS_MARK, "")
        self._pending = ""
        return text


def detect_miss(result: Optional["SelectionResult"], raw_content: str,
                had_tool_calls: bool) -> bool:
    """
    漏选判定 (设计 3.3, v6 P2-2)——纯函数，由 agent.py DONE 分支调用:
      - 主信号 [TOOLSET_MISS] 出现即计数（允许 run 内有 tool_calls，
        解决"先调用工具、后发现缺工具"漏统计）
      - 仅中文降级信号时要求整轮无 tool_calls（防误报）
      - 全量兜底 (names=None) 与闲聊空集 (names=[]) 不计数
    """
    mark_hit = TOOLSET_MISS_MARK in raw_content
    stripped = strip_miss_mark(raw_content)
    signal_hit = any(s in stripped for s in MISS_SIGNALS)
    return (result is not None
            and bool(result.names)
            and (mark_hit or (signal_hit and not had_tool_calls)))


# ---------------------------------------------------------------------------
# 领域映射 (设计 3.2, v7)
# 工具两级分层按用户风险划分:
#   default_tools        查询意图直接可用（允许内部缓存/审计/结果投递）
#   explicit_intent_tools 需明确拉取/推送/同步/发布/修改意图才注入
#   explicit_routes       动作级路由: 命中后只注入对应子集（跨 route 允许多匹配，
#                         同族动作 pattern 必须互斥: 具体规则在前 + 通用规则负向断言）
# ---------------------------------------------------------------------------

CORE_DOMAIN_MAP: Dict[str, Dict] = {
    "system": {
        "pattern": r"(系统状态|负载|内存|磁盘|日志|进程|crontab|定时任务)",
        "default_tools": [
            "ops_sys_status", "ops_read_logs", "ops_read_journal",
            "ops_self_check", "ops_list_crontab",
        ],
        "explicit_intent_tools": [
            "ops_run_script",
        ],
        "explicit_patterns": r"(执行|运行|跑一下)",
    },
    "llm": {
        "pattern": r"(模型余额|API费用|token用量|模型消耗|大模型消耗)",
        "default_tools": [
            "llm_check_balance", "llm_usage_report",
        ],
        "explicit_intent_tools": [],
        "explicit_patterns": None,
    },
}

# 全局领域映射字典（默认只包含内核核心通用领域；业务领域由各 features 插件装配时注册）
DOMAIN_MAP: Dict[str, Dict] = dict(CORE_DOMAIN_MAP)


def register_domain(domain_name: str, domain_spec: Dict[str, Any]) -> None:
    """向全局 DOMAIN_MAP 注册业务领域（由插件装配或测试初始化注入）。"""
    DOMAIN_MAP[domain_name] = domain_spec


def unregister_domain(domain_name: str) -> None:
    """从全局 DOMAIN_MAP 注销业务领域。"""
    DOMAIN_MAP.pop(domain_name, None)



# ---------------------------------------------------------------------------
# 核心接口 (设计 3.1)
# ---------------------------------------------------------------------------

@dataclass
class SelectionResult:
    names: Optional[List[str]]      # None=全量, []=纯文本, [...]=子集（已按优先级排序）
    domains: List[str]              # 命中的领域标签
    confidence: str                 # "high" | "medium" | "low" | "none"
    reason: str                     # 命中原因描述
    read_only_mode: bool = False    # 仅注入默认工具（未命中任何 explicit_patterns 即 True，设计 3.4）


class RequestSelector:
    """
    动态工具选择器——在 Agent.__init__ 中构造一次（设计 3.5），
    __init__ 内完成启动校验: DOMAIN_MAP 引用有效性 + 未映射技能可见 (设计 3.6)。
    """

    def __init__(self, skill_engine, domain_map: Optional[Dict[str, Dict]] = None):
        self.engine = skill_engine
        self._registered = skill_engine.get_all_names()
        self._miss_count = 0
        self.domain_map = deepcopy(domain_map if domain_map is not None else DOMAIN_MAP)
        self._rebuild_cache()

    def register_domain(self, domain: str, cfg: Dict[str, Any]) -> None:
        """动态注册业务领域（由插件装配或测试注入）。"""
        if domain in self.domain_map:
            raise ValueError(f"领域已登记，禁止覆盖: {domain}")
        old_cache = self._name_to_domain
        self.domain_map[domain] = deepcopy(cfg)
        try:
            self._rebuild_cache()
        except Exception:
            self.domain_map.pop(domain, None)
            self._name_to_domain = old_cache
            raise

    def unregister_domain(self, domain: str) -> None:
        """动态注销业务领域。"""
        self.domain_map.pop(domain, None)
        self._rebuild_cache()

    def _rebuild_cache(self) -> None:
        self._registered = self.engine.get_all_names()
        # 校验 1: domain_map 引用了不存在的技能
        mapped = set()
        unmapped_refs = []
        for domain, cfg in self.domain_map.items():
            for name in cfg.get("default_tools", []) + cfg.get("explicit_intent_tools", []):
                mapped.add(name)
                if name not in self._registered:
                    unmapped_refs.append((domain, name))

        if unmapped_refs:
            for domain, name in unmapped_refs:
                print(f"⚠️ [RequestSelector] 领域 '{domain}' 引用了不存在的技能: {name}")
            if os.environ.get("LITE_AGENT_STRICT_SELECTOR") == "1":
                raise RuntimeError(f"RequestSelector 映射错误: {unmapped_refs}")

        # 校验 2: 未映射技能可见（P2-3: 未映射技能只在 names=None 回退全量时可用，不参与领域裁剪）
        unmapped_skills = self._registered - mapped
        if unmapped_skills:
            print(f"ℹ️ [RequestSelector] 未映射技能 ({len(unmapped_skills)} 个): "
                  f"{sorted(unmapped_skills)}")

        # name -> domain 反查表（用于 tool_calls 反推与 miss 分析）
        self._name_to_domain: Dict[str, str] = {}
        for domain, cfg in self.domain_map.items():
            for name in cfg.get("default_tools", []) + cfg.get("explicit_intent_tools", []):
                self._name_to_domain[name] = domain

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def select(
        self,
        text: str,
        history: Optional[List[dict]] = None,
        is_guest: bool = False,
        max_tools: int = 15,
    ) -> SelectionResult:
        """
        三态返回:
          []  -> 明确闲聊/问答
          None -> 不确定，回退全量
          [...] -> 明确领域子集（已按优先级排序：动作 > 默认 > 其他显式）
        不在此处检测漏选，检测在 agent.py 出口统一完成 (设计 3.3)。
        """
        return self._initial_select(text, history, max_tools, is_guest)

    def record_miss(self, result: SelectionResult) -> None:
        """
        漏选检测：只计数 + 打日志，不改变任何行为。
        调用侧由 selector_result 门控：shadow/默认阶段恒为 None，本方法不会被
        调用，漏选统计只在 ENABLED 阶段产生真实数据。
        注意：日志只含 domain/names，不含用户原文（与第 6 节风险表一致）。
        """
        self._miss_count += 1
        print(f"⚠️ [RequestSelector] selection_miss #{self._miss_count}: "
              f"domains={result.domains}, confidence={result.confidence}, "
              f"names={result.names}")

    # ------------------------------------------------------------------
    # 内部实现 (设计 3.4)
    # ------------------------------------------------------------------

    def _match_domains(self, text: str) -> List[str]:
        """按 self.domain_map 声明顺序返回命中领域（即命中优先级顺序）。"""
        text = text or ""
        return [d for d, cfg in self.domain_map.items()
                if cfg.get("pattern") and re.search(cfg["pattern"], text)]

    def _domains_from_tool_calls(self, tool_calls) -> List[str]:
        """由 assistant 消息的 tool_calls 反推领域（设计 3.4 步骤 2）。"""
        domains: List[str] = []
        for tc in tool_calls or []:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            name = func.get("name", "") if isinstance(func, dict) else ""
            domain = self._name_to_domain.get(name)
            if domain and domain not in domains:
                domains.append(domain)
        return domains

    def _write_inherit_allowed(self, history, current_text: str, domains) -> bool:
        """
        v6/v7 三条件守卫，全部满足才允许从历史继承写意图：
        1. 当前用户消息是短确认词（CONFIRM_WORDS，长度 <= 6）
        2. 最近一条助手消息是明确的操作确认请求（含"确认"且以 ? / ？ 结尾）
        3. 该确认请求与继承 domain 相关——只认该 domain 自己的 pattern 或
           explicit_patterns，不用跨领域通用动作词兜底（v7 P2-2 修正：
           通用词"发布/推送/同步"会让 mail 域误继承 blog 的写意图）
        普通陈述如"已确认发布完成"、"还需要查看结果吗？"会被条件 1/2 拒绝。
        """
        # 条件 1：当前消息必须是短确认词
        stripped = (current_text or "").strip()
        if stripped not in CONFIRM_WORDS or len(stripped) > 6:
            return False
        # 条件 2+3：最近一条助手消息必须是带"确认"的问句，且与继承 domain 相关
        for msg in reversed(history[-4:] if history else []):
            if msg.get("role") == "assistant":
                c = (msg.get("content") or "").strip()
                is_confirm_request = ("确认" in c) and c.endswith(("?", "？"))
                if not is_confirm_request:
                    return False
                # v7 P2-2 domain 相关性：只认本 domain 的 pattern / explicit_patterns
                #（explicit_patterns 为 None 的域用永不匹配的 r"$^"）
                domain_hit = any(
                    self.domain_map.get(d, {}).get("pattern") and
                    re.search(self.domain_map[d]["pattern"], c)
                    for d in domains
                )
                domain_action_hit = any(
                    re.search(self.domain_map.get(d, {}).get("explicit_patterns") or r"$^", c)
                    for d in domains
                )
                return bool(domain_hit or domain_action_hit)
        return False

    def _initial_select(self, text: str, history, max_tools: int,
                        is_guest: bool = False) -> SelectionResult:
        domains = self._match_domains(text)
        confidence = "high"
        write_scope_text = text          # 领域来源消息；默认=当前消息

        # 1. 当前消息无命中 -> 从历史继承（领域 + 来源消息文本）
        if not domains and history:
            for msg in reversed(history[-6:]):  # 最近 3 轮
                if msg.get("role") == "user":
                    inherited = self._match_domains(msg.get("content", ""))
                    if inherited:
                        domains = inherited
                        confidence = "medium"
                        # v6 P2-1：三条件守卫（短确认词+确认请求+domain 一致）
                        # Phase 0 实施修正（验收 #17）：守卫拒绝时写意图完全不继承——
                        # write_scope_text 置空，防止"已确认发布完成"类含动作词的
                        # 普通陈述经当前消息一侧绕过守卫注入写工具
                        if self._write_inherit_allowed(history, text, domains):
                            write_scope_text = msg.get("content", "")
                        else:
                            write_scope_text = ""
                        break

        # 2. 助手上一轮有工具调用 -> 反推 domain（来源视为当前消息，不继承写意图）
        if not domains and history:
            for msg in reversed(history[-4:]):
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    inferred = self._domains_from_tool_calls(msg["tool_calls"])
                    if inferred:
                        domains = inferred
                        confidence = "medium"
                        break

        # 3. 仍无命中 → 返回 None（不确定）
        if not domains:
            return SelectionResult(names=None, domains=[], confidence="none",
                                   reason="无关键词命中", read_only_mode=False)

        # 4. 按 domain 优先级并集（双文本检查 explicit 意图）
        return self._merge_domains(
            domains, text, write_scope_text, max_tools, is_guest,
            confidence=confidence,
        )

    def _merge_domains(self, domains: List[str], text: str, write_scope_text: str,
                       max_tools: int, is_guest: bool = False,
                       confidence: str = "high") -> SelectionResult:
        default_names: List[str] = []
        explicit_matched = False
        action_names: List[str] = []      # v6 P1-2：explicit_routes 明确命中的动作工具，最高优先级
        other_explicit: List[str] = []    # 兜底 explicit_intent_tools（未走 routes 的域）
        for d in domains:                                  # domains 按命中优先级排序
            cfg = self.domain_map[d]
            default_names += cfg["default_tools"]
            ep = cfg.get("explicit_patterns")
            # 写意图唯一来源 = write_scope_text（3.4 写意图继承）:
            #   直接命中时 = 当前消息；历史继承时仅当三条件守卫批准才 = 来源消息，
            #   否则为空串——防止"已确认发布完成"类陈述经当前消息动作词绕过守卫（验收 #17）
            if ep and write_scope_text and re.search(ep, write_scope_text):
                explicit_matched = True
                routes = cfg.get("explicit_routes")
                # v7 P2-1 匹配语义（写死）：routes 允许多匹配——不同 route 代表正交动作；
                # 但同族动作必须通过互斥 pattern 保证只命中一个：具体规则排在前面 + 通用规则用负向后行断言排除细分词。
                if routes:
                    routed = False
                    for pat, tools in routes:
                        if write_scope_text and re.search(pat, write_scope_text):
                            action_names += tools
                            routed = True
                    if not routed:
                        other_explicit += cfg["explicit_intent_tools"]   # 域兜底
                else:
                    other_explicit += cfg["explicit_intent_tools"]

        # read_only_mode 计算规则（v5 写死，无例外）：
        #   read_only_mode = 当且仅当未命中任何已选 domain 的 explicit_patterns
        #   （检查范围含写意图继承的来源消息）
        read_only_mode = not explicit_matched

        # v6 P1-2 分层拼装：明确动作 > 各域默认 > 其他候选。
        mandatory = list(dict.fromkeys(action_names + default_names))

        # v7 P1 数学保证：mandatory 超过上限时回退全量，不静默裁剪默认工具
        if len(mandatory) > max_tools:
            return SelectionResult(
                names=None,  # 复杂跨域请求回退全量
                domains=domains,
                confidence="low",
                reason=f"必要工具({len(mandatory)})超过上限({max_tools})，回退全量",
                read_only_mode=False,
            )

        remaining = max_tools - len(mandatory)
        other_explicit_dedup = list(dict.fromkeys(other_explicit))

        # v7 P1：other_explicit 放不下时也回退全量，不静默删除可能需要的能力
        if len(other_explicit_dedup) > remaining:
            return SelectionResult(
                names=None,
                domains=domains,
                confidence="low",
                reason=f"显式工具({len(other_explicit_dedup)})超过剩余配额({remaining})，回退全量",
                read_only_mode=False,
            )

        ordered = self._apply_guest_filter(mandatory + other_explicit_dedup, is_guest)

        return SelectionResult(
            names=ordered, domains=domains, confidence=confidence,
            reason=f"命中领域 {domains}" + ("（含显式意图工具）" if explicit_matched else "（仅默认工具）"),
            read_only_mode=read_only_mode,
        )

    def _apply_guest_filter(self, names: List[str], is_guest: bool) -> List[str]:
        """Guest 安全 (设计 3.10)：使用公开接口 get_guest_schemas()，不触碰 engine 私有结构。"""
        if not is_guest:
            return names
        guest_names = {schema["function"]["name"]
                       for schema in self.engine.get_guest_schemas()}
        return [n for n in names if n in guest_names]
