# Gemini 前端任务：完善 Lite Agent 复杂任务管理界面

你负责完善 Lite Agent Web Dashboard 的“复杂任务”前端。后端 TaskSpec API 已存在，不要修改后端路由、数据结构、安全策略或模型调度逻辑。先阅读现有代码，再在现有视觉体系内完成交互，不要引入前端框架或构建系统。

## 目标体验

主流程必须是：

1. 用户在 Dashboard 的“复杂任务”页面输入自然语言目标。
2. 点击“创建任务规则”，后端立即生成基础 TaskSpec、落库并返回；这个动作不调用 LLM。
3. 页面收到结果后直接展示结构化、可交互的编辑界面，而不是要求用户下载 JSON。
4. 用户可以直接编辑，也可以点击“AI 完善规则（可选）”，让高价值模型补充条件和执行计划。
5. 用户可以修改目标、上下文、条件、模型、预算、联网策略、执行计划、调度和输出方式。
6. 用户保存后执行确定性校验和高价值模型复核。
7. 校验通过后可以立即执行，或设置一次/重复定时执行。

下载 JSON、离线修改、上传 JSON 是可选的备份和迁移能力，不能成为正常使用的必经步骤。

## 现有文件

- `web_dashboard/modules/task_specs.js`：复杂任务模块，主要修改位置。
- `web_dashboard/main.js`：统一标签页和生命周期。除非确认存在通用生命周期缺陷，否则不要修改。
- `web_dashboard/style.css`：在现有暗色 Dashboard 风格上补充必要样式。
- `web_dashboard/index.html`：已有复杂任务入口和脚本引用，不要重复创建入口。

保持原生 JavaScript、HTML 和 CSS，不增加 npm、React、Vue 或新的构建步骤。

## API 契约

所有路径均使用 `/agent` 前缀：

- `GET /agent/api/v1/task-specs`：任务列表，返回 `{data, total}`。
- `GET /agent/api/v1/task-specs/meta`：模型、能力、不可变策略和 schema 信息。
- `GET /agent/api/v1/task-specs/{id}`：任务详情。
- `POST /agent/api/v1/task-specs`，body `{goal, name?}`：主创建接口；不调用模型，立即创建基础规则并返回 HTTP 201。
- `POST /agent/api/v1/task-specs/generate`，body `{goal, name?}`：兼容旧前端的立即创建接口，同样不调用模型。响应的 `generation.status` 为 `not_started`。
- `POST /agent/api/v1/task-specs`，body `{spec}`：导入 JSON。
- `PATCH /agent/api/v1/task-specs/{id}`，body `{spec}`：保存编辑结果。
- `POST /agent/api/v1/task-specs/{id}/enrich`：可选的 AI 完善。它在已有任务上调用高价值生成模型，成功返回完善后的同一个任务。
- `POST /agent/api/v1/task-specs/{id}/confirm`：确认未经修改的模型草案。
- `POST /agent/api/v1/task-specs/{id}/validate`：高价值模型复核。
- `POST /agent/api/v1/task-specs/{id}/acknowledge`，body `{rationale}`：用户经验覆盖可确认建议。
- `POST /agent/api/v1/task-specs/{id}/run`：立即执行，返回 HTTP 202。
- `POST /agent/api/v1/task-specs/{id}/schedule`，body `{enabled}`：启用或暂停调度。
- `DELETE /agent/api/v1/task-specs/{id}`：删除。

`enrich` 可能耗时几十秒，最长约 120 秒。模型超时或不可用时，已有基础规则不会丢失，HTTP 200 响应包含：

```json
{
  "generation": {
    "status": "fallback",
    "code": "AUTHOR_MODEL_TIMEOUT或AUTHOR_MODEL_FAILED",
    "message": "基础规则已保留，可稍后重试"
  }
}
```

如果 AI 完善期间用户保存了编辑，`enrich` 返回 HTTP 409：

