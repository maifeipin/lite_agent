# 阶段二第十三轮：六单元独立验收结果

日期：2026-10-16。HEAD `5c4016682eeb9d5b14638b0e37e65f8342183bd1`，分支 `codex/kernel-composition`。沿用原六单元，无新增业务范围或目录要求。

## 裁定

**上轮备份正文和 resolver 丢失两项已修复；186 项分类填写不等于完成等价审计，阶段二仍阻塞在 S2-C02/C03/C05。**

本轮验证了 56 项“纯搬迁”声明；发现 80 项共用同一句算法等价理由，且其中确实存在旧签名、安全检查和功能删改。另有 15 条 mapped_old 指向旧基线中不存在的文件或符号，无法成立“全部已关联旧符号”的结论。

独立验收状态保存在 `architecture/stage-2-closeout-review-round13-20261016.json`，不覆盖执行方原始分类。

| 单元 | 结果 | 已有证据及剩余事项 |
|---|---|---|
| C01 冻结 | 本轮捕获通过 | 已记录当前源码哈希，结束时再次检查 |
| C02 差异处置 | 阻塞 | 56项函数体匹配；通用说明未消除具体行为差异 |
| C03 行为退化恢复 | 阻塞 | 上轮两项通过，当前三项旧契约反例失败 |
| C04 唯一实现及行为 | 待复核 | 结构归位改善，原能力/参数存在丢失 |
| C05 追溯 | 阻塞 | 15条旧映射无对应文件/符号 |
| C06 最终门禁 | 见补录 | 容器真实zip反例被skip，不能声明双环境该行为均已验证 |

## 已认可的进展

1. 真实备份 zip 已包含源数据，不再只保存暂存符号链接。
2. 候选探测恢复临时 JSON、resolver 映射、默认超时和进程终止等待；失败结果恢复 ip/loc 字段。
3. 健康报告恢复更多旧巡检内容，HedgeDoc 分包及备份状态兼容字段已补回。此项是结构/差异核实，不承诺未测试的每个备份分支已完整等价。
4. 本轮提交的服务迁移长期专项 **5 passed**。56项纯搬迁经独立归一化 AST 对照无函数体/签名差异（忽略文档、类型标注、装饰器），模块级配置仍按原C02要求单独审查。

## C02/C03：现存行为改变被归类为“算法等价”（P1）

### 博客导出路径拒绝检查被删除

旧 `skills/ops_blog.py:blog_export_articles(export_dir='/root/blog_export')` 在建目录/请求前拒绝系统关键目录。新 `features/content_ops/content_service.py:blog_export_articles(output_dir='./halo_backup')` 删除检查，并先建目录再请求。默认目录、导出内容格式和请求流程也改变。

独立反例传入 `/etc/review-export`，将 makedirs 和 halo_request 全部 mock，不触碰 /etc 或外网。旧契约要求拒绝；新函数返回“没有可导出的文章”，实际已进入建目录/请求分支。反例失败。

### Sentinel 工具接口与基线确认流程改变

旧 `sentinel_findings(hours=24, min_severity='medium')` 改成 `sentinel_findings(limit=20)`；旧查询的时间和严重级别过滤不再通过原参数调用。真实技能入口调用旧参数直接 TypeError，独立反例失败。

同一模块还将 `sentinel_scan(mode='quick')` 改成 update_baseline 参数；`sentinel_baseline(action='show', confirm='')` 的 refresh + YES 显式确认接口被替换为无参数读取。不是单纯模块搬迁。需要按旧语义恢复扫描、过滤与明确确认边界，不能以新参数schema同样可运行代替兼容。

### 网页剪藏接口及原功能链被替换

旧 `web_clip(urls, screenshot=False, force_hedgedoc=False)` 支持最多三个URL、截图开关、缓存和原智能投递路径；新服务和工具改成 `web_clip(url, output_format='markdown', auto_upload=True)`，重写请求端点及返回处理。

独立反例通过真实技能入口调用旧 `urls/screenshot/force_hedgedoc` 参数，网络端口设为拒绝，调用在联网前即 TypeError。需要恢复原参数/默认值及批量、缓存、交付策略，不可只加一个别名解决单个TypeError。

