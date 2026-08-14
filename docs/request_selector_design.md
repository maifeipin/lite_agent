# RequestSelector 动态工具选择 - 设计方案 (v7)

> **背景任务**: VPS1 待办 3539db - 优化主路径：引入 RequestSelector 动态工具选择与任务分流
> **状态**: Phase 0 / Gate 1 已通过；Phase 1 Shadow 自 2026-08-14 起在 VPS1 运行，`ENABLED=0`，等待 2026-08-28 Gate 2 评审
> **日期**: 2026-08-14
> **下一阶段待办**: VPS1 TODO `cb5ddf`（2026-08-28 10:00 CST 到期，提前 1 天提醒）

## 0. 实施状态与强制评审门（后续工作上下文入口）

本节记录已经发生的实施事实和后续推进约束。恢复任务时先读本节，再结合第 7 节的设计与验收细节执行；不得仅凭环境变量直接跨过评审门。

| Phase | 内容 | 里程碑 | 评审要求 | 当前状态 |
|---|---|---|---|---|
| Phase 0 | Selector、启动校验、单元/集成测试 | M1：20 类验收全部通过 | **Gate 1，强制** | ✅ 2026-08-14 通过 |
| Phase 1 | Shadow 运行 1–2 周，真实请求仍使用全量工具 | M2：Shadow 数据报告 | **Gate 2，强制** | 🔄 运行中（2026-08-14 至 2026-08-28） |
| Phase 2 | 开启真实裁剪和 miss 计数 | M3：生产效果与安全验证 | **Gate 3，强制** | ⏸ 未开始 |
| Phase 3 | 评估扩展到 Planner/编排路径 | M4：Planner 扩展决策 | 单独架构评审，可选 | ⏸ 未开始 |

### 0.1 已上线代码与生产快照

- `1b0bea3 feat(selector): add shadow-safe request tool selection`
  - 新增 `core/request_selector.py`、DOMAIN_MAP、启动校验、三态选择、历史继承、显式动作保护、Shadow/Enabled 三分支和漏选检测。
  - 默认与 Shadow 路径继续注入原有全量工具；只有 `LITE_AGENT_SELECTOR_ENABLED=1` 才真实裁剪。
- `1816c6a fix(session): serialize immutable tool call metadata`
  - 修复生产 smoke 暴露的嵌套 `mappingproxy` 无法写入 Session 问题，并保留 Gemini `thought_signature: bytes` 的持久化往返。
- VPS1 当前开关：`LITE_AGENT_SELECTOR_SHADOW=1`、`LITE_AGENT_SELECTOR_ENABLED=0`。
- VPS1 API smoke：`todo` 请求得到 Shadow 候选 2 个工具，真实请求仍走全量工具；`todo_list` 调用成功，HTTP 200。
- 真实技能注册表：83 个技能；DOMAIN_MAP 不存在引用 0 个；未映射 3 个：`ops_decision`、`ops_rss_node_status`、`sync_meili`。
- 验证记录：20 类设计验收均覆盖；Selector/Agent 定向回归 147 项通过；非账本测试 288 项通过；VPS1 Python 3.9 编译通过。
- 全量 Windows 测试曾出现 54 个 `test_execution_ledger.py` SQLite 文件句柄 teardown 错误；用例主体通过，判定为既有 Windows 测试环境问题，未修改无关账本代码。

### 0.2 Gate 2 必交材料

正式评审待办：VPS1 TODO `cb5ddf`，到期时间 `2026-08-28T10:00:00+08:00`，提前 1440 分钟提醒。报告统计窗口固定为 2026-08-14 至 2026-08-28，至少包含：

1. domain 命中比例；
2. `None` / `[]` / 子集三态分布；
3. 平均候选工具数；
4. `mandatory` 或显式工具超过 15 后回退全量的比例；
5. 典型误判案例；
6. 未映射技能清单及是否需要补映射；
7. Shadow 日志敏感信息复核；
8. 是否允许进入 Phase 2 的明确结论、观察期和回滚条件。

**统计口径 caveat**：当前管理员请求无领域命中时返回 `None`；`[]` 主要由 Guest 过滤后为空产生，不能把 `[]` 直接解释为全部闲聊比例。现有 Shadow 日志可统计 domain、候选数量、confidence 和 read-only，但没有请求原文；典型误判案例应在不复制敏感内容的前提下结合会话人工复核。

**硬约束**：Gate 2 未形成书面报告并经人工评审通过前，不得设置 `LITE_AGENT_SELECTOR_ENABLED=1`。Phase 2 通过 Gate 3 前，不得自动进入 Phase 3；Phase 3 是新的可选架构决策。

### 0.3 关联安全待办

- VPS1 TODO `506508`：修复 Telegram 凭据暴露于进程参数和服务状态的问题。待办与日志中不得记录实际凭据值；完成实现后应轮换相关凭据。

**v7 修改清单**（对应 v6 复审意见）：

| 编号 | 意见 | v7 修正 | 位置 |
|---|---|---|---|
| P1 | `ordered[:max_tools]` 在 `action+default` 本身超 15 时仍裁剪默认工具，"不裁剪"保证数学上不成立 | `mandatory` 超上限时返回 `names=None` 回退全量；`other_explicit` 超剩余配额时同样回退全量——任何情况都不静默裁剪 | 3.4 |
| P2-1 | "只抓取邮件"同时命中裸词"抓取"和"只抓取"，注入两个工具 | 具体规则（`仅拉取\|只抓取\|只拉取`）在前 + 通用规则负向后行断言 `(?<!只)(?<!仅)抓取邮件`；匹配语义写死：跨 route 允许多匹配（正交动作），同族动作 pattern 必须互斥 | 3.2 / 3.4 |
| P2-2 | 守卫条件 3 的通用动作词（发布/推送/同步…）会让 mail 域误继承 blog 写意图 | 条件 3 只认继承 domain 自己的 `pattern` / `explicit_patterns`（None 用永不匹配的 `r"$^"`），删除跨领域通用动作词兜底 | 3.4 |

---

**v6 修改清单**（对应 v5 复审意见）：

| 编号 | 意见 | v6 修正 | 位置 |
|---|---|---|---|
| P1-1 | `names: list[str] \| None` 在 Python 3.9 无 `__future__` 时类定义阶段即 TypeError | 全部注解改为 `Optional[List[...]]` / `List[...]`（`from typing import ...`）；Phase 0 验收新增 VPS1 Python 3.9 `python -m py_compile` 编译测试 | 3.1 |
| P1-2 | 跨域拼装 `ordered[:15]` 直接截断，尾部域的明确动作工具可能被裁掉 | 引入 `explicit_routes` 动作级路由（todo/mail 示例化）；`_merge_domains` 改三层拼装 `action_names > default_names > other_explicit`，`max_tools` 只截断尾部 other_explicit | 3.2 / 3.4 |
| P2-1 | 确认守卫过宽："已确认发布完成"等普通陈述也通过 | 三条件同时满足才允许继承：当前消息是短确认词（CONFIRM_WORDS ≤6 字）+ 最近助手消息是带"确认"的问句 + 与继承 domain 相关（命中 domain pattern 或含发布/推送/同步等动作词） | 3.4 |
| P2-2 | `had_tool_calls` 为 run 级标志，"先调用工具、后发现缺工具"不计数 | 固定标记 `[TOOLSET_MISS]` 出现即计数（允许 run 内有 tool_calls）；仅中文降级信号才要求整轮无 tool_calls；标记无条件剥离防泄漏 | 3.3 |
| P2-3 | web pattern 仍含宽泛"链接"，与 3.9 收紧说明不一致 | web pattern 移除"抓取\|链接"，与 3.9 表统一为 `http://\|https://\|剪藏.*链接` | 3.2 |

