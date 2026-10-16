# 阶段二第七轮集中复核：worker专项经修补通过，整体仍有原计划未完成项

日期：2026-10-16，Codex。Agent阶段一基线 `5c4016682eeb9d5b14638b0e37e65f8342183bd1`，阶段二未提交；既有暂存删除不变。未push、部署、重启或真实外发，未进入阶段三。

## 本轮确认及直接收尾

认可本次已将Socks5 worker从ApiServer.start移出，归network_ops登记；stop_event等待取代固定sleep，正常空闲关闭及部分算法回归已有用例。原始core启动反例的根因已修，不重复列作未修。

交付相关专项独立 **22 passed，1.03s**。随后独立补出两个真实缺陷：

1. 两个宿主复用同一worker，却得到同一个全局停止语义；第一个宿主关闭即可停止第二个宿主的worker。
2. join超时仍清空活线程引用和started标志，允许旧worker在运行时再起新worker，且清理报告假成功。

2项独立反例原始均失败。Codex已直接修补：
- 同一个外部代理资源的进程级worker采用宿主租约；每个句柄只释放自己的租约，最后一个才停止，重复释放无作用。
- 句柄绑定具体stop_event代次，旧句柄不能停止新worker。
- join在锁外等待；超时保留活线程/停止状态并抛TimeoutError，宿主清理可汇总真实失败，旧worker未结束时拒绝启动新worker。
- 切换及告警前检查停止信号；已在执行的协议调用不能被Python强制取消，超时据实失败，不承诺调用被强杀。
- 新增tests/test_phase2_worker_closeout.py的4个长期反例：共享所有者、超时保留/拒绝重启、旧句柄隔离、在途探测完成后不再切换/告警。

修补后2个原始反例通过；4个长期反例及相关专项 **26 passed，0.89s**。保留原3分钟、连续失败阈值、候选过滤、15分钟退避算法。

首次组合命令同时指定-p tests.conftest和仓库测试导致pytest重复注册配置插件，属于测试命令问题；已分开临时反例与仓库测试重跑，未算代码失败或成功证据。

## 日志与验证口径

证据目录 `/tmp/kernel-stage2-codex-round7-20261016/`：
- submitted-tests.log：原提交专项22项。
- test_worker_ownership.py、ownership-red.log：2个独立worker反例red。
- ownership-green.log：同反例green。
- worker-closeout-tests.log：纳管后的26项专项green。
- test_remaining_boundaries.py、remaining-boundaries.log：下述剩余2个独立反例失败。
- full-gates-final.log：修补后整套8项门禁，最终结果见末尾补录。

无真实外网：worker测试用Event阻塞的假探测器、假切换器与假告警；原core启动反例Mock HTTPServer/worker；其余项目回归用脱敏快照、临时数据、假模型和回环端口。

## 为什么仍不能认定阶段二整体完成

worker专项已按上述修补收尾；但对照最初阶段二的整体验收，以下不是阶段三新目标，也不是新追加要求。前几轮报告已列为“剩余公共宿主依赖、业务实现清退、通知保真”，不能在每次单项变绿后丢失。

### 1. 高优邮件通知仍依赖已删除的全局Agent注入（P1，独立实测）

features/mail_ops登记/mail_fetch后仍调用skills.ops_mail_reader.mail_fetch_cron()；该函数在返回高优邮件时只对模块级_agent发送，_agent默认None，main的新装配不再赋值它。mail_llm_enrich等相同旧路径也需核对。

独立反例：真实加载mail_ops插件，注入宿主agent，将mail_fetch_summaries替换为固定“✉️ 高优邮件”，捕获现有push_alert端口，调用应用实例/mail_fetch；发送调用次数为0。没有读取邮件、调用模型或真实外发。

整改：注入当前宿主的受限发送端口，保留真实目标、优先级、去重与失败回执；工具、命令、cron共用同一业务服务，不恢复模块全局_agent。测试用FakeChannel/临时回执确认发送次数、目标、失败重试和双实例隔离。反例Mock的是当前实现的端口，不要求未来必须保留push_alert函数名，要求行为真实贯通。

### 2. 公共channels仍允许反向导入features（P1，独立实测）

本次ArchitectureVisitor只禁止channels -> skills/scripts，特意没有把features列入；channels/api.py仍有约20处features.*业务代理（RSS、Edge、邮件、Todo、Socks5）。这与原依赖表“公共宿主不直接导入业务”不同。

