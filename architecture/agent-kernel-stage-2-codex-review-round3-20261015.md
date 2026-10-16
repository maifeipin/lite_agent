# 阶段二第三轮独立复核：仍未通过，已直接修补一项小问题

日期：2026-10-15；Codex。Agent阶段一基线 `5c4016682eeb9d5b14638b0e37e65f8342183bd1`，当前阶段二未提交。仍存在暂存删除，补交所称“全部未暂存”不准确；本轮没有改变暂存区。

## 结果与证据

本次不是否定已经完成的修复：GET/POST鉴权前置、skill cron实例检索、Todo简报URL、全局快照和cron索引清理已有修复；真实main物理缺包测试确实新增了普通对话、流式与SIGTERM验证。

- 交付方指定30项专项，Codex允许本机回环端口后实测 **30 passed，19.20s**。日志 `/tmp/kernel-stage2-codex-round3-20261015/submitted-suite.log`。这是本轮前缀小修前的结果。
- 架构扫描实测238文件，25核准finding，0未登记违规。它不能证明业务实现已清退。
- 新增5项独立检查最初5项失败；Codex修复路由前缀后 **4 failed，1 passed，0.14s**。
- 小修后的长期新增测试和既有反例/注册专项 **31 passed，0.71s**，日志 `closeout-tests.log`。
- 完整994项、Parser205项、双版本8项本轮没有独立重跑，不将交付方汇总当本轮独立证据。

独立反例源码 `/tmp/kernel-stage2-codex-round3-20261015/test_review_round3.py`；修改前 `probes.log`，修改后 `probes-after-prefix-fix.log`。命令：在Agent根目录执行 `PYTHONPATH="$PWD" python3.13 -m pytest -q -p no:cacheprovider --tb=short /tmp/kernel-stage2-codex-round3-20261015/test_review_round3.py`，pytest真实退出码1。

反例均离线：HTTP检查执行真实ApiHandler分发并替换业务回调以免变更；通知启动器替换为Mock，验证收到的Agent；TodoService替换为Mock验证实际实例工具与HTTP选择的路径，**没有读写真实Todo数据库**。不要将这些最小检查误写成实际网络业务端到端。

## T1 [P1] core/禁用插件仍可进入业务入口

channels/api.py 的 do_POST 无条件保留 `/api/v1/auth -> _handle_auth()`，也保留 `/api/report`、`/api/task_result`、`/api/edge_task` 等直接业务分发；GET仍有 `/api/pull_task`。空RouteRegistry不能控制这些旧路径。

实测：空registry下 `/api/v1/auth` 调用会议登录handler；管理员认证通过的 `/api/report` 调用Edge业务handler。这里不是声称匿名Edge越权，而是**插件未启用仍存在业务入口**。core smoke只测Todo404遗漏这些入口。

修正：所有业务入口由对应插件唯一登记；删除公共宿主直接业务分发；会议登录作为会议插件的公开/自鉴权route保留原安全语义。穷举core/full禁用插件的旧URL与/agent别名，检查无业务处理、DB或发送发生。

## T2 [P1] 通知线程可停止，但始终拿不到真实Agent

main.py先load_all插件，再创建Agent；PluginContext没有在启动后注入agent。features/review_meeting/register中 `getattr(context,"agent",None)`因此为None，并被start_notifier闭包固定捕获。deliver_once在配置有效owner_channels时需要agent.channels，收到None会反复异常，无法保留原通知行为。

实测：沿实际create_app+PluginLoader调用真实会议register、替换实际start_notifier，收到的第一个参数为None。增加stop_event只是解决停止信号，不能证明通知功能恢复。源码中也未找到补交声明的 `test_s7_meeting_notifier_thread_lifecycle_closed_on_shutdown` 用例（rg检查tests）。

修正：明确register/start分期；注册阶段不启动依赖真实通道的线程；Agent和通道可用后用受限发送端口启动，失败回滚、有序停止。测试必须证明实际发送器收到正确目标及次数、shutdown后不再发送，不能只证明stop函数可调用。

