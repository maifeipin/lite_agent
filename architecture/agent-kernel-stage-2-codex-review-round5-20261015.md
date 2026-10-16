# 阶段二第五轮复核与直接修补：U1–U3通过，阶段二整体尚未通过

2026-10-15，Codex。Agent阶段一基线 `5c4016682eeb9d5b14638b0e37e65f8342183bd1`；Parser `918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc`且工作区干净。Agent阶段二仍未提交，已有暂存删除未改变。未commit/push/部署/重启/真实外发，未进入阶段三。

## 已确认的进展与最终门禁

上一轮U1双Agent、U2初始化异常吞并、U3顶层业务导入的原始3个反例和4个长期测试，本轮独立 **7 passed，0.89s**。唯一Agent持有关系、异常上抛和公共API纯导入已改善，不再把这些旧问题列为未修。

本轮进一步发现命令权限与实例隔离问题，已直接修补（见下文）。对最终生产代码重新执行verify_e0.py，**8/8通过**：

| 门禁 | 独立实测 |
|---|---|
| 本机架构 | 通过；交付基线242文件、25核准finding、0未登记违规 |
| 本机Agent Python3.13 | 1015 passed，4 subtests passed，69.39s |
| 本机Parser | 205 tests，2.21s |
| Agent / Parser diff --check | 均通过 |
| Docker Python3.9 Parser | 205 tests，7.14s |
| Docker Python3.9架构 | 通过，1.17s |
| Docker Python3.9 Agent | 1015 passed，88.30s |

原交付版本1011项的8/8也已先独立复现；权限修补后重新跑以上整套门禁，不能把旧日志当最终代码证据。新增4项长期用例使1011增至1015；相关专项47 passed，0.80s。

日志目录 `/tmp/kernel-stage2-codex-round5-20261015/`：
- `previous-probes.log`：U1–U3原始及长期测试。
- `full-gates.log`：修补前1011项整套门禁。
- `full-gates-after-command-fix.log`：修补后1015项最终整套门禁，verify_e0真实退出码0。
- `command-isolation.log`：2个独立命令反例原始red。
- `command-fix-tests.log`：修补后的47项专项green。
- `cron-0.log`、`cron-1.log`、`cron-2.log`：3个示例cron回调独立进程失败证据。
- `rss-commands.log`：RSS命令未登记到实例的反例。
- `full-manifest-missing-dependency.log`：本机真实full清单加载失败证据。

## 已直接修补：实例命令权限误用全局表与全局回退

agent.py._handle_builtin原先检查全局_registry的权限，却从实例command_registry执行命令。因此只在实例登记的admin_only命令，Guest实测仍调用handler。实例空表未命中又回退全局，core实例也能执行其他宿主的全局业务命令。双冒号入口同样始终使用全局表。

2个离线反例执行真实分发，Mock业务回调确认被执行；未触碰任何真实业务状态。修补后：
1. `/`及`::`都用同一选中的实例注册表进行权限检查和分发。
2. 显式实例表（含空表）是能力边界；仅未注入表的legacy入口保留旧全局路径。
3. 新增tests/test_phase2_closeout.py中4项长期用例，分别验证两入口的Guest拒绝和不执行外部全局命令。

```diff
--- agent.py
+++ agent.py
@@ / 与 :: 入口分别使用此原则
- perm_denied = _registry.check_permission(cmd, is_guest)
+ cmd_reg = getattr(self, "command_registry", None)
+ registry = cmd_reg if cmd_reg is not None else _registry
+ perm_denied = registry.check_permission(cmd, is_guest)
@@
- 实例未命中即回退全局dispatch
+ 有显式实例表时只调用该实例dispatch；仅缺失注入时使用legacy全局dispatch
+++ tests/test_phase2_closeout.py
+ /、:: 两入口各2项权限/实例边界回归
```

不要为了恢复旧命令而撤销这项隔离；应将旧业务命令正式登记到对应插件实例。

## V1 [P1] 示例配置中的3种合法cron回调仍无法执行

config.example.json仍包含 `ops_rss::rss_precompute`、`ops_rss::rss_push`、`ops_self_check::daily_health`。main._register_configured_cron_jobs只是取最后一段名称并engine.get_tool查询；这三项是旧作业/专用处理器而非模型工具，新feature未登记对应的通用可执行回调。

独立实测3个新进程分别失败，均在实际job.func()执行时抛出“未找到指定的Cron技能”。反例保留真实content/sys插件register与真实main调度；真实ops_rss/ops_self_check的外部工作函数替换为Mock，其他不相关协议模块替身隔离缺依赖，未发网络/通知/生产操作。第一次尝试真实全量manifest被pymongo缺包提前阻断，单独保留，不能把它当cron算法失败。

