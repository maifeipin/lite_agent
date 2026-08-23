# Gemini 前端任务：完善 Lite Agent 复杂任务管理界面

你负责完善 Lite Agent Web Dashboard 的“复杂任务”前端。后端 TaskSpec API 已存在，不要修改后端路由、数据结构、安全策略或模型调度逻辑。先阅读现有代码，再在现有视觉体系内完成交互，不要引入前端框架或构建系统。

## 目标体验

主流程必须是：

1. 用户在 Dashboard 的“复杂任务”页面输入自然语言目标。
2. 点击“生成任务规则”，调用高价值模型生成 TaskSpec，并由后端立即保存。
3. 页面收到结果后直接展示结构化、可交互的编辑界面，而不是要求用户下载 JSON。
4. 用户可以修改目标、上下文、条件、模型、预算、联网策略、执行计划、调度和输出方式。
5. 用户保存后执行确定性校验和高价值模型复核。
6. 校验通过后可以立即执行，或设置一次/重复定时执行。

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
- `POST /agent/api/v1/task-specs/generate`，body `{goal, name?}`：模型生成并保存规则，成功返回 HTTP 201。
- `POST /agent/api/v1/task-specs`，body `{goal, name?}`：不调用模型，立即创建基础规则。
- `POST /agent/api/v1/task-specs`，body `{spec}`：导入 JSON。
- `PATCH /agent/api/v1/task-specs/{id}`，body `{spec}`：保存编辑结果。
- `POST /agent/api/v1/task-specs/{id}/confirm`：确认未经修改的模型草案。
- `POST /agent/api/v1/task-specs/{id}/validate`：高价值模型复核。
- `POST /agent/api/v1/task-specs/{id}/acknowledge`，body `{rationale}`：用户经验覆盖可确认建议。
- `POST /agent/api/v1/task-specs/{id}/run`：立即执行，返回 HTTP 202。
- `POST /agent/api/v1/task-specs/{id}/schedule`，body `{enabled}`：启用或暂停调度。
- `DELETE /agent/api/v1/task-specs/{id}`：删除。

生成模型超时或不可用时，后端仍会返回 HTTP 201，并创建可编辑基础规则。此时响应包含：

```json
{
  "generation": {
    "status": "fallback",
    "code": "AUTHOR_MODEL_TIMEOUT或AUTHOR_MODEL_FAILED",
    "message": "面向用户的说明"
  }
}
```

前端必须明显提示“已创建基础规则，需要手动补充计划并复核”，不能表现为没有响应。

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

- 点击生成后立即显示 loading、禁用重复提交，并显示“高价值模型生成中，可能需要几十秒”。
- 请求结束后无论成功或失败都恢复按钮。
- 非 2xx 响应必须读取 JSON `error` 并显示醒目的内联错误或 toast。
- 捕获网络超时、JSON 解析失败和后端 fallback 状态。
- 任务输入框中的 Enter 不得触发全局搜索、聊天发送或简单任务路由；多行输入正常换行，可用 Ctrl/Cmd+Enter 触发生成。
- 每个按钮只绑定一次事件，标签切换、列表刷新后不能失效或重复提交。
- 保存、复核、执行、调度后刷新当前任务数据，并保留用户所在标签页。
- 不把用户目标发送到 `/agent/api/v1/chat`。

## 验收场景

至少手动验证：

1. 输入“查看一下最近的外币账单”，点击生成；产生一条可编辑 TaskSpec，而不是进入聊天简单路由。
2. 模拟生成模型超时；页面展示基础规则和 fallback 提示。
3. 修改模型、Token、步骤数、调度和输出方式，保存后刷新仍保留。
4. 缺少必需输入时展示阻断原因，不能执行。
5. 复核建议与用户经验冲突时，可以填写理由确认。
6. 选择邮件输出后保存，任务卡片能看到当前输出方式。
7. JSON 导出再导入能创建新任务 ID，且不可变策略不能被篡改。

完成后列出修改文件、关键交互决策和手动测试结果。不要改后端 API。
