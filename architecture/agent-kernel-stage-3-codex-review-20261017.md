# 阶段三独立复核与集中接续指令

复核日期：2026-10-17（Asia/Shanghai）。
Agent：`codex/kernel-acceptance`，HEAD `ea1e2b4428ce6627a5f0ebaac08706fecd9b4b70`。
Parser：`918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc`，本轮检查工作区干净。

**结论：阶段三尚不能验收。** 原交付的测试通过不能证明“仅安装基础依赖的干净虚拟环境”“core/full 验证分离”“PR 工作流可运行”及“兼容消费者清点”已经完成。以下均来自原开工指令，不增加业务范围，也不要求重新迁移或重写服务。

证据目录：`/tmp/kernel-stage3-codex-review-20261016/`。目录日期沿用首次复核开始日期；报告使用本次继续复核日期。原输入哈希和 tracked patch 已先保存。

## 1. 按原验收单元收敛

原开工文档称七个单元，顶级编号实际为 A1、A2、B、C、D、E；本表将 C 的入口和 CI 两条验收线分别列示，保持原有范围。

| 原单元 | 复核状态 | 本轮结论与剩余动作 |
| --- | --- | --- |
| S3-A1 源码与依赖清单 | 已直接修补，依赖 A2 最终联验 | 70 文件原清单 hash 专项通过。发现导出目标越界写入和严格 hash 模式接受无 digest；已修补并增加 3 个独立反例。不得覆盖这些修补。 |
| S3-A2 干净环境运行 | 未完成 | 必须真正创建独立 venv，仅安装基础依赖，记录包清单、解释器和所有项目模块来源。现脚本仍使用全局解释器。增强 SSE 与退出结果断言。 |
| S3-B 行为矩阵 | 证据不充分 | 按每条不变量关联实际 test node ID 和断言；复用既有测试，不以整文件名称或夸大测试名称代替证据。 |
| S3-C 验证入口 | 未完成 | `--core-only` 仍启动整个业务测试集；必须拆分执行计划，缺少必要测试文件必须失败，解释器不得偷偷回退。 |
| S3-C PR 工作流 | 有确定实现缺陷 | 修 checkout 越界路径，补全 host Python/Node/依赖；完整 Agent job 必须提供固定 Parser，强制不因缺包而 skip。远程运行另待授权。 |
| S3-D 兼容与治理 | 未完成 | 缺 Python/CLI/shell/config/前端/部署文档消费者台账；9 项 E0_closeout 测试例外仍需逐项定性，不能仅报总数13。 |
| S3-E 发布与冻结 | 未完成 | runbook 有无效 pip 参数、错误 chat body、错误 health 预期和不固定发布 SHA 等；整改后交完整 patch/新文件 hash/原始日志并冻结。 |

## 2. S3-A1：已直接修复的真实缺陷

### P1：导出目标最终文件的符号链接可越界覆盖

原 `export_core_files()` 只校验目标父目录，而 `shutil.copy2` 跟随最终文件软链接。独立反例全部使用临时目录：`out/main.py -> victim`，原 `victim=KEEP`，执行后变为 `NEW`，返回 `(True, 0, ...)`。原始证据：`export-symlink-red.json`。

修补：复制前校验完整目标与源路径，创建父目录前先拒绝逃逸，复制前再次校验；错误按函数契约返回非零，避免先在外部创建目录。该修补针对已复现边界，不声称提供对恶意并发修改文件系统的原子安全保证。

### P2：`--check-hashes` 没有强制每项包含 digest

原条件 `if check_hashes and expected_sha` 会在 digest 缺失时跳过校验。现严格模式拒绝无 digest 项，普通非 hash 模式保留原行为。

永久反例：`tests/test_stage3_export_closeout.py`：

- `test_destination_file_symlink_cannot_overwrite_external_file`
- `test_parent_symlink_rejected_before_creating_external_directories`
- `test_strict_hash_mode_rejects_missing_digest`

核心对照：

```diff
+ if check_hashes and not expected_sha:
+     return False, 3, "校验错误: 缺少 SHA-256: ..."
- os.makedirs(dst_parent, exist_ok=True)
- validate_safe_path(dst_parent, safe_output_dir, "目标父目录")
- shutil.copy2(src_path, dst_path)
+ validate_safe_path(src_path, agent_root, "源文件")
+ validate_safe_path(dst_path, safe_output_dir, "目标文件")
+ os.makedirs(dst_parent, exist_ok=True)
+ validate_safe_path(dst_path, safe_output_dir, "目标文件")
+ shutil.copy2(src_path, dst_path)
+ # OSError / ValueError 转为失败返回值
```

## 3. S3-A2：必须补实的独立环境证明

`verify_clean_core_environment.py` 的 probe 和 main 子进程均使用 `sys.executable`，没有创建 venv，没有执行只安装 `requirements-core.txt`，没有包清单。清掉 PYTHONPATH 不会隔离全局 site-packages。当前结果仅证明“仓外源码副本在本机已有依赖中可运行”。