## T3 [P1] 同一应用的Todo工具与HTTP指向不同数据库

features/todo/adapter.get_todo_service读取app/agent.todo.db_path；插件登记的todo_add等仍直接使用skills.ops_todo函数，其get_service无参数时使用模块级DB_PATH。cron同样调用模块函数，不绑定实例配置。

实测：应用配置todo.db_path为 `/tmp/review-instance-todo.db`；HTTP实例选择此路径；执行该应用登记的真实todo_add工具时，构造TodoService选择项目默认 `data/todos.db`。两个构造器均Mock，未写实际DB。

修正：实例级TodoService/仓储/通知端口绑定配置，工具、HTTP和cron共享同一实例服务。两个应用分别用不同临时DB，通过实际工具创建→HTTP查询→cron通知→关闭逐项验证互不污染。禁止修改全局DB_PATH、全局config或全局Agent来让测试通过。

## T4 [P1] S6依然是部分搬迁和重复实现，未达到阶段二边界

channels/api.py仍有约600行会议处理、安全与SQL、完整登录、邮件HTML、RSS、Socks5、OCR及Edge业务方法。新features/review_meeting/http_adapter、mail_ops/email_adapter、network_ops/socks5_adapter与公共宿主旧方法并存；Todo甚至重复定义_handle_todos，并通过hasattr优先调用宿主旧私有方法，属于为旧mock/调用路径保留分叉。

skills.ops_socks5仍承载DB/协议/故障转移实现，scripts/review_meeting.py仍含会议主体；通用output_delivery/alerts等也未按阶段二清单归位。新增adapter文件不等于唯一实现与边界迁移完成。

修正：按既定迁移盘点删旧实现，内部调用只指向canonical业务服务/adapter；公共宿主不写业务名、不导入业务，不通过hasattr回退旧业务私有方法；技能与CLI只做输入/权限/输出适配。所有保留旧入口逐条给出外部使用证据、同对象导出及移除期限；不以原有测试patch位置当外部契约。

## 已直接修补 T5 [P2] 路由前缀吞并另一资源名

services/routes.py 的is_prefix原来使用原始startswith，使 `/api/v1/todos_typo` 命中 `/api/v1/todos`。已改为路径相等或下一段以 `/` 分隔；新增 tests/test_phase2_closeout.py 的5个长期用例覆盖根路径、子路径及两个碰撞反例。新反例已由red转green。

```diff
--- services/routes.py
+++ services/routes.py
@@
-                if path.startswith(r["pattern"]):
+                if path == r["pattern"] or path.startswith(r["pattern"].rstrip("/") + "/"):
                     return r
+++ tests/test_phase2_closeout.py
+ 5个路径边界参数化回归用例
```

本轮生产修改仅上述通用匹配条件；新增长期测试及此复核报告。没有提交、推送、部署、重启或真实外发。diff --check对暂存/未暂存均检查；新文件内容已在工作区提供。

## 接续要求

当前不能认定S1–S8全部闭环，不能作为阶段二完成基线、不能进入阶段三。T1–T4是原规则已明确要求的缺口，不新增业务或扩大范围；不是把已修小问题机械打回。

> Antigravity请先读本报告，保留Codex已修路由前缀及新增测试，不覆盖。自主补齐T1–T4：业务入口唯一受插件控制、通知在真实发送端口就绪后启动、Todo工具/HTTP/cron实例配置贯通、删除公共宿主及skills/scripts重复业务实现。把本轮有效反例纳入长期行为测试并保留red/green；特别验证旧业务URL全集与不同数据库双实例，不以单个Todo404、注册表存在或线程可stop代替完成。修订handoff逐项标明真实状态，提供含暂存删除和新文件的完整diff及最终原始门禁日志。禁止commit/push/部署/重启/真实外发，不进入阶段三；完成主体目标后再交Codex收尾。
