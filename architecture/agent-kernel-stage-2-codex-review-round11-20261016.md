# 阶段二第十一轮独立复核与收尾指令

日期：2026-10-16。分支 `codex/kernel-composition`，HEAD `5c4016682eeb9d5b14638b0e37e65f8342183bd1`。检查当前完整工作区，未提交、推送、部署、重启或真实外发。

## 裁定

本轮确有大量实际迁移：新增 billing、mail reader/query、content、Edge、health/security、workspace 等服务，不能继续把上一轮的 59 工具、3 作业当作当前未迁出数量。

**但阶段二仍不能关闭。** 独立反例发现 Socks5 环境路径、端口排除与共享 worker 代次退化，Codex 已直接修补；RSS、会议邀请及邮件重复实现等原迁出工作仍未清完。盘点符号追溯也需纠正。完整测试结果见最终补录，不以测试数量替代职责验收。

## Codex 本轮直接修补

### 1. Socks5 数据路径与端口策略

迁移后的服务忽略 `SOCKS5_DB_PATH` / `AI_DB_PATH`，默认指向 `data/bot.db`，而第八轮已复核源码默认是 `data/ai.db`，并支持已有环境路径及既有 Windows 路径。生产节点可能因此表现为“数据库空了”，即使原库并未删除。

本轮恢复已有环境路径优先、Windows 兼容路径及 ai.db 默认名，保留服务已支持的显式 socks5 配置读取。环境路径存在性语义按原基线保持；长期反例使用真实存在的临时空文件。另恢复旧端口排除集合 `{1080,1081,1082,7890,7891,8080,8887,8888,18988}`，避免迁移把本地监听端口当作远程探测端口。

### 2. 旧共享 worker 句柄不能扣减新代所有权

迁移改为计数所有者后，旧句柄先扣减全局计数、最后才在 stop 函数判断 event 代次。两位旧所有者被强制停止后，新一代有两位所有者；调用旧的第二个句柄再释放新的第一个句柄，会提前停掉仍有所有者的 worker。

独立反例仅运行等待 Event 的假循环，原实现失败。本轮在扣减前先校验代次，创建新代时重置计数为 1，防止死线程残余计数继承。保留停止超时、租约幂等和代次停止保护。

### 3. 健康检查内部兼容绕行

`features.sys_ops` 的 `/check` 与 daily_health 原先经 `skills.ops_self_check._get_health_report` 再回到 health_service。已改为直接调用 health_service，删除无外部消费者证据的旧别名，相关测试 patch 移到 canonical 服务。

新增 `tests/test_phase2_service_migration_closeout.py` 三项长期反例：环境路径、端口排除、旧共享句柄隔离。最终相关专项 **21 passed**。

## 剩余工作包：完成唯一实现和实际调用链

以下均是原阶段二范围，不新增目录数量或每工具一服务要求。

### R11-A：仍有业务主体或第二份实现留在 skills（P1）

1. **RSS**：`skills/ops_rss.py` 仍含 MongoDB 获取、关键词评分、预计算、简报合成、命令业务与发送。content_ops 的 `/rss_fetch`、`/rss_list`、`/rss_log`、`/rss_topic`、RSS 推送与预计算作业仍直接使用它。`features/content_ops/content_service.py:get_rss_node_status` 也反向取其 `_get_db`。它不是仅剩 schema 的薄入口。RSS 没有被 90 工具列表充分代表，不能只核对工具表就漏掉命令/作业链。
2. **会议邀请**：`features/review_meeting/channel_commands.py:handle` 仍从 `skills.ops_review_meeting` 导入 `invitation_parts`、`claim_invitation`。后者模块还持有 DB、邀请领取/校验及写入协调等实现，而盘点将会议工具概括成全部委托 room 的薄入口。真正的业务和状态协调应归 feature，技能与频道共用服务；纯格式处理可以保留入口。
3. **邮件重复实现**：`skills/ops_mail_reader.py` 的 `mail_fetch_summaries`、`mail_fetch_cron`、`mail_fetch_only`、`mail_llm_enrich` 仍保留过程逻辑，而 `features/mail_ops/reader_service.py` 也存在同名业务过程。若为保留旧测试 patch 路径而各留一份，会使工具、命令和 canonical 服务日后行为漂移。旧 `_agent` 状态也仍留在技能模块，不能把它作为新装配的必要通知依赖。
4. **跨领域通知调用**：Socks5 默认告警仍取 `skills.ops_todo.send_im_alert`，应核对真正的公共发送端口并归位内部调用，保留 owner-only、去重及真实回执，避免 feature 通过其他领域的模型工具层取通用能力。