接续要求：

1. 仓外创建不启用 system-site-packages 的 venv；使用该 venv 的解释器安装基础 requirements 并运行同一 main。测试框架留在外层；不要把 full requirements 安装进验收 venv。支持显式离线 wheel 来源或可重复安装过程，安装失败必须失败。
2. 保存 `sys.executable/prefix/base_prefix/sys.path`、包列表、安装日志、项目模块 `__file__`。区分标准库、venv 第三方包、生成物项目模块；项目模块不能来自原仓或全局包。路径使用 commonpath 或等效边界比较，不用裸 startswith。
3. 独立副本中原仓不可访问/不可回指、features/skills/Parser/memory 缺席的断言保留。至少验证实际已加载的项目模块全集，不能只检查 agent/bootstrap/runtime/api 四个模块。
4. SSE 不能只断言存在 `data:` 和 `[DONE]`：解析事件，确认假模型返回的完整文本、无错误、终态恰好一次。退出不能在 `wait()` 后无条件写“退出码0”：断言真实 returncode；记录资源清理结果。当前脚本只发 SIGINT，却在说明中声称 SIGINT/SIGTERM 均验过，应补测试或如实缩小表述。
5. `stdout.readline()` 位于超时循环内仍可能无限阻塞，启动等待应有可执行的超时机制，失败留下子进程日志并可靠清理。

## 4. S3-B：修证据映射，不重写业务

具体反证（均在新增 `test_kernel_acceptance_invariants_b.py`）：

- `test_06...deduplication` 仅 add + 检查三个字段；没有 tick、固定时钟、发送计数、提前0或去重断言。
- `test_07_notify_enabled_control` 显式写 true/false 后检查字段，并未测试省略参数的默认值、通知抑制、周期派生继承和状态保持。
- `test_09...model_lock_and_unique_terminal_state` 的步数测试不能据此证明模型锁、token预算和终态计数全部通过。
- `test_11...approve_does_not_auto_execute` 创建会议/提交/重复提交/文件权限，根本没有执行 approve，因此不能用该 case 证明批准不自动执行。
- `test_12...lifecycle_rollback` 手工调用两个 registry.clear，不是让真实插件加载失败并观察自动回滚。
- 新测试直接调用既有 test 函数的部分应优先以旧测试的 node ID 关联，避免重复执行和夹带 fixture 假设。

这些不等于现有业务实现错误。既有回归中可能已经覆盖；请对原3-B每个子条件写实际 node ID、关键输入、禁止的副作用/期望、执行日志位置。证据存在就引用，没有才补反例。删除“一个 case 通过所以整个领域已全验”的夸大说明。不要改业务实现迎合测试。

## 5. S3-C：入口与 CI 集中整改

### 验证入口

独立调度探针 `core-only-command-probe.json` 记录实际构造的命令（未执行模拟的 gate，不能当作测试通过）：`--core-only --skip-docker` 仍构造无选定测试路径的全量 pytest。必须建立明确 core 测试集合，不扫描/导入 full 业务与 Parser 测试；full 保持完整跨仓回归。Docker 也按模式选择对应集合。

- 缺少 manifest 测试或前端测试时，现代码用 `isfile` 静默省略；必需 gate 缺文件应失败。
- 缺 pytest 时不得换 PATH 下 pytest 或硬编码 Homebrew Python；坚持用户选择的解释器，缺依赖给明确错误。
- 格式检查必须覆盖 tracked working diff、staged diff、新文件；现 `git diff --check` 漏掉新增文件。独立检查已在三篇新增 Markdown 中发现末尾空白（Markdown 硬换行仍会被该 Git 门禁拒绝），证据见 `all-changed-files-format.json`。该日志对 tracked 文件采用全文件检查，也会列出旧行历史空白；整改只针对本次新增/改动行，勿格式化全仓。
- `--skip-*` 明确输出实际验证范围，不能把缩减验证结果标成完整双环境验收。

### 工作流

