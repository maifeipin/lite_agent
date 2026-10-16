# 阶段二第八轮独立复核、直接修补与接续指令

日期：2026-10-16。基线：`codex/kernel-composition`，HEAD `5c4016682eeb9d5b14638b0e37e65f8342183bd1`。本报告检查当前完整未提交工作区，不把 Antigravity 的汇报当作验证证据。

## 结论

第七轮要求的公共 channels 反向依赖清退、12 条阶段二 billing 债务清退、会议主体归位及业务领域数据迁出 core 均有实质落地。邮件命令已注入宿主，但模型工具入口仍漏绑；新领域注册还存在实例隔离、过期工具快照、重复覆盖与失败残留问题。本轮 Codex 已直接修补并新增六个长期回归。

**阶段二仍不能作为完成基线：剩余业务实现迁出未完成，按需插件装配的领域引用也未闭环。** 下列两项是原阶段二要求，不是阶段三新增目标。完整自动化门禁结果在本文末尾补录；门禁通过不替代这些已核实的缺口。

## Codex 本轮直接收尾

1. `features/mail_ops/__init__.py`：`mail_llm_enrich` 模型工具元数据绑定当前宿主，与已经修复的命令入口保持一致。此前工具直接使用原函数，模块 `_agent=None` 时不会发出高优提醒。测试只捕获 `core.alerts.push_alert`，没有真实邮件、模型或 IM 外发。
2. `core/request_selector.py`：注册领域前刷新实际工具集合，解决先创建空引擎/selector、后由插件加入工具时严格校验误报；领域配置深拷贝；同名领域拒绝覆盖；校验失败恢复此前配置及反查缓存。
3. `agent.py`、`app/bootstrap.py`：生产装配显式传入实例级空领域表，避免测试 fixture 或旧全局 DOMAIN_MAP 注入 core；插件再登记实际领域。直接构造旧 Agent 的兼容默认值保持原语义。
4. `app/plugins.py`：selector 成功后再登记 context 资源；重复拒绝；关闭时先释放领域、再释放其引用的工具。失败反例确认 selector/context 均无残留。
5. `features/sys_ops/__init__.py`：生产装配不再依赖全局领域表后，显式登记该插件所提供工具的 system/llm 通用描述。
6. `scripts/review_meeting.py`：薄 CLI 支持仓外 cwd、清空 PYTHONPATH 后以绝对路径直接执行。采用仓库既有的入口根路径写法，未放宽架构规则。
7. `tests/test_phase2_assembly_closeout.py`：纳管上述六个独立反例。邮件测试显式处理 autouse fixture 清空技能注册表而缓存模块仍存在的情形：在隔离的旧命令注册表里重跑真实装饰器，未修改生产代码以适配测试。
8. `tests/test_request_selector.py`：旧源码字串断言因新增 domain_map 参数失效，改为 AST 验证 Agent 构造一次 RequestSelector 且使用 self.skill_engine。

三项装配/工具/CLI原始反例、三项领域隔离/重复/失败原始反例，修补前均失败，修补后均通过。最终相关长期专项为 **68 passed**。

## 剩余事项 A：业务主体仍未迁出（P1，原 2-D）

原执行细则第 4 节 2-D 明确：“业务归 feature，协议归 integration，工具入口保留 schema/权限适配”。第七轮第 4 项也已要求核对全部 ops 的真实迁移状态。现在多数插件仍从旧 skills 取业务函数，不是只保留薄工具入口。

已核实的代表性调用链：

| 能力 | 真实 canonical 实现与调用链 | 盘点声称的目标 | 当前缺口 |
|---|---|---|---|
| 备份/云同步 | features.sys_ops 注册 skills.ops_backup.ops_backup_cloud；该文件仍持有 do_backup、do_backup_and_sync、多个数据库/产品备份与云同步过程 | features/sys_ops/skills.py:backup_data、backup_cloud | 目标文件不存在；业务/协议主体仍在 skills |
| Socks5 | features.network_ops 与 socks5_adapter 调用 skills.ops_socks5 的节点 CRUD、apply_active_proxy_to_vps1、failover_worker_loop、start/stop 等 | features/network_ops/skills.py:* | 目标文件不存在；数据库、切换策略、后台资源及协议仍混在工具目录 |
| 搜索同步 | features.content_ops 注册 skills.ops_meili_sync.sync_meili 与其命令入口；该文件仍含状态持久化、分类、源数据读取与索引同步 | features/content_ops/skills.py:sync_meili | 目标文件不存在；业务及协议实现尚未归位 |

这三项是可复查样本，不代表其余所有工具已通过。盘点当前仍主要记录“目标路径”，不能据此声称全部迁移完成；例如账单、邮件等表项也应逐项对照实际文件与符号。

**整改验收：** 全量核对盘点中的工具、命令、API、cron，增加“实际 canonical 文件/符号、内部调用者、薄入口或外部兼容理由、测试证据、完成状态”。按领域搬迁真实业务实现，协议实现按实际依赖归 integration；普通工具允许保留 schema、权限、参数归一及委托。禁止为清目录复制算法、空建目标文件或只移动 import 字串。所有内部调用走 canonical 服务，迁移时保留 worker 租约、代次、停止/超时语义、通知及回执。原技能装饰器/外部公开工具参数应保持。

