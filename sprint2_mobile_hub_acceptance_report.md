# Sprint 2 掌上中枢与会审决策验收审计报告

> **报告版本**: v2.0 (包含 4 项代码复核缺口闭环)  
> **审计基线 Commit**: `484c48e` -> 本次修复提交  
> **审计时间**: 2026-10-24  
> **验证环境**: OnePlus 13 (Android 15) @ `192.168.2.137:32981` / 生产后端 VPS1 (`https://mail.maifeipin.com`)  
> **CI 状态**: GitHub Actions CI 自动化构建与测试通过  

---

## 1. 审计缺口闭环整改说明 (4 项整改全数通过)

在第一轮验收复核中发现的 4 项缺口，现已在客户端与服务端实现严格闭环整改：

### 缺口 1 [P1]: 切换账号后旧请求污染新账号缓存与 UI
- **问题根因**: `TodosViewModel` 与 `MeetingsViewModel` 未维护会话代次，未取消在途 Job，未对回写目标会话与数据库实例进行一致性校验。
- **整改措施**:
  - `TodosViewModel` 与 `MeetingsViewModel` 引入 `sessionEpoch: Long` 代次计数器与 `Job` 引用管理 (`activeInitJob`, `activeFetchJob`, `activeLoadJob`, `activeDetailJob`, `activeDecisionJob`)。
  - 账号切换 (`onSessionChanged`) 或退出登录 (`onLoggedOut`) 时，立即主动取消在途协程并递增 `sessionEpoch`；在 `TodosViewModel` 中立即注销并置空当前 `dbHelper`。
  - 所有网络回调、数据库回写和 UI 状态流赋值均执行 `currentEpoch == sessionEpoch && helper == dbHelper` 强校验，过期会话的异步结果彻底丢弃，杜绝跨账号数据污染。

### 缺口 2 [P1]: 审批超时核验可能误报成功
- **问题根因**: 原超时恢复逻辑仅以议题不再是 `awaiting_approval` 作为成功判断条件，当自己提交超时失败但他人完成否决或裁决时，会误报“审批已成功同步”。
- **整改措施**:
  - `MeetingsViewModel.decideMeeting()` 在超时或瞬态异常时，拉取议题最新详情与事件流。
  - 严格检索事件列表中是否存在 `it.kind == "decision" && it.requestId == requestId` 的审计事件。
  - 若存在且 `decisionValue` 与本次裁决一致，才判定为“审批已成功同步至服务端”；
  - 若未匹配到当前 `request_id` 且议题状态已改变（如他人完成否决/批准），明确向用户提示 **“状态已变化，本次提交结果未确认”** 并刷新最新状态，杜绝任何误导提示。

### 缺口 3 [P1]: 缓存键合并不同身份语义
- **问题根因**: 原 `TodoDatabaseHelper.computeDbKey()` 对用户名和 URL 全路径统一转小写，并采用 32 位 `hashCode()`，导致 `Alice` 与 `alice`、不同大小写的代理前缀冲突合并。
- **整改措施**:
  - 规范化保留协议和主机名小写，但**严格保留用户名与 URL 路径的实际大小写语义**。
  - 采用标准 `SHA-256` 算法计算 `"$cleanUrl|$trimmedUsername"` 规范化字符串摘要，截取 32 位十六进制编码作为沙箱数据库标识。
  - 单元测试覆盖了主机大小写规范化、用户名大小写敏感区分 (`Alice` != `alice`)、代理路径大小写敏感区分 (`/Proxy` != `/proxy`)，确保绝对身份沙箱隔离。

### 缺口 4 [P2]: 审批 UUID 幂等闭环
- **问题根因**: 原后端仅将 `request_id` 作为审计元数据写入事件流，重复提交相同 `request_id` 时直接抛出 400 状态不合法。
- **整改措施**:
  - 在 `lite_agent/features/review_meeting/http_adapter.py` 的决策处理分支中，前置查询该议题下是否已存在包含相同 `request_id` 的 `decision` 事件。
  - 若匹配已存在的 `request_id`，直接返回当前最新会审快照（HTTP 200 OK），不追加重复事件、不更新状态、不抛出异常，实现真正的端到端幂等。

---

## 2. 自动化测试与 CI 验证矩阵

### 2.1 后端自动化测试 (`pytest`)
- **测试命令**: `/opt/homebrew/bin/pytest dev/tests/features/test_review_meeting_api.py`
- **执行结果**: `39 passed in 29.49s`
- **新增用例**:
  - `test_decide_approval_endpoint_and_conflict_check`: 验证包含 `request_id` 的审批提交，以及携带相同 `request_id` 的重复提交返回幂等快照（不产生重复 decision 事件）。

### 2.2 移动端 JVM 单元测试 (`SanityTest.kt`)
- **测试覆盖项**:
  - `todo_database_helper_db_key_computation`: 验证 SHA-256 缓存键长度 (32)、用户名大小写隔离、路径大小写隔离。
  - `meetings_view_model_decision_concurrency_and_conflict_handling`: 验证 409 Conflict 拦截与版本回弹。
  - `meetings_view_model_timeout_verification_checks_request_id_and_audit_event`: 验证场景 A（超时但 request_id 匹配成功）、场景 B（他人并发裁决未匹配到 request_id 提示“状态已变化，本次提交结果未确认”）。
  - `meetings_view_model_account_switch_discards_stale_data`: 验证账号切换与登出取消在途 Job、隔离旧会话数据。

---

## 3. 真机端到端与生产后端验证证据

### 3.1 验证设备与网络
- **物理设备**: OnePlus 13 (Android 15, PKB110)
- **调试连接**: `192.168.2.137:32981`
- **生产服务端**: `https://mail.maifeipin.com` (已部署最新 `http_adapter.py` 并平滑重启 `lite-agent.service`)

### 3.2 离线验收留存截图清单 (保存在 `.local/plans/artifacts/sprint2/`)
所有验收截图均已同步归档至 `.local/plans/artifacts/sprint2/` 目录，供离线审计查阅：

1. **登录与输入框定位**:
   - `login_dialog_current.png`: 统一登录弹窗标准视图。
   - `login_empty_error_real.png`: 空字段合法性前端拦截。
   - `login_form_filled.png`: 填充服务端地址与管理员身份凭证。
   - `after_logged_in.png`: 登录成功无缝进入掌上中枢主界面。

2. **会审决策列表与详情抽屉**:
   - `device_meetings_list.png`: 会审列表多状态展示，待裁决项置顶徽标提示。
   - `device_detail_sheet_open.png`: 点击会审议题展开全功能 BottomSheet 抽屉。

3. **并发冲突与掌上裁决操作**:
   - `decision_confirm_dialog.png` / `decision_dialog_filled.png`: 二次确认弹窗，展示裁决动作、附言输入与当前版本号。
   - `after_submit_decision.png` / `after_conflict_dismissed.png`: 模拟并发修改触发 409 Conflict，前端弹窗友好提示并自动刷新拉取最新序号 (v5)。
   - `after_submit_with_v5.png`: 携带最新预期序号与 UUID `request_id` 提交。
   - `after_approval_success.png`: 掌上裁决成功生效，展示“审批成功：已批准”绿色确认条。
   - `after_approval_main_list.png`: 主列表即时更新，议题状态变更为“已批准”，待办徽标实时刷新。

---

## 4. 结论

Sprint 2 移动端掌上中枢（会议会审决策、待办沙箱隔离、并发冲突回弹、幂等重试与会话代次安全）经代码整改、自动化单测及真机实机验证，4 项缺口全部闭环，达到生产就绪标准。
