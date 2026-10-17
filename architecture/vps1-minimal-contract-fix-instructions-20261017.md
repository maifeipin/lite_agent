# VPS1原功能最小契约修复方案（用户已批准）

日期：2026-10-17。当前候选a1be86a已确认存在生产定时契约缺口。此文件仅把建议写成可执行、可复核范围；未批准前不得修改业务代码或生产。用户此前排除新补丁，本轮必须明确批准本最小修复才执行。四项前端补丁始终排除。

## 唯一允许的五项实现范围

1. features/mail_ops/__init__.py：登记ops_mail_reader::mail_fetch_cron专用job handler，调用现有reader_service.mail_fetch_cron并绑定当前应用agent，原months等参数保持不变；不得改为模型工具，不得改用mail_fetch_summaries替代含通知逻辑，不新增模块级agent。
2. main.py _register_configured_cron_jobs：有name及合法command/skill的任务，即使含_comment说明也须注册；仅注释占位且无name才跳过。保留现有合法性校验，不以过滤修复为由吞掉无时间/无目标错误。
3. features/network_ops/__init__.py：在现有edge_service.edge_health外围恢复旧生产🔴结果的push_alert包装、旧标题/颜色/去重key语义；检查函数返回值与无异常回归。内置cron与旧配置的执行路径必须调用同一规范包装，专用handler绑定原ops_edge_health::edge_health目标；不要向edge_service查询算法里塞新的投递或修改节点阈值。旧edge_health_sweep是否移除须等对照表核验后作配置裁决，先确保一个有效调度、保留告警语义，不靠重命名掩盖重复。
4. features/sys_ops/__init__.py：ops_sentinel::sentinel_scan专用handler保留原kwargs，调用现有sentinel_scan；仅按旧main的风险结果匹配⚠️/[HIGH]/High:发送原安全告警卡片，保留原标题/颜色与无风险不发规则。不得改扫描算法、更新baseline或新建告警判断体系。
5. main.py：通用skill作业恢复旧push_on_output语义：明确true才处理；沿用原非空、不以✅或(开头的输出条件与旧颜色/标题规则，并保持返回值和异常行为。专用已自带投递的mail_fetch_cron、rss_push、daily_health、sentinel及edge路径不得再经通用分支重复推送。以旧fd17d67:main.py为契约来源，不按报告的“仅异常”简化原实际行为。字符串转换或None处理依旧契约核对，不对异常容错扩展范围。

允许改动上述四个实现文件（network_ops、sys_ops、mail_ops各一个及main.py），新增一份集中长期反例测试和一份修补说明；与候选来源相关文档可定点更新。不得修改其他运行时源码、数据、schema、默认模型、频道、路由、外部服务或前端四文件。确需超范围先说明具体原因，不自己扩展。

## 离线反例：必须先红后绿

- 注释占位跳过、含_comment的合法Sentinel注册、非法任务仍失败。
- mail_fetch_cron目标真实解析到handler，原参数与当前实例agent正确传递；不触发真实邮箱或LLM；两实例不串宿主。
- Edge正常不通知、失联通知一次且去重key/标题/颜色正确；内置和原配置目标都经过同一包装。
- Sentinel正常不通知、三种旧风险标志匹配、mode透传、不重复投递。
- push_on_output缺省/false/true；抑制输出、实际输出、颜色判断、返回值；专用路径不重复投递。

使用真实注册器和回调边界，外部业务函数与发送端口用隔离stub/mock，不连生产。不得通过删除旧断言、增加skip、放宽架构例外或改动业务fixture使测试通过。

专项通过后运行现有完整verify_e0.py一次，双环境、Parser、架构及前端既有检查仍须通过。原1114通过不是新源码的证据；遇失败留完整日志并停止归因，不无限重跑。该回归不代表重新扩大VPS2抽样范围。

## 不需要代码修复的事实与只读核对

- 根据转交的旧现场日志，Socks5内部worker原已启动，每180秒，候选保持其行为和唯一执行者。不凭naive.service推论，也不改为60秒；不实际切换节点试验。
- task_specs_tick在旧/候选channels/api.py均已有注册代码，应列入实际任务表，不作为新增实现工作。
- Todo两项、账单两项、旧备份+网盘作业原已存在，保持；不得把do_backup_and_sync描述为仅本地备份。
- RSS与Meili实际归属features.content_ops，修正报告中的features/media_ops和features/search_ops虚构路径；sync_meili已有模型工具，可由通用cron解析，不虚报已有专用handler。
- 表A的生产数据库实际路径按补充只读记录用于备份计划；不下载、不移动、不建新库。

## 提交与发布交接

交付修前失败、修后通过、完整门禁日志索引、精确diff、最新任务映射和生产配置最小差异建议，交Codex独立复核。Antigravity不自行git commit/push/部署；Codex复核后按用户授权创建新的本地候选提交及干净包，原a1be86a不移动、不伪称未变。

新候选替换发布文档中旧SHA和包哈希后，继续原“七插件+六频道、保持业务”流程。禁止把旧归档包当作新候选交付；VPS1实际发布仍需明确授权。若用户不批准本最小修复，暂停本次发布、继续旧生产fd17d67，不把“暂停”写成永久取消。

## 实施状态更新

用户已明确回复“批准”。Codex已直接在本地完成五项修复及21个反例，Antigravity不再重复实现。实现与验证结果见 `vps1-minimal-contract-fix-review-20261017.md`；待复核时只读审查实际diff与证据，发现问题先报告，不扩展修改。该批准不包含VPS1生产发布。
