# 给新增本机执行者的独立诊断提示词

> **任务状态更新（2026-10-17）**：独立诊断已交付，用户已明确接受历史残余风险，阶段三本地验收已确认。本文以下为历史任务提示词，不应据此重新启动调查或重复回归。后续附加缺陷、清理、提交与上线须按新的明确任务和授权处理。当前记录见 `architecture/agent-kernel-stage-3-risk-acceptance-review-20261017.md`。

以下全文可直接作为新会话的首条提示词。你没有本次重构的历史上下文，请先读必要材料，再自主开展限定范围内的调查，不要重新规划或重做整个重构。

## 最新接续状态（优先于下文旧复现计划）

Antigravity已完成固定镜像、固定快照的有限3次回归。Codex已核验三份日志哈希、1108通过和exit0；镜像ID存在。证据位于 `/tmp/kernel-stage3-codex-round3-evidence/manifest.json`，独立审计在 `/tmp/kernel-stage3-finite-run-audit-20261017/`，新增说明为 `/Users/lilee/Projects/lite_agent/architecture/stage-3-finite-run-evidence-review-20261017.md`。

测试快照与当前脱敏源码逐文件相比，仅scripts/verify_e0.py的日志保存与防御过滤存在差异；业务源码、测试和Parser内容一致。历史根因仍为HISTORY_UNRESOLVED，没有凭3次通过改称已修复。

**全团队的这批有限3次计划已经完成，不因增加执行者重置次数。你当前优先做独立证据审计、源码/fixture/模块状态分析与有明确假设的最小实验，不自动重复3次全量。** 下文B的三次方案是原始方案，只用于理解既有证据。若发现新的可检验线索，先用最小实验验证；确需新增全量执行时先给Codex具体实验理由和预期区分的假设，由其协调，避免两个执行者重复消耗。没有新线索时交付独立风险判断即可，不编造根因。

## 你的角色、目标和权限

你是新增的独立诊断执行者。Codex负责统筹、独立复核、最终收尾；Antigravity是原实施者。你的任务是提供新的可验证证据，推进阶段三最后一条未关闭验收线：**Docker Python 3.9完整回归曾出现57项失败/错误，之后相同规模回归又通过，历史根因尚未确认。**

本轮授权你读取相关源码、分析历史证据、创建脱敏临时快照、编写临时诊断工具、运行本机假服务和只读无外网测试容器。正常读文件和隔离测试不需要逐项向用户确认。必须尊重操作系统或执行工具本身的审批约束，不绕过权限。

**不要直接修改两个原仓库的任何文件，不要更新已有交接报告，不要替其他执行者收口。** 你的脚本、诊断插件、报告和建议补丁全部写入独立的系统临时目录。可以修改你自己创建的脱敏实验副本，但要保留未修改对照副本和清晰diff。Codex复核后决定是否将建议补丁纳入共享工作区，避免多执行者同时写同一源码。

禁止commit、push、tag、reset、clean、stash、部署、重启、修改远程设置或生产配置。禁止连接真实模型、IM、SMTP、生产数据库或VPS。不要操作机器上无关的运行容器。不要输出或复制私人config.json、.env、凭据、邮件原文、生产库。日志中的测试路径可以记录，环境变量只收集明确批准的非敏感路径与测试开关，不输出完整环境。

## 最小背景与真实基线

这是一项三阶段Agent内核重构：

1. 阶段一建立contracts、边界和架构门禁；已完成。
2. 阶段二将业务移出通用内核，建立受控插件、生命周期和core/full装配，并移除SMTP对外部Parser的依赖；已完成独立收尾并本地提交。
3. 阶段三验证基础源码可独立运行、旧行为保真、CI与发布准备；主体已实施，当前卡在上述Docker结果不一致。不要增加第四阶段、微服务、SDK重写或新的业务迁移。

本机路径与读到的基线：

- Agent：`/Users/lilee/Projects/lite_agent`
- Agent分支：`codex/kernel-acceptance`
- Agent HEAD：`ea1e2b4428ce6627a5f0ebaac08706fecd9b4b70`（阶段二收尾提交）
- Parser：`/Users/lilee/Projects/dev/mail-statement-parser`
- Parser HEAD：`918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc`，最近核对工作区干净。

