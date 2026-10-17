# VPS1生产定时契约最小修复与复核

日期：2026-10-17。基于a1be86a，用户明确批准五项最小修复，四项前端补丁排除。Codex在本地实现并核验，未让Antigravity或其它执行者对VPS1做写入，不将本地自查冒充第二执行者独立复核。生产发布仍需用户明确授权。

## 范围与核心diff

只改main.py和三个插件装配文件，新增集中反例，main对应core manifest哈希同步；不改变业务算法、数据库schema、模型、频道或外部应用。

```diff
 # main.py
- if "name" not in job or "_comment" in job:
+ if "name" not in job:
      continue
 # 通用工具作业恢复旧输出条件，专用handler仍直接返回避免重复推送
- return tool_info["func"](...)
+ text = tool_info["func"](...)
+ if push and text and not text.startswith("✅") and not text.startswith("("):
+     send_owner_response(host_channels, channels_config,
+                         AgentResponse(text, title=job_name, color=legacy_color))
+ return text
 # mail_ops
+ register_job_handler("ops_mail_reader::mail_fetch_cron", host_bound_mail_handler)
 # network_ops：同一个告警包装同时承接内置cron和旧目标
- register_cron("edge_health", "*/10 * * * *", edge_service.edge_health)
+ register_cron("edge_health", "*/10 * * * *", edge_health_with_legacy_alert)
+ register_job_handler("ops_edge_health::edge_health", edge_health_with_legacy_alert)
 # sys_ops
- register_job_handler("ops_sentinel::sentinel_scan", scan_only)
+ register_job_handler("ops_sentinel::sentinel_scan", scan_with_legacy_risk_notice)
```

上面为语义摘要，准确diff在归档补丁中。通用输出颜色保持旧失败/错误/✉️/⚠️/❌/Error/Exception标记规则；专用邮件cron绑定当前实例agent，Sentinel传参、三种风险标志及红色告警保持；Edge恢复🔴条件、原标题和stale_batch十分钟去重key，故障提示亦保留。无模块级新全局代理，不注册新的模型工具。

## 注册与生产配置裁决

五项源码契约修复不自动改生产配置。新候选验证通过后，批准原表B第8项的最小去重方式：仅在真实条目确为edge_health_sweep→ops_edge_health::edge_health、*/10 * * * *、无额外kwargs/推送差异时，从候选cron_jobs配置移除此旧调度条目，完整备份原配置。唯一执行改由network_ops内置edge_health，现已具备相同告警包装。条目若与表B不符停止报告。不得只凭数组“第8项”下标删除。

其余12项原配置保持原样，含第7项_comment说明和第11项push_on_output=true；全部七插件、六个原频道、原数据及外部依赖保留。Todo、billing、backup内置原行为及task_specs_tick保留，不增删功能；Socks5按现场已有唯一180秒worker保持。发布前仍核对完整作业表，不通过手动触发生产任务验证。

## 验证与证据

新增tests/test_release_cron_contracts.py：21个展开用例。修前16失败/5通过；修后21通过。覆盖注释/非法任务、通用发送条件与颜色及闭包绑定、异常传播、两宿主邮件handler隔离和不重复发送、Sentinel三种风险信号、Edge内置与旧handler同一包装及原告警参数。

所有新用例使用真实注册器/插件装配边界，外部业务与发信端口为惰性mock，不读取生产配置或执行真实邮件/告警。

完整回归本机Agent1135 passed/4 subtests，Docker Python3.9 Agent1135 passed，Parser两环境各205，0 skip；架构281文件、13核准例外、0违规；core导出8项及Node5场景通过。首次完整runner因此前未跟踪交接资料的尾随空白为9/10、exit1；规范台账排版并将原始前端diff逐字节压缩后，格式专项复查通过，最终10项检查均满足，但没有声称同一次完整runner exit0。未重复整套业务测试。原始日志、源码冻结、精确补丁和修正后格式结果在architecture/vps1-contract-fix-evidence-20261017/index.json及raw-evidence.tar.gz。原始前端补丁未应用。历史HISTORY_UNRESOLVED仍保留，本轮不声称解决历史失败根因。

## 提交边界

.agents/AGENTS.md要求：“确认后提交/上线：在用户没有明确反馈‘另一个AI已确认，可以提交/上线’之前……”。用户已批准实施修复；本轮由Codex直接实现，不把自查冒充另一AI复核，因此暂不创建提交或部署。Antigravity按vps1-contract-fix-peer-review-request-20261017.md只读复核通过并交用户确认后，再生成新候选提交和包；四项前端补丁仍排除。

## 独立复核通过与提交授权确认

用户转交Antigravity 2026-10-17 20:56只读复核报告，结论APPROVED FOR CANDIDATE COMMIT CREATION；其独立运行21个专项用例通过，核对源码/manifest/证据哈希、前端无差异及生产未操作。Codex提交前再次验证全部冻结文件与归档成员哈希均一致。依据既有最小修复及候选提交授权，现创建本地新候选；此前“待独立复核/暂不提交”为过程状态，以本条为准。生产发布仍未获授权。
