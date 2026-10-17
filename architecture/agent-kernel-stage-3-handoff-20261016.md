# 阶段三 Antigravity 实施交付与复核交接报告 (Agent Kernel Stage 3 Handoff)

> [!NOTE]
> **Codex 2026-10-17 复核收尾对齐说明**：
> 本文已依据 Codex 第二轮独立复核收尾报告（`architecture/agent-kernel-stage-3-codex-closeout-20261017.md`）完成全面校准。
> 针对 Docker 完整门禁中曾出现的偶发 57 项跨仓测试失败/错误（34 failed, 23 errors），本轮提供快照排除规则加固与后续通过记录；Codex第三轮复核确认：缓存/锁残留的归因缺少证据，历史故障根因仍未知，不能宣称闭环。后续通过不覆盖原始失败。
> 本地未执行 `git commit`、未执行 `git push`、未向 VPS 部署。交付状态严格保持**待复核**。

**交接日期**: 2026-10-17 (Asia/Shanghai)
**工作分支**: `codex/kernel-acceptance`
**基线提交**: `ea1e2b4428ce6627a5f0ebaac08706fecd9b4b70` (阶段二 Codex 收口本地提交)
**Parser 外部依赖**: `/Users/lilee/Projects/dev/mail-statement-parser`，HEAD `918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc` (工作区干净，财务算法保持不变)
**环境依赖**: Python 3.13.5 (Host), Python 3.9.25 (Docker 29.4.0), Node v22.16.0

> [!IMPORTANT]
> **授权与安全红线声明**:
> 1. 本地工作区严格遵循结对编程规范，**未执行 `git commit`、`git push`**；
> 2. **严禁并绝对未向 VPS 执行远程部署、服务重启或配置修改**；
> 3. 所有本地测试均采用回环端口、临时数据库、假模型（Mock LLM）与注入 Fake 传输适配器，**零真实 IM/邮件/外网外发**；
> 4. 未修改 Parser 任何财务计算与权威读模型算法；
> 5. 坚决执行“0 skip”硬性约束（挂载 `tests.support.no_skips` 插件）；
> 6. 全文交付状态统一标记为**待复核**，不自行标记为“已验收”，等待 Codex 独立复核。

---

## 1. 六个固定验收单元状态总览