反例源码：`test_cron_compatibility.py`。三个参数分别单进程运行，防止测试隔离卸载skills后全局slash装饰器重复导入污染。早期同进程试跑曾出现重复/rss_fetch注册，未将该脚手架污染当最终cron证据。

整改：由所属feature显式登记既有作业回调，通用调度服务按注入的回调端口分发，不把所有cron目标等同于LLM工具，不恢复全局表兜底。完整保留RSS预计算时段、RSS卡片推送、daily_health广播、sentinel/edge告警条件、kwargs、push_on_output及发送回执；去重插件默认作业与旧配置展开作业，禁止同一槽重复触发。针对示例真实配置逐项执行回调，Fake发送器核对目标/次数，而不是只断言任务名存在。

## V2 [P1] 旧业务slash命令尚未迁入实例注册，不能靠全局回退维持

实际content插件加载后，`/rss_fetch`在default_command_registry存在，但app.command_registry中不存在；独立反例失败。源码另有/rss_list、/rss_log、/rss_topic，以及mail多个旧命令、/balance、/sync_meili等模块底部slash注册没有对应完整实例盘点。

修补前部分命令能靠Agent全局回退执行，因此不能简单声称原来所有命令都失效；正确结论是**旧命令依赖全局污染，未完成插件所有权迁移**。隔离修补后，未登记命令不再越过实例边界执行；必须在认定阶段二完成前迁入正确插件。

整改：盘点全部@slash_command与显式slash_command调用，逐个保持handler参数、返回值、guest/admin元信息、dashboard展示及/和::别名；由context.register_command登记并事务注销。不得批量拷贝全局表或用sys.modules反查以躲过真实迁移。检查命令目录/帮助展示同样使用当前实例。

## V3 [P1] 本机默认full清单实际加载仍缺依赖，门禁没有覆盖默认完整启动

受隔离保护的真实FULL_MODE_PLUGINS加载在features.content_ops导入skills.ops_meili_sync时失败：`ModuleNotFoundError: No module named 'pymongo'`。当前requirements.txt/requirements-test.txt均未列pymongo。这里明确是当前验证环境/依赖声明问题，不据此断言生产VPS一定缺包。

旧SkillEngine扫描可能捕获单技能导入失败继续启动，新full插件聚合导入会中止整个插件/应用；需要明确既有可选能力与full默认启用集合的兼容语义，不能仅以单个Todo/meeting插件测试当全量full启动已验证。

整改：给出实际基础/业务依赖边界及full所需依赖声明、配置预检；保持安全禁用语义，不静默跳过后声称完整。用脱敏配置和Fake协议/模型完整加载默认manifest，验证能力清单、旧命令、cron与有序关闭。缺环境时明确标阻塞，不能安装/修改生产环境或写秘密；也不能在测试中伪造pymongo存在来宣称真实full启动成功。

## 仍需诚实报告的边界

U3的顶层纯导入已修，但channels/api.py仍有大量延迟导入feature的业务代理及会议状态/业务名分支；架构visitor仍不将channels判为基础层，25核准项仍包含基础→业务兼容边。因此“AST全绿”不能替代原方案最终公共宿主依赖约束。请按原执行方案逐条清退/提供外部兼容证据；不要通过容错代理或未扫描公共层来宣称全量迁出。

test_main_startup_with_fake_channel_receives_meeting_notification目前Mock了真实notifier，且在main关闭后手工调用FakeChannel.send_message。它证明对象关联，不证明真实会议outbox扫描/发送/关停。需要实际deliver_once/outbox与Fake发送器贯通的集成证据，不把该手动发送写成生产链路已验证。

## 结论与发回指令

**U1–U3本轮复核通过；阶段二整体仍未通过。** 缺口是原方案要求的full功能保真和插件完整所有权，不是追加新业务。最终8/8全绿与这些独立反例可以同时成立。阶段二不能提交为完成基线，不能进入阶段三。

> 请先保留Codex本轮agent.py命令权限/实例隔离修补及4项长期测试。按V1–V3补齐旧cron处理器、全部业务slash命令的实例登记与真实full默认依赖/启动证明，不恢复全局兜底；根据原阶段二清单清退公共宿主反向业务代理，补实际outbox发送链验证。先读真实config.example/HEAD旧实现/调用者，按能力清单逐项验收，不只按反例名称绿化。所有常规修补与迭代自主完成，重大兼容取舍给出具体方案。提交覆盖最终源码的原始门禁日志、完整diff含暂存删除和新文件、修订handoff。禁止commit/push/部署/重启/真实外发，不进入阶段三。

本轮完整源码patch与未跟踪文件哈希清单另存系统临时目录；报告完成后重新生成，确保包含本报告。不得将私有配置、凭据、DB或邮件证据加入patch。
