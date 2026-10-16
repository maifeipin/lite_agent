# 阶段二第六轮独立复核与直接修补

日期：2026-10-16（北京时间），Codex。Agent阶段一基线 `5c4016682eeb9d5b14638b0e37e65f8342183bd1`；Parser基线 `918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc`，Parser工作区干净。未提交、推送、部署、重启、真实外发，未进入阶段三；既有暂存删除未改变。

## 判断

本次V1专用cron回调、V2实例slash登记、V3缺依赖时明确错误与默认full清单可加载，已有真实进展。新增加的真实会议outbox用例也比先前手工调用FakeChannel更有意义。第五轮Codex的命令权限/隔离修补仍保留。

但独立执行回调发现4个缺陷，Codex已直接修补并补长期反例。仍有一个实际启动边界阻塞：**core启动API时，会启动未启用网络业务的Socks5自动故障转移worker，且无对应关闭所有权**。因此阶段二整体仍未通过，不能提交为完成基线或进入阶段三。

## 实测与日志

证据目录 `/tmp/kernel-stage2-codex-round6-20261016/`：

| 检查 | 独立实测/证据 |
|---|---|
| 补交新增V1–V3/上一轮/前缀专项 | 17 passed，1.02s，submitted-tests.log |
| 4个新增回调行为反例 | 修补前4 failed，0.68s，probes.log；修补后4 passed，0.76s，probes-after-fix.log |
| 纳管长期回归后相关专项 | 55 passed，1.25s，closeout-tests.log |
| core启动实际分发反例 | 1 failed，0.09s，core-start-boundary.log |
| 最终8项全量门禁 | 见本文末最终补录；full-gates-final.log |

最初full-gates.log运行期间发生源码收尾修改，该运行即使汇总8/8、1019项通过，也不能作为最终代码的证据。之后已冻结生产/测试源码，重新执行 `python3.13 -u scripts/verify_e0.py`，最终结果单独记录，避免混用日志。

所有测试均使用项目隔离守卫、临时数据、假回调/发送器和必要回环端口。第六轮新增4个反例使用真实默认full manifest，不再只测单个插件。worker反例执行真实ApiServer.start，但替换HTTPServer、线程和worker入口为Mock，**没有启动真实网络探测、故障转移或业务线程**。

## 已直接修补的4项

### A. RSS/巡检收件人错误与伪成功

原features/content_ops和sys_ops新增回调优先使用飞书字段，缺少配置时猜测 `admin`；再把该ID用于首个其他通道或广播。PluginLoader还会改写共享context.config，使延迟回调读到后续插件投影。实际Telegram配置admin_chat_id='123456'，发送器收到的是'admin'；send_to返回False仍返回“已推送”。这违反旧目的地语义和真实回执要求。

已新增services/owner_delivery.py通用宿主投递机制：沿原main的飞书→企业微信→微信→钉钉→Telegram顺序，按各通道已配置管理员字段选择目标；缺字段跳过，不猜ID，不广播；只以send_to真实确认成功为准。通过PluginContext.send_owner_response绑定当前宿主的原始配置和活体通道，业务回调不读取会被后续插件改写的配置投影。

RSS与巡检回调已改用这一机制；失败返回真实失败文本。没有改任何真实收件人或凭据，单次只记录测试Fake发送。

### B. RSS预计算重复时槽

原content插件无条件新增固定09:50的rss_precompute，同时config.example时间范围又生成“RSS预计算#09:50”，名字不同导致remove_job_by_name无法去重。独立反例实测两个作业。

已删除阶段二额外添加的固定预计算作业，保留专用handler，由原config.cron_jobs展开决定频率。没有修改用户配置的时间段或提前/通知语义。

### C. 专用job_handler静默覆盖

CronService.register_handler之前直接覆盖同名handler，后注册插件失败回滚时还可能删除原有处理器。已改为同名立即ValueError，保留原所有者，事务只记录成功登记的资源。没有通过全局回退修复。

### D. 回归纳管

上述4个反例已纳入tests/test_phase2_closeout.py。新增用例覆盖真实full插件加载后的Telegram目的地、失败回执、默认/配置同槽重复以及处理器重名保护。既有9项前缀/命令安全回归保留。

## 尚未通过：Socks5业务worker绕过插件启停（P1）

位置：channels/api.py的ApiServer.start末尾仍有：

```python
from skills.ops_socks5 import start_failover_worker
start_failover_worker()
```

这与mode/启用manifest/route_registry无关。完整源码中的core、full显式[]或禁用network_ops，只要API启用就导入并启动该业务。物理缺skills副本里恰好ImportError被捕获，所以现有缺包smoke通过不能证明完整源码core不加载业务。

