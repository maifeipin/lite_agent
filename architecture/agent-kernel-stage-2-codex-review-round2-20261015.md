# 阶段二补交第二轮独立复核：未通过

日期：2026-10-15；复核方：Codex。基线仍为阶段一提交 `5c4016682eeb9d5b14638b0e37e65f8342183bd1`，阶段二在 `codex/kernel-composition` 工作区，包含已暂存删除和未跟踪新增文件。

## 结论与实测范围

本轮补交有实质改进：插件不再空注册，main 从空实例工具集开始，上一轮7个最小反例已通过，会议三个模块从 core 迁出。但“R1–R12全部闭环”和“阶段二90%完成”仍不成立：新增 GET/POST 插件分发绕过鉴权，cron 注册后执行立即报错，旧 Todo URL 分发错误；业务公共宿主迁出、生命周期与真实入口证明仍未完成。

不能提交为阶段二完成基线，不能进入阶段三。本轮没有改阶段二生产源码、已有测试或架构规则，只新增本报告，反例/日志保存在系统临时目录；未 commit/push/部署/重启/真实外发。缺口仍涉及主体边界实施，不宜局部打补丁后声称收尾完成。

| 本轮独立验证 | 实测结果 |
|---|---|
| 原7个复核反例 + 注册生命周期 | 14 passed，0.76s |
| 补交声明的7文件专项组合 | 沙箱内50 passed、3端口权限失败；允许本机回环端口后同命令53 passed，3.78s |
| 架构扫描 | 232 Python 文件，25核准 finding，0未登记违规 |
| Agent unstaged/staged diff --check | 两者通过 |
| 本轮新增12个独立用例 | 10 failed、2 passed，0.89s；6个失败对应鉴权，另4个对应cron执行/全局泄漏/URL/清空 |
| 完整982项/Parser205项/双版本8项 | 本轮未重跑；保留为交付方声明，不能当Codex已独立复现 |

证据目录：`/tmp/kernel-stage2-codex-round2-20261015/`，含 `test_review_round2.py`、`probes.log`、`original-tests.log`、`submitted-suite.log`、`submitted-suite-unrestricted.log`。

独立反例命令（Agent根目录）：

```sh
PYTHONPATH="$PWD" python3.13 -m pytest -q -p no:cacheprovider --tb=short /tmp/kernel-stage2-codex-round2-20261015/test_review_round2.py
```

真实pytest退出码1，外层读取日志命令的0不是pytest结果。反例不使用真实数据和外网；HTTP反例执行真实ApiHandler分发方法，以Mock业务回调/鉴权结果避免业务写入，证明调用顺序与授权缺失，不将其冒充网络端到端。PATCH/DELETE在明确is_edge=False的有效测试设置下通过，不能泛称四种方法全部绕过。

## S1 [P1，阻塞] GET/POST插件路由绕过鉴权与权限隔离

位置：`channels/api.py:114-120`、`:211-217`；`services/routes.py` 的 auth_required 元数据没有在该分支执行。

GET/POST在 `_auth()` 以及guest/edge权限判断之前调用 matched handler并return。所有插件业务路由默认auth_required=True，但该标志被忽略。Todo、邮件HTML、Socks5、Edge提交等注册adapter直接调用旧HTTP业务方法；这些旧方法依赖公共入口完成鉴权，不能假定各自再次鉴权。

实测：GET/POST拒绝鉴权的请求仍调用业务处理器；Guest/Edge身份的GET/POST也直接调用业务处理器。6个独立反例失败。原full专项只使用正确管理员token请求200，不能证明未授权被拒绝。

整改：统一路由分发的认证/授权机制，明确公开登录、会议Cookie/capability和普通Bearer路径各自策略；保持原guest/edge白名单、owner/私聊保护，在读取/写入/投递前执行，不能把会议原有认证粗暴替换为普通token。为匿名/非法token/guest/edge/admin、GET/POST及/agent别名补实际HTTP负面用例；断言处理器和变更/发送均未发生。禁止把auth_required默认改false或给所有路由放行以修测试。