独立合成 `channels/review_probe.py: from features.todo import TodoService`，实际visitor无finding。当前“25核准、0未登记”没有证明公共层无反向业务依赖。

整改：让feature route adapter持有自身业务服务，由宿主通用registry调用；移除公共宿主按业务名的延迟/容错代理与hasattr旧handler回退。同步测试调用canonical adapter/注入边界。静态/识别的动态features导入均纳管，保留此反例red/green；禁止扩大例外来通过。

### 3. 阶段二清退标记仍在被内部调用（P1，静态调用链核实）

architecture_rules.json仍有12条core/billing_workflow.py、core/billing_snapshot.py -> features.billing符号依赖，remove_by为kernel_stage_2。skills/ops_billing.py:137仍导入core.billing_snapshot.build_snapshot/model_evidence，是内部调用，不能当仅剩外部兼容入口留到阶段三。

整改：所有内部调用改canonical实现，清退该12条债务。若确有外部消费者必须保留旧入口，列消费者及迁移设计，不能以旧测试patch路径充当外部消费者。保留受控加载器与真实隔离fixture例外，不与业务债务混报。

### 4. 业务主体仍在脚本/core数据表（P1，静态源码核实）

scripts/review_meeting.py仍包含会议数据库、状态机、授权及outbox主体，features/review_meeting多个模块反向import该脚本；它不是薄argparse CLI。core/request_selector.py仍硬编码mail/billing等业务关键词、工具集合和动作路由（DOMAIN_MAP），没有按原4.1将业务路由描述交由插件提供。大量ops协议/策略实现也仍未按盘点状态给出完整迁移证明。

整改按原阶段二清单完成唯一实现归位，通用机制保留算法、业务数据由插件提供；不复制算法、不恢复全局Agent、不开新业务。对迁移盘点逐项标“canonical实现/内部调用/外部契约/测试证据”，而非只有目标路径。

## 收尾结论及接续指令

**本轮Socks5启停/所有权专项经Codex直接修补后通过。阶段二整体尚未达到原施工方案，不能提交为完成基线，不能进入阶段三。**

> 请保留Codex本轮worker租约、代次隔离、超时真实失败与4项长期测试，以及此前owner_delivery/命令权限等修补。接下来按原阶段二整体验收，一次补齐本报告1–4：高优邮件发送注入、公共channels无features反向依赖、12条kernel_stage_2内部债务清退、会议薄CLI和业务路由描述迁出core，并核对剩余ops盘点。不要仅围绕最新worker探针再宣称全部完成。常规迁移和修补自主完成；重大公开契约/业务语义取舍提交具体设计，其余独立工作继续。提交完整最终diff（含暂存删除和新文件）、实际调用链与资源所有权、原始完整门禁日志和逐项验收矩阵。禁止commit/push/部署/重启/真实外发，不进入阶段三。

## 本轮修改总结与Diff概要

```diff
--- skills/ops_socks5.py
+++ skills/ops_socks5.py
+ 每宿主租约及代次绑定的停止句柄
+ 超时保留线程并抛错、阻止重启；在锁外join
+ 切换/告警前停止信号检查
- 任意宿主句柄无条件全局停止、超时清空仍活线程
+++ tests/test_phase2_worker_closeout.py
+ 4个独立所有权/超时/代次/在途取消回归
+++ architecture/agent-kernel-stage-2-codex-review-round7-20261016.md
+ worker专项结论及原阶段二剩余事项集中核对
```

本轮生产改动仅worker生命周期代码，没有提交或部署。完整工作区patch另存系统临时目录，不包含私人config/env/DB或邮件证据。

## 最终门禁补录

最终verify_e0退出码0，**8/8通过**，覆盖本轮worker所有权修补和4项长期测试：

- 本机Agent Python3.13：1033 passed，4 subtests passed。
- Docker Python3.9 Agent：1033 passed。
- 两环境Parser：各205 tests passed。
- 架构：246文件，25核准finding，0未登记违规；本机及容器均通过。
- 两仓diff --check均通过，另查暂存diff无格式问题。

原交付为1029项，本轮新增4个所有权测试后为1033项。最终完整原始日志见full-gates-final.log。已知的邮件通知和channels→features独立反例仍失败，不能以8/8代替阶段二完整目标验收。

Parser HEAD为918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc且工作区干净，未修改Parser。