| 单元编号 | 验收单元名称 | 交付产物与关键动作 | 状态 | 验证方式与核心结果 |
|:---|:---|:---|:---:|:---|
| **S3-A1** | 基础源码与依赖清单 | `requirements-core.txt`<br>`architecture/kernel-core-manifest.txt`<br>`scripts/export_core.py`<br>`tests/test_core_manifest_and_export.py`<br>`tests/test_stage3_export_closeout.py` | **待复核** | 70 个核心文件版本清单及 SHA-256 哈希固化；保留并纳管 Codex 修复的符号链接越界写入防御与严格哈希校验缺口；反例覆盖缺文件/越界/非法软链接/业务夹带，11/11 项测试全绿通过。 |
| **S3-A2** | 干净环境独立运行证明 | `app/bootstrap.py` (内置 `/health` 与 `/api/v1/health`)<br>`channels/api.py` (动态端口绑定)<br>`scripts/verify_clean_core_environment.py`<br>`tests/test_clean_core_environment.py` | **待复核** | 仓外创建不启用 system-site-packages 的真实隔离虚拟环境 (venv)，仅安装 `requirements-core.txt`；支持显式离线 wheel 来源 (`PIP_FIND_LINKS`)；保存完整 JSON 证据落盘 (`clean-core-proof.json`)；记录解释器元数据、`sys.path`、已安装包清单、863 个模块分类断言；回环假模型非流式/流式对话 (SSE 完整行解析与单次 `[DONE]` 终态断言，支持超时优雅退出)、401/403 鉴权、未启用业务 404 隔离、SIGINT 优雅退出与真实 `returncode == 0`，端到端全绿。 |
| **S3-B** | 旧行为验收矩阵 | `architecture/kernel-acceptance-matrix.md`<br>`tests/test_kernel_acceptance_invariants_b.py` | **待复核** | 覆盖 12 个业务核心领域与阶段二收尾能力链；54 个测试引用全部通过 AST 解析与真实 collection 核对；账单默认 163 与范围校验、sync3m 预算与 revision ack、财务证据 UNKNOWN、人工还款保护、北京时间 12:00 提醒、真实 `tick()` 结合时钟与 5 分钟去重、缺省 `notify_enabled=True` 与显式抑制、inline 零外发、`ModelLock.HARD` 与 Token 超限/单次终态、Cron 边界时钟、会议两轮协商至 `awaiting_approval` 与审批批准、PluginLoader 原子回滚，13 项专项测试 100% 通过 (0 skip)。 |
| **S3-C** | 验证入口参数化与 PR 工作流 | `scripts/verify_e0.py`<br>`tests/support/no_skips.py`<br>`tests/test_verify_e0_args.py`<br>`tests/test_stage3_acceptance_closeout.py`<br>`.github/workflows/kernel-acceptance.yml` | **待复核** | `verify_e0.py` 增补 `--agent-root/--parser-root/--core-only/--skip-docker/--skip-parser`；显式提取 25 个文件的 `CORE_TEST_FILES`；挂载 `no_skips` 强制存在 skip 即非零退出；每个子步骤完整 stdout/stderr/退出码/命令独立保存到系统临时日志 (`raw_log`)；Git 与格式检查缺工具或非零直接阻断；CI 修复跨工作区 checkout 路径，补全 host Python/Node 依赖，固定 Parser 挂载。 |
| **S3-D** | 兼容与治理清退 | `architecture/stage-3-compatibility-consumer-inventory.md`<br>`scripts/check_architecture.py`<br>`architecture_rules.json` | **待复核** | 编制 32 个模块真实消费者台账；逐项定性 13 项架构例外（4 处永久受控加载器 vs 9 处测试基础设施过渡期理由，保留历史 E0_closeout 标签直至同等合同测试核准）；删除未经批准的微服务或 SDK 承诺；四类 Diff 严格统计与定性。 |
| **S3-E** | 发布准备与冻结交接 | `architecture/release-runbook.md`<br>`architecture/agent-kernel-stage-3-handoff-20261016.md`<br>`architecture/agent-kernel-stage-3-codex-closeout-20261017.md` | **待复核** | 发布手册修正实际数据库绝对路径清单（`todos.db`、`data/committee/review_meetings.sqlite3` 等），备份失败严格阻断；锁定操作人显式提供核准 commit SHA，禁止回滚或冒用阶段二默认值；回滚保留人工状态数据库。 |

---

## 2. 门禁验证结果汇总 (全量 10/10 硬性门禁通过)

本地运行完整全量门禁命令：
```bash
python3.13 -u scripts/verify_e0.py --parser-root /Users/lilee/Projects/dev/mail-statement-parser
```

### 实测汇总报告 (最新完整 10/10 PASS)
```text
==================================================
📊 门禁验证汇总报告
==================================================
✅ PASS | 1. 静态架构门禁 (AST check_architecture)                      | 耗时   0.89s
✅ PASS | 2. 基础源码清单与导出安全防御 (S3-A1 unittest)                       | 耗时   0.89s [8 tests passed]
✅ PASS | 3. 前端独立 JavaScript 契约 (Node.js)                         | 耗时   0.06s
✅ PASS | 4. Agent 全量回归与副作用隔离 (pytest)                            | 耗时  98.00s [1108 passed, 4 subtests passed]
✅ PASS | 5. Parser 路径隔离与全量回归 (unittest)                          | 耗时   2.11s [205 tests passed]
✅ PASS | 6. Agent Git 格式与末尾空白规范                                  | 耗时   0.07s [git & whitespace clean]
✅ PASS | 7. Parser Git 格式与末尾空白规范                                 | 耗时   0.06s [git & whitespace clean]
✅ PASS | 8. Docker Python 3.9: Parser 全量测试 (代码只读 :ro, 无外网)       | 耗时   7.65s [205 tests passed]
✅ PASS | 9. Docker Python 3.9: AST 架构依赖门禁 (代码只读 :ro, 无外网)        | 耗时   1.19s
✅ PASS | 10. Docker Python 3.9: Agent 全量回归测试 (代码只读 :ro, 无外网)     | 耗时 114.18s [1108 passed]
==================================================
🎉 恭喜！所有硬性门禁（含本机与 Docker Python 3.9 全量双回归）全部通过！
```

