# 统一指令管理方案 — 审查总结与实施记录

> 审查对象: `implementation_plan.md` (Gemini Antigravity IDE)
> 最后更新: 2026-07-13  **全部完成** ✅

---

## 1. 总体评价

方向正确（统一命名 + 装饰器注册）。已全部实施完成，仅剩一条 config.json cron 注册需手动添加。

---

## 2. 遗漏项 → 已补齐

| # | 函数/指令 | 实际位置 | 结果 |
|---|-----------|----------|------|
| 1 | `mail_fetch_cron()` | [ops_mail_reader.py:206](file:///c:/Projects/Pys/lite_agent/skills/ops_mail_reader.py#L206) | ✅ 映射为 `/mail_fetch` + `@slash_command` |
| 2 | `mail_llm_enrich()` | [ops_mail_reader.py:250](file:///c:/Projects/Pys/lite_agent/skills/ops_mail_reader.py#L250) | ✅ 映射为 `/mail_enrich` + `@slash_command` |
| 3 | `mail_view_original()` | [ops_mail_reader.py:416](file:///c:/Projects/Pys/lite_agent/skills/ops_mail_reader.py#L416) | ✅ 映射为 `/mail_view <account> <uid>` + `@slash_command` |

---

## 3. 重复/冗余 → 已梳理

### 3.1 4 个拉取函数

- `mail_fetch_only` / `mail_fetch_summaries` / `mail_fetch_cron` 三个共享 `/mail_fetch` 入口
- `mail_llm_enrich` 独立为 `/mail_enrich`
- 函数体未合并（保持兼容，后续可选）

### 3.2 `/cmd`

未删除，保留可用。`/cmd fs` 内部引用 `mail_fetch_summaries` 不变。

---

## 4. 命名不一致 → 已统一

| 旧名 | 新名 | 状态 |
|------|------|:--:|
| `/mail` | `/mail_list` | ✅ |
| `/search` | `/mail_search` | ✅ |
| `/mailstats` | `/mail_stats` | ✅ |
| `/reprocess` | `/mail_reprocess` | ✅ |
| `/ok` | `/mail_ok` | ✅ |
| `/noise` | `/mail_noise` | ✅ |
| `/unnoise` | `/mail_unnoise` | ✅ |
| `/headers` | `/mail_headers` | ✅ |
| `/missed` | `/mail_missed` | ✅ |
| `/noiselist` | `/mail_noiselist` | ✅ |
| `/memory` | `/memory_stats` | ✅ |
| `/remember` | `/memory_add` | ✅ |
| `/persona` | `/memory_persona` | ✅ |
| `goal` | `/goal` | ✅ |
| `rss` | `/rss_fetch` | ✅ |

无别名过渡（个人项目一次到位）。

---

## 5. 向后兼容

跳过（用户明确不需要过渡期，一次改到位）。

---

## 6. Dashboard 暴露逻辑

`index.html` 已改为动态加载，通过 `GET /api/v1/dashboard` 从注册表获取。

当前仪表盘显示（`show_in_dashboard=True`）:

| 指令 | 显示 | 理由 |
|------|:--:|------|
| `/new` | ✅ | 高频操作 |
| `/mail_list` | ✅ | 高频查看 |
| `/mail_fetch` | ✅ | 高频同步 |
| `/mail_enrich` | ✅ | 补打 LLM |
| `/mail_backfill` | ✅ | 回填修复 |
| `/mail_stats` | ✅ | 高频统计 |
| `/sync_meili` | ✅ | 搜索引擎同步 |
| `/mail_reprocess` | ❌ | 故障修复用 |

---

## 7. 任务清单（实施记录）

### Phase 1: 补齐遗漏 ✅

- [x] 1.1 `mail_fetch_cron` → `/mail_fetch` + `@slash_command`
- [x] 1.2 `mail_llm_enrich` → `/mail_enrich` + `@slash_command`
- [x] 1.3 `mail_view_original` → `/mail_view <account> <uid>` + `@slash_command`
- [x] 1.4 梳理合并方案（暂不合并函数体）

### Phase 2: 向后兼容 ✅（跳过 alias，直接改名）

- [x] 2.1 ~~旧名保留 30 天~~ → 跳过（个人项目）
- [x] 2.2 `/cmd` 不移除
- [x] 2.3 cron 引用检查 → `mail_fetch_cron` 待手动加 config.json

### Phase 3: 执行改名 ✅

- [x] 3.1 按计划逐项改名
- [x] 3.2 邮件域: `/mail_list` `/mail_search` `/mail_stats` 等
- [x] 3.3 记忆域: `/memory_stats` `/memory_add` `/memory_persona`
- [x] 3.4 `/goal` `/rss_fetch` 加 `/` 前缀
- [x] 3.5 Dashboard 标记: `mail_reprocess` 隐藏

### Phase 4: 装饰器注册 ✅

- [x] 4.1 `@slash_command` 注册表 → [core/command_registry.py](file:///c:/Projects/Pys/lite_agent/core/command_registry.py)
- [x] 4.2 agent.py if/elif 链已删除（~118行） → 全走注册表 dispatch
- [x] 4.3 cron 引用 → 已在 config.json 添加 `mail_fetch_cron` 定时任务 ✅

---

## 8. 最终指令清单

### 全局系统
`/new` `/status` `/help` `/history` `/stop` `/balance` `/cron` `/check`

### 邮件域
`/mail_list` `/mail_search` `/mail_stats` `/mail_fetch` `/mail_enrich`
`/mail_reprocess` `/mail_backfill` `/mail_headers` `/mail_missed`
`/mail_ok` `/mail_noise` `/mail_unnoise` `/mail_noiselist` `/mail_view`

### 记忆域
`/memory_stats` `/memory_add` `/memory_persona`

### 其他
`/rss_fetch` `/goal` `/ai` `/sync_meili`

---

## 9. 新增架构

### 指令注册三要素

```python
# 1. 写 handler（签名为 agent, msg, args）
def _cmd_mail_stats(agent, msg, args):
    return _compute_stats()

# 2. 注册
from core.command_registry import slash_command
slash_command('/mail_stats', category='邮件管理',
              description='邮件处理统计', show_in_dashboard=True,
              guest_ok=False)(_cmd_mail_stats)

# 3. 重启 → agent.py 自动发现，dashboard 自动渲染
```

### 关键文件

| 文件 | 职责 |
|------|------|
| [core/command_registry.py](file:///c:/Projects/Pys/lite_agent/core/command_registry.py) | 单例注册表 + `@slash_command` + `check_permission` |
| [agent.py:399](file:///c:/Projects/Pys/lite_agent/agent.py#L399) | `_registry_dispatch(cmd, self, msg, args)` 单一路由 |
| [channels/api.py:98](file:///c:/Projects/Pys/lite_agent/channels/api.py#L98) | `GET /api/v1/dashboard` 免认证 JSON |
| [index.html](file:///c:/Projects/Pys/lite_agent/web_dashboard/index.html) | `fetch('/agent/api/v1/dashboard')` 动态渲染 |

### Router 分发

```
agent._handle_builtin(message)
  ├─ _registry.check_permission(cmd)      # 权限 → admin_only / guest_ok
  ├─ _registry_dispatch(cmd, agent, msg)  # 注册表命中 → 直接返回
  └─ if/elif 链                           # 未命中 → 系统命令回退
```

---

## 10. Phase 4.3 收尾 — 已完成 ✅

已在 `config.json` 的 `cron_jobs` 数组添加:

```json
{"name": "智能邮件助手同步", "time": "*/20 * * * *", "skill": "ops_mail_reader::mail_fetch_cron", "push_on_output": true}
```

**VPS 同步**: 将本地 `config.json` 同步到 `/home/liteagent/lite_agent/config.json` 后重启服务即可生效。