**整改：** 按真实链条迁出唯一实现，工具与频道/cron/HTTP 都调用同一 canonical 服务。测试直接 patch canonical/协议端口或显式注入，不复制业务流程维持旧 mocks。保留原参数、预算、人工状态、时区、通知目标与失败语义。不是机械要求 skills 变空；schema、权限、参数归一与结果展示可留。

### R11-B：符号和调用者追溯仍不实（P2）

盘点已修正部分函数，但仍将 `features/billing/__init__.py:_billing_fetch_cron`、`_billing_health_cron`、`features/mail_ops/__init__.py:_mail_reader_cron`、若干 `_cmd_mail_*` 等描述为实际符号。实际 register 使用服务函数、技能函数或 lambda，不存在这些同名本地定义。

自动 AST 核查得到 20 个需手工核实的候选，保存在 `inventory-symbol-audit.json`；**20 不是确认错误总数**，其中可能是合法导入再导出或别名，例如 content_service 的 ops_rss_node_status 就是静态别名。应按实际定义、导入和赋值逐条消歧，不用单一 AST 定义搜索误判。

整改后每行给出真实 canonical 符号、真实调用者/登记位置及实际行为用例。文件存在性测试不能替代此项。不得为让清单成立创建无意义 wrapper，也不得把闭包名称虚构成模块函数。

## 集中接续指令

> 保留 Codex 本轮环境路径/端口策略、worker 共享旧句柄隔离、健康检查直连及三项长期反例。此前已确认的领域拆分、纯显式依赖转发、路由权限等保持。集中完成 R11-A 的 RSS、会议邀请、邮件唯一实现和跨领域通知调用链，顺手核查其余技能是否还有同类业务残留或双份实现；按 R11-B 修正真实符号追溯。不要仅为了旧 patch 保留重复业务或反向取工具层函数。按第八轮冻结源码核对配置来源、默认 DB、端口、参数、通知与停止语义，迁移不重写算法。交付实际调用链、完整 diff 和原始门禁日志；未清完不得称阶段二完成，不进入阶段三，禁止 commit/push/部署/重启/真实外发。

## 本轮修改总结与 Diff

```diff
--- features/network_ops/socks5_service.py
+++ features/network_ops/socks5_service.py
+ 恢复已有环境 DB 路径、Windows 路径、默认 ai.db 及旧端口排除策略
+ 旧句柄扣减前验证 event 代次；新代所有者计数重置
--- features/sys_ops/__init__.py
+++ features/sys_ops/__init__.py
- 通过 skills 旧健康报告别名绕行
+ /check 与 daily_health 直接使用 health_service
--- skills/ops_self_check.py
+++ skills/ops_self_check.py
- 无外部消费者证据的 _get_health_report 别名
--- tests/test_codex_review_round5_probes.py
+++ tests/test_codex_review_round5_probes.py
+ patch canonical health_service
+++ tests/test_phase2_service_migration_closeout.py
+ 环境 DB、端口策略、旧共享句柄三项反例
```

证据目录 `/tmp/kernel-stage2-codex-round11-20261016/`：`probes-red.log` 原始失败；环境反例随后精确化为已存在路径；`targeted-final.log` 最终 21 项专项；`inventory-symbol-audit.json` 静态候选；`full-gates.log` 最终完整回归。测试未操作真实 DB、代理服务或外发。

## 最终验证补录

- 本机 Python 3.13 Agent：**1054 passed，4 subtests passed**。
- 只读无网 Docker Python 3.9 Agent：**1054 passed**。
- 两环境 Parser：各 **205 tests passed**，工作区干净。
- 两环境架构：**266 文件、13 处核准 finding、0 未登记违规**。

完整 verify_e0 首次退出码 **1**，7/8 通过：唯一失败是本轮删除健康别名后留下文件末尾多余空行。已只清理该空行，`git diff --check` 单独重跑退出码 **0**，日志 `diff-check-corrected.log`；另核查 `git diff --check HEAD` 通过。没有再次整套执行 verify_e0，不能把首次日志改写成整套退出码 0。格式修正不改变 Python 语义，不额外重复双版本行为测试。

完整工作区 patch、变更源码哈希清单和 patch.sha256 保存在上述证据目录，包含新文件与暂存删除，不含私人配置或业务数据，不改 Git index。

**最终裁定：本轮直接修补与双版本回归通过，原业务唯一实现及追溯缺口仍未关闭；阶段二不提交完成基线，不进入阶段三。**