## S2 [P1，阻塞] skill cron只注册成功，首次执行就AttributeError

位置：`main.py:135`，调用 `app.skill_engine.get_tool(fn_name)`，但SkillEngine没有get_tool方法。

独立用例用实际SkillEngine登记合法实例工具，调用生成的job.func，实测 `AttributeError: 'SkillEngine' object has no attribute 'get_tool'`。旧反例只断言任务名存在，因此虽绿仍未验证能执行。其后全局_skill_registry兜底还会重新执行未启用插件能力。

整改：通用平台提供明确实例调用端口，校验目标存在、权限与执行语义，禁止全局兜底；业务cron策略由所属feature保留。不能只补get_tool后宣称保真：旧rss_push/daily_health等并非简单同名工具调用，原专用回调、告警条件、push_on_output、kwargs、time_range、发送回执与日志行为仍需逐项核对。不得把所有作业变成直接func调用来绕过既有保护。

## S3 [P1] 旧Todo简报URL误入创建接口

位置：`features/todo/__init__.py:32-36`。新adapter判断后缀 `/push-brief`，原URL是 `/api/v1/todos/brief/push`（channels/api.py原POST分支仍可看到）。前置插件匹配后返回，不再到原分支。

独立反例以真实插件register取得adapter、屏蔽技能模块副作用，实测旧URL未调用brief handler，而走 `_handle_post_todo()`。这可能报缺title或按错误业务处理body。不能把改前端URL当保留契约。

整改：精确登记既有路径及方法，核对/agent前缀规范化（registry匹配用去前缀路径，但adapter收到的parsed_url.path仍含前缀）；Todo及Socks5子路由使用明确分发，不用宽prefix吞并未知路径或错误方法。

## S4 [P2] “字典不是同一对象”不等于内容不串实例

位置：`core/skill_engine.py` 的 `_registry` property。auto_load路径每次读取仍从全局_skill_registry吸入新项，即便实例创建时已完成快照。

独立用例在引擎创建后向全局登记一个新工具，再读取实例，工具自动出现；原is not断言只验证对象identity。生产main显式tools={}是进展，但“所有实例完全隔离”声明仍不成立，clear后的legacy实例也能再吸入全局内容。

整改：生产和新宿主路径严格实例隔离；若保留legacy模式须限定消费者、显式记录兼容语义与退出条件，不能为了测试动态@skill增加生产字典自动补全。测试使用明确注入/登记接口。

## S5 [P2] CronService.clear被第二个定义覆盖，任务名没有清空

位置：`services/cron.py:116`和`:178`重复定义clear；后者仅清jobs，覆盖前者清jobs及_job_names的实现。

独立用例 add_job → clear 后，_job_names仍为 `{'review_job': 1}`，无法按同名重新注册。

整改：只保留一个完整清理实现，覆盖注册表及名字索引，验证clear可重复及重启/重注册。不要通过全局重置/新对象掩盖资源释放。

## S6 [P1] 业务实现仍在公共宿主，登记adapter回调旧私有方法未完成迁出

Todo/network/content/mail等插件主要把路由再转给 http_handler._handle_*；channels/api.py仍包含TodoService构造、CRUD、会议状态/数据库、安全、邮件、Socks5等大块业务逻辑，并新增business_prefixes业务名单。

这是“插件选路 + 公共宿主继续做业务”，不满足阶段二公共宿主无业务实现，也违反原规则不能以开关挡旧代码代替迁移。会议只迁出三个模块，scripts/review_meeting.py仍是业务主体。其余ops业务实现也仍多留在skills；features取全局装饰器字典、若工具缺失就静默跳过，不是完整显式能力校验。

整改：把真实业务HTTP adapter及状态/仓储调用移到features；公共宿主仅做通用匹配、认证授权、错误/流式/传输。业务路径由插件清单注册描述，不在宿主写业务前缀名单。技能只保留schema/参数/权限适配，业务服务和协议客户端归位。保留旧URL、方法、body、错误契约与数据路径，禁止复制第二套实现。

