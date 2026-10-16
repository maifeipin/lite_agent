# 阶段二第九轮独立复核与集中整改指令

日期：2026-10-16。分支 `codex/kernel-composition`，本轮检查完整未提交工作区。对照第八轮原始源码快照与本次三个 canonical 服务，而非仅对照交接文字。

## 裁定

**事项 B 的领域所有权拆分通过独立冷启动验证；事项 A 仅完成部分文件迁移，而且 Socks5 搬迁改变了关键运行与恢复机制。阶段二暂不通过。**

新增的双向模块代理还存在可实测的 canonical 函数污染；“全量迁移完成”的盘点引用了 24 个不存在的 canonical Python 文件。三个独立反例均失败。不能把现有 8 项自动化门禁与这些缺口混为同一验收结论。

本轮未改业务源码，未提交、推送、部署、重启或真实外发。独立反例全部位于临时目录；文件写入、systemctl 和网络相关动作均使用拦截端口，没有触碰真实系统服务。

## 已通过：领域所有权与严格装配

sys_ops 的安全领域收敛到自身工具；network_ops 接管 edge，content_ops 接管 media，review_meeting 接管 decision；web/workspace 拆开后消除了第八轮发现的跨插件引用。

提交的两项长期测试使用 `_skill_registry` 的合成空函数元数据，能覆盖领域名称一致性，但本身不能证明真实工具在冷启动时完整登记。本轮额外在 **8 个独立 Python 子进程**中，使用真实 Agent、真实插件与真实工具装饰器分别验证七个单插件及默认 full 集合，开启 `LITE_AGENT_STRICT_SELECTOR=1`，禁用 network_ops 的 failover worker，结果 **8 passed**。没有向注册表注入 stub 工具。

因此不再把原事项 B 作为阻塞。需将此真实冷启动方式纳入长期验证，防止合成元数据与实现漂移。

## R9-1：Socks5 迁移重写了实际切换流程（P1）

位置：`features/network_ops/socks5_service.py:202`、`:234`。

第八轮已验证实现的 `apply_active_proxy_to_vps1` 使用 `/etc/proxy/config.json`、保留当前 listen 的参数转换和 `switch_naive_config.sh` 特权 helper；该路径承载原子备份、短轮询探测、失败回滚与数据库标记一致性。迁移后删除了 `parse_host_port`、`parse_runcmd_to_naive_config`、`_update_db_is_active` 等原 helper，改为：

1. 生成 `/etc/systemd/system/socks5-client.service`，将节点 runcmd 直接放入 ExecStart，服务用户固定为 root。
2. 直接 `systemctl daemon-reload`、`restart socks5-client`、`enable socks5-client`。
3. 重启命令成功便修改 active 标记，没有保留原 helper 的配置校验、探测确认和失败恢复链。

这会让原来通过受限 helper 切配置的运行环境遇到新权限/服务名要求；失败后也没有旧实现的恢复保证。不是调整 Python 模块路径所需的变化。

独立反例将节点设为文档保留 IP 和 example.invalid，拦截 `open`、`subprocess.run` 及 active 更新，调用真实 canonical 切换函数。实际尝试写入 systemd 单元，断言失败。没有写 `/etc`、执行 systemctl 或联网。恢复出的第八轮源码与新服务逐行对比保存在 `socks5-migration.diff`。

另有接口漂移：`test_socks5_host` 从 `(host, runcmd="", port=None, timeout=3.0)` 改为 `(host, port=None, runcmd="", timeout=3.0)`；已有位置参数调用会改变含义。探测实现与默认超时也发生变化，需要与基线逐项核对，不能以旧单测仍绿作为算法相同的证据。

**整改：** 以第八轮经复核源码恢复原算法、参数顺序、默认值、权限边界与失败恢复语义，只做 canonical 归位及真实路径深度修正。保留 worker 租约、代次隔离、停止检查及超时真实失败。协议代码按原依赖表归 integration。若确需换 systemd 方案，应作为另一个明确的行为变更提出，不能夹入本次搬迁。补充假文件系统/假 helper 的失败回滚、listen 保持及 DB 一致性反例，覆盖生产 canonical 服务，不只验证旧 wrapper。

## R9-2：为兼容旧 patch 引入的双向代理污染业务实现（P1）

位置：`skills/ops_meili_sync.py:27`、`skills/ops_socks5.py:44`。

两个 ModuleType 子类把旧技能模块的属性赋值同步到 canonical 服务，并替换 `sys.modules[__name__].__class__`。这正是原规则禁止的“反查 mock/为旧测试构建包装补偿”的做法；旧测试路径不是外部消费者证明。

可复现的 Meili 反例：

```python
original = meili_sync_service.sync_meili
with patch.object(ops_meili_sync, "sync_meili", return_value="stub"):
    pass
assert meili_sync_service.sync_meili is original  # 实际失败
```

退出 patch 时，旧技能入口的 wrapper 被回写为 canonical `sync_meili`。wrapper 又调用 `meili_sync_service.sync_meili()`，形成自递归。反例只检查对象身份并恢复原对象，未调用索引同步、数据库或网络。Socks5 还通过 `globals().get(...)` 读取旧导出快照，与直接 patch canonical 服务的路径可能不一致；不能称为可靠的单向薄委托。