三项反例保存于 `semantic-gaps.log`，**3 failed**。这些问题均属于原C02差异保真、C03回归恢复及C04原能力保持，不新增验收范围。`health_service.ops_read_journal` 从 service_name/keyword 变成 unit/priority 也是同类未解释接口变化，已保存逐函数diff供同批处理。

## C05：15条旧映射不存在（P1，审计证据失真）

按实际冻结基线直接核对 mapped_old，发现15条明确无对应文件/顶层符号。例子：

- `skills/ops_hedgedoc.py` 不存在；实际上传逻辑在 ops_web_clipper 中。
- `skills/ops_billing.py:billing_fetch_and_consume`、`billing_check_parse_health` 不存在，不能当作重命名前符号。
- `skills/ops_review_meeting.py:meeting_join/meeting_status/meeting_review` 不存在；实际旧函数带 ops_review_meeting_* 名称。
- `skills/ops_rss.py:rss_fetch/rss_push_job` 不存在，相关入口应定位到实际命令闭包、rss_brief或原装配位置。

完整15条列表在独立验收 JSON 与 `invalid-old-mappings.json`。这次检查的是旧文件顶层函数，并非以新文件纯AST定义搜索误判合法导入别名。

## 固定清单下的接续动作

1. **C02/C05先做真实处置表**：修正15条旧映射；为80项通用说明补对应旧→新diff、参数/默认值/异常/副作用差异及具体结论。已经核实的纯调用改名不必重写，只需准确关联证据。不得批量复制“算法等价”作为逐项审查。
2. **C03/C04按旧功能恢复整组**：博客、剪藏、Sentinel、日志等按冻结源码恢复完整契约；同模块其他差异一并处理。不是只修三个最新探针。未经说明的行为改变应恢复，必要缺陷修补单列依据与反例。
3. **C06补齐真实zip验证环境**：当前容器因缺zip跳过唯一真实archive验证，必须记录为该行为未验证。为测试镜像提供所需工具并实际执行，或提供能在Python3.9中真实验证同一打包路径的等价方案；不能用永久skip代表该关键行为通过。
4. 继续保持交付冻结。下一次交付仍更新原六单元，不新增一套编号、服务层或业务目标。通过项仅在受影响时重新打开。未完成项如实标阻塞，不宣称全部闭环。

## 证据与本轮改动

目录 `/tmp/kernel-stage2-codex-round13-20261016/`：

- `frozen-source-hashes.json`：本轮源码指纹。
- `pure-claim-mismatches.json`：空列表，56项纯搬迁函数体声明已核实。
- `invalid-old-mappings.json`：15条错误旧映射。
- `diffs/`：88份同名函数归一化差异，不等于88个缺陷。
- `submitted-probes.log`：提交专项5 passed。
- `semantic-gaps.log`：三项当前行为反例失败。
- `full-gates.log`：本轮独立完整门禁。

本轮仅新增复核报告和独立验收JSON，没有改业务源码；未提交、推送、部署、重启或真实外发。

```diff
+++ architecture/stage-2-closeout-review-round13-20261016.json
+ 原六单元独立结果、56项已核实、15条错误追溯
+++ architecture/agent-kernel-stage-2-codex-review-round13-20261016.md
+ 上轮修补认可、现存行为差异及固定单元接续动作
```

## 最终独立门禁补录

`verify_e0.py` 退出码 **0**、脚本汇总 **8/8通过**：

- 本机 Python 3.13 Agent：**1056 passed，4 subtests passed**。
- 无网只读 Docker Python 3.9 Agent：**1055 passed，1 skipped**；skip 为缺zip时的真实archive反例。
- 两环境 Parser：各 **205 tests passed**，Parser工作区干净。
- 两环境架构：**268文件、13处核准finding、0未登记违规**；两仓diff检查通过。

既有门禁通过不代表上述三项旧契约反例通过；本轮 `semantic-gaps.log` 仍为 **3 failed**。容器中被跳过的真实archive路径也保持未验证状态，不能写成双环境行为完全一致。

结束时已捕获源码哈希未变；本轮仅新增审查文件。完整工作区 patch、source-manifest.json 与 patch.sha256 保存于证据目录，包含新文件及暂存删除，不改Git index、不包含私人配置/业务数据。

**最终裁定：按原六单元继续；C02/C03/C05未通过，C06存在已知skip验证缺口，阶段二不关闭、不进入阶段三。**