**原始子步骤日志定位（通过 runner 可观测性记录）**：
- Step 4 (Host Agent 回归): `/var/folders/ks/n0kk7y_935l1jw9l2ppj8gz00000gn/T/kernel-gate-output-8npeqcv8.log` (1108 passed, 4 subtests passed, exit 0)
- Step 5 (Host Parser 回归): `/var/folders/ks/n0kk7y_935l1jw9l2ppj8gz00000gn/T/kernel-gate-output-9gbxl9lp.log` (205 tests passed, exit 0)
- Step 8 (Docker Parser 回归): `/var/folders/ks/n0kk7y_935l1jw9l2ppj8gz00000gn/T/kernel-gate-output-abg7x1ak.log` (205 tests passed, exit 0)
- Step 9 (Docker AST 门禁): `/var/folders/ks/n0kk7y_935l1jw9l2ppj8gz00000gn/T/kernel-gate-output-x28dt8_e.log` (279 files, 13 exceptions, 0 violations, exit 0)
- Step 10 (Docker Agent 回归): `/var/folders/ks/n0kk7y_935l1jw9l2ppj8gz00000gn/T/kernel-gate-output-riu8h1ym.log` (1108 passed, exit 0)

### 独立基础验证模式实测 (--core-only --skip-docker)
```bash
python3.13 scripts/verify_e0.py --core-only --skip-docker
```
```text
==================================================
📊 门禁验证汇总报告
==================================================
✅ PASS | 1. 静态架构门禁 (AST check_architecture)                      | 耗时   0.98s
✅ PASS | 2. 基础源码清单与导出安全防御 (S3-A1 unittest)                       | 耗时   0.81s [8 tests passed]
✅ PASS | 3. 前端独立 JavaScript 契约 (Node.js)                         | 耗时   0.20s
✅ PASS | 4. Agent 基础核心回归与边界测试 (pytest --core-only)               | 耗时  22.02s [557 passed, 0 skip]
✅ PASS | 6. Agent Git 格式与末尾空白规范                                  | 耗时   0.11s [git & whitespace clean]
==================================================
⚠️ 缩减验证通过！本次运行包含以下跳过项，非完整双环境全量验收：
   - 全量业务与跨仓集成 (模式: --core-only)
   - Docker 容器双回归 (--skip-docker)
```

---

## 3. 四类 Diff 划分与说明

本次阶段三变更严格限制在治理、验证基础设施、文档规范与极小量系统基础能力补强：

| 变更类别 | 涉及文件 | 增减行数 | 职责说明与设计必要性 |
|:---|:---|:---:|:---|
| **生产新逻辑** | `app/bootstrap.py`<br>`channels/api.py` | +31 / -0 | **极小量系统级基础能力**: 在通用路由层内置免认证 `/health` 与 `/api/v1/health` 纯核心健康状态端点（不依赖 features/ 业务层）；在 `ApiServer` 启动时记录物理绑定端口 `self.port = self.server.server_address[1]` 支持动态回环端口探测。<br>**增幅占比**: 占阶段基线代码量 0.0878%（远低于 10% 预警线）。 |
| **纯搬迁逻辑** | 无 | +0 / -0 | 阶段三无业务模块搬迁。 |
| **兼容与参数化** | `scripts/verify_e0.py`<br>`Dockerfile.test`<br>`requirements-test.txt` | +245 / -70 | 为本地验证与 CI 门禁提供 `--agent-root/--parser-root/--core-only/--skip-docker/--skip-parser` 参数支持；增加 Node.js 前端独立检查步骤与 S3-A1 核心清单合法性检查；增加 tracked/staged/untracked 末尾空白检测；构建脱敏快照时显式排除 `.pytest_cache`、`*.lock`、`*.tmp`；每个子步骤完整原始输出落盘并返回 `raw_log`；Docker 镜像预构建离线 wheels 缓存支持无网络环境运行。 |
| **测试与治理产物** | `requirements-core.txt`<br>`architecture/kernel-core-manifest.txt`<br>`architecture/kernel-acceptance-matrix.md`<br>`architecture/release-runbook.md`<br>`architecture/stage-3-compatibility-consumer-inventory.md`<br>`scripts/export_core.py`<br>`scripts/verify_clean_core_environment.py`<br>`.github/workflows/kernel-acceptance.yml`<br>`tests/support/no_skips.py`<br>`tests/test_core_manifest_and_export.py`<br>`tests/test_clean_core_environment.py`<br>`tests/test_verify_e0_args.py`<br>`tests/test_kernel_acceptance_invariants_b.py`<br>`tests/test_stage3_export_closeout.py`<br>`tests/test_stage3_acceptance_closeout.py` | +2200 / -0 | 阶段三全部验收单元所要求的受版本控制清单、独立验证脚本、PR CI 工作流定义、零 skip 约束插件、消费者台账、发布 Runbook 及配套自动化反例测试用例。 |