---

**v5 修改清单**（对应 v4 复审意见）：

| 编号 | 意见 | v5 修正 | 位置 |
|---|---|---|---|
| P1-1 | miss 钩子引用了 `_stream_ai_loop` 中不存在的 `final_text` | 接入点改到 `RuntimeEventType.DONE` 分支（agent.py:1099）读取 `content`；新增 run 级 `had_tool_calls` 标志；system prompt 改用固定标记 `[TOOLSET_MISS]`，中文信号仅作降级兜底 | 3.3 |
| P1-2 | 多轮只继承 domain，不继承写意图（"确认"拿不到 publish 工具） | `_initial_select` 追踪领域来源消息 `write_scope_text`；`_merge_domains` 对当前文本与来源消息双检查；新增"确认类追问"守卫 `_write_inherit_allowed` | 3.4 |
| P1-3 | 3.7 示例无 feature flag，实施即改变生产行为 | 新增 `LITE_AGENT_SELECTOR_ENABLED` / `SHADOW` 三分支：enabled 真实裁剪、shadow 全量+记录、默认全量现状；selector 异常兜底全量 | 3.7 |
| P2 | 4 个工具副作用与"无落盘"定义不一致 | 按用户风险重定义：`default_tools`（查询意图直接可用，允许内部缓存/审计） / `explicit_intent_tools`（需明确拉取/推送/同步/发布/修改意图）；`mail_fetch_summaries`、`todo_push_brief` 移入 explicit，`web_clip`、`sentinel_scan` 留在 default 并说明理由 | 3.2 |

---

**v4 修改清单**（对应 v3 复审意见）：

| 编号 | 意见 | v4 修正 | 位置 |
|---|---|---|---|
| P1-1 | Guest 过滤访问不存在的 `engine._skill_registry` | 改用公开接口 `engine.get_guest_schemas()` | 3.10 |
| P1-2 | "漏选恢复"的 `expand()` 未接入链路，`_miss_count` 永不增长 | 改名"漏选检测"；新增 `record_miss()` 并接入 `agent.py` 出口钩子；`expand()` 移至 3.3.2 后续增强 | 3.3 / 3.7 |
| P2-1 | `blog_export_articles` 等副作用工具错置 read_tools | `blog_export_articles` 移入 write_tools；写死归类判定原则表 | 3.2 |
| P2-2 | `read_only_mode` 计算规则未写死 | 规则写死：未命中任何已选 domain 的 write_patterns 即 True，并给出三条推论 | 3.4 |
| P2-3 | 未映射技能可用范围不清 | 写死：仅在 `names=None` 全量兜底路径可用，附三态对照表 | 3.6 |

---

## 1. 问题定义

当前 lite_agent 注册了 **83 个 Skill**（有 tags: 18 个，无 tags: 65 个），每次 LLM 请求通过双通道全量注入：

| 注入通道 | 位置 | 实测大小 |
|---|---|---|
| `tools` 参数 (Function Calling) | `agent.py:929` → `model_invoker.py:145-147` | ~26,230 字符 JSON Schema |
| system prompt 文本清单 | `agent.py:936` → `_build_system_prompt()` → `agent.py:337` → `list_skills()` | ~8,082 字符 Markdown |

**后果**：
- 每次请求携带 ~34KB 工具描述，首字延迟 (TTFT) 被拖慢
- 候选工具过多干扰中小模型 (glm-5.3/flash)，导致误调不相关工具
- 用户问"今天天气怎么样"也要背负 83 个工具的 Schema 开销
- 用户问"查看待办"也会看到 todo_done/todo_drop 等写工具，存在误调风险

---

## 2. 当前架构关键事实

### 2.1 工具注入的实际代码路径

```
agent.py:_handle_locked() 或 handle_stream()
  └─ 普通消息 → _stream_ai_loop() (agent.py:911)
       │
       │  // tools 参数注入
       ├─ agent.py:924-931:
       │    if msg.is_guest:
       │        tools = get_guest_schemas()       ← 访客白名单
       │    else:
       │        tools = get_all_schemas()          ← 管理员全量 83 个
       │
       │  // system prompt 注入
       ├─ agent.py:936:
       │    system_content = _build_system_prompt(is_guest=msg.is_guest)
       │    └─ agent.py:337: skills_summary = list_skills(is_guest=is_guest)
       │       ← 同样全量注入
       │
       └─ agent_runtime.py: run(messages, tools, ctx)
            └─ model_invoker.py: call_kwargs["tools"] = tools
```

**注意**: `agent.py:309` 的 `self.system_prompt = self._build_system_prompt()` 仅在 `__init__` 中赋值但**无任何引用**，是死代码。实际每轮请求都在 `agent.py:936` 重新调用 `_build_system_prompt(is_guest=...)`。

### 2.2 访客权限模型

- `get_guest_schemas()` 按 `policy.guest_ok` 过滤 → tools 参数安全
- `list_skills(is_guest=True)` 同样按 `guest_ok` 过滤 → system prompt 安全
- **`list_skills_filtered(names)` 无 guest 过滤参数** → 必须由调用方保证 names 已被 guest 过滤

### 2.3 SkillPolicy 副作用元数据

