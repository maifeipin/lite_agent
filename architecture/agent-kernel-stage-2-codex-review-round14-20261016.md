# 阶段二第十四轮：旧契约恢复与六单元验收

日期：2026-10-16。分支 `codex/kernel-composition`，HEAD `5c4016682eeb9d5b14638b0e37e65f8342183bd1`。沿用原六单元，未新增业务目标。

## 裁定

**旧目录拒绝、Sentinel扫描/过滤/YES确认、Journalctl过滤、15条旧映射和容器zip工具均有实质修正。博客两项和Sentinel默认截断由Codex本轮直接修补。当前仍有剪藏完整行为链未恢复，阶段二暂不关闭。**

不是以新的参数名或新的目录要求阻塞：网页剪藏协议、缓存、大小写响应与智能投递，均已在前两轮C02/C03/C04要求中，且冻结旧源码中存在。

## 六单元状态

| 单元 | 本轮独立结果 |
|---|---|
| C01 冻结 | 捕获交付哈希；Codex小修另记录前后哈希，最终门禁针对小修后源码 |
| C02 差异处置 | 通用模板已具体化；博客说明与实际入口不一致已直接恢复；剪藏仍未等价 |
| C03 行为恢复 | 上轮三项反例通过；本轮博客两项修补后通过，剪藏协议反例仍失败 |
| C04 唯一实现与关键行为 | 结构改善；剪藏原能力未完整保留，待完成 |
| C05 追溯 | 先前15条失效mapped_old均已核实存在，认可此项修正 |
| C06 最终验证 | Dockerfile已补zip；独立完整结果见补录 |

执行方在原账本自行填写“全部已通过”不是Codex独立验收结论。本报告及独立复核JSON为本轮验收状态。

## 已通过与认可的整改

- 原三个语义反例重新运行 **3 passed**：系统目录提前拒绝、Sentinel旧过滤参数、剪藏旧参数可以传入。该剪藏用例只验证参数兼容，不证明完整协议/缓存/投递恢复。
- Ledger中15条失效旧映射已逐条核对旧冻结基线，**0条缺失**。逐函数理由已不再使用原80项相同模板，但理由仍需与实际源码核对。
- Sentinel类恢复原8类扫描，baseline刷新需要YES；Journalctl恢复service_name/keyword与已有unit/priority入口。
- Dockerfile.test安装zip，交付声称重建镜像；本轮独立容器运行核验见末尾，不仅查看Dockerfile文本。

## Codex直接修补：博客旧调用契约

账本说明 `blog_list_articles` “技能保留limit默认参数”，但实际技能和服务仍只有page/size，旧 `blog_list_articles(limit=3)` TypeError。

发布函数将旧第三位置参数status移到了第六位。旧 `blog_publish_article('review','body','DRAFT')` 实际将DRAFT作为summary，并向假Halo端口构造 `status='PUBLISHED'` 的请求。这是草稿/发布语义退化，不能仅称“发布入参丰富化”。没有真实发布文章或调用外网。

本轮以旧冻结源码恢复这两个服务函数原逻辑，只将 `_halo_request` 引用改为 canonical `halo_request`；技能保留原schema与参数顺序，函数体仅委托服务。恢复limit默认5、第三参数status及原请求字段/摘要语义，没有新增另一套兼容实现。

新增长期 `tests/test_phase2_blog_closeout.py` 两项，独立原始失败 **3 failed** 中的两个博客用例修补后通过；剪藏用例仍失败，见下节。

## Codex直接修补：Sentinel默认不截断旧查询

服务 `sentinel_findings` 默认limit=None，但技能仍默认limit=20，导致原来时间/严重度范围内返回全部记录的调用被隐式截断。本轮将技能可选limit默认恢复为None，保留显式limit用途；添加21条匹配临时告警的真实文件测试，确认默认全部返回。

最终博客、服务迁移、注册/装配及既有整改专项 **25 passed**。新增三项长期用例，完整门禁数量相应增加。

## 唯一已确认的剩余实现阻塞：剪藏链仍为重新实现（P1，C02/C03/C04）

位置：`features/content_ops/content_service.py:web_clip`，以及当前技能入口。

