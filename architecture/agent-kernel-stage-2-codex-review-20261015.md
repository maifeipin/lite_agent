# 阶段二独立复核：未通过，需补齐主体实施

复核日期：2026-10-15。复核方：Codex。

## 结论

当前交付不能认定阶段二完成约90%，不能提交为阶段二完成基线，也不能进入阶段三。不是对测试数量的异议：现有测试通过与未覆盖的真实缺陷同时成立。业务迁出、生产配置兼容和真实宿主验证仍有主体工作未完成，超出约定的“小范围直接修补”。因此本轮保存独立反例及补实施指令，不局部修补后宣称整体收尾。

Agent 基线为 `5c4016682eeb9d5b14638b0e37e65f8342183bd1`（阶段一收尾提交），阶段二保持工作区未提交。本轮未改生产源码，未提交、推送、部署、重启或真实外发。新增本复核报告；临时反例及日志放系统 /tmp。

## 独立实测

| 检查 | 本轮结果 | 证据 |
|---|---|---|
| 对方阶段二专项两文件 | 17 passed，2.26s | `/tmp/kernel-stage2-codex-review-20261015/submitted-tests.log` |
| 静态架构 | 231 文件，30 核准 finding，0 未登记违规 | `/tmp/kernel-stage2-codex-review-20261015/architecture.log` |
| 独立行为反例 | 7 failed，0.62s（预期应通过的正确行为均不满足） | `/tmp/kernel-stage2-codex-review-20261015/probes.log` |
| Agent diff --check | 通过 | 本轮实际运行 |
| 本机/容器完整8项 | 本轮未复跑 | 已出现阻塞，原交付日志汇总不能替代独立复核 |

反例源码：`/tmp/kernel-stage2-codex-review-20261015/test_review_probes.py`。复现：在 Agent 根目录执行 `PYTHONPATH="$PWD" python3.13 -m pytest -q -p no:cacheprovider --tb=short /tmp/kernel-stage2-codex-review-20261015/test_review_probes.py`，真实 pytest 退出码为1。复核工具批处理随后读取日志，外层 shell 的退出码不是 pytest 结果。

这些是针对真实方法的离线最小复现；HTTP 反例直接执行真实 ApiHandler.do_GET 并替换处理器以避免业务写入，证明分发错误，不声称已完成实际网络端到端。新反例不在项目全量测试目录内，不影响原测试结果，也没有删除或弱化原断言。

## 阻塞发现与证据

### R1 [P1] 业务迁出未实施，五个空插件不能视为业务归位

`features/todo/__init__.py`、`features/review_meeting/__init__.py`、`features/mail_ops/__init__.py`、`features/network_ops/__init__.py`、`features/content_ops/__init__.py` 的 register 只有 pass。没有真实工具/路由/作业登记。billing/sys_ops 主要新增工作流/指令，仍反向调用 skills 的业务实现。core/meeting_*、core/review_meeting_*、scripts/review_meeting.py、skills 大量业务实现未迁出，通用 output/alerts 等仍在旧位置。

main.py:127 与 app/bootstrap.py 的 full 分支仍 `SkillEngine(auto_load=True)`，先盲扫全技能，再过滤禁用插件。这使禁用插件无法阻止其技能导入和暴露；模块底部 CronManager 注册仍存在（如 skills/ops_backup.py:884），mail_reader 的全局 _agent 也未被替换为注入服务。

要求：按完整调用链真实迁移，生产 full 也从实例空工具集开始，由所选插件显式登记全部能力；禁用插件不导入、不注册、不启动任务。不能用开关挡旧代码或继续保留基线债务代替迁移。

### R2 [P1] 公共 HTTP 宿主仍直接分发业务，core 路由隔离不成立

channels/api.py:144 直接调用 `_handle_todos`，会议分支在自定义 registry 前分发；邮件、Socks5、RSS 等同样仍在公共宿主。新增 `_dispatch_custom_route` 位于旧业务分支之后，不影响旧分发。独立 `test_core_http_todo_does_not_dispatch_business` 实测 `_handle_todos` 被调用一次，违背空 registry 应返回404的规则。

原 test_core_mode_unknown_route_returns_not_found_safely 只断言 RouteRegistry.match 返回 None，根本没有执行 HTTP handler，因此不能证明 core 返回404。