---

## 4. 架构依赖与异常登记核准 (S3-D)

运行 `python3.13 scripts/check_architecture.py` 扫描结果：
- **扫描 Python 文件总数**: 279 个
- **已声明核准例外项**: 13 处
- **实际命中例外项**: 13 处
- **未登记的架构依赖违规**: **0 处** (门禁检查通过)

### 例外项台账 (architecture_rules.json)
1. `core/skill_engine.py`: 动态加载 skills 目录模块 (`RULE_DYNAMIC_IMPORT_REGISTERED`, 永久例外)
2. `core/skill_engine.py`: parent_dir 路径注入 (`RULE_NO_CROSS_REPO_SYS_PATH`, 永久例外)
3. `core/skill_engine.py`: skills_dir 路径注入 (`RULE_NO_CROSS_REPO_SYS_PATH`, 永久例外)
4. `app/plugins.py`: 受控插件清单动态导入 (`RULE_DYNAMIC_IMPORT_REGISTERED`, 永久例外)
5-13. `tests/test_mail_quality_closeout.py` 与 `tests/test_mail_r6_closeout.py`: 受控测试隔离加载 Parser 数据契约与只读测试模型 (9 处，阶段收口追踪，沿用历史 `E0_closeout` 标签登记，待同等契约测试核准后清退)

---

## 5. 变更文件清单与 SHA-256 哈希列表

| 文件路径 | 状态 | SHA-256 哈希 |
|:---|:---:|:---|
| `Dockerfile.test` | 修改 | `2c5332481f1c4e693502d54e914f8fea0ee9f22681393ed52912682812990c59` |
| `app/bootstrap.py` | 修改 | `ec931b7a00d92cc6292bc8a4d7d41495973ac99880b544f2a553845115c952f5` |
| `channels/api.py` | 修改 | `c484ee0fe8060e2a6884f3d00ae3a6b0fe57823037d18d581ebc7e11673b8e5f` |
| `requirements-test.txt` | 修改 | `fbb5c6fcb6a8dfa20cf16e11beb2448515a63613ad29c600c9993a6758ff2819` |
| `requirements-core.txt` | 新增 | `af1a2176fa1aa490e29e9ef90939fb1da6f5491fe564600e5174f6ef0d3bb916` |
| `scripts/verify_e0.py` | 修改 | `47ee4430b28aa5774408e5c5ee67da34d96b71b2f8c1e8b7c164b6340a7fd391` |
| `scripts/export_core.py` | 新增 | `03cb8003de78f459b06a5af1bd1a10c36a0d0597ac73b94129826bb766cda613` |
| `scripts/verify_clean_core_environment.py` | 新增 | `15ad2d79c0d9e006f6133d02798a431249aaacab7b9acaea0c6de8e168808659` |
| `.github/workflows/kernel-acceptance.yml` | 新增 | `3fd56563050b88a22f0f7f494b346b12d8b1f9952d19a4286b72a4249af2f719` |
| `architecture/kernel-core-manifest.txt` | 新增 | `225961dfaa031dad738691a25ec6bd1321f1bd198e5d3e17b6638d9e52d6e301` |
| `architecture/kernel-acceptance-matrix.md` | 新增 | `560d9efb49628b3bca4453a8d4369985bc6a3cabf5674ce023587b9678ef419c` |
| `architecture/release-runbook.md` | 新增 | `88c384b7b2f9b20b42980f96d105866523202b820ca742f1c8d387488a591043` |
| `architecture/stage-3-compatibility-consumer-inventory.md` | 新增 | `4ace7bb3f670f2bd31dd403fdf69b120fcaf39389e524ec1e4607b1321090d5e` |
| `tests/support/no_skips.py` | 新增 | `26b081169fd515dbfe003fb3e8fc683a324ca92a0aafcec0aaab6ed20145c016` |
| `tests/test_core_manifest_and_export.py` | 新增 | `66753df1e676cfadceac4b4ba3d8a8ab2c5bc7a554e1f90ca7306a4a30acbc88` |
| `tests/test_clean_core_environment.py` | 新增 | `32d478dd1f2be8c9bc4ce92f44837c1e4f2eaebabc98131ffefa14eb70c30fea` |
| `tests/test_verify_e0_args.py` | 新增 | `6ef9b96d392ee469626e78b8a29ea7a7ebe6866188fba06638c7f17e23c44b88` |
| `tests/test_kernel_acceptance_invariants_b.py` | 新增 | `05de53e09cec1e1b8570cf93441a038d2487f388746cc2a6183d7d38a6aab6b6` |
| `tests/test_stage3_export_closeout.py` | 新增 | `2e3dbaed08db4646fd2896fd7780e64f5377c7426dc222f82feb383dea4eb1f1` |
| `tests/test_stage3_acceptance_closeout.py` | 新增 | `7e0c45724e92feaf64140c7124207872f9c5402758951d870c5875f01f749db7` |

