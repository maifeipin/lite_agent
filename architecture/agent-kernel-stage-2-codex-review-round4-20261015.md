# 阶段二第四轮独立复核：未通过

日期：2026-10-15，Codex。阶段一基线仍为 `5c4016682eeb9d5b14638b0e37e65f8342183bd1`，阶段二工作区未提交，包含既有暂存删除。本轮只新增本报告，未改生产源码、已有测试、规则或暂存区；未commit/push/部署/重启/真实外发。

## 本轮独立结果

- 交付方指定的5文件专项组合已独立复现：**43 passed，18.95s**；在允许回环端口的环境运行，使用项目临时数据与假模型隔离机制。
- 架构实测：241文件，**25核准finding，0未登记违规**。不能省略25核准项而将其描述为零依赖。
- 暂存/未暂存diff --check均通过。
- 新增3个独立反例：**3 failed，0.77s**。
- 完整1007项、Parser205项、双版本8项本轮未重跑，不作为Codex独立通过证据。

证据目录 `/tmp/kernel-stage2-codex-round4-20261015/`：`submitted-suite.log`、`test_review_round4.py`、`probes.log`。

反例命令（Agent根目录）：

```sh
PYTHONPATH="$PWD" python3.13 -m pytest -q -p no:cacheprovider --tb=short /tmp/kernel-stage2-codex-round4-20261015/test_review_round4.py
```

pytest真实退出码1。前两项使用真实bootstrap/main装配与受控Mock Agent/通知器；不连接真实模型、不启动通知或cron工作线程、不访问真实数据。第三项新进程直接导入实际channels.api，检查sys.modules。初次反例曾因patch全局time.sleep使cron线程产生测试警告；最终版屏蔽cron.start后重跑，3项相同失败且无该警告，不把测试脚手架警告计为产品问题。

## U1 [P1] 同一次生产启动创建两个Agent，通知仍绑定错误实例

app/bootstrap.py:80-114新建Agent并交给PluginContext；main.py:199-220调用create_app/load_all后再次Agent(...)；main.py:294只给第二个Agent注入channels。通知在插件register时捕获第一个Agent，后续不会自动指向第二个。

实测真实main路径Agent构造器调用2次。第一个实例channels保持空集合，因此配置正确的会议通知仍无法找到生产通道。这不是“非空即可”：上一轮not None探针变绿，但功能并未接通。额外Agent还会重复初始化会话/执行账本/模型等资源。

必须修改：唯一Agent由唯一装配入口拥有；通知仅在实际发送端口/通道就绪后启动，register与start分期；禁止第二个Agent、临时假Agent、namespace或未来补属性代替正确依赖顺序。app.agent、插件持有的受限发送端口、HTTP/IM服务的Agent必须属于同一运行时。

验收：实际full启动记录构造次数1；在main真实启动/通道挂载后触发一条隔离会议outbox，FakeChannel收到正确内容及次数；shutdown后无线程/发送残留；启动中途失败释放已获取资源。不能只测start_notifier第一个参数非空。

## U2 [P1] bootstrap吞掉Agent初始化失败，返回假成功应用

app/bootstrap.py:81-103遇到任意Agent构造异常直接pass，并创建SimpleNamespace作为agent。缺配置也会静默采用该替代对象。真实资源初始化可能已经部分完成，但外层没有收到失败与资源所有权。

独立反例注入Agent构造RuntimeError，create_app未抛错，证实异常被吞掉。该机制不能视为活体Agent装配，不应为了让无配置的插件单测通过而进入生产代码。

必须修改：分离“仅创建注册服务的测试/装配阶段”与“启动实际运行时”；前者通过明确API或注入测试替身，后者必须拥有完整Agent并真实失败、回滚和非零退出。生产路径禁止except Exception: pass + namespace降级；确保create_app自身初始化失败也被资源回滚覆盖（当前main的try在create_app之后）。

验收：模型/会话/DB/通道等初始化失败注入，逐项断言失败退出、无就绪宣告、无伪Agent、无先前资源残留。

## U3 [P1] 公共宿主顶层导入会议业务，core禁用插件仍加载它

channels/api.py:17起新增try导入features.review_meeting.http_adapter中的会议全局状态，并在ImportError时回退。捕获缺包只证明物理副本不崩溃；在完整源码中，core也会实际加载禁用业务。

独立新进程 `import channels.api` 后，sys.modules中实际存在 features、features.review_meeting、features.review_meeting.api、features.review_meeting.http_adapter，禁用插件零导入要求失败。公共宿主仍有多处按业务名的代理方法/回退分支，仍是宿主反向依赖业务，非原方案的受控单向插件登记。

必须修改：移除公共宿主对feature的直接/容错/延迟导入及业务全局状态；插件登记闭包、adapter和状态由feature拥有，宿主只执行通用注册契约。旧测试改测canonical adapter或注入服务，不为保留旧patch位置在生产宿主保留业务兼容层。确有外部契约的旧符号应单列证据和处理设计，不靠吞ImportError放行。

验收：完整源码core、full显式[]、仅选其他插件，均从新进程验证会议feature不进入sys.modules；启用会议时由受控loader导入且原HTTP/鉴权契约通过。把公共channels纳入实际依赖门禁，保留非法反例，不能以未扫描/核准例外掩盖逆向依赖。

## 对已修内容的判断

T1旧入口已增加空registry保护；Todo工具绑定实例DB的修复及双实例路径测试通过；上轮Codex路由前缀修补仍保留。这些进展认可，但T2仍是错误对象装配，T4新增顶层业务依赖，故不能认定T1–T4整体收尾。

本轮额外“全仓硬编码清退”须在最终报告单列配置默认值/路径选择变化及反例，不与纯搬迁混报；本次未因该额外事项引入新范围要求。

## 给实施方的接续指令

> 请读取本报告，优先按真实启动依赖链解决U1–U3，不再围绕not None或目录存在增加生产兜底来绿化最小探针。将本轮独立反例纳入长期测试，先red再green；测试替身通过注入显式提供，禁止吞初始化异常或制造假Agent。唯一runtime装配完成、通道就绪后启动通知；实际full/FakeChannel/outbox验证一次发送和关闭，公共宿主禁用业务零导入，补静态规则反例。保留Codex路由前缀及既有合法修补。交付含暂存删除/全部新文件的完整diff、原始最终日志、真实资源所有权和时序图；按原阶段二目标逐项验收，不只将本报告3个探针变绿。禁止commit/push/部署/重启/真实外发，不进入阶段三。

结论：阶段二仍未通过，不能提交为阶段二完成基线。这里是原有“唯一装配、通知保真、禁用业务零导入”的验收，未追加新业务。后续小问题仍由Codex直接修补；本次运行时及依赖方向问题需先把主体装配修正。

## 本轮修改总结 / Diff

```diff
+++ architecture/agent-kernel-stage-2-codex-review-round4-20261015.md
+ 43项专项通过与3项独立反例失败的实测记录
+ U1双Agent、U2异常吞并、U3公共宿主业务导入的源码证据
+ 真实发送/失败回滚/禁用零导入验收及接续指令
```

仅新增长期复核报告；反例在系统临时目录，未将草稿或测试DB加入仓库。
