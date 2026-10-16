# 阶段二第十二轮：冻结交付、迁移保真复核与收敛执行

日期：2026-10-16。HEAD `5c4016682eeb9d5b14638b0e37e65f8342183bd1`，分支 `codex/kernel-composition`。

## 裁定与策略调整

**R11-A 的文件归位和多处反向依赖清理确已完成，但当前交付仍含未经说明的行为重写，阶段二不通过。** 本轮不再以“发现几个样本→只修这些样本→再次声称全部完成”的方式推进。

已建立两份长期文件：

- `architecture/stage-2-closeout-control-20261016.md`：冻结、差异分类、增量验收及收口规则。
- `architecture/stage-2-closeout-ledger-20261016.json`：六个固定验收单元、186 个服务函数诊断及本轮源码指纹。

它们落实原阶段二要求，不新增业务目标，也不要求一工具一服务或对每个纯搬迁函数新增测试。代码复核与生产验收继续分开，提交/部署权限不变。

## 本轮证据基线

旧源码使用 **HEAD 加第八轮冻结完整 patch** 恢复的 33 个 skills Python 文件，而非只用尚未包含完整阶段二成果的 HEAD。恢复文件位于 `/tmp/kernel-stage2-codex-round12-20261016/baseline/skills/`。

当前记录 298 个源码/规则/脚本/测试/静态资源文件哈希，见同目录 `frozen-source-hashes.json`。核对期间已捕获源码未发生变化，最终结束时再次核对。本轮仅新增审计与治理文件，没有修改交付业务实现。

## R11 已落实的部分

- RSS service、邀请 service 已实际落地；频道/内容服务到旧技能 helper 的多处反向调用已清理。
- 邮件 Reader 技能改为委托，旧全局 Agent 状态清退；相关测试 patch 转向服务。
- Socks5 默认告警使用 integrations.notifications。
- 业务从旧 ops 实际进入了 feature，较上一轮有进展。不能继续用旧“59 工具、3 作业待迁出”描述当前状态。

但上述结构完成不代表迁移行为保持，也不能据此免除新旧函数对照。

## 已证实阻塞一：备份 zip 只存链接，未存源数据（P1，S2-C03）

位置：`features/sys_ops/backup_service.py:do_backup`。

旧实现创建临时暂存目录，用 `_safe_symlink` 将 data、statements.db、email-downloads、validation-reports 链入，然后执行 `zip -r`，将链接指向的数据收入备份。新实现压缩参数加入 `-y`，改成保存符号链接本身。

**独立实测：** 临时应用目录中放入 `data/review.txt`，内容固定为 `review-backup-content`；调用真实 `do_backup`。所有 ssh/docker/外部备份函数均拦截，仅允许真实 zip 对临时数据执行。生成的 data zip 条目只有 `['data']`，没有 `data/review.txt`，而该过程仍将核心备份列为 OK。

这会使异地备份缺少真正的数据内容，无法按旧行为恢复。不是注释、类型提示或文件移动产生的差异。没有读取私人配置值、生产库或邮件，没有调用 ssh/docker，也没有上传备份。

恢复要求：保留旧暂存链接所指向的数据内容、打包结构与恢复能力；对其他备份分支同步按 S2-C02 完整差异表核对，不只删除一个参数便宣布备份迁移通过。长期反例应解包或读取真实临时 archive，不能只断言 subprocess 被调用。

## 已证实阻塞二：候选代理探测丢失 resolver 配置（P1，S2-C03）

位置：`features/network_ops/socks5_service.py:test_socks5_proxy_outbound`。

旧实现通过 `parse_runcmd_to_naive_config` 保留 proxy、host-resolver-rules 与隔离 listen，生成临时 JSON 后启动既有 naive 二进制。新实现重新用正则拼接命令，只复制 proxy 和部分额外参数。默认 timeout 也从 5 改成 12，临时进程启动与终止等待策略改变。

**独立实测：** 输入带 `--host-resolver-rules="MAP example.invalid 192.0.2.20"` 的非 active 节点，拦截 Popen、socket、sleep 和出站测试，不启动进程或联网。真实函数构造的启动参数仅为：

```text
naive --listen=socks://127.0.0.1:29877 --proxy=https://user:pass@example.invalid
```

其中 user:pass 是反例中的固定占位值，不是凭据。resolver 映射丢失，也没有采用保存该映射的旧临时配置。依赖域名映射的节点可能无法正确探测，影响候选选择与故障转移。

