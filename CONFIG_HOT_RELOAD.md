# 配置热重载与长持有变量说明 (Config Hot Reload)

Lite-Agent 采用了基于四层字典深层合并（`config.json` + `conf.d/` + `SQLite overrides` + `.env`）与 5秒 TTL 的热更新架构。

绝大多数技能（通过 `.get()` 链路链式读取）均享受 **5秒级自动热更新**，修改后无需重启服务。但为了保证部分核心模块的安全与连接稳定性，以下模块在进程初始化时对配置进行了**长持有 (Long-held)**。

> [!WARNING]
> **以下模块修改后必须重启进程方可生效**：即使通过 Web UI 修改了 SQLite 或 `conf.d/`，它们也不会读取到新值。开发 Web UI 时，请在这些配置项旁添加“需重启服务生效”的强提示。

## 需重启生效的模块清单

1. **核心通道 (Channels)**
   - `channels/api.py`
   - `channels/wecom.py`
   - 监听端口、Webhook Secret 等参数在 HTTP Server 启动时绑定，无法热更。
2. **计费模块 (ops_billing)**
   - 全局费率卡与计费策略在模块导入时即被缓存实例化。
3. **边缘节点指令系统 (ops_edge_cmd)**
   - 下发给边缘节点的 Ed25519 签名与校验规则受严格的安全红线保护，不参与热加载。
4. **备份模块 (ops_backup)**
   - 备份路径与留存策略在启动时长持有。

## Web UI 开发者注意
当修改上述配置项时，请务必在提交成功后，提示管理员手动重启 Lite-Agent 服务。
