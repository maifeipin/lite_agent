# 阶段三 Codex 独立复核与收尾记录（尚未验收）

日期：2026-10-17（Asia/Shanghai）。Agent 分支 `codex/kernel-acceptance`，HEAD `ea1e2b4428ce6627a5f0ebaac08706fecd9b4b70`。Parser 基线 `918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc`，复核时工作区干净。

本轮依据上一份集中指令复核 Antigravity 整改，并直接完成小量工程与证据收尾，没有再迁移或重写业务工具。**结论：尚未通过阶段三验收。** 文档与工程缺口已直接收尾，但Docker完整门禁出现结果不一致，必须定位并解释失败后才能收口。

证据目录：`/tmp/kernel-stage3-codex-review-round2-20261017/`。

## 原验收单元核对

| 单元 | 本轮核对与收尾 |
| --- | --- |
| S3-A1 | 70项源码manifest与基础requirements保留；上一轮目标软链接、创建外部目录、无digest反例完整保留。 |
| S3-A2 | 已确认真venv，system-site-packages关闭，仅安装core requirements；真实main/API使用假模型与回环端口。补上来源拒绝断言、完整JSON证据落盘、完整SSE文本和尾部终态检查、实际退出码核验。 |
| S3-B | 原矩阵28个测试引用有10个不存在。已校正并补子条件，最终54个Agent测试引用通过AST解析；进一步用实际pytest collection核对。Parser的重解析保护、UNKNOWN和字段清空保护落实到真实unittest ID。 |
| S3-C 入口 | core模式按显式清单执行，本轮含26个测试文件；缺Parser仍完成core测试。补缺Git/ls-files失败不能报绿。新增验收插件使真实skip导致非零退出。 |
| S3-C CI | 越界checkout已改，host Python/Node/依赖与固定Parser挂载已补。原job名称写“Disallow Skipped Tests”但命令没有约束，本轮补插件到Agent job与本机/Docker门禁。远程Actions未运行。 |
| S3-D | 重建32个实际ops模块的规范导入、定义符号与静态消费者表，逐项列13例外。删除Agent≠AgentRuntime错误替代、虚构python -m app、未经批准阶段四SDK/微服务计划。未知外部消费者如实标未核实；已有契约保留，不凭猜测删除。 |
| S3-E | 修复漏备真实Todo/会议库、静默跳缺库、阶段二SHA可误部署默认值；改为实际数据库绝对路径清单、备份失败阻断和必须显式核准SHA。更新真实chat状态与生产待授权范围。 |

原文称七单元，顶级编号实际为A1、A2、B、C、D、E；上表分列C入口/CI两条验收线，未增加范围。

## 关键反例与直接修补

### 1. “0 skip”必须是执行约束

原workflow的Agent job仅写名称，pytest默认仍会允许skip成功退出。新增 `tests/support/no_skips.py`，session结束若存在skip，强制exit1。`tests/test_stage3_acceptance_closeout.py` 使用临时独立子测试验证“故意skip→exit1”和“正常通过→exit0”，同时验证Git缺失与新增文件列表失败不可报绿。

```diff
- python -m pytest tests/ -q -p no:cacheprovider --tb=short
+ python -m pytest tests/ -q -p no:cacheprovider -p tests.support.no_skips --tb=short

- return True, "环境未安装 git，跳过格式与 diff 检查"
+ return False, "环境未安装 git，无法完成格式与 diff 检查"
+ # git ls-files 非零也返回失败
```

同一插件已接入verify_e0的本机与Docker测试，core/full均使用，不改变业务运行。

### 2. 独立运行证明必须保存来源证据

实现方已经创建真实隔离venv，安装core依赖。原脚本收集的包/模块字典仅存内存，命令结束丢失；本轮增加 `--report-json`，省略时也在系统临时目录保存完整证据路径。