```json
{
  "error": "任务在 AI 完善期间已被修改；模型结果未覆盖当前版本，请刷新后重试",
  "code": "REVISION_CONFLICT"
}
```

前端收到 409 时必须保留用户当前内容，刷新服务器上的最新版本，并明确提示“AI 结果未覆盖你的修改”。不要自动重试或自动覆盖。

## 编辑器字段

普通用户直接编辑以下结构化字段：

- 任务：名称、目标、背景、约束、验收标准、必需输入。
- 执行：复杂度、模型或成本等级、是否联网、Token 上限、步骤上限、总时长、并发数。
- 能力：根据 meta API 返回的 capability map 选择。
- 计划：至少提供步骤列表编辑；高级用户可以切换到 JSON 视图。不要强迫普通用户直接编辑整份 JSON。
- 调度：手动、一次、重复；按模式显示相关时间字段。
- 输出：`auto`、`email`、`hedgedoc`、`sqlite`、`inline`；同时选择 `summary` 或 `preview`。
- 外部发布确认：选择 `auto` 或 `hedgedoc` 时明确说明可能产生公开链接。

系统不可变策略只读展示，不允许编辑。

## 状态与动作

清楚区分：`draft`、`review_required`、`blocked`、`needs_ack`、`approved`。

- 显示确定性校验 findings 和模型复核 findings。
- 每条 finding 显示 code、message、resolution 和字段路径。
- `blocked` 不允许执行或启用调度。
- `needs_ack` 允许用户填写理由后确认；不要把模型建议伪装成不可覆盖硬规则。
- `approved` 才显示“立即执行”和“启用调度”。
- 展示最近执行状态和结果。

## 交互可靠性

- 点击“创建任务规则”后显示短暂 loading、禁用重复提交；该请求应很快返回，不能显示“高价值模型生成中”。
- 创建成功后立即选中新任务并展示编辑器。即使用户刷新页面，基础任务也已经在列表中。
- “AI 完善规则（可选）”是独立按钮。点击后显示“AI 正在完善，可能需要几十秒”，只禁用该任务的重复完善按钮，不锁住编辑器或整个页面。
- AI 完善期间允许用户编辑，但保存可能导致完善接口返回 409；按上述冲突流程处理。
- 浏览器刷新或网络断开不会删除已创建的基础任务。重新进入页面时从任务列表恢复，不要再次创建同一任务。
- 请求结束后无论成功或失败都恢复按钮。
- 非 2xx 响应必须读取 JSON `error` 并显示醒目的内联错误或 toast。
- 捕获网络超时、JSON 解析失败和后端 fallback 状态。
- 任务输入框中的 Enter 不得触发全局搜索、聊天发送或简单任务路由；多行输入正常换行，可用 Ctrl/Cmd+Enter 触发生成。
- 每个按钮只绑定一次事件，标签切换、列表刷新后不能失效或重复提交。
- 保存、复核、执行、调度后刷新当前任务数据，并保留用户所在标签页。
- 不把用户目标发送到 `/agent/api/v1/chat`。

## 验收场景

至少手动验证：

1. 输入“查看一下最近的外币账单”，点击创建；应在正常网络下迅速产生一条可编辑 TaskSpec，而不是进入聊天简单路由。
2. 对该任务点击“AI 完善规则（可选）”，成功后仍是同一任务 ID，编辑器显示模型补充内容。
3. 模拟完善模型超时；页面保留基础规则并展示 fallback 提示，可以再次完善。
4. 模拟 AI 完善期间用户保存编辑；409 后用户内容不被模型覆盖。
5. 修改模型、Token、步骤数、调度和输出方式，保存后刷新仍保留。
6. 缺少必需输入时展示阻断原因，不能执行。
7. 复核建议与用户经验冲突时，可以填写理由确认。
8. 选择邮件输出后保存，任务卡片能看到当前输出方式。
9. JSON 导出再导入能创建新任务 ID，且不可变策略不能被篡改。

完成后列出修改文件、关键交互决策和手动测试结果。不要改后端 API。