要求：迁移业务 adapter 并真正通过 registry 挂载，使用实际 core/full HTTP 请求验证未启用404、启用行为、鉴权、流式和错误结构。

### R3 [P1] 已配置 skill 型 cron 被静默删除，full 行为回归

main.py:76 的 `_register_configured_cron_jobs` 只处理 command。HEAD 原实现处理 skill、kwargs、time_range、推送及多个专用回调；新插件没有承接这些作业。独立 `test_configured_skill_cron_is_preserved` 对旧合法 rss_precompute 配置实测零作业。

要求：由业务插件承接原配置全部语义、发送路径及生命周期，以真实 config.example 脱敏结构及旧调用链逐条映射验证；未知配置明确失败，不静默忽略。time_range 展开需稳定唯一作业ID，不能因重名拒绝丢时段。

### R4 [P1] 注册失败只是删除 cleanup 记录，没有释放资源

app/plugins.py:31-35 的 rollback 调用 unregister_resources，未调用 cleanup_fn。独立 test_failed_registration_executes_cleanup 获取资源后抛错，cleanup 实测调用0次；之后 app.shutdown 也无法找到已经删除的清理记录。

要求：事务保留按实际获取顺序的统一撤销操作，逆序执行真实释放，失败继续其余并汇总，不按资源类别分组冒充全局逆序。覆盖单插件和应用后续失败，保留基础宿主资源回滚。

### R5 [P1] full 工具实例共享全局注册表

core/skill_engine.py:142-153，未传 tools 时 `_instance_registry=None`，_registry 回退模块全局字典。两个生产 full 引擎共享同一对象，独立 test_full_engines_do_not_share_tool_registry 失败。原双实例测试仅使用显式空 tools 的两个 core 实例，未覆盖 full，也非报告声称的 core/full 并发实测。

要求：生产实例级装配，消除全局 registry/Agent/cron 对实际装配的影响；真实 full/full、core/full 注册及关闭隔离测试。

### R6 [P1] CronManager 每次构造重新初始化，清空已有作业

core/cron_engine.py:16-21 的 __new__ 返回同一实例，但没有防止继承的 CronService.__init__ 每次重置 jobs/executor/running。独立 test_legacy_cron_constructor_does_not_erase_registered_jobs 两次构造之间登记的任务消失。若已启动还会失去旧线程/executor 所有权。

要求：生产去单例；确需兼容的入口保留同对象或严格初始化语义和外部使用证据，不能保留有破坏性副作用的子类包装。

### R7 [P1] 正常 shutdown 没有撤销插件工具和作业

bootstrap 只清命令/workflow/route；cron stop 不 clear；没有把成功插件的事务撤销登记到关闭流程；skill_engine 未清理。独立 test_shutdown_unregisters_plugin_tools_and_jobs 实测工具仍在（首断言失败；源码同时确认作业表保留）。CronService.stop 使用 executor.shutdown(wait=False)，也未证明正在运行/队列任务终止或被等待。

要求：成功注册资源有明确关闭所有者，停止调度后处理运行中任务，等待/超时策略明确，再逆序释放。不靠进程退出掩盖泄漏。main 业务 notifier 未登记 stop、启动异常缺少统一 try/finally，也要补齐；会话清理线程应设置停止并 join。

### R8 [P1] Agent 匹配仍读取全局 workflow，注入路径失效

agent.py:174-177 保存 self.workflow_registry，但 `_matches_billing_workflow` 在904-911定义为 staticmethod，始终读取 default_workflow_registry。生产插件登记的是 app 实例 registry，Agent 前置匹配看不到。独立 test_agent_workflow_match_uses_injected_registry 对仅本实例登记的精确 matcher 实测 False。

要求：Agent、流式与 TaskOrchestrator/TaskSpecService 所有入口传递和使用同一实例 registry，删除业务命名/特判；验证真实请求进入登记工作流而非悄悄走通用模型，并保证其他实例不命中。

### R9 [P1] “物理缺包真实入口”测试实际没有启动 main/API

test_physical_absence_of_features_and_skills_core_smoke 的子进程手工 create_app/Agent，调用 `/new`，没有 import main、main()、启动 API、普通模型回合或流式。副本还未包含新 SMTP integration，不能证明平台能力完整。插件导入测试仅 hasattr 和线程数量，没有审计文件/DB/socket，因此“0文件、0DB、0网络”无证据。