**阶段三源码主要处于未提交工作区，包含大量untracked文件。只checkout HEAD、另建一个普通worktree或只复制git diff都会漏掉当前实现。** 先核对实际HEAD、branch、status；不能用reset恢复基线。如果实际状态与本文不同，只记录差异并核实，不覆盖任何修改。

保留业务算法、参数签名、默认值、配置优先级、财务权威读模型、人工还款/Todo/会议状态、Worker租约与代次隔离。不要为了让测试通过改这些语义。

## 必读材料（按顺序，不需要先读全部历史）

1. `/Users/lilee/Projects/lite_agent/.agents/AGENTS.md`
2. `/Users/lilee/Projects/lite_agent/architecture/agent-kernel-stage-3-codex-review-round3-20261017.md`
3. `/Users/lilee/Projects/lite_agent/architecture/agent-kernel-stage-3-codex-closeout-20261017.md`
4. `/Users/lilee/Projects/lite_agent/architecture/agent-kernel-stage-3-handoff-20261016.md`
5. `/Users/lilee/Projects/lite_agent/scripts/verify_e0.py`
6. `/Users/lilee/Projects/lite_agent/tests/conftest.py`、`tests/support/isolation_guard.py`、`tests/support/no_skips.py`
7. `/Users/lilee/Projects/lite_agent/tests/test_mail_quality_closeout.py`、`tests/test_mail_r6_closeout.py`，以及它们真正使用的Parser模块和fixture。

需要理解范围时再查：

- `/Users/lilee/Projects/lite_agent/docs/agent-kernel-refactor-execution-plan-20261015.md`
- `/Users/lilee/Projects/lite_agent/architecture/stage-2-3-antigravity-rules-20261015.md`
- `/Users/lilee/Projects/lite_agent/architecture/agent-kernel-stage-2-closeout-review-20261016.md`
- `/Users/lilee/Projects/lite_agent/architecture/kernel-acceptance-matrix.md`

报告冲突时，区分当时的交付声明与可读取的原始证据；最新独立复核已否定的归因不能继续引用为事实。

## 已确认事实：不要重新推测或混淆

- 本机Agent曾1108通过，core独立计划557通过；Parser本机与Docker各205通过；架构279文件、13核准例外、0未登记违规。
- 一次完整门禁的Docker Agent结果为：**34 failed、1051 passed、23 errors，总计1108项**。该次完整命令exit1，9/10 gate通过。
- 后续新脱敏快照中Docker完整1108通过；Antigravity后来也提供了通过日志。通过记录是真实的，但不能替代故障因果证明。
- 历史失败时，verify脚本只打印输出尾部，原始完整堆栈和所有失败ID未保存；不能假装这些信息还在。现在verify的run_step已保存完整子进程输出并打印日志路径。
- 两个测试文件在一次collection中的位置是430–459和460–485，共56项。**这不是已证明的失败边界，56也解释不了57。** parser_repo至少被5个测试文件引用，不能称只有这两个文件使用它。
- FAILED不仅指断言失败，也可能是测试call阶段异常；ERROR不能单凭摘要确定是init_db、某个import或锁冲突。
- Antigravity曾把根因定性为“旧缓存/锁残留被快照带入只读容器”。Codex独立反例确认：普通pytest缓存、.lock、.tmp、.sqlite在旧白名单下已经不会复制；这些样本在旧、新构建器中输出相同。新增排除可以保留为纵深防御，**没有证明修复了历史故障**。
- 原失败命令已经使用`-p no:cacheprovider`、`PYTHONDONTWRITEBYTECODE=1`及`-o cache_dir=/tmp/.pytest_cache`，不能将这些旧参数当成本轮新修复。
- 不能把未经验证的“全局状态污染”“缓存”“时序”“环境问题”写成确定根因。它们最多是需要设计反例检验的假设。

## 已有证据位置

先检查是否仍存在；不存在就明确记录，不编造。

`/tmp/kernel-stage3-codex-review-round2-20261017/`：

- `full-gates-final.log`：历史失败摘要，非通过报告。
- `docker-repro.log`：单例通过。
- `docker-first-failure.log`：新快照全量1108通过；命令带-x，但没有失败触发提前停止。
- `repro-snapshot.json`：保留脱敏快照位置。
- `collected-node-ids.log`、`matrix-collection-proof.json`：实际收集与矩阵核对。
- `final-file-hashes.json`、`final-code-freeze.json`：当时冻结字节。
- `clean-core-proof-final.json`：独立venv来源证据。