## 剩余事项 B：插件领域声明跨越未声明依赖（P2，原 2-E）

真实反例：开启 `LITE_AGENT_STRICT_SELECTOR=1`，以真实 Agent 创建 full 应用，仅调用 `load_plugin_by_entry('features.sys_ops', 'sys_ops')`。注册 security 领域失败并回滚，报告缺少 `fleet_audit`、`edge_health`、`edge_query`、`edge_cmd`、`edge_sweep`。这些由 network_ops 提供；清单却为 sys_ops 声明 `dependencies=[]`。

另有 sys_ops 的 media 描述引用 content_ops 的音乐工具，以及 content_ops 的 web 描述引用 sys_ops 的工具。默认清单先 sys_ops、后 network_ops/content_ops，逐插件严格验证也会遇到注册时序问题。反例在刷新工具快照修补后仍失败，已排除过期缓存这个独立原因。非严格默认模式打印警告后可继续，不能据此声称受控插件依赖已闭环。

**整改验收：** 明确每个领域实际工具所有者。优先把领域数据归真实能力所有者，跨插件组合由通用装配机制根据实际启用集在明确时点完成，或者明确必要依赖并保证无环与注册顺序；不能通过关闭严格模式、过滤掉不存在工具或扩大架构例外掩盖清单错误。保留旧完整模式的关键词、只读/写意图及工具裁剪语义。真实 Agent 验证默认 full、显式空集合、每个可独立启用插件、合法组合；严格和非严格模式都覆盖。缺必要依赖应在登记资源前给出清楚错误并干净退出。

## 接续指令（可原文交给 Antigravity）

保留 Codex 本轮六项行为修补、六项长期反例以及前几轮权限、路由、owner_delivery、worker 生命周期修补。按本报告 A/B 一次完成原阶段二余项；先更新可核验的全量迁移状态，再迁出实际业务和协议实现，同时修复真实插件所有权/领域依赖。不要仅针对最新六个探针变绿再宣布完成。无需逐项请求常规迁移授权。提交完整调用链、全量验收矩阵、含新文件和暂存删除的最终 diff、完整原始双版本门禁日志。未完成项如实标记阻塞，禁止进入阶段三或 commit/push/部署/重启/真实外发。

## 修改总结与核心 Diff

```diff
--- core/request_selector.py
+++ core/request_selector.py
+ 实例配置 deepcopy；刷新当前引擎工具集合
+ 同名拒绝；校验失败回滚 domain_map 与反查缓存
--- agent.py / app/bootstrap.py
+++ agent.py / app/bootstrap.py
+ Agent 接收 domain_map；生产装配显式 domain_map={}
--- app/plugins.py
+++ app/plugins.py
+ 先校验 selector 再记录资源；关闭时先领域后工具
--- features/mail_ops/__init__.py
+++ features/mail_ops/__init__.py
+ 模型工具 mail_llm_enrich 绑定当前 context.agent
--- features/sys_ops/__init__.py
+++ features/sys_ops/__init__.py
+ 显式登记自身 system/llm 工具描述
--- scripts/review_meeting.py
+++ scripts/review_meeting.py
+ 直接 CLI 执行时建立仓库入口路径
+++ tests/test_phase2_assembly_closeout.py
+ 六项行为回归
--- tests/test_request_selector.py
+++ tests/test_request_selector.py
+ 字串断言替换为构造调用 AST 验证
```

证据目录：`/tmp/kernel-stage2-codex-round8-20261016/`。`probes-red.log` / `probes-green.log`、`domains-red.log` / `domains-green.log` 保存独立 red/green；`selected-plugin-strict.log` 保存仍失败的真实插件反例；`targeted-last.log` 为 68 项专项。首次全量失败日志 `full-gates-final.log` 如实保留；其中 CLI 路径写法与新增测试隔离问题已纠正，不能当最终结果。`full-gates-corrected.log` 是中断的验证，不计通过证据。最终冻结源码运行使用 `full-gates-verified.log`。

未提交、推送或部署；未改 Parser 业务算法；没有真实外发。

## 最终冻结源码门禁结果

`python3.13 -u scripts/verify_e0.py` 最终退出码 **0**，**8/8 通过**，原始日志为 `full-gates-verified.log`：

- 本机 Python 3.13 Agent：**1044 passed，4 subtests passed**。
- 无网、只读 Docker Python 3.9 Agent：**1044 passed**。
- 本机及容器 Parser：各 **205 tests passed**。
- 两环境架构检查：**248 文件、13 处核准 finding、0 未登记违规**。
- 两仓 diff 格式检查通过；另核查 Agent 相对 HEAD 的完整 diff，覆盖暂存与未暂存变化。

Parser HEAD `918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc`，工作区干净。1038 项交付测试加上本轮六项长期反例为 1044 项；专项 strict 选择性插件反例仍失败，未把它混报为通过。

完整工作区补丁 `full-working-tree.patch`、变更文件 SHA-256 清单 `source-manifest.json` 及补丁校验 `patch.sha256` 保存于同一证据目录。覆盖相对 HEAD 的暂存/未暂存变更及受控源码目录的新增文件，不含私人配置、数据库、邮件原件或凭据。不改动 Git index。

**最终裁定：本轮直接修补已验证，整体阶段二仍待 A/B 两项收口；不提交为完成基线，不进入阶段三。**
