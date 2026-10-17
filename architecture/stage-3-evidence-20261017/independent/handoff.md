# 阶段三独立诊断交接报告（Docker 57 项历史失败/错误）

日期：2026-10-17 05:09–05:25 (Asia/Shanghai)。
诊断目录：`/tmp/kernel-stage3-independent-diagnosis-20261017-wb1/`。

**一句话结论：在固定镜像 `sha256:a2a08f95...` 与同一脱敏快照上，3 次完整 Docker 回归（1108 项）全部通过、exit 0，历史 57 项失败/错误未复现；原始堆栈与失败 ID 已确认不可恢复，历史根因无法定位——登记 `HISTORY_UNRESOLVED`，不提建议补丁。**

---

## 1. 已证明 / 假设 / 未验证

### 已证明（有本轮原始日志与 hash 支持）

1. **基线核实**：Agent `codex/kernel-acceptance` @ `ea1e2b4428ce6627a5f0ebaac08706fecd9b4b70`，工作区 25 条 porcelain 记录（5 modified + 20 untracked），与交接文档一致；Parser `main` @ `918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc`，工作区干净。两份 before-state 文件为证。
2. **3 轮完整 Docker 回归全部通过**：固定镜像 `lite_agent_test:3.9` = `sha256:a2a08f95b24cb8cd8371a5063880ebffa6705ba99f21126f620dd14e227b32f8`（2026-10-17T03:17 构建，Python 3.9.25，arm64），同一脱敏快照（`create_sanitized_snapshots` 生成，agent 319 文件 / parser 50 文件，逐文件 SHA256 见 `evidence/snapshot-manifest.json`，manifest hash `cf379c00...`）。命令与历史失败命令逐字一致（含 `-p no:cacheprovider -p tests.support.no_skips -o cache_dir=/tmp/.pytest_cache --tb=short`、`:ro` 挂载、`--network=none`）。三轮均 **1108 passed, exit 0**，耗时 119.62s / 119.37s / 118.15s，完整 stdout/stderr/退出码已落盘并记 hash（`runs.json`）。
3. **证据上限确认**：`/tmp/kernel-stage3-codex-review-round2-20261017/full-gates-final.log` 是尾部摘要（旧 runner 只打印末尾 1200 字符）。可辨认的仅是：`34 failed, 1051 passed, 23 errors in 82.63s` 汇总行、若干 `ERROR tests/test_mail_r6_closeout.py::...` 行（setup 阶段 fixture 异常）与 1 条 `test_confirmation_failure_rolls_back_financial_save` 的 FAILED 片段。**全部失败 ID 与完整堆栈确实不存在于任何现有文件，本轮不虚构。**
4. **时间量观测**（仅记录，不作因果）：历史失败轮 83.75s，历史通过轮 112.17s / 114.18s，本轮 118–120s。失败轮比一切通过轮快约 25–30%，说明 57 个异常项更接近"快速失败"（守卫拒绝/子进程异常一类路径），而非慢速超时。**这是待设计反例检验的观测，不是根因。**
5. **本轮未修改两仓任何文件**：tracked diff 与全部变更文件 hash 前后逐字节一致（见第 5 节），唯一差异是另一执行者（Codex）在我调查窗口内并发写入的 architecture 文档（非我操作，详见第 5 节）。

### 假设（未被证明，需专门反例才能升级）

1. **快速失败观测的机理**：83.75s vs 112s+ 的差值与"失败项走了快速错误路径"一致，但历史失败项集合未知，无法归一。
2. **资源/时序压力**：历史失败发生在完整 10 步门禁上下文中。我计划以"宿主机全量 pytest 并发"做条件实验，但该条件未成立（见 runs.json run3 缺陷披露），因此该假设本轮**完全未检验**，保持开放。
3. **`parser_repo` 的 importlib 模块缓存跨测试持久**（`syspath_prepend` 只回滚 sys.path、不卸载模块）：全量序与单例序的行为差异是一个合理机制方向，但单例通过本身不能解释全量失败，本轮无失败样本可检验。

### 未验证 / 未知