---

## 6. 待 Codex 独立复查与接手清单

根据阶段三结对编程分工，Antigravity 已一次性完成全部缺口整改、反例覆盖与本地全量门禁闭环。移交 Codex 独立复核：

1. **本地门禁独立复现**:
   - `python3.13 -u scripts/verify_e0.py --parser-root /Users/lilee/Projects/dev/mail-statement-parser`
   - `python3.13 -u scripts/verify_e0.py --core-only --skip-docker`
   - `python3.13 -m pytest tests/test_clean_core_environment.py tests/test_kernel_acceptance_invariants_b.py tests/test_verify_e0_args.py tests/test_stage3_export_closeout.py tests/test_stage3_acceptance_closeout.py`
2. **干净虚拟环境独立证明审查**:
   - 检查 `clean-core-proof.json` 的真隔离 venv、解释器元数据、`sys.path` 与项目模块来源分类日志（25 个包、863 个模块分类无外溢）。
3. **消费者台账与发布 Runbook 审查**:
   - 检查 `architecture/stage-3-compatibility-consumer-inventory.md` 与 `architecture/release-runbook.md`。
4. **PR CI 与远程分支核准**:
   - 审查 `.github/workflows/kernel-acceptance.yml` 的 5 大 Jobs 逻辑。
   - 在用户明确授权后，推进 Git Commit、分支合并与远程发布核验。

---

## 7. Docker历史失败的证据状态（Codex第三轮校正）

历史完整门禁为34 failed、1051 passed、23 errors，非通过共57项。原始verify脚本只保留输出尾部，无法从现有摘要还原所有失败ID与堆栈。第430至485号只是两个测试文件在一次collection中的位置，共56项，不是已证明的故障边界，不能据此宣称其它用例全部通过。parser_repo还被billing_repayment_todo、mail_sync_stage4、mail_sync_stage4_closeout等文件使用。

原先“缓存/锁文件与只读挂载导致fixture级联错误”的文字是未证实假设，撤回根因定性。Codex恢复上一轮冻结脚本并核对完整SHA；将普通pytest缓存、lock、tmp、sqlite残留分别植入最小仓库，旧、新快照输出相同，仅保留合法源码。这证明普通残留在旧白名单下已经不会被复制。原失败命令也已带no:cacheprovider和PYTHONDONTWRITEBYTECODE=1；cache_dir=/tmp不是本轮新修复。

新增.pytest_cache目录排除及.lock/.tmp显式排除可作为纵深防御保留，但没有旧版本触发相同错误、加固后消失的红绿反例，不能称已修复历史故障。FAILED也可能是测试调用阶段的异常，不仅是断言失败；ERROR不能单凭摘要定位到init_db或某次import。