- `SkillPolicy.side_effect` 字段在 [execution.py:292](file:///c:/Projects/Pys/lite_agent/core/execution.py#L292) 已定义
- **`skills/` 目录零声明 `side_effect=True`**（grep 无命中）—— 副作用信息不可靠
- **本方案不依赖此元数据**，读写分层通过显式 domain 配置手动维护

### 2.4 多轮对话上下文

- `session_mgr.get_history(msg.session_key)` 返回完整历史消息（agent.py:944）
- RequestSelector 若只看当前 `msg.text`，多轮追问（"继续"、"刚才那个"）会丢失领域上下文

### 2.5 模型绑定

- `self.model` / `self.client` / `self.model_invoker` 在 `Agent.__init__` 时固定（agent.py:110-145）
- **RequestSelector 无法改变当前请求的模型**，`model_hint` 不可在本方案中实现

### 2.6 编排路径

- `task_orchestrator.py:110` 的 `_plan()` 仍调 `get_all_schemas()` 构建 PLANNER_PROMPT
- **本方案只改主路径**，编排路径的 Token 开销不受影响

---

## 3. RequestSelector 设计方案 (v7)

### 3.1 核心接口

```python
from typing import List, Optional   # ★ VPS1 运行 Python 3.9：禁止裸写 list[str] | None
                                    #   （PEP 585 泛型 + PEP 604 联合均为 3.10+；
                                    #    无 __future__ import 时 | None 在类定义阶段即 TypeError）

@dataclass
class SelectionResult:
    names: Optional[List[str]]      # None=全量, []=纯文本, [...]=子集（已按优先级排序）
    domains: List[str]              # 命中的领域标签
    confidence: str                 # "high" | "medium" | "low" | "none"
    reason: str                     # 命中原因描述
    read_only_mode: bool = False    # 是否只注入默认工具（计算规则见 3.4：未命中任何 explicit_patterns 即 True）

class RequestSelector:
    def __init__(self, skill_engine):
        """单次初始化: 加载 registry、构建 DOMAIN_MAP 索引、运行启动校验"""

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
          [...] -> 明确领域子集（已按优先级排序：default 在前 explicit 在后）
        """
```

### 3.2 工具两级分层：按用户风险而非技术落盘 (v5 重定义，取代 v3/v4 的 read/write 术语)

**v4 的 "read_tools = 无落盘写入" 定义与实际技能行为不一致**（复审实证：`mail_fetch_summaries` 抓取邮件并账单入库、`todo_push_brief` 向 IM 推送、`web_clip` 写缓存且长文自动上传 HedgeDoc、`sentinel_scan` 追加写 findings 文件）。v5 改按**用户风险**划分：

- **`default_tools`**：可因查询意图直接使用。允许内部缓存、审计日志、结果投递等基础设施副作用，但不得改变业务数据、不得向外部系统主动推送。
- **`explicit_intent_tools`**：需要用户明确表达拉取、推送、同步、发布、修改、删除等意图才注入。
- **`explicit_patterns`**：触发 explicit 注入的意图关键词（匹配来源 = write_scope_text：直接命中时为当前消息，历史继承时为守卫批准的来源消息，见 3.4）。

每个 domain 显式拆分如下：

```python
DOMAIN_MAP = {
    "mail": {
        "pattern": r"(邮件|邮箱|收件箱|mail|inbox)",
        "default_tools": [
            "mail_list", "mail_show_headers",
            "mail_show_missed", "mail_search", "mail_stats",
        ],
        "explicit_intent_tools": [
            # v5: mail_fetch_summaries 抓取邮件+账单入库+LLM 摘要（耗时且有数据变更），从 default 移入
            "mail_fetch_summaries", "mail_fetch_only", "mail_llm_enrich",
            "mail_view_original", "mail_reprocess", "backfill_bodies",
        ],
        "explicit_patterns": r"(拉取|抓取|重新处理|重新解析|回填|补全|ocr|识别|摘要)",
        "explicit_routes": [
            # v7 P2-1：互斥修正——具体规则在前 + 负向后行断言，
            # "只抓取邮件"/"仅拉取邮件"不再同时命中通用规则（fetch_only 与 fetch_summaries 二选一）
            (r"仅拉取|只抓取|只拉取", ["mail_fetch_only"]),
            (r"抓取.*摘要|拉取.*摘要|摘要.*邮件|(?<!只)(?<!仅)抓取邮件|(?<!只)(?<!仅)拉取邮件",
                                          ["mail_fetch_summaries"]),
            (r"重新处理|重新解析",     ["mail_reprocess"]),
            (r"回填|补全",            ["backfill_bodies"]),
        ],
    },
    "billing": {
        "pattern": r"(账单|bill|invoice|还款|信用卡|交易|流水|对账|大额)",
        "default_tools": [
            "billing_report", "billing_due_soon", "billing_unpaid",
            "billing_reconcile", "billing_recent",
            "billing_txns_over", "billing_parse_health",
        ],
        "explicit_intent_tools": [
            "billing_mark_paid", "billing_fetch",
        ],
        "explicit_patterns": r"(标记.*已还|拉取最新|下载账单|同步账单|更新账单)",
    },
    "todo": {
        "pattern": r"(待办|todo|提醒|延期|搁置)",
        "default_tools": [
            "todo_list", "todo_get",
        ],
        "explicit_intent_tools": [
            # v5: todo_push_brief 向 IM 频道推送（外部推送副作用），从 default 移入
            "todo_push_brief",
            "todo_add", "todo_start", "todo_done", "todo_drop",
            "todo_snooze", "todo_shelve", "todo_resume",
            "todo_update", "todo_dispatch",
        ],
        # Phase 0 实施修正: "标记完成"→"标记.*完成"（验收 #6/#16 的"标记这个待办为完成"非连续命中，与 routes 语义对齐）
        "explicit_patterns": r"(添加|新建|标记.*完成|标记.*延期|搁置|恢复|更新|派发|删除|放弃|推送)",
        # v6 P1-2：动作级路由——命中后只注入对应子集，而非全量 explicit_intent_tools。
        # 避免"标记完成"一次注入 9 个写工具，也保证 max_tools 裁剪优先保留明确命中的动作。
        "explicit_routes": [
            (r"标记.*完成|完成.*待办", ["todo_done"]),
            (r"删除|放弃",             ["todo_drop"]),
            (r"推送|简报",             ["todo_push_brief"]),
            (r"添加|新建",             ["todo_add"]),
            (r"延期|搁置",             ["todo_snooze", "todo_shelve"]),
        ],
        # 兜底：命中 explicit_patterns 但未命中任何 explicit_routes 时，仍注入全量 explicit_intent_tools
        #（保持 v5 行为，防止动作词改写漏给）
    },
    "socks5": {
        "pattern": r"(代理|socks5|节点|出站)",
        "default_tools": [
            "socks5_list", "socks5_get_active",
            "socks5_test", "socks5_test_outbound",
        ],
        "explicit_intent_tools": [
            "socks5_add", "socks5_update", "socks5_delete",
            "socks5_set_active",
        ],
        "explicit_patterns": r"(添加|新增|删除|切换|设为.*主节点|更新.*节点)",
    },
    "security": {
        "pattern": r"(巡检|安全扫描|入侵|爆破|基线|漂移|审计)",
        "default_tools": [
            # v5: sentinel_scan 留在 default——findings 落盘属于审计记录（允许的内部副作用）
            "sentinel_scan", "sentinel_findings",
            "fleet_audit", "ops_security_audit",
            "edge_health", "edge_query",
        ],
        "explicit_intent_tools": [
            "sentinel_baseline", "edge_cmd", "edge_sweep",
        ],
        "explicit_patterns": r"(刷新基线|接受基线|下发命令|清理)",
    },
    "gaokao": {
        "pattern": r"(高考|位次|志愿|录取|院校|专业)",
        "default_tools": [
            "gaokao_score_rank", "gaokao_school_admission",
            "gaokao_major_admission", "gaokao_recommend",
            "gaokao_school_info", "gaokao_sql", "gaokao_stats",
        ],
        "explicit_intent_tools": [],
        "explicit_patterns": None,
    },
    "web": {
        "pattern": r"(网页|剪藏|http://|https://|剪藏.*链接)",
        "default_tools": [
            # v5: web_clip 留在 default——抓取用户显式给出的 URL 并交付 Markdown 属查询意图，
            # 缓存与 HedgeDoc 投递（>2500 字自动）属结果交付机制，不改变业务系统状态
            "web_search", "web_clip", "ops_web_fetch",
        ],
        "explicit_intent_tools": [
            "hedgedoc_upload", "ops_workspace_run",
        ],
        "explicit_patterns": r"(上传到\s*hedgedoc|写入|保存到)",
    },
    "blog": {
        "pattern": r"(博客|halo)",
        "default_tools": [
            "blog_list_articles",
        ],
        "explicit_intent_tools": [
            # v4 从 read 移入（导出落盘）；v5 重命名并按"导出=显式交付意图"归入 explicit_intent_tools
            "blog_export_articles",
            "blog_publish_article",
        ],
        "explicit_patterns": r"(发布|发表|更新文章|导出|导出文章)",
    },
    "backup": {
        "pattern": r"(备份|网盘|百度网盘|bypy)",
        "default_tools": [
            "bypy_info",
        ],
        "explicit_intent_tools": [
            "bypy_mkdir", "bypy_syncup", "bypy_syncdown",
            "ops_backup_data", "ops_backup_cloud",
        ],
        "explicit_patterns": r"(同步|备份|创建目录|上传|下载)",
    },
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
    "media": {
        "pattern": r"(音乐|歌曲|媒体库|封面|重复歌曲)",
        "default_tools": [
            "ops_media_music_stats", "ops_media_music_duplicates",
        ],
        "explicit_intent_tools": [],
        "explicit_patterns": None,
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
```

**默认行为**：只注入 `default_tools`。当用户消息（或领域来源的历史消息，见 3.4）命中 `explicit_patterns` 时，才合并 `explicit_intent_tools`。这样 "查看待办" 只会看到 2 个默认工具，模型无法误调 todo_done。

**归类判定原则（v5 按用户风险写死）**：

| 类别 | 判定标准 | 允许的副作用 | 示例 |
|---|---|---|---|
| `default_tools` | 查询意图可直接使用 | 内部缓存、审计日志（如 findings 落盘）、结果投递（如 HedgeDoc 链接交付）；**不允许**变更业务数据、向 IM 等外部频道推送 | `todo_list`、`sentinel_scan`（审计落盘）、`web_clip`（缓存+投递） |
| `explicit_intent_tools` | 需用户明确表达拉取/推送/同步/发布/修改/删除意图 | 任意 | `blog_publish_article`、`todo_push_brief`（IM 推送）、`mail_fetch_summaries`（抓取入库）、`bypy_syncup`（上传） |

**复审 4 个争议工具的 v5 归属**（逐一实证后）：

| 工具 | 实证行为 | v5 归属 | 理由 |
|---|---|---|---|
| `mail_fetch_summaries` | 批量抓取邮件 + 账单识别入库 + LLM 生成摘要，耗时 | **移入 explicit** | 抓取+入库是业务数据变更，且触发外部 LLM 消耗 |
| `todo_push_brief` | 汇总待办并推送到 IM 聊天频道 | **移入 explicit** | 向外部频道推送，超出"内部副作用"边界 |
| `web_clip` | 抓取 URL 写缓存，>2500 字或含图自动上传 HedgeDoc | **留在 default** | 抓取的是用户显式给出的 URL；缓存与 HedgeDoc 属结果交付机制，不改变业务系统状态 |
| `sentinel_scan` | 扫描后追加写 findings 文件、首次运行建基线 | **留在 default** | findings 落盘属审计记录（default 明确允许）；变更基线的是 `sentinel_baseline`（explicit） |

### 3.3 漏选检测机制 (v5 修正真实接入点)

v3 将本节称为"漏选恢复"并设计了 `expand()` 自动扩展，但 `expand()` 从未接入任何运行链路；v4 虽改名"漏选检测"，但示例引用了 `_stream_ai_loop()` 中**不存在的 `final_text`**（该函数只维护每步重置的 `step_text`，`final_text` 仅存在于外层兼容接口 `_run_ai_loop()`，SSE 主路径拿不到）。

**v5 真实接入点**（逐行核对 [agent.py](file:///c:/Projects/Pys/lite_agent/agent.py) 后确定）：

1. **最终全文**：`RuntimeEventType.DONE` 分支（agent.py:1099）的 `content = event.data.get("content", "")`（agent.py:1103）--这是 SSE 路径上唯一能拿到完整最终回复的位置。
2. **无工具调用判定**：run 级 `had_tool_calls` 标志，在 `TOOL_CALLS_READY`（agent.py:1023）/ `TOOL_CALL`（agent.py:1033）分支置 True。
3. **检测信号**：主信号为 system prompt 指示模型输出的固定标记 `[TOOLSET_MISS]`（见 3.7 提示词），不依赖不同模型恰好生成特定中文句子；`MISS_SIGNALS` 关键词仅作降级兜底。

```python
# agent.py _stream_ai_loop

# ---- 状态追踪区新增（agent.py:990-993 附近）----
had_tool_calls = False

# ---- 事件循环内新增 ----
elif t == RuntimeEventType.TOOL_CALLS_READY:      # agent.py:1023 现有分支
    had_tool_calls = True
    ...

elif t == RuntimeEventType.TOOL_CALL:             # agent.py:1033 现有分支
    had_tool_calls = True
    ...

elif t == RuntimeEventType.DONE:                  # agent.py:1099 现有分支
    if not terminal_error:
        content = event.data.get("content", "")   # agent.py:1103，即最终全文

        # ---- 漏选检测（3.3）：DONE 分支内完成 ----
        # selector_result 定义见 3.7 flag 分支：仅 ENABLED 且 candidate 有效时非 None，
        # 因此 shadow/默认/异常兜底路径此处恒不触发
        content = content.replace(TOOLSET_MISS_MARK, "").strip()  # ★ v6 P2-2：无条件剥离，防异常泄漏

        # v6 P2-2：固定标记出现即计数（允许 run 内有 tool_calls，解决"先调用后缺工具"漏统计）；
        # 仅中文降级信号才要求整轮无工具调用（防误报）
        mark_hit = TOOLSET_MISS_MARK in event.data.get("content", "")   # 剥离前判断
        signal_hit = any(s in content for s in MISS_SIGNALS)
        miss = (selector_result is not None            # 本次确实走了裁剪
                and selector_result.names               # 排除全量兜底 None 与闲聊空集 []
                and (mark_hit                           # 主信号：出现即计数，不要求无 tool_calls
                     or (signal_hit and not had_tool_calls)))  # 降级信号：仍要求无 tool_calls
        if miss:
            self.request_selector.record_miss(selector_result)

        self.session_mgr.add_message(msg.session_key, "assistant", content)
        ...
```

`select()` 本身不做任何恢复逻辑：

```python
def select(self, text, history=None, is_guest=False, max_tools=15):
    return self._initial_select(text, history, max_tools, is_guest)
    # 三态返回 [] / None / [...]；不在此处检测漏选，检测在 agent.py 出口统一完成
```

#### 3.3.1 检测信号与 record_miss 定义

`core/request_selector.py` 模块级：

```python
TOOLSET_MISS_MARK = "[TOOLSET_MISS]"   # 主信号：system prompt 指示模型输出（见 3.7）

MISS_SIGNALS = [                        # 降级兜底：模型未按指示输出标记时匹配
    "当前可用工具无法",
    "没有合适的工具",
    "我无法通过现有工具",
    "需要切换到更通用的对话模式",
]

class RequestSelector:
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
```

信号触发条件（v6 P2-2 放宽）：**DONE 的 `content` 含 `[TOOLSET_MISS]` 标记即计数**（允许 run 内有 `tool_calls`，解决"先调用工具、后发现缺工具"漏统计）；仅中文降级信号才要求整轮无 `tool_calls`（`had_tool_calls=False`）。全量兜底（`names is None`）与闲聊空集（`names == []`）不计数；shadow 阶段请求走全量工具、`selector_result` 恒为 None，天然不计数，漏选统计只在启用阶段产生真实数据（shadow 阶段记录的 candidate 命中分布另作误判分析用）。无论是否计数，`[TOOLSET_MISS]` 都会在持久化/展示前**无条件剥离**，防止异常路径向用户显示内部标记。

#### 3.3.2 expand() 自动扩展 -- 后续增强，不在 Phase 0-2 范围

真正的自动恢复需要 AgentRuntime 支持中断-扩展-重发的状态机改动：

```
runtime 工具循环中检测 miss 信号
  -> 暂停本轮 -> selector.expand() 合并工具集 -> 重建请求重发（限一次）
```

此链路涉及 runtime 状态机与 token 预算控制，推迟到 Phase 3 之后按 `selection_miss` 统计数据决定是否实施。若 1-2 周 shadow 数据显示漏选率 < 2%，则永不实施。

### 3.4 多轮上下文继承与合并规则 (v5 修正写意图继承)

**v4 缺陷**：多轮继承只回传 `domains`，`_merge_domains` 只用**当前消息**匹配 explicit_patterns。复审反例：

```
用户：发布这篇博客          -> 命中 blog + explicit（发布）
助手：确认发布吗？
用户：确认                  -> 继承 blog domain，但"确认"不匹配 explicit_patterns
                              -> 只注入 blog_list_articles，拿不到 blog_publish_article ❌
```

**v5 修正**：`_initial_select` 追踪领域来源消息 `write_scope_text`；当 domain 从历史继承时，explicit 意图同时检查当前消息与来源消息，并加"确认类追问"守卫防止写意图无限期残留。

**v6 P2-1 守卫收紧**：v5 守卫仅看助手消息是否问句/含"确认"，以下普通陈述会误通过——"已确认发布完成"、"已经确认处理成功"、"还需要查看结果吗？"。v6 要求**三条件同时满足**才允许继承写意图。

```python
CONFIRM_WORDS = {"确认", "是", "好的", "继续", "执行吧", "行", "可以", "嗯", "ok", "OK", "确定"}

def _write_inherit_allowed(self, history, current_text, domains) -> bool:
    """
    v6/v7 P2-1 三条件守卫，全部满足才允许从历史继承写意图：
    1. 当前用户消息是短确认词（CONFIRM_WORDS，长度 <= 6）
    2. 最近一条助手消息是明确的操作确认请求（含"确认"且以 ? / ？ 结尾）
    3. 该确认请求与继承 domain 相关——只认该 domain 自己的 pattern 或
       explicit_patterns，不用跨领域通用动作词兜底（v7 P2-2 修正：
       通用词"发布/推送/同步"会让 mail 域误继承 blog 的写意图）
    普通陈述如"已确认发布完成"、"还需要查看结果吗？"会被条件 1/2 拒绝。
    """
    # 条件 1：当前消息必须是短确认词
    if current_text.strip() not in CONFIRM_WORDS or len(current_text.strip()) > 6:
        return False
    # 条件 2+3：最近一条助手消息必须是带"确认"的问句，且与继承 domain 相关
    for msg in reversed(history[-4:]):
        if msg.get("role") == "assistant":
            c = (msg.get("content") or "").strip()
            is_confirm_request = ("确认" in c) and c.endswith(("?", "？"))
            if not is_confirm_request:
                return False
            # v7 P2-2 domain 相关性：只认本 domain 的 pattern / explicit_patterns
            #（explicit_patterns 为 None 的域用永不匹配的 r"$^"）
            domain_hit = any(re.search(DOMAIN_MAP[d]["pattern"], c) for d in domains)
            domain_action_hit = any(
                re.search(DOMAIN_MAP[d].get("explicit_patterns") or r"$^", c)
                for d in domains
            )
            return bool(domain_hit or domain_action_hit)
    return False
```

```python
def _initial_select(self, text, history, max_tools, is_guest=False):
    domains = self._match_domains(text)
    confidence = "high"
    write_scope_text = text          # ★ 领域来源消息；默认=当前消息

    # 1. 当前消息无命中 -> 从历史继承（领域 + 来源消息文本）
    if not domains and history:
        for msg in reversed(history[-6:]):  # 最近 3 轮
            if msg.get("role") == "user":
                inherited = self._match_domains(msg.get("content", ""))
                if inherited:
                    domains = inherited
                    confidence = "medium"
                    # ★ v6 P2-1：三条件守卫（短确认词+确认请求+domain 一致）
                    # ★ Phase 0 实施修正（验收 #17）：守卫拒绝时写意图完全不继承——
                    #   write_scope_text 置空，防止"已确认发布完成"类含动作词的普通
                    #   陈述经当前消息一侧绕过守卫注入写工具
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
    return self._merge_domains(domains, text, write_scope_text, max_tools, is_guest)
```

```python
def _merge_domains(self, domains: list, text: str, write_scope_text: str,
                   max_tools: int, is_guest: bool = False) -> SelectionResult:
    default_names: list = []
    explicit_matched = False
    action_names: list = []      # v6 P1-2：explicit_routes 明确命中的动作工具，最高优先级
    other_explicit: list = []    # 兜底 explicit_intent_tools（未走 routes 的域）
    for d in domains:                                  # domains 按命中优先级排序
        cfg = DOMAIN_MAP[d]
        default_names += cfg["default_tools"]
        ep = cfg.get("explicit_patterns")
        # ★ 写意图唯一来源 = write_scope_text（Phase 0 实施修正，验收 #17）:
        #   直接命中时 = 当前消息；历史继承时仅当三条件守卫批准才 = 来源消息，
        #   否则为空串——防止"已确认发布完成"类陈述经当前消息动作词绕过守卫
        if ep and write_scope_text and re.search(ep, write_scope_text):
            explicit_matched = True
            routes = cfg.get("explicit_routes")
            # v7 P2-1 匹配语义（写死）：routes 允许多匹配——不同 route 代表正交动作
            # （如"抓取摘要并回填"应同时得到两个动作工具）；但**同族动作**（如
            # mail_fetch_only / mail_fetch_summaries）必须通过互斥 pattern 保证只命中
            # 一个：具体规则排在前面 + 通用规则用负向后行断言排除细分词。
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

    # ★ read_only_mode 计算规则（v5 写死，无例外）：
    #   read_only_mode = 当且仅当未命中任何已选 domain 的 explicit_patterns
    #   （检查范围含写意图继承的来源消息）
    read_only_mode = not explicit_matched

    # v6 P1-2 分层拼装：明确动作 > 各域默认 > 其他候选。
    mandatory = list(dict.fromkeys(action_names + default_names))

    # ★ v7 P1 数学保证：mandatory 超过上限时回退全量，不静默裁剪默认工具
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

    # ★ v7 P1：other_explicit 放不下时也回退全量，不静默删除可能需要的能力
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
        names=ordered, domains=domains, confidence="high",
        reason=f"命中领域 {domains}" + ("（含显式意图工具）" if explicit_matched else "（仅默认工具）"),
        read_only_mode=read_only_mode,
    )
```

推论（由写死的规则直接导出，无额外分支）：
- `read_only_mode=True` 时 `names` 中必然不含任何 explicit_intent_tools；
- `read_only_mode=False` 时至少一个 domain 的 explicit_intent_tools 已并入；
- 多 domain 请求中任一 domain 命中 explicit_patterns（含继承源消息），整体即为 `read_only_mode=False`；
- **v6 P1-2**：命中 `explicit_routes` 的动作工具排在 `ordered` 头部；
- **v7 P1 数学保证**：`mandatory`（动作+默认）超过 `max_tools` 时**回退全量**；`other_explicit` 超过剩余配额时也**回退全量**——任何情况下都不静默裁剪工具。

复审反例在 v7 下的执行路径：
- **写意图继承（P1-2 复审原反例）**：`"确认"` 无领域命中 -> 从历史继承 blog（来源消息"发布这篇博客"）-> 三条件守卫：条件 1 "确认" ∈ CONFIRM_WORDS ✓；条件 2 助手消息"确认发布吗？"含"确认"且问号结尾 ✓；条件 3 blog 的 explicit_patterns `(发布|发表|...)` 命中"发布" ✓ -> `write_scope_text="发布这篇博客"` -> explicit_patterns 匹配 -> blog 无 explicit_routes -> `blog_publish_article` 进入 other_explicit -> 注入 ✅。反向场景：发布完成、助手回复正常陈述后用户说"谢谢" -> 条件 1 拒绝 -> 仅默认工具注入 ✅
- **跨域守卫拒绝（v7 P2-2 新增）**：用户上一条是"抓取邮件摘要"，助手问"确认发布吗？"，用户说"确认" -> 继承 mail domain -> 条件 3：mail 的 pattern `(邮件|邮箱|...)` 与 explicit_patterns `(拉取|抓取|...)` 均不命中"确认发布吗？" -> 守卫拒绝 -> 不继承写意图，仅 mail 默认工具注入 ✅
- **显式动作不丢（v6 新增）**：`"抓取邮件并同步到网盘"` -> mail+backup 命中 -> mail explicit_routes：`(?<!只)(?<!仅)抓取邮件` 匹配"抓取邮件"（前面无"只/仅"）-> `mail_fetch_summaries` 入 action_names；backup explicit_patterns 匹配"同步"但无 routes -> 全部 backup explicit 入 other_explicit；mandatory = 1+5+1 = 7，other_explicit = 5，remaining = 8 ≥ 5，全部注入 ✅
- **路由互斥（v7 P2-1 新增）**：`"只抓取邮件"` -> 路由 1 `只抓取` 命中 -> `mail_fetch_only`；路由 2 的 `(?<!只)抓取邮件` 因前缀"只"被负向后行断言拒绝 -> 只注入 `mail_fetch_only`，不再同时注入 `mail_fetch_summaries` ✅
- **v7 回退场景**：同时命中 mail(5+6) + billing(7+2) + system(5+1) + security(6+3) -> mandatory = 5+7+5+6 = 23 > 15 -> 回退全量，不静默裁剪默认工具 ✅

### 3.5 实例化位置 (v2 评审 P1-3)

启动校验必须只执行一次：

```python
# agent.py Agent.__init__
self.skill_engine = SkillEngine()
self.request_selector = RequestSelector(self.skill_engine)  # 启动校验在此完成

# agent.py _stream_ai_loop：直接复用实例，完整调用与 feature flag 三分支见 3.7
# 不再每次请求都 new 一个 selector
```

### 3.6 启动校验 (P1-3 修正；含 P2-3 未映射技能可用范围)

`RequestSelector.__init__` 中执行三类校验：

```python
def __init__(self, skill_engine):
    self.engine = skill_engine
    self._registered = skill_engine.get_all_names()
    self._miss_count = 0

    mapped = set()
    unmapped_refs = []  # DOMAIN_MAP 引用了不存在的技能
    for domain, cfg in DOMAIN_MAP.items():
        for name in cfg.get("default_tools", []) + cfg.get("explicit_intent_tools", []):
            mapped.add(name)
            if name not in self._registered:
                unmapped_refs.append((domain, name))

    if unmapped_refs:
        for domain, name in unmapped_refs:
            print(f"⚠️ [RequestSelector] 领域 '{domain}' 引用了不存在的技能: {name}")
        if os.environ.get("LITE_AGENT_STRICT_SELECTOR") == "1":
            raise RuntimeError(f"RequestSelector 映射错误: {unmapped_refs}")

    # 未映射技能可见
    unmapped_skills = self._registered - mapped
    if unmapped_skills:
        print(f"ℹ️ [RequestSelector] 未映射技能 ({len(unmapped_skills)} 个): "
              f"{sorted(unmapped_skills)}")
        # 这些技能的可用范围见下方说明（仅全量兜底路径可用）
        # P2-3: 未映射技能只在 names=None 回退全量时可用，不参与领域裁剪

    # 构建 name -> domain 反查表（用于 tool_calls 反推与 miss 分析）
    self._name_to_domain = {}
    for domain, cfg in DOMAIN_MAP.items():
        for name in cfg.get("default_tools", []) + cfg.get("explicit_intent_tools", []):
            self._name_to_domain[name] = domain
```

**未映射技能的可用范围（v4 明确写死，P2-3）**：

未映射技能**只在 `names=None`（不确定 -> 全量兜底）路径下可用**：

| selector 返回 | 未映射技能是否注入 |
|---|---|
| `None`（不确定，回退全量） | **可用** -- 走 `get_all_schemas()` / `list_skills(is_guest=...)` 原全量通道 |
| `[...]`（领域子集） | **不可用** -- 子集仅由 DOMAIN_MAP 的 read/write 工具构成 |
| `[]`（闲聊空集） | **不可用** -- 无任何工具 |

推论：新技能上线后若未及时映射 DOMAIN_MAP，则该技能**仅在全量兜底请求中可被调用**，任何命中领域的请求都看不到它。这是有意设计而非缺陷--宁可少给，也不能因映射不全导致领域子集不可控。配套流程：新增技能必须同步更新 DOMAIN_MAP（或明确接受其仅兜底可用），启动日志的未映射列表即为审计入口。

### 3.7 agent.py 改动 (v5 重写：feature flag 三分支 + 真实接入点)

```python
# 1. _build_system_prompt 签名扩展 (agent.py:334)
def _build_system_prompt(self, is_guest: bool = False, skill_names: list = None,
                        read_only_mode: bool = False) -> str:
    if skill_names is not None:
        skills_summary = self.skill_engine.list_skills_filtered(skill_names)
    else:
        skills_summary = self.skill_engine.list_skills(is_guest=is_guest)

    extra_hint = ""
    if skill_names is not None and len(skill_names) > 0:
        extra_hint = ("\n\n⚠️ 当前仅为你注入了相关领域的工具。"
                      "如果用户的需求超出当前可用工具，请在回复开头包含 [TOOLSET_MISS] 标记，"
                      "并说明缺失的能力。")
    if read_only_mode:
        extra_hint += ("\n\n🔒 当前为只读模式：未注入修改/推送/同步类工具。"
                       "如需此类操作，请用户明确表达意图后再请求。")

    # 拼接到 system prompt
    return base_prompt + extra_hint

# 2. _stream_ai_loop 中调用 selector (agent.py:924-936)：feature flag 三分支
SELECTOR_ENABLED = os.environ.get("LITE_AGENT_SELECTOR_ENABLED") == "1"
SELECTOR_SHADOW = os.environ.get("LITE_AGENT_SELECTOR_SHADOW") == "1"

def _full_tools_and_names(msg):
    """全量现有行为，逐字节不变（shadow/默认/异常兜底共用）"""
    tools = (self.skill_engine.get_guest_schemas() if msg.is_guest
             else self.skill_engine.get_all_schemas())
    return tools, None  # None = system prompt 走全量文本清单

selector_result = None  # 仅 enabled 命中时非 None；shadow/默认恒 None（3.3 的 miss 检测天然关闭）
try:
    candidate = self.request_selector.select(
        text=msg.text,
        history=self.session_mgr.get_history(msg.session_key),
        is_guest=msg.is_guest,
    )
except Exception:
    candidate = None    # selector 自身异常 -> 一律全量兜底（原 FALLBACK_ALL 语义内化为 except 分支）

if SELECTOR_ENABLED and candidate is not None:
    selector_result = candidate                     # enabled：真实裁剪
    if candidate.names is None:                     # 不确定 -> 全量
        tools, system_names = _full_tools_and_names(msg)
    elif len(candidate.names) == 0:                 # 闲聊 -> 空集
        tools, system_names = [], []
    else:                                           # 领域子集
        tools = self.skill_engine.get_schemas_by_names(candidate.names)
        system_names = candidate.names
else:
    tools, system_names = _full_tools_and_names(msg)  # 现有全量行为，逐字节不变
    if SELECTOR_SHADOW and candidate is not None:
        print(f"[Selector-Shadow] domains={candidate.domains}, "
              f"names={len(candidate.names) if candidate.names else candidate.names}, "
              f"confidence={candidate.confidence}, read_only={candidate.read_only_mode}")

# 3. system prompt 同步（仅 enabled 裁剪路径带裁剪提示词与 [TOOLSET_MISS] 指示）
system_content = self._build_system_prompt(
    is_guest=msg.is_guest,
    skill_names=system_names,
    read_only_mode=bool(selector_result and selector_result.read_only_mode),
)

# 4. 漏选检测钩子在 RuntimeEventType.DONE 分支内（完整实现见 3.3，此处不重复）
```

**Feature flag 语义表（P1-3 修正核心）**：

| flag 组合 | 实际注入 | 行为变化 | miss 计数 |
|---|---|---|---|
| 均未设置（默认） | 全量（现有行为） | 无 | 否（`selector_result` 恒 None，3.3 检测关闭） |
| `LITE_AGENT_SELECTOR_SHADOW=1` | 全量（现有行为） | 无，仅 stdout 记录 candidate | 否 |
| `LITE_AGENT_SELECTOR_ENABLED=1` | selector 三态结果 | 真实裁剪 | 是 |
| `ENABLED=1` 且 selector 抛异常 | 全量（现有行为） | 单请求兜底，不影响后续请求 | 否 |

要点：
- **Phase 推进即切换 flag**：Phase 1 上线 `SHADOW` 观察，Phase 2 切 `ENABLED` 真实裁剪（见第 7 节），全程不改代码。
- **`LITE_AGENT_SELECTOR_FALLBACK_ALL` 取消独立开关**：其语义（selector 异常时回退全量）已内化为 `except Exception: candidate = None` 分支，任何 flag 组合下异常都自动兜底，无需人工干预。
- `ENABLED` 与 `SHADOW` 同时设置时以 `ENABLED` 为准（代码顺序保证）。

### 3.8 工具顺序控制 (P2-1 修正)

`get_schemas_by_names` 按 registry 顺序返回，不受 names 顺序影响。本方案不依赖工具顺序，因此**只读优先通过显式排除 explicit_intent_tools 实现，不通过顺序控制**。

如未来需要顺序控制，可新增 `get_schemas_by_names_ordered(names)` 接口或修改 `get_schemas_by_names` 的实现，但**当前方案不引入此复杂度**。

### 3.9 关键词收紧 (v2 评审 P2-3)

宽泛词处理：

| 原关键词 | 问题 | 修正 |
|---|---|---|
| `任务` | 误触发 todo | 改为 `待办\s*任务\|todo\s*任务`（需组合） |
| `分数` | 误触发 gaokao | 改为 `高考.*分数\|分数.*位次`（需组合） |
| `发布` | 误触发 blog | 改为 `发布文章\|发布博客\|halo`（需显式领域词） |
| `链接` | 误触发 web | 改为 `http://\|https://\|剪藏.*链接`（需显式特征） |

修正后的关键词表见 3.2 各 domain 的 `pattern` 字段。

### 3.10 Guest 安全 (P1-1 修正)

v3 的 `self.engine._skill_registry` 访问了**不存在的实例属性**（`_skill_registry` 是 [skill_engine.py](file:///c:/Projects/Pys/lite_agent/core/skill_engine.py) 的模块级全局变量，不是 SkillEngine 实例属性），运行时必然 AttributeError。v4 改用公开接口 `get_guest_schemas()`，其内部已按 `policy.guest_ok` 过滤：

```python
def _apply_guest_filter(self, names: list, is_guest: bool) -> list:
    if not is_guest:
        return names
    # v4: 使用公开接口，不触碰 engine 私有/模块级内部结构
    guest_names = {schema["function"]["name"]
                   for schema in self.engine.get_guest_schemas()}
    return [n for n in names if n in guest_names]
```

在 `_merge_domains` 之后、`_build_system_prompt` 之前调用（已内联进 3.4 的 `_merge_domains`），确保 tools 参数与 system prompt 双通道一致。guest 请求即使 selector 判定出错，最终注入的工具集也必然是 `guest_ok` 白名单的子集。

---

## 4. 关联任务评估

### 4.1 7059ce — 巡检类任务路由到 flash

**拆出，不并入本方案。** 理由：
- `self.model` / `self.client` 在 `Agent.__init__` 时固定（agent.py:110-145）
- RequestSelector 只负责工具筛选，无法改变当前请求的模型
- 7059ce 需要独立的模型路由层，建议单独实施

### 4.2 2b8d34 — DAG Plan-Execute 重构

**建议在本方案落地后再评估。** 本方案只改主路径，`task_orchestrator.py:110` 的 PLANNER_PROMPT 仍携带全量工具清单。收益边界必须明确：**本方案只影响直接聊天主路径，编排路径不受影响**。

---

## 5. 预期收益（修正后）

| 指标 | 当前（主路径） | 目标（主路径） | 变化 |
|---|---|---|---|
| 普通查询 tools Schema 字符数 | ~26,230 | ~1,500-3,000 | **-85~95%** |
| 普通查询携带的显式意图工具数 | 全部 | 0 | **-100%** |
| 写操作时 tools Schema 字符数 | ~26,230 | ~3,000-5,000 | **-80~90%** |
| system prompt 技能清单字符数 | ~8,082 | ~300-1,500 | **-80~95%** |
| 首字延迟 (TTFT) | 慢 | 快 | 显著提升 |
| 工具命中率 (中小模型) | 低（83 候选干扰） | 高（≤15 精准候选） | 显著提升 |
| 闲聊场景 | 全量 83 工具 | 0 工具 | **-100%** |
| **编排路径 (Planner)** | **不变** | **不变** | **0%** |

---

## 6. 风险与缓解

| 风险 | 概率 | 缓解措施 |
|---|---|---|
| 关键词误判漏给工具 | 中 | 不确定回退全量 + 多轮历史继承 + 启动校验 + 漏选检测计数（`record_miss`，DONE 分支 + `[TOOLSET_MISS]` 标记，仅 ENABLED 阶段计数） |
| 多轮追问丢失领域 | 中 | 最近 3 轮 user 消息 + 上轮 tool_calls 反推 |
| 写工具漏给（用户明确要写） | 中 | explicit_patterns 匹配 write_scope_text（直接命中=当前消息；继承=守卫批准的来源消息，3.4）+ 确认类问句守卫（`_write_inherit_allowed`）+ miss 检测兜底 |
| 写意图误继承（确认守卫放宽导致意外注入写工具） | 低 | 守卫仅认最近一条助手消息为确认类问句/含"确认"，继承源消息必须本身触发该 domain；Phase 0 验收 #11 正反双向测试 |
| guest 看到非 guest 工具 | 低 | selector 内强制 guest 过滤（公开接口 `get_guest_schemas()`）+ `list_skills_filtered` 天然按 names 过滤 |
| 新技能未映射 | 低 | 启动校验输出未映射列表；未映射技能只在全量兜底（`names=None`）路径可用，不参与领域裁剪（见 3.6） |
| 跨领域请求超上限 | 低 | `mandatory` 或 `other_explicit` 超限即返回 `names=None` 回退全量，不静默裁剪（3.4） |
| selector 启动校验每次执行 | 已解决 | 单例化（Agent.__init__ 一次） |
| shadow 日志泄漏敏感信息 | 中 | 仅记录 domain 名称 + 命中关键词类型，**不记录完整用户文本**（`record_miss` 同样只记 domain/names） |

---

## 7. 实施计划 (Phase 0 验收标准)

### Phase 0 — 纯函数实现 + 启动校验

**实现内容**：
- `core/request_selector.py` 单例类
- DOMAIN_MAP（含 default_tools/explicit_intent_tools/explicit_patterns）
- 启动校验：DOMAIN_MAP 引用有效性 + 未映射技能可见
- 单元测试覆盖：单领域、多领域、跨域超限、只读模式（read_only_mode 规则）、漏选检测计数

**验收标准**：
1. 启动时若 DOMAIN_MAP 引用不存在的技能，输出明确警告
2. 启动时输出未映射技能列表
3. selector 仅在 `Agent.__init__` 中初始化一次
4. guest 请求不会返回非 guest_ok 工具（通过公开接口 `get_guest_schemas()` 实现，单元测试断言子集关系）
5. "查看待办" 消息只返回 `todo_list/todo_get` 两个默认工具（`todo_push_brief` 已归入 explicit_intent_tools），且 `read_only_mode=True`
6. "标记这个待办为完成" 同时返回默认工具和 `todo_done`，且 `read_only_mode=False`
7. `read_only_mode` 严格等于 "未命中任何已选 domain 的 explicit_patterns（含继承源消息）"：多 domain 请求任一命中即 False（单元测试覆盖）
8. "导出博客文章" 返回 `blog_export_articles`；"抓取邮件摘要" / "推送待办简报" 分别返回 `mail_fetch_summaries` / `todo_push_brief`（验证 P2 归类修正生效）
9. 多领域 "查账单并同步到网盘" 返回 billing+backup 工具且 ≤ 15 个（billing 无 explicit_routes，backup 兜底入 other_explicit；超限时回退全量而非截断，见验收 #18）
10. 多轮 "继续" / "刚才那个" 继承最近 user 消息的领域
11. **写操作确认多轮（P1-2）**："发布这篇博客" -> 助手 "确认发布吗？" -> "确认"：继承 blog domain **且** `blog_publish_article` 被注入（explicit_patterns 命中继承源消息）；对照场景 "谢谢"（守卫拒绝）不注入 publish
12. shadow 日志只记录 domain 名和命中数，**不记录完整用户文本或工具参数**；`record_miss` 日志同样只含 domain/names
13. 模拟回复含 `[TOOLSET_MISS]` 标记时 `_miss_count` 递增（**无论是否有 tool_calls**）；仅中文信号且 `had_tool_calls=False` 时递增；全量兜底与空集请求不递增；标记被无条件剥离不出现在最终回复
14. **flag 行为断言（P1-3）**：`SHADOW=1` 时注入工具集与未设 flag 时逐字节一致（单元测试直接比对 schemas 列表）；`ENABLED=1` 时按三态裁剪；selector 抛异常时全量兜底
15. **Python 3.9 编译测试（P1-1）**：在 VPS1 Python 3.9 上执行 `python -m py_compile core/request_selector.py` 无错误；禁止裸写 `list[str] | None` 等 3.10+ 注解
16. **explicit_routes 不丢动作（v6 P1-2）**："标记这个待办为完成" 只返回 `todo_done`（而非全部 9 个 todo 写工具）；"抓取邮件并同步到网盘" 中 `mail_fetch_summaries` 与 `ops_backup_data`/`ops_backup_cloud` 均在结果内
17. **确认守卫三条件（v6 P2-1）**："已确认发布完成"（用户非短确认词）、"还需要查看结果吗？"（助手非确认请求）均不触发写意图继承；"确认" 且助手问"确认发布吗？" 才触发
18. **mandatory 超限回退全量（v7 P1）**：构造同时命中 mail+billing+system+security 的消息（如"查邮件账单和系统日志并做安全巡检"），mandatory > 15 时返回 `names=None`（回退全量）且 `confidence="low"`，结果中不出现被截断的默认工具列表
19. **mail 路由互斥（v7 P2-1）**："只抓取邮件" 只注入 `mail_fetch_only`，不同时注入 `mail_fetch_summaries`；"抓取邮件摘要" 只注入 `mail_fetch_summaries`；"抓取摘要并回填正文" 同时注入两个正交动作工具（多匹配语义）
20. **守卫跨域拒绝（v7 P2-2）**：继承 mail domain 时，助手问"确认发布吗？" + 用户"确认" 不触发写意图继承（mail 的 pattern/explicit_patterns 均不命中该问句）；同句式下继承 blog domain 则正常触发

### Phase 1 - Shadow 模式

- 加 `LITE_AGENT_SELECTOR_SHADOW=1` 环境变量
- 实际请求仍走全量工具（与未设 flag 时逐字节一致），但同时计算 selector 结果并打 log
- **miss 计数在 shadow 阶段不生效**：`selector_result` 恒 None，3.3 的 DONE 分支检测天然关闭；本阶段仅观察 domain 命中分布与三态（不确定/空集/子集）比例
- 持续 1-2 周观察"工具漏选"和"误判多给"两类问题

### Phase 2 - Feature Flag 开启

- `LITE_AGENT_SELECTOR_ENABLED=1` 启用真实裁剪，miss 计数（`record_miss`）自此阶段生效
- `LITE_AGENT_SELECTOR_FALLBACK_ALL` **不再作为独立开关**：异常回退全量已内化为 `except Exception` 分支（见 3.7），任何时刻 selector 抛异常都自动兜底

### Phase 3 — 评估编排路径

- 是否将 selector 同样应用于 `task_orchestrator.py:110`
- 单独评估，不在本方案范围