`/tmp/kernel-stage3-codex-round3-20261017/`：

- `cache-lock-counterexample.json`：旧、新快照构建器对普通残留的独立比较。
- `scope-audit.json`、`provided-log-audit.json`：范围核对与真实通过日志校验。
- `kernel-gate-output-riu8h1ym.log`：Docker1108通过的已备份原始日志。
- `kernel-gate-output-upks5uts.log`：core557通过的已备份原始日志。
- `input-hashes.json`、`final-file-hashes.json`：第三轮输入和文档校正后的状态。

临时路径和哈希只能说明当时的证据，不假定它们自动等于你接手时的最新工作区。

## 执行步骤与收敛条件

### A. 先独立审计，建立你自己的冻结对象

创建唯一目录，例如`/tmp/kernel-stage3-independent-diagnosis-<unique>/`。记录HEAD、分支、状态、必要源码及测试的SHA、准确命令、Python版本、实际Docker image ID。镜像tag可能变化，不能只写lite_agent_test:3.9。

先审核verify_e0中的白名单，再复用`create_sanitized_snapshots`生成实验副本。不要直接把整个原仓挂进容器，也不要tar整仓备份。生成物只含明确源码、示例配置及已核准脱敏fixture。保留完整文件清单和hash；实验前后核对两原仓未被你修改。

先检查现有证据是否支持任何更具体的定位。若历史堆栈或失败快照已无法恢复，马上写明这个证据上限，不要耗费时间“补全”不存在的信息。

### B. 有限复现，优先捕获首错

在固定image ID、同一脱敏源码快照上，**最多3次完整Docker回归，不是必须跑满**。使用原测试集、0skip插件、:ro挂载、--network=none和原有隔离环境。第一轮先保留未加诊断插件的对照，不用改变随机种子或测试顺序来制造“稳定”。如改变条件实验，要单独标记。

每轮保存完整stdout、stderr、退出码、收集数量、失败/错误/skip数量及日志hash。不能只保留末尾摘要或不可读取的Task编号。

任次失败：停止刷全绿，立即保留原始对象并定位首个实际错误。优先记录setup/call/teardown阶段、完整异常、模块来源、相关Parser模块的路径属性、实际测试DB路径、fixture与清理顺序、相关文件的存在/权限/只读情况。只记录非敏感信息。之后用少量前驱用例加失败用例缩减，单例通过本身不能解释全量失败。

如需仪器化，在独立实验副本或/tmp外置pytest插件中实施，保留未仪器化对照；仪器化可能改变导入顺序/时序，要披露这个局限。不得向原仓写调试逻辑。

### C. 只有观测支持后才提修补

能复现时，提交最小触发条件与首个根因证据，再在副本验证建议补丁的红绿差异。建议补丁必须绑定输入SHA，供Codex审查；不要在共享原仓直接应用。

若3次均未复现或证据不足，停止完整重跑，交付`HISTORY_UNRESOLVED`。说明已验证对象、次数、覆盖范围、仍未知内容和可行的下一步。**三次通过不是历史根因证明。** 是否按残余风险收口由用户与Codex看完整证据后决定，你不能自行宣布阶段三已验收。

不要重开SMTP迁移、工具服务重写、发布上线等其它工作；不要扩大架构例外、修改默认值、删失败测试或放松0skip。

## 交付格式

输出到你的/tmp独立目录，不改原交接文档：

1. `handoff.md`：一句话结论，分清“已证明、假设、未验证”，列具体文件/函数/行号。
2. `runs.json`：冻结对象、image ID、版本、每轮准确命令、退出码/数量、实际日志路径与hash。
3. 失败的完整堆栈与最小复现；若未复现，交付明确的`HISTORY_UNRESOLVED`，不要虚构失败材料。
4. 若有建议修补：`proposed.patch`、基准SHA、红绿日志、影响与局限。没有就写无，不凑补丁。
5. 原仓调查前后状态核对，证明你没有覆盖其它执行者的代码。

最终回复给出绝对路径，方便用户将你的结果交回Codex。不通过消息工具自行联系其它执行者；用户会协调回传。你提供独立证据和可审查建议，不代替Codex验收或执行发布。

现在开始：先报告真实基线、你建立的证据目录和第一项可检验假设，然后自主推进，不以长篇计划代替实际调查。