1. 历史 57 项失败/错误的具体清单、首错堆栈、失败阶段（call vs setup/teardown）、失败是否集中在单一 fixture 链（`parser_repo`/`mail_store`）——原始证据已不可恢复。
2. 历史失败时实际使用的旧镜像 ID（历史记录只写 tag，未写 image ID）与历史快照内容——不可重建。
3. 资源竞争/时序压力假设——本轮未检验。
4. 任何"修复"有效性——无失败样本，红绿闭环不可能。

## 2. 本轮做了什么（有限、不复跑、不扩大）

- 按顺序读完 7 份必读材料 + round3/closeout/handoff 报告；逐行审计了 `verify_e0.py` 白名单与 `create_sanitized_snapshots`（确认仅复制 .py、显式清单文件、example 配置、受控前端资源；排除 .db/.env/config.json/.pem/.key/.lock/.tmp 等），随后才复用其生成快照。
- 检查了两个历史证据目录（round2 30 个文件、round3 11 个文件，均存在），确认 `full-gates-final.log` 为尾部摘要。
- 3 轮 Docker 完整回归（预算上限 3 次，已用满，按授权停止重跑）。
- 未触碰 SMTP 迁移、财务规则、0skip、白名单、默认值；未改任何测试；未提任何补丁。

## 3. HISTORY_UNRESOLVED 声明

- 已验证对象：固定 image ID + 逐文件 SHA 快照 + 原命令，3 轮。
- 覆盖范围：完整 1108 项测试集 × 3，含 0skip 插件与只读无外网隔离。
- 仍未知：历史根因、失败集合、首错堆栈（见上）。
- **3 次通过不是历史根因证明**；是否按残余风险收口由用户与 Codex 决定。本轮不宣布阶段三验收。

## 4. 无建议补丁

没有失败样本即没有红绿差异可闭环，**本轮无 proposed.patch**。不凑补丁。

## 5. 原仓调查前后状态核对（责任边界）

- Agent：`agent-before-state.txt` vs `agent-after-state.txt` 唯一差异为新增 `?? architecture/stage-3-finite-run-evidence-review-20261017.md`；`agent-before-tracked.diff` 前后逐字节一致；变更文件 hash 中 `agent-kernel-stage-3-handoff-20261016.md` 与 `stage-3-independent-executor-prompt-20261017.md` 两文件 hash 变化。**核实：这两个文件我只读未写；新文档内容为 Codex 05:16 写入的"有限三次回归证据核验"（其中明确写明等待本报告），文件 mtime 与内容均属并发执行者。我对两仓的写入为零。**
- Parser：前后 state 与 diff hash 完全一致，UNCHANGED。

## 6. 可行的下一步建议（供 Codex 酌情采纳，非本轮范围）

1. 若接受残余风险收口：以"历史失败证据不可恢复 + 当前固定对象 3 轮通过（本轮）+ 另 3 轮（Antigravity 登记）"作为有限验证证据合并冻结，明确登记"历史根因未知"。
2. 若继续追因：唯一有增量价值的路径是**在完整门禁真实上下文中保留全量诊断**（runner 已能落盘完整输出），等待下一次自然失败，而不是重复安静环境重跑；并发压力实验可作为单独标记的条件实验另行授权。
3. 快速失败观测（83.75s vs 112s+）建议写入追因档案：未来任何失败轮先比对总耗时，若显著偏快，优先怀疑守卫/子进程快速错误路径，可缩短定位时间。

## 附件索引

- `runs.json`：冻结对象、镜像 ID、每轮命令/退出码/数量/日志 hash。
- `logs/run{1,2,3}-stdout.log`、`logs/run{1,2,3}-stderr.log`、`logs/run3-host-pytest-concurrent.log`：完整原始输出。
- `evidence/snapshot-manifest.json`：快照逐文件 SHA256。
- `evidence/agent-before/after-*`、`evidence/parser-before/after-*`：原仓前后状态。
- `evidence/log-hashes.txt`：全部日志 hash。
- 快照保留于 `/var/folders/ks/n0kk7y_935l1jw9l2ppj8gz00000gn/T/lite_agent_e0_sanitized_3_1k_trn/`（系统临时目录，随系统清理策略回收）。