## S7 [P1] 实例业务配置与会议通知生命周期尚未贯通

PluginLoader仍复用并改写同一个context；_project_config仅包含common_keys与同名配置块。review_meeting插件需要的review_meeting_notifications、channels等被投影排除，所以已配置通知不会启动。即使修投影，register调用 `start_notifier(None,cfg)`，Agent和channels尚未创建；该函数while True线程无stop事件且不返回stop_fn，context也没有record_cleanup方法。当前实现不能证明通知保留与有序关闭。

Todo工具/cron函数来自ops_todo，get_service默认取模块DB_PATH而非app配置；公共HTTP又用app.todo.db_path，两者可能操作不同库。mail_reader._agent仍None，无注入替代，高优邮件发送行为不能宣称保留。

main初始化依然不是bootstrap统一托管的try/finally启动事务；插件/Agent/通道后续失败时未统一关闭此前资源。CronService.stop的timeout仅限制调度线程join，executor.shutdown(wait=True)无该超时，不能称已解决运行中任务有界退出。

整改：每插件稳定配置/服务上下文，先装配基础服务与可用端口，再按明确start阶段连接真实Agent能力和通道；配置路径/时区/凭据来源保持，任务与通知有真实停止句柄，失败统一回滚。以两个应用不同临时DB及发送器、启停/启动半失败验证，不只测注册表不相同。

## S8 [P1] 真实main普通/流式证明仍缺失，报告有超出测试的声明

物理缺包测试已改善：真实启动ApiServer、验证/v1/models 401/200和Todo404，并检查业务sys.modules缺席。但子进程仍手工create_app/Agent/ApiServer，没有import/run main，没有普通模型回合或流式请求；其docstring声称“流式正常”而测试没有发流式。

逐插件导入用例仍只hasattr/register和线程数量，没有audit拦写/DB/socket。表述“0文件、0数据库、0网络”超出证据。完整技能盘点、全量配置保真与所有公开契约亦缺逐项执行证据。

整改：隔离副本实际main入口，离线假模型，真实HTTP普通/流式/鉴权/唯一终态/空工具；记录模块来源，禁止原仓库泄漏。插件import audit零副作用、register/start阶段合法资源及失败释放分别测。阶段二必须做到smoke，阶段三再固化干净依赖构建/CI；不能继续把阶段二硬要求后置。

## 重新交付要求与可直接下发指令

> 请读取本第二轮报告及原阶段二三细则。当前补交的53项专项Codex已独立复现通过，但新增12个反例10失败2通过，不能认定R1–R12闭环。先将 /tmp/kernel-stage2-codex-round2-20261015/test_review_round2.py 的有效反例纳入长期测试并保留red/green证据，优先解决S1鉴权绕过、S2实际cron执行和S3旧URL，再完成S4–S8主体边界与生命周期。按“实际请求/回调执行后的结果”验收，不再只断言登记存在、字典identity或目录存在。请自主补齐，不逐个询问常规修补；重大协议/配置兼容选择提出具体方案。交付前必须实际执行完整full装配、配置cron回调、全部业务路由权限矩阵、不同配置双实例、真实main物理缺包普通/流式。核对最终diff含暂存删除和全部新文件，保留原始日志，修订handoff为真实状态。禁止削弱断言、增加全局兜底、扩大架构例外或继续添加宿主业务分支；禁止commit/push/部署/重启/真实外发，不进入阶段三。完成后再交Codex集中复核。

本轮修改总结：仅新增此复核文档，生产与既有测试未修改。Diff概要：

```diff
+++ architecture/agent-kernel-stage-2-codex-review-round2-20261015.md
+ 承认已修内容与53项专项独立通过证据
+ 新增12项独立用例的10失败2通过记录
+ S1–S8源码定位、影响、整改及验收要求
+ 禁止提交为完成基线、禁止进入阶段三的交付结论
```