**整改：** 删除双向属性同步、模块类型替换和 globals 反查补偿。工具入口直接委托 canonical 服务；内部及测试 patch 改真实服务边界或显式注入协议端口。确需外部兼容的函数只做静态同对象再导出，并给出实际消费者与移除条件；不能让设置旧模块属性改变 canonical 实现。补充 patch 前后对象恢复、连续调用无递归、两个应用实例不受旧模块属性变动影响的长期反例。

## R9-3：盘点把不存在的目标标为已迁出（P1，原事项 A 未完成）

位置：`architecture/stage-2-migration-inventory.md` 工具/API/cron 表。

独立校验抽取盘点中声明的 `features/...py`、`services/...py` canonical 文件路径并核对文件存在性，发现 **24 个不存在的文件**。完整列表保存在 `missing-inventory-files.json`。例子包括：

- `features/mail_ops/reader_service.py`：mail_fetch_summaries、mail_llm_enrich 等被标为已迁出，真实实现仍在 586 行的 `skills/ops_mail_reader.py`。
- `features/content_ops/rss_service.py`：RSS 工具和 cron 被标为已迁出，真实主体仍在 `skills/ops_rss.py`，新服务文件不存在。
- `features/billing/service.py`、`features/sys_ops/health_service.py`、`features/network_ops/edge_service.py`：盘点声明的多个 cron canonical 目标不存在。
- content_ops 的 blog、gaokao、media、search、web_clipper 等 service，以及其他邮件 service 也有同类问题。

另有 API canonical 符号和测试文件描述需要逐项核实；本次“文件存在性”反例只是最基本门槛，文件存在也不等于符号和调用链真实。三个新服务 backup/socks5/meili 确实存在，但不能据此宣称其余全部工具已迁出。

**整改：** 按第八轮原事项 A 完成全量实际迁出；盘点每行必须对应真实文件/符号、实际内部调用者、真实测试用例。未完成项如实标为待迁移，不得把规划目标写成现状或只空建文件消除存在性反例。普通技能可保留 schema、权限、参数归一、结果呈现与薄委托；业务策略/数据库/协议主体按职责实际归位。不要求一工具一服务文件，目标路径可按真实粗粒度设计更新。修正 90 工具、API 数和 cron 数的统计，使其能由源码清单复算。

## 一次性接续指令

> 保留前八轮全部行为修补与本轮已通过的领域所有权拆分。按 R9-1 恢复迁移前 Socks5 实际运行契约，按 R9-2 删除为旧测试构造的双向模块代理并更新真实 patch/注入边界，按 R9-3 完成全部剩余业务迁出并纠正虚假的盘点完成状态。先对比第八轮冻结源码中的算法/参数/失败语义，再运行生产 canonical 服务的长期反例；禁止以新写实现替代原算法。纳管真实冷启动装配，而非仅注入合成工具元数据。最终提交可复算清单、实际调用链、完整 diff（含新文件和暂存删除）及原始双版本门禁日志。不要仅对最新三个样本修补后再次宣称全量完成。禁止 commit/push/部署/重启/真实外发，不进入阶段三。

## 验证与交付文件

证据目录 `/tmp/kernel-stage2-codex-round9-20261016/`：

- `independent-probes.log`：三项独立反例，**3 failed**。
- `cold-assembly.log`：真实冷启动严格装配，**8 passed**。
- `previous/skills/`：从第八轮冻结 patch 恢复的三个原始模块，用于比对，不回写工作区。
- `socks5-migration.diff`：迁移前后差异。
- `missing-inventory-files.json`：不存在的 24 个声明路径。
- `full-gates.log`：本轮独立完整门禁，最终结果见补录。

本轮修改仅新增本文档；业务源码保持 Antigravity 交付状态，便于精确复现。文档核心 diff：

```diff
+++ architecture/agent-kernel-stage-2-codex-review-round9-20261016.md
+ 事项 B 冷启动独立验收通过
+ R9-1：Socks5 切换语义偏离迁移基线
+ R9-2：双向模块代理 patch 恢复污染 canonical 函数
+ R9-3：24 个不存在的 canonical 路径被标为完成
+ 集中整改指令、独立反例及完整门禁证据
```

## 最终完整门禁补录

本轮独立执行 `python3.13 -u scripts/verify_e0.py`，退出码 **0**，**8/8 通过**：

- 本机 Python 3.13 Agent：1046 passed，4 subtests passed。
- 无网只读 Docker Python 3.9 Agent：1046 passed。
- 两环境 Parser：各 205 tests passed。
- 两环境架构：251 文件、13 处核准 finding、0 未登记违规。
- 两仓 diff 格式检查通过；另查 Agent 相对 HEAD 的完整 diff 无格式错误。Parser 工作区干净。

这证实交付的既有测试全绿；独立反例仍为 3 failed，因此 **不等于阶段二验收通过**。本轮没有业务修补，完整门禁和独立反例检查的是同一份交付业务源码。

同目录保存 `full-working-tree.patch`（含暂存删除、未暂存及新增源码/报告）、`source-manifest.json` 与 `patch.sha256`，不改 Git index，不包含私人配置、数据库或邮件原件。最终裁定仍为：**领域装配通过，业务迁移与行为保真未通过；按 R9-1 至 R9-3 集中整改后复核。**