恢复要求：按旧基线恢复候选探针的解析、配置、二进制选择、平台限制、默认值和清理语义；若发现旧缺陷，另列缺陷修补理由与反例，不把重新实现称为纯迁移。出站失败结果的 ip/loc 字段也存在删减，须在同一差异处置中核对消费者。

## 完整差异审计范围（S2-C02）

当前扫描 feature 中 186 个顶层服务函数，与旧技能函数比较：

| 诊断类别 | 数量 | 含义 |
|---|---:|---|
| 同名候选的函数体/签名匹配 | 57 | 忽略 docstring、装饰器、函数名及类型提示；仍需核对模块配置/常量/路径 |
| 存在同名候选但内容不同 | 89 | 可能是合法调用适配，也可能有行为改变，需分类 |
| 无直接同名映射或已改名 | 40 | 需人工建立追溯，包含新装配职责与重命名 |

**不是 129 个已确认缺陷，也不能换算完成百分比。** 例如 `_backup_vaultwarden` 的差异主要是返回类型标注，不能误判为算法重写；`do_backup` 和候选探针则有已经实测的行为改变。模块级常量与环境读取也必须检查，函数相同不足以证明等价。

以此固定队列完成逐项分类，跟踪原 33 个模块函数的保留/合并/删除去向。对每项给出纯搬迁、必要适配、已确认修补或未经说明行为改变的证据。未说明的行为改变恢复基线；无须为每个搬迁函数建立镜像测试。

## 原始验证证据

目录 `/tmp/kernel-stage2-codex-round12-20261016/`：

- `behavior-red.log`：两个独立行为反例，**2 failed**。
- `test_behavior_preservation.py`：完整复现，真实 zip + 假外部进程/网络。
- `function-migration-audit.json`：186 项顶层函数诊断。
- `do_backup-migration.diff`、`test_socks5_proxy_outbound-migration.diff` 等：新旧函数差异。
- `baseline/skills/`：恢复的旧源码，仅在临时目录作比较。
- `frozen-source-hashes.json`：本轮捕获源码指纹。
- `full-gates.log`：独立双环境全量门禁，结果见补录。

## 接续指令

> 按 stage-2-closeout-control 和 ledger 的六个固定单元推进。先给 S2-C02 完整函数/模块状态差异处置表，再集中恢复未经说明的重写，不继续凭 service 文件出现和旧测试全绿宣称完成。S2-C03 两个已证实退化必须纳入真实行为反例；它们是批量审计中的已确认问题，不代表只修两处即可收口。既有通过项保持，只有相关源码或契约变化才重新打开。交付后停止源码修改，提供完整 patch 与哈希，Codex 对同一冻结交付复核。没有新增业务、没有一工具一服务要求；禁止 commit/push/部署/重启/真实外发，不进入阶段三。

## 本轮修改总结与 Diff

```diff
+++ architecture/stage-2-closeout-control-20261016.md
+ 六个固定验收单元、冻结交付和差异分类规则
+++ architecture/stage-2-closeout-ledger-20261016.json
+ 六单元状态、186项函数诊断、交付源码指纹
+++ architecture/agent-kernel-stage-2-codex-review-round12-20261016.md
+ 结构整改认可、两项独立行为失败及集中恢复指令
```

本轮未修改业务源码，没有提交、推送、部署、重启或真实外发。

## 最终验证补录

独立完整 `verify_e0.py` 退出码 **0**，既有 **8/8 门禁通过**：本机 Python 3.13 Agent **1054 passed，4 subtests passed**；只读无网 Docker Python 3.9 Agent **1054 passed**；两环境 Parser 各 **205 tests passed**；两环境架构 **268 文件、13 处核准 finding、0 未登记违规**；两仓 diff 格式检查通过。Parser 工作区干净。

两项额外行为反例仍 **2 failed**。这是同一份冻结业务源码的不同验证维度，不能合并声称迁移保真通过。

结束时重新核对：298 个已捕获文件哈希未变，只有本轮新增治理账本 JSON 属于新增受控清单项，未发现新增业务源码。审计/治理文件不改变业务运行行为。

同目录保存完整工作区 patch、source-manifest.json 与 patch.sha256，包含新文件和暂存删除，不含私人配置、数据库或邮件，不改 Git index。阶段二保持未通过；接续采用六单元账本，先完成差异处置再最终验收。