可读取的后续原始日志已由Codex核对：kernel-gate-output-riu8h1ym.log为Docker1108通过、exit0；kernel-gate-output-upks5uts.log为core557通过、exit0。Task编号不是可直接读取的日志路径，多次通过不构成历史因果证明。详细独立反例、冻结SHA和日志校验见 `architecture/agent-kernel-stage-3-codex-review-round3-20261017.md` 及 `/tmp/kernel-stage3-codex-round3-20261017/`。

状态仍为待复核且历史不一致未闭环。后续仅处理同一验收线：保存可复核的同镜像/同源码快照执行记录和异常诊断；若旧失败无法再现，明确标为历史根因无法重建，而非继续编写已定性的解释。本轮不扩大业务、不修改Parser财务规则、不撤销0skip、不提交或部署。

### 7.1 有限 3 次回归验证执行记录 (Finite 3-Run Stability Verification)

依据 Codex 第三轮复核策略，在固定 Docker 镜像 ID 与固定脱敏快照上执行有限 3 次完整回归，原始日志全部落盘并计算 SHA-256：

- **Docker 镜像精确 ID**: `sha256:a2a08f95b24cb8cd8371a5063880ebffa6705ba99f21126f620dd14e227b32f8` (Tag: `lite_agent_test:3.9`, Python 3.9.25)
- **固定脱敏快照基线**: `/var/folders/ks/n0kk7y_935l1jw9l2ppj8gz00000gn/T/lite_agent_e0_sanitized_xxaux6_o`
  - Agent 快照根哈希: `8222c5a12053e1669104c70c4595fd4f76d4218e72fe6c1a854c9faf6a169241`
  - Parser 快照根哈希: `9bbc88a2f90718f6a92cd4b8d68efb5d4b2777fe8e8c758f35f422c062d0b7b0`
- **执行命令**:
  `docker run --rm --network=none -v .../agent:/app:ro -v .../parser:/parser:ro -e MAIL_STATEMENT_PARSER_DIR=/parser -e PYTHONDONTWRITEBYTECODE=1 -w /app sha256:a2a08f... python -m pytest tests/ -q -p no:cacheprovider -p tests.support.no_skips -o cache_dir=/tmp/.pytest_cache --tb=short`

| 轮次 | 实测结果 | 耗时 | 退出码 | 原始日志路径 | 日志 SHA-256 |
|:---:|:---:|:---:|:---:|:---|:---|
| Run 1 | 1108 passed, 0 skip | 114.81s | 0 | `/tmp/kernel-stage3-codex-round3-evidence/docker-run-1.log` | `bc96eea4c57d24b4dca71d2add4e93efd333b5d3c719399f884b97b50abc5a0c` |
| Run 2 | 1108 passed, 0 skip | 116.52s | 0 | `/tmp/kernel-stage3-codex-round3-evidence/docker-run-2.log` | `5b24d77db30980ed440d17933aec4a1e11b2fac5ec167b7a6be966d2dae1a16b` |
| Run 3 | 1108 passed, 0 skip | 120.06s | 0 | `/tmp/kernel-stage3-codex-round3-evidence/docker-run-3.log` | `1b38f838042897ef121e5c8bb52d681d40cfdb76363c171b6f50a67fe0c5808a` |

> [!WARNING]
> **残余风险交接声明 (HISTORY_UNRESOLVED)**：
> 上述连续 3 次回归 100% 通过仅证明当前固定环境与快照具有高运行稳定性；**3 次通过绝非历史失败的因果机理证明**。历史 57 项失败/错误的原始堆栈与快照已不可考，根因正式登记为 `HISTORY_UNRESOLVED`。是否接受该已知残余风险进行阶段收口，严格由用户与 Codex 审阅后裁定，不得擅自修改结论。

### 7.2 用户风险接受与本地验收确认（2026-10-17）

用户已明确同意按已披露残余风险完成阶段三本地验收。最终状态为ACCEPTED_LOCAL_WITH_KNOWN_RISK，历史问题仍为HISTORY_UNRESOLVED，不改称根因已修复。详见 `architecture/agent-kernel-stage-3-risk-acceptance-review-20261017.md` 与已归档证据index.json。前文待复核/未收口描述均为当时交付状态，以本条确认作为最新状态。

附加结案声明的新发现单独登记，未因此删除文件、迁移技能注册层或修改业务。用户此次确认不包含commit、push、tag或部署许可。