```diff
+ parser.add_argument("--report-json", help="保存可复核的解释器、包、模块来源与步骤证据")
+ report["dependency_install"] = {"command": ..., "exit_code": ..., "stdout": ..., "stderr": ...}
+ # 断言prefix确为独立venv，拒绝全局site-packages和未分类的外部模块
+ # integrations也纳入生成物项目模块来源检查
+ json.dump(report, evidence_file, ensure_ascii=False, indent=2)
```

真实证据 `clean-core-proof.json`：passed=true；25个安装包、863个已加载模块。来源分类仅core_project、stdlib、venv_third_party，解释器prefix与base_prefix不同。源码导出物没有features/skills/Parser/memory；实际普通/SSE、鉴权、health/dashboard、业务404、SIGINT退出0均通过。没有使用editable install或PYTHONPATH回指原仓。

### 3. SSE终态检查不能先break再数次数

原脚本收到第一个DONE就break，随后断言done_count==1无法识别尾部重复终态。现读取有界尾部，拒绝错误事件、要求完整假模型文本，再数DONE。

本轮首次强化时直接等EOF，现有API响应会保持连接，导致专项超时；该次失败保留在 `full-gates.log`（非最终通过日志）。已修正：读到终态后才允许空闲超时结束；未收到终态的超时仍失败。兼容Python3.9的socket.timeout与新版本TimeoutError。未更改生产SSE协议。

```diff
- if line_str == "data: [DONE]":
-     break
+ try:
+     line = resp.readline()
+ except (TimeoutError, socket.timeout):
+     if "data: [DONE]" in sse_lines:
+         break
+     raise
+ assert not chunk_json.get("error")
+ # 完整文本必须匹配假模型的同步或流式完整响应，DONE恰好一次
```

这是有界本地协议验收，不声称可以排除任意长延迟后未来出现的非法帧。

### 4. 追溯和发布文档纠偏

- 原矩阵10个不存在的测试定位见 `matrix-symbol-probe.json`；校正后54个引用无缺失，见 `matrix-symbol-probe-after.json`。补北京时间跨天、claim失败重试、人工状态、静默继承、Cron回拨/并发、owner/outbox的真实旧测试，避免重复写算法凑数。
- 兼容表据源码AST生成32个模块的真实记录，原始机械审计见 `compatibility-static-audit.json`；静态扫描局限在表中明确，不把动态调用、外部主机或模型工具视为已穷尽。90工具的注册追溯沿用阶段二最终盘点。
- 13项例外范围未扩大。4永久受控加载器、9临时跨仓测试；9项E0_closeout历史标签仍是已登记债务，有同等真实合同测试替代并核准后才能移除，不承诺未经批准的“阶段四微服务”。
- 原runbook的todo.db和room.sqlite3不是源码默认的todos.db与data/committee/review_meetings.sqlite3；现要求核实目标环境实际绝对路径清单，缺库/备份失败阻断。没有访问生产或下载数据。
- 最终发布commit尚未生成，因此runbook必须由操作人显式提供核准SHA，不提供可误用的阶段二默认值。回滚保留人工状态数据库，只回滚代码及相容依赖。

## 本轮验证

- 专项25 passed，exit0：`target-tests.log`。
- 独立venv真实运行exit0：`clean-core-proof.log`、`clean-core-proof.json`。
- core-only指定不存在的Parser路径，5项基础gate通过，557 passed，0 skip，exit0：`core-gates.log`。明确跳Docker与full，不伪装为完整验收。
- 最终完整命令：`python3.13 -u scripts/verify_e0.py --parser-root /Users/lilee/Projects/dev/mail-statement-parser`，日志 `full-gates-final.log`。结果见文末。
- pytest实际收集：`collected-node-ids.log`；Parser财务子条件ID：`parser-evidence-nodes.json`。
- 未向VPS部署、未重启、未真实模型/IM/邮件外发。隔离venv安装依赖可访问包源；Docker回归仍只读、无外网。

## 源码范围与冻结纪律

本轮直接修改verify_e0、clean-proof、workflow以及矩阵/兼容台账/runbook/交接文档，新增no_skips插件、3个永久反例与本报告。没有修改Agent/Parser业务算法、财务规则、默认值或schema。运行时健康路由和绑定端口记录是实施方原阶段三交付，本轮未再改变。