实测：构造空业务RouteRegistry的ApiServer，执行真实start并Mockworker，start_failover_worker实际调用1次。独立反例在 `test_core_start_boundary.py`，运行命令：

```sh
PYTHONPATH="$PWD" python3.13 -m pytest -q -p tests.conftest -p no:cacheprovider --tb=short /tmp/kernel-stage2-codex-round6-20261016/test_core_start_boundary.py
```

ApiServer.stop只异步关闭HTTP server，没有停止该worker；skills/ops_socks5.py的failover_worker_loop是while True/time.sleep(180)，start_failover_worker用模块全局标志，不返回停止句柄。该worker会主动探测网络、切换代理、发送告警，故不能视为无害导入或“只是后台残留”。

### 实施要求

1. 从公共API移除业务worker导入/启动。由选中的network插件拥有该能力，保留原探测频率、故障累计、候选节点、退避及告警算法，不靠删掉full功能解决core问题。
2. 明确实例资源与外部协议服务所有权，提供可停止/等待句柄；启动失败回滚、关闭后无后续探测/切换/告警。不得仅把现有无限循环换个目录，或只删全局标志导致多实例重复操作同一外部资源。
3. 在完整源码新进程测试core、full显式[]、禁用network_ops：启动真实API后仍无skills.ops_socks5导入、无worker、零外发；启用network则用Fake探测器/切换器验证原行为、停用及关闭。
4. 公共channels基础依赖门禁需真实纳管该非法导入，保留red/green反例。当前ArchitectureVisitor的is_kernel未包含channels，因此“0未登记违规”没有覆盖这条实际基础→业务依赖；不能添加例外放行。

这是原阶段二的“公共宿主无业务反向依赖、禁用业务零加载、stop释放任务”要求，不是新增业务。修补它需要处理真实worker所有权，超出本轮4个局部收尾点，故没有用简单删代码掩盖full退化。

## 本轮代码修改总结与Diff概要

```diff
+++ services/owner_delivery.py
+ 通用管理员目标解析、旧通道优先级及真实发送回执
--- app/plugins.py
+++ app/plugins.py
+ send_owner_response使用当前宿主配置/通道，不依赖可变插件投影
--- features/content_ops/__init__.py
+++ features/content_ops/__init__.py
- 猜admin / 广播 / 忽略回执的发送分支
+ 使用宿主投递端口并返回真实失败
- 固定09:50额外预计算cron（原配置负责展开）
--- features/sys_ops/__init__.py
+++ features/sys_ops/__init__.py
- 同类不安全目的地与假成功分支
+ 使用宿主投递端口与真实回执
--- services/cron.py
+++ services/cron.py
+ register_handler重名拒绝，不覆盖已有所有者
--- tests/test_phase2_closeout.py
+++ tests/test_phase2_closeout.py
+ 4个full装配后的独立回归用例
```

暂存/未暂存diff --check均检查；完整源码patch包含未跟踪新文件，保存于证据目录。不要将私有配置、DB、凭据或邮件加入patch。

## 可直接发给实施方

> V1–V3的装配进展认可；Codex已直接修补RSS/巡检收件人、失败回执、预计算重复时槽及job_handler静默覆盖，保留这些代码和4个长期测试，不撤销或覆盖。请读取本报告，集中处理仍留在ApiServer.start的Socks5自动故障转移业务worker，按上述4条要求迁出公共宿主并完善启停/失败回滚、完整源码core禁用零加载及静态门禁。不要仅让物理缺包或Mockworker探针通过，也不要删除full已有故障转移功能。按原阶段二整体验收检查剩余公共宿主反向代理和业务生命周期，交付实际回调/资源释放证据及最终门禁原始日志。禁止commit/push/部署/重启/真实外发，不进入阶段三。

## 最终门禁补录

最终verify_e0退出码0，**8/8通过**，覆盖本轮4项生产修补和新增长期测试：

| 项目 | 最终实测 |
|---|---|
| 本机架构 | 244文件，25核准finding，0未登记违规 |
| 本机Agent Python3.13 | 1023 passed、4 subtests passed，70.53s |
| 本机Parser | 205 tests passed，2.22s |
| 两仓diff --check | 均通过 |
| Docker Python3.9 Parser | 205 tests passed，7.84s |
| Docker架构 | 通过，1.17s |
| Docker Python3.9 Agent | 1023 passed，93.31s |

不能把这份全绿记录解释为Socks5 worker边界已通过：独立启动反例仍失败，原门禁尚未覆盖此行为。当前结论仍为阶段二未通过。