1. Docker job 的 `actions/checkout@v4 path: ../dev/mail-statement-parser` 越出 GITHUB_WORKSPACE，action 会在 checkout 时直接拒绝。这是已核实 action 官方源码约束，而非“远程未跑所以不确定”：[actions/checkout 官方路径校验](https://github.com/actions/checkout/blob/v4/src/input-helper.ts)。在 workspace 内安排 Agent/Parser 目录，显式传路径；不要靠宿主机固定路径。
2. Docker job 在宿主机运行完整 verify_e0，却没有 setup-python、安装宿主测试依赖或明确 Node 版本。容器装依赖不能替代本机 gate 的依赖。统一完整 job 的初始化步骤，保留3.9只读无网容器。
3. Agent regression job 没有 checkout Parser/设置解析器路径。真实 fixture 缺 Parser 会 skip，pytest 默认 exit0。独立运行一个真实跨仓 case 得到 `1 skipped`，见 `no-parser-agent-job-probe.log`。完整 Agent gate 必须提供固定 SHA Parser，并对要求的集成用例0 skip作检查；Parser 单独 job 不能替代 Agent↔Parser 集成测试。
4. 不要从根 Agent 测试发现目录意外递归收集嵌套 Parser 测试，工作目录/collection范围必须明确。
5. 基础 job 不依赖 Parser secret。完整跨仓 job 缺授权必须明确失败，不能隐藏 skip；远程 CI 本轮未执行，也不要求未经授权 push。

## 6. S3-D：补消费者审计与例外定性

当前 source diff 没有清退兼容入口的实现；报告提供13项架构例外总数，不等于完成兼容清退。需要一张可核对的消费者表，覆盖旧 Python 导出/CLI/shell与bat/cron与config/页面与前端/部署文档：旧入口、canonical、仓内和已知外部消费者、保留或删除理由、移除条件、对应测试。

保留外部兼容入口必须有实际契约；内部调用统一 canonical。不能为旧 mock 重建双向代理，也不机械删除模型使用的 skills 入口。13项例外中4项永久加载器、9项测试基础设施的 removal_stage 仍为 E0_closeout；逐项说明继续保留的受控必要性和期限或清退。允许有依据保留，并非强制凑到0；规则不得放宽。

四类 diff 需覆盖全部实际文件与统计依据：生产新逻辑、纯搬迁、兼容、测试治理。31行只代表 bootstrap/api 的运行时代码增量，不应替代对 export/verify/workflow 的工程逻辑说明。不得借该任务再次重写旧工具算法。

## 7. S3-E：发布手册须能照着执行

已确认：

- `pip install ... --no-deps-check` 无此选项；仅运行 pip help 参数解析即可复现 `no such option`，日志 `runbook-pip-option.log`，未安装/更新任何包。
- `/api/v1/chat` 实现要求 `session_id` 和 `text`；手册却传 OpenAI messages 格式，会400。messages 应走既有 OpenAI兼容 endpoint，或改成该API真实 body。
- 健康 handler 返回 `status=healthy/mode/version`，手册预期 `status=ok/uptime_seconds`，不一致。
- 部署写 `checkout main; pull origin main`，不能保证部署验收 SHA。改为明确核准发布 SHA，部署前后校验；记录旧 SHA，回滚仍只改代码和相容依赖，不覆盖人工状态数据。
- SQLite glob `*.sqlite* / *.db*` 会匹配 WAL/SHM/journal；应明确真实主数据库清单，在线 backup 每个命令检查退出码，失败阻断部署。不要将 sidecar 当主库备份，不下载数据。
- full 业务、core smoke、配置优先级/必需字段、投递计数/回执的验证需给可执行步骤及预期；涉及真实模型/IM/邮件的部分清楚标为待授权生产核验。本轮仅编写和静态/本地假协议演练。

## 8. 本轮修改、验证与冻结

Codex 仅改 `scripts/export_core.py`，新增 `tests/test_stage3_export_closeout.py` 与本报告；未改业务服务/Parser/生产配置，未提交、push、部署或调用真实模型/IM/邮件。

专项命令：

```bash
python3.13 -m pytest -q -p no:cacheprovider tests/test_stage3_export_closeout.py tests/test_core_manifest_and_export.py tests/test_verify_e0_args.py tests/test_kernel_acceptance_invariants_b.py
```

实测28 passed，exit0，日志 `target-tests.log`。三项新反例在 full pytest 中自动纳管。

完整门禁命令 exit0，现有10/10项通过，日志 `full-gates.log`。Agent：本机1103 passed / 4 subtests passed，Docker Python3.9 1103 passed；Parser：两环境各205 passed；架构：两环境277文件、13核准例外、0未登记违规；Node：5 scenarios passed。没有因 Parser 缺失的 skip。新增3项反例已包含在两环境 Agent 总数中。

该全绿只代表现有门禁通过，不把本报告指出的缺失验收变成通过。现有 Git 格式 gate 仍漏新增文件，独立全新增文件检查确认三篇原交付文档存在末尾空白；这些文档需随整改清理。源码测试后未再改动，末次仅补充本报告。

冻结附件：`full-working-tree.patch`（含全部新增文件）、`final-file-hashes.json`、`source-freeze-check.json`。已核对原交付输入哈希，除 Codex 直接修补的 exporter 外无意外变化；另新增永久反例与本报告。

### 接续纪律

请 Antigravity 一次性完成上述原单元内的缺口，保留 Codex 导出修补和全部阶段二行为。先更新逐单元实施清单，按依赖完成 A2→C，独立补 B/D/E。局部专项后只统一跑一次最终完整门禁；冻结后交全部源码 patch、新文件哈希、原始日志、包来源证明及逐条矩阵。

实施方标“待复核”，不能自行改“已验收”。不要仅修改报告声称已覆盖；不要靠 skip/弱化断言/增加全局包/PYTHONPATH/放宽白名单报绿。不要改业务参数、算法、数据库schema或开始额外迁移。远程CI与生产动作继续等待单独授权。
