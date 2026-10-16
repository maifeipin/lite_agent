# 阶段二第十轮独立复核与剩余迁出清单

日期：2026-10-16。分支 `codex/kernel-composition`，基线 HEAD `5c4016682eeb9d5b14638b0e37e65f8342183bd1`。检查当前完整未提交工作区。

## 结论

**R9-1 的 Python 切换入口与原参数顺序已恢复，R9-2 的双向写代理已清退；残余 mock 反查由 Codex 本轮直接修补。R9-3 的文件路径虚报已改正，但原要求的全量业务迁出仍未完成，阶段二不能关闭。**

现盘点明确标注 **59 项工具、3 项定时/后台作业仍待迁出**，合计 62 行，不是 62 个独立模块，也不直接代表剩余工时比例。原事项 A 要求实际迁出，不能把状态改成“待迁出”后视为该事项闭环。

## 本轮已核实通过的整改

1. `features/network_ops/socks5_service.py`：切换路径恢复读取 `/etc/proxy/config.json`、保持 listen、调用特权 helper，失败返回时不提前更新 active；不再直接写 root systemd 单元。实际代码的首选 helper 是 `/usr/local/sbin/switch_naive_config.sh`，随后回退仓库 scripts，交接文字的 `/usr/local/bin` 不是实际路径。
2. `test_socks5_host` 恢复 `(host, runcmd="", port=None, timeout=3.0)` 参数顺序；保留 worker 所有权、代次隔离与停止超时策略。
3. 两个旧技能模块不再替换 ModuleType 或双向写入 canonical 属性；Meili patch 退出后对象身份保持，薄入口连续调用不再递归。
4. 盘点文件存在性检查通过；表格可直接复算出 90 工具、25 指令行、21 路由行、7 作业行。这些是表格统计，不代表已逐项证明源码真实能力总数与这些计数完全一致。

新增 helper 失败测试将 subprocess.run 替换为固定失败结果，验证的是 Python 分支与 DB 标记保持；**没有真实执行 shell helper，不能据此声称已实测原子配置回滚**。实际 helper 的失败恢复应按原阶段三行为矩阵用假文件系统/假 systemctl 验证，现有证据不包含该结论。本轮没有生产服务操作。

## Codex 直接修补的 R9-2 残余

`skills/ops_socks5.py:start_failover_worker` 虽删除写代理，仍用 `globals().get` 取旧导出属性并与 canonical 比较，发现不同则自动作为注入函数。这是按旧 patch 状态反查测试依赖，仍违反上轮明确要求。

本轮直接删除该补偿，只透传显式 `get_proxies_fn/get_active_fn` 参数。第六轮测试的 getter patch 路径改为 `features.network_ops.socks5_service` 的真实边界；未放宽断言。新增长期回归 `test_worker_adapter_uses_only_explicit_dependency_injection`，验证旧模块属性变化不隐式更改 worker 依赖，显式参数原样转发。

相关专项：新增四项整改反例、worker 生命周期、装配及第六轮相关回归，最终 **22 passed**。完整门禁见本文末尾补录。

## 唯一剩余工作包：完成原事项 A，并同步准确的符号追溯

### A1. 完成 62 行待迁出条目的职责核对和真实迁出

临时证据 `pending-inventory.json` 保存每行原文与行号，长期来源为 `architecture/stage-2-migration-inventory.md`。

原执行细则 2-D 已明确业务归 feature、协议归 integration、工具保留 schema/权限/参数适配。剩余条目包括邮件、RSS、账单工具、健康巡检、Edge、内容及专项能力等，不应再只处理三个代表性样本。

对每一项：

- 已是纯参数/权限适配或结果呈现的工具，保留入口并以真实调用链证明不含业务主体，修正分类，无需机械拆文件。
- 有业务策略、数据库访问、外部协议主体的，按现有粗粒度领域迁出唯一 canonical 实现；保留算法、默认值、异常、回执、预算、人工状态及启停语义。
- 纯通用能力按实际依赖表归通用层，不为减少 skills 数量强塞业务插件。
- 明确的外部兼容再导出给出消费者和移除条件；旧测试 patch 路径不是外部消费者。

不要求一行一个新文件，不要求工具入口数量归零；要求每行职责归位且证据可核验。不能通过改名、只改完成状态或创建空服务文件完成验收。

### A2. 校验 canonical 符号及实际调用者，而不止文件存在