| 原冻结实现 | 当前实现 | 未恢复影响 |
|---|---|---|
| 单条请求 `/api/Url2Md`，批量 `/api/Url2Md/batch` | 逐URL请求 `/api/v1/clip` | 对端协议改变，未有对端迁移依据 |
| 接受success/Success、markdown/Markdown等旧响应字段 | 主要检查success、content/markdown | 旧大写成功响应被当失败，正文可能丢失 |
| SQLite缓存；截图请求绕过缓存 | 未恢复旧缓存链 | 重复抓取、缓存有效期与截图行为改变 |
| 短文本无图直接返回；长文/含图/显式force才上传，复用缓存HedgeDoc链接 | auto_upload默认True，启用HedgeDoc便尝试上传 | 通知/外发、副作用与重复上传语义改变 |
| 多URL调用批量端口并逐条复用智能交付 | 顺序单条抓取与截断拼接 | 批处理与交付语义不同 |

独立反例将旧API大写响应作为假HTTP返回，调用真实canonical函数。实际请求仍是 `/api/v1/clip`，反例失败。所有HTTP和上传端口均拦截，没有联网、上传或发送。

**处置固定为完整恢复这条旧能力链**：从冻结的 `skills/ops_web_clipper.py` 原实现搬迁配置/缓存/URL归一化、单/批请求、响应解析和智能交付所需函数到合适canonical位置，按真实模块深度修正路径；技能只做schema与委托。无需为通过本报告创建指定文件名或额外服务层。不要只改一个端点、增加一个关键字，或继续用“支持旧参数”代替缓存和投递行为保真。

依现有控制规则对该链整体diff分类；无必要的output_format/auto_upload等新功能不得改变旧默认行为。若确有对端协议升级证据，应单独列出而不是默认升级本次重构范围。

## 接续与证据

保留本轮博客和Sentinel默认契约恢复及三项长期用例。按原C02/C03/C04集中恢复剪藏整条旧能力链，其余已认可项保持；在相同账本中更新真实状态，不另开业务范围。完成后对原调用、缓存命中/截图绕过、大小写响应、批量、短文零上传、长文/含图/force上传及重复链接复用给出假协议验证；这些是旧路径的关键分支，不要求每个内部函数新增镜像测试。

证据目录 `/tmp/kernel-stage2-codex-round14-20261016/`：

- `frozen-source-hashes.json`：收到交付时哈希。
- `codex-changed-source-hashes.json`：本轮小修前后哈希。
- `post-fix-source-hashes.json`：小修后验证基线。
- `previous-regressions.log`：上一轮3项通过。
- `remaining-contracts.log`：本轮初始3项失败。
- `contracts-after-blog-fix.log`：博客2项通过，剪藏1项失败。
- `targeted-final.log`：最终25项专项。
- `full-gates.log`：小修后完整门禁。

## 本轮修改总结与Diff

```diff
--- features/content_ops/content_service.py
+++ features/content_ops/content_service.py
+ 恢复旧blog_list_articles(limit=5)与blog_publish_article(title, content, status)
--- skills/ops_blog.py
+++ skills/ops_blog.py
+ 原schema/参数顺序，薄委托canonical函数
--- skills/ops_sentinel.py
+++ skills/ops_sentinel.py
+ 查询默认limit=None，保留时间/严重度范围内全部记录
+++ tests/test_phase2_blog_closeout.py
+ limit关键字、第三位置参数草稿状态两项回归
--- tests/test_phase2_service_migration_closeout.py
+++ tests/test_phase2_service_migration_closeout.py
+ 21条告警默认不截断回归
```

未提交、推送、部署、重启或真实外发，不进入阶段三。

## 最终独立验证补录

小修后完整 `verify_e0.py` 退出码 **0**，**8/8通过**：

- 本机 Python 3.13 Agent：**1063 passed，4 subtests passed**。
- 无网只读 Docker Python 3.9 Agent：**1063 passed，0 skipped**；真实zip备份反例已实际执行，认可原容器缺口关闭。
- 两环境 Parser：各 **205 tests passed**，Parser工作区干净。
- 两环境架构：**269文件、13处核准finding、0未登记违规**；两仓diff检查通过。

交付1060项，本轮新增博客2项与Sentinel1项后为1063。独立的剩余剪藏协议反例仍失败，保存在 `contracts-after-blog-fix.log`；不得将其混报为通过。

小修后已捕获源码哈希保持一致。本轮独立六单元状态存入 `architecture/stage-2-closeout-review-round14-20261016.json`。完整工作区patch、source-manifest.json、patch.sha256保存于证据目录，包含新文件与暂存删除，不含私人配置/业务数据，不改Git index。

**最终裁定：C05原15条追溯问题及C06容器缺口关闭；博客与Sentinel默认契约经Codex收尾通过。C02/C03/C04仍因剪藏旧链未恢复而未关闭。后续集中完成该链并保持已有修补，不扩大范围。**