原输入哈希：`input-hashes.json`；最终源码冻结：`final-code-freeze.json`；最终完整patch与新文件哈希在完成后一起归档。原实施方交接文档标注为历史快照，避免1105计数或旧哈希被误当最终版本。

远程CI实际run、required checks、commit/tag/push、生产配置核验、发布及回滚演练均未执行；这些是后续授权动作，不用本地测试结果替代。当前不执行Git提交或任何生产操作。

## 最终实测结果与唯一剩余验收阻塞

| 检查 | 真实结果 |
| --- | --- |
| 本机最终Agent全量 | 1108 passed，4 subtests passed，0 skip |
| core独立计划（Parser路径不存在） | 557 passed，0 skip，5项基础gate通过 |
| Parser本机 / Docker3.9 | 各205通过 |
| 架构本机 / Docker3.9 | 各279文件、13核准例外、0未登记违规 |
| 原始最终完整门禁命令 | exit1，9/10通过；Docker Agent为34 failed、1051 passed、23 errors |
| Docker单例复现 | 出错fixture的一项真实用例单独运行1 passed |
| 新脱敏快照完整顺序复现（-x，长堆栈输出） | 1108 passed，exit0，112.17s；本次没有触发任何失败 |
| 增补原始日志保存后的runner专项 | 9 passed，exit0；另用受控exit2命令验证stdout/stderr完整落盘 |

不能以重跑一次通过推翻那次57项失败/错误，也不能在没有堆栈的情况下断言“环境偶发问题”“同一根因”或“已修复”。这仍是S3-C完整Docker验收的真实阻塞。源码业务没有据此改动；其它单元不重新开工。

原始失败命令的verify_e0只输出尾部1200字符，实际异常堆栈已丢失。因此本轮进一步修补run_step：每个已完成子步骤完整stdout/stderr/退出码/命令保存到系统临时日志，输出路径并在结果中返回raw_log。只增加可观测性，未改变通过标准。该最后一处runner修改已跑9项相关验证；没有把此前全量结果冒充这处修改后的统一10/10。

证据：`full-gates-final.log`（失败汇总，不能写成通过）、`docker-repro.log`（单例通过）、`docker-first-failure.log`（新快照完整1108通过，-x未触发停止）、`runner-after-logging.log`、`runner-log-proof.json`。原EOF检查超时的更早失败另在 `full-gates.log`，已与本次Docker结果不一致区分。

### 集中接续，只处理这条验收线

1. 保留本轮全部修补和文档，不再次重写ops工具或服务。冻结字节、记录Docker镜像ID、Python版本、命令、源快照哈希和环境中与测试相关的非敏感变量。脱敏复现快照位置在 `repro-snapshot.json`。
2. 使用已增强的verify_e0重现完整双环境门禁；若Docker再次失败，立即保留打印的完整原始输出，定位首个实际错误，而非只看失败数。同时保留容器镜像ID和同一份源码快照，不能仅靠新快照重跑报绿。
3. 若仅全量失败、单例通过，再依据首个堆栈缩小前驱测试、模块缓存/fixture、资源和环境差异；这些目前仅是待排查方向，不是已证实根因。不得新增skip、放宽断言、调整财务算法或把0skip约束撤掉。
4. 找到根因后只修对应验收单元，保留原失败反例及修后证据，最后统一完整门禁并冻结交回。即使后续连续通过，仍需说明此前失败证据能够解释到什么程度，不将未复现等同已解决。

## 冻结附件与四类diff

最终工作区完整patch（含新增文件）、逐文件hash、输入到输出的变更清单与四类diff统计均在本证据目录。生产新增31行，相对阶段二非tests/scripts的Python物理行35319行为0.0878%；本阶段没有新的业务纯搬迁或兼容包装源码变更。其余是构建、测试与治理。

未commit、push、tag、部署或重启；Parser工作区干净。当前不进入发布授权，先闭环上述本地Docker验收阻塞。