文件路径真实化仍有符号不实。例如：

| 盘点声明 | 源码核实 |
|---|---|
| `features/billing/workflow.py:BillingWorkflow.get_due_soon` | 类中没有此方法；当前工具调用 `skills.ops_billing._run_billing_cmd` |
| `features/billing/sync_service.py:fetch_and_parse`（标为已迁出） | 没有此函数；该文件导出 `sync_and_consume` 等，技能实际使用 `integrations.mail.sync_service.sync_and_consume` |
| `features/billing/__init__.py:_billing_fetch_cron` / `_billing_health_cron` 等调用者描述 | 实际 register 中使用 `skills.ops_billing` 中的同名函数，不在该 __init__ 定义 |

这不是运行时直接调用不存在符号的断言，而是**迁移追溯记录与真实运行链不一致**。文件存在性测试无法发现。对 wildcard、闭包和类方法分别给出可定位入口，不为适配测试虚构函数。

### A3. 最终验收口径固定

每行有实际 canonical、真实调用者与行为测试；待迁出清零或有通用/薄入口的逐条职责证明；生产算法相对经复核基线保持；已有领域冷启动、生命周期、通知、权限反例全部保留。最后统一执行双版本完整门禁并交付原始日志。完成此工作包后才进行阶段二最终收尾，不把实施欠账后置阶段三。

## 可直接交给 Antigravity 的接续指令

> 保留 Codex 本轮纯显式 worker 参数转发及新长期反例，不恢复 globals 反查、双向代理或旧 patch 补偿。此前 R9-1、R9-2 行为修补及领域拆分已认可，无需围绕它们再次做重复交付。当前集中完成原事项 A：逐条处理盘点中 59 工具、3 作业的真实职责与实现迁出，顺手核实其余已完成条目的 canonical 符号和调用者，修正本报告 A2 的错误。纯薄入口可保留并给出证据，业务/协议主体必须归位，不复制重写算法、不虚构路径/符号、不只改状态。交付完整矩阵、相对基线行为保真证据、含新文件及暂存删除的 diff 和最终原始门禁日志。未完成时如实报告剩余量，不能声称阶段二全部闭环。禁止 commit/push/部署/重启/真实外发，不进入阶段三。

## 本轮修改总结与核心 Diff

```diff
--- skills/ops_socks5.py
+++ skills/ops_socks5.py
- globals().get getter 与 canonical 比较后隐式替换依赖
+ get_proxies_fn/get_active_fn 原样显式透传
--- tests/test_codex_review_round6_probes.py
+++ tests/test_codex_review_round6_probes.py
- patch 旧 skills getter
+ patch 真实 canonical 服务 getter
--- tests/test_codex_review_round9_probes.py
+++ tests/test_codex_review_round9_probes.py
+ 旧模块属性变化不隐式改写 worker 依赖；显式参数原样转发
+++ architecture/agent-kernel-stage-2-codex-review-round10-20261016.md
+ 已通过项、62行真实待办、符号追溯核对及集中接续指令
```

证据目录：`/tmp/kernel-stage2-codex-round10-20261016/`。`targeted-final.log` 为最终 22 项专项；`pending-inventory.json` 为待迁出清单；`full-gates.log` 为冻结修补后完整门禁日志。本轮没有提交、推送、部署或真实外发。

## 最终门禁补录

本轮独立完整门禁退出码 **0**，**8/8 通过**：

- 本机 Python 3.13 Agent：**1051 passed，4 subtests passed**。
- 只读无网 Docker Python 3.9 Agent：**1051 passed**。
- 两环境 Parser：各 **205 tests passed**，Parser 工作区干净。
- 两环境架构：**252 文件、13 处核准 finding、0 未登记违规**。
- 两仓 diff 格式检查通过，另查相对 HEAD 的完整 Agent diff 无格式问题。

交付原为 1050 项，本轮新增一项显式依赖转发长期反例后为 1051。最终日志覆盖本轮生产修补，后续仅补录报告和证据文件。完整 `full-working-tree.patch`、`source-manifest.json` 及 `patch.sha256` 保存于上述临时目录，包含新文件和暂存删除，不含私人配置/数据，不改 Git index。

**最终裁定：局部整改及本轮小修通过；阶段二原业务迁出工作包仍待完成，不提交完成基线、不进入阶段三。**