要求：补物理副本真实 main + 实际公共宿主、离线假模型 HTTP 完整回合、流式、鉴权、终态，验证 import 来源、缺业务及禁用模块缺席；插件新进程 audit 拦截真实副作用。阶段二需 smoke，阶段三再固化完整基础清单/干净依赖构建。

### R10 [P1] SMTP 配置兼容未闭环，说明与实现不一致

新 integrations/smtp/client.py 去掉 Parser 借用是有效进展，但 config.example.json 未随迁移更新，仍写发信账户来自 Parser accounts；旧 output_delivery.email.provider_order 选择语义被移除，旧 Graph/OAuth 支持也不能直接假定由 SMTP 用户名密码替代。新表声称支持 LITE_AGENT_SMTP_ENABLED/USE_SSL、LITE_AGENT_EMAIL_PREFIX，代码未读取这些变量。SMTP 配置默认 use_ssl=True，直接配置587且未显式覆盖时会走 SMTP_SSL，和文档默认 STARTTLS 不一致。

要求：核对旧协议能力与明确兼容范围，给出保留独立协议适配或必要用户配置迁移方案，不能静默降级；示例补占位字段，映射/env/SSL/收件人/账户顺序与实现一致，以假协议验证。不读真实凭据、不改生产配置。

### R11 [P2] 插件配置投影与显式启用语义尚未接通

同一个 host PluginContext 被交给所有插件；plugin_name 一直为 host，投影在初始化时已完成，_begin_transaction 未更新配置，插件拿不到自己的配置块。main 仅读 disabled，不支持已约定的显式空启用列表、core 与业务启用冲突和未知禁用项检查；拓扑的集合遍历也使无依赖插件顺序不稳定。

要求：每插件独立受限上下文，按字段投影所需配置；未设置/显式[]/未知项/禁用依赖明确验证，清单顺序稳定，未知设置不静默忽略。

### R12 [P1] 基础业务债务仍未清零，交付声明缺少可追溯证据

architecture_rules.json 仍含 core/meeting_*、core/review_meeting_*、core/billing_* 等反向业务条目；agent.py 仍硬编码 /meeting 和业务分流。30核准 finding 不等于阶段二完成标准成立。盘点写了不存在或未实现的目标 handler，缺逐项迁移状态、真实调用者及可定位测试。handoff 未附完整含新文件源码 diff 与原始完整门禁日志位置，只有汇总。

要求：逐项给出当前事实和迁移完成证据；基础业务实现及反向依赖本阶段清零；受控加载器等精确例外另列。修订原报告，区分已实现/实测/待完成，不以目录和 register 存在代表能力实现。

## 发回 Antigravity 的修正执行指令

> 阶段二 Codex 独立复核未通过。请先读 architecture/agent-kernel-stage-2-codex-review-20261015.md 和既有阶段二三执行细则，按 R1-R12 补齐阶段二主体实施，不进入阶段三，不提交/推送/部署/重启/真实外发。当前原专项17项通过，但 Codex独立7项反例全部失败，不能继续引用8/8作为阶段完成证据。先把 /tmp/kernel-stage2-codex-review-20261015/test_review_probes.py 的有效反例纳入受跟踪的长期测试并验证 red，再修复至 green；不要按测试硬编码或删除断言。完成五个空插件及全部业务调用链真实迁出、full显式实例注册、旧cron配置保真、实际HTTP路由隔离、实例workflow贯通、事务真实释放及启停资源闭环、SMTP兼容配置，再补 main/API物理缺包普通/流式/鉴权证明。自主完成范围内修补和迭代，遇重大兼容设计选择提交具体方案，其余独立工作继续。最后交付覆盖最终源码的全量门禁原始日志、含新文件完整diff、逐项真实验收映射和修订handoff，再交Codex集中复核。

## 本轮修改总结及 Diff

本轮只新增本报告，未修改任何阶段二生产逻辑、测试或架构规则。离线7项反例在系统临时目录，以便完整保留失败证据；交付者应纳入长期测试并修复，不应把临时路径当永久CI依赖。

```diff
+++ architecture/agent-kernel-stage-2-codex-review-20261015.md
+ 阶段二复核未通过结论
+ 独立17项原专项通过、7项反例失败的实测证据
+ R1-R12源码发现、影响及修正验收要求
+ Antigravity补实施指令及不提交/不进入阶段三边界
```
