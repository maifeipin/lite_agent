# 本地验收后缺陷修补与注册审计

日期：2026-10-17。依据用户“继续”，处理已登记的提交前事项；保留已确认的阶段三本地验收及HISTORY_UNRESOLVED风险记录。未把这次修补声称为历史57项异常的根因修复。

## 修补内容与原始反例

### F01 仪表盘读取本实例注册表

原_handle_dashboard每次新建CommandRegistry，因此无论宿主已注册多少指令，都返回空列表。改为从公共HTTP宿主的agent.command_registry读取，按已有show_in_dashboard元数据筛选；不读取其它应用或默认全局表。缺少宿主注册表时明确503，不再以200空表掩盖装配问题。公开列表协议仍只返回name/description，不改变鉴权或指令执行权限。

```diff
- from core.command_registry import CommandRegistry
- items = CommandRegistry().items_for_dashboard()
+ host = _extract_api_server(self)
+ agent = getattr(host, "agent", None)
+ registry = getattr(agent, "command_registry", None)
+ if registry is None:
+     self.send_error(503, "Command registry unavailable")
+     return
+ items = registry.items_for_dashboard()
```

永久反例使用真实ThreadingHTTPServer和真实ApiHandler，验证core含/new、full内容插件可见/rss_list与/rss_topic、不可见/rss_fetch不展示、两个实例并行不串表、外来全局指令不泄漏、缺失注册表503。没有调用任何业务指令或真实外部服务。

独立core证明脚本亦加强为确切验证仪表盘包含唯一/new，而非仅检查返回类型list。channels/api.py对应的core manifest哈希已同步。

### F02 删除不存在的备份导出声明

backup_service.__all__列出_backup_meilisearch，但该名称从未定义，星号导入抛AttributeError。仅删除该不存在的声明，保留所有实际函数及现有导出，不新增别名或重写备份算法。_create_meili_dump等实际实现未变。

```diff
 __all__ = [
     "do_backup",
     "do_backup_and_sync",
     "_bdpan_sync_dir",
     "_backup_vaultwarden",
-    "_backup_meilisearch",
     ...
 ]
```

永久反例执行真实星号导入，确认导出集合与__all__一致且所有对象与规范模块同一对象；不执行备份、云同步或数据库操作。已有真实临时zip归档反例同时回归。

## F03 注册重叠审计：保留实际兼容路径

AST复算：skills层21个静态slash声明，features插件24个静态命令名称，两者交集21。bootstrap的/new及动态注入不包含在这项静态统计内。

这些名称分别注册在默认全局表和应用实例表；跨两张表同名不等于同一表重复注册。Agent构造器未注入注册表时仍选择默认表；_handle_builtin与_handle_double_colon也保留未注入时的全局分发。因此“全部全局注册生产不可达、可以直接删除”缺少依据：main装配不使用它们，不代表旧宿主接口没有消费者。

增加两个真实Agent分发反例，覆盖/与::旧宿主兼容路径；已有显式实例注册表不得执行外来全局命令的反例同时回归。保留同表重复register抛ValueError和实例隔离，不合并两张表、不删除装饰器、不迁移skills元数据。

审计原始定位及名称清单见证据目录registration-audit.json。F03结论是“已核对并保留兼容”，不是已经清退21项。F04技能注册迁移与F05文件清理仍不在本轮执行范围。

## 验证与范围

新增永久反例文件：tests/test_post_acceptance_fixes.py，6个参数展开用例。

- 修前：4项功能反例全部失败，日志red.log。
- 修后专项：39 passed，exit0，覆盖新反例、实例权限/隔离、真实zip备份、core manifest等，日志green.log。
- 修改的源码与测试在source-freeze.json中固定。最终完整双环境门禁exit0、10/10通过；本机Agent1114 passed / 4 subtests passed，Docker Python3.9 Agent1114 passed，两环境均0 skip；Parser各205通过；架构两环境均280文件、13核准例外、0未登记违规；Node5个场景通过。日志full-gates.log，不以旧1108通过证明新增变更。

证据目录：`/tmp/kernel-post-acceptance-fixes-20261017/`。本轮未修改Parser、备份算法、通知投递、业务默认值或数据库schema；未删除缓存、运行数据、脚本、skills或.workbuddy。未commit、push、部署或重启。

用户先前的风险接受不自动授权提交或上线；本轮只是修补与回归交付。已归档的历史验收原始日志和快照清单保留，不重写为新源码的证据。

## 最终证据冻结

39项专项与最终10/10完整门禁均已通过；测试后冻结的源码没有再变更。与已接受快照相比，只有仪表盘、备份导出声明、独立core脚本及manifest四个已有文件变化，新增一个6用例反例文件；Parser逐文件无变化。

长期归档：`architecture/post-acceptance-evidence-20261017/index.json`，其中列出原始日志、修前失败、修后通过、注册审计、源码SHA和基于已接受快照的完整补丁。原始日志与补丁压缩保存，保留原始字节，不为通过格式检查修改证据。可用tar读取其中成员。

结论：F01/F02修补完成并验证；F03有可执行旧宿主兼容路径，保留独立表与同表重名拒绝。F04/F05没有迁移或删除授权，不执行。当前成果可供另一执行者复核；本轮未提交、push或部署，历史HISTORY_UNRESOLVED仍保留。
