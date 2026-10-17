# Lite Agent 生产发布与应急回滚 Runbook (Release & Rollback Runbook)

**文档版本**: v1.1 (内核重构阶段三整改与发布基线)
**发布基线**:
- Agent 验收提交: `codex/kernel-acceptance` (基于基线 `ea1e2b4428ce6627a5f0ebaac08706fecd9b4b70`)
- Parser 兼容提交: `918bce6b6d80dbf6d2af61cac35cbe24e44a7cdc` (权威读模型与 MailCLI 稳定版本)
- 运行环境: 沿用目标机现有已核验的 Python；本轮验证3.9/3.13，不借重构升级生产解释器。3.9容器只读无网；前端独立Node检查。

> [!CAUTION]
> **红线纪律**:
> 1. 本 Runbook 仅作为部署指导文档；实施方在获得用户明确授权前，**严禁执行任何远程推送、生产部署、服务重启或外网真实外发**。
> 2. 生产数据备份必须留在 VPS 本地安全目录，**严禁下载生产数据库、邮件原文或凭据至本地开发环境**。
> 3. 代码回滚时必须严格只回滚代码与相容依赖，**绝不回滚或覆盖生产新产生的人工还款、Todo 或会议审批状态数据**。

---

## 1. 发布前置检查与配置预检 (Pre-flight Checks)

获得单独生产授权后，在 VPS 以部署用户使用同一 Bash 会话执行；开始设置 `set -euo pipefail`，任意预检/安装/备份失败必须停止。发布前明确审批的SHA、回滚SHA、实际工作目录、服务名称、API端口、venv和配置/环境路径，不把 `/opt/lite_agent`、8000等示例当成已核实的生产值。

### 1.1 代码版本与工作区洁净度核验
```bash
cd /opt/lite_agent
git status --porcelain
# 期望：输出为空，工作区无未跟踪或未暂存修改
```

### 1.2 生产配置预检与权限安全
```bash
# 1. 确认 config.json 权限严格限制为 0600
ls -la config.json
# 期望：-rw------- 1 liteagent liteagent ... config.json

# 2. 校验 JSON 语法与必要字段
python3 -c "import json; cfg=json.load(open('config.json')); assert 'security' in cfg; assert 'billing' in cfg; print('Config JSON syntax OK')"
```

---

## 2. VPS 本地生产冷备份 (Local VPS Backup)

在执行代码更新或重启前，必须在 VPS 本地安全归档当前生产代码与数据目录：

```bash
BACKUP_DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/opt/lite_agent_backups/${BACKUP_DATE}"
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

# 1. 备份当前运行代码
tar --exclude='.git' --exclude='venv' --exclude='*.pyc' -czf "${BACKUP_DIR}/code_backup.tar.gz" -C /opt lite_agent

# 2. 备份生产 SQLite 真实主数据库清单
# 注意：严禁直接拷贝或 glob *.sqlite* / *.db*，避免损坏或夹带 WAL/SHM/journal 文件
# 在配置预检时从目标机实际配置/环境解析所有数据库绝对路径，写入本地清单。
# 必须覆盖 Agent 会话、Todo、会议、TaskSpec、ledger/edge/archive，以及受本次操作影响的 Parser 库。
# 源码默认值示例：data/todos.db、data/committee/review_meetings.sqlite3、data/sessions.db；
# 这些仅为源码示例，不代表目标机实际路径，禁止以固定文件名列表跳过不存在的库。
: "${SQLITE_DB_MANIFEST:?请指定已核实的 VPS 本地数据库绝对路径清单}"
test -s "${SQLITE_DB_MANIFEST}" || exit 1
while IFS= read -r db_file || [ -n "$db_file" ]; do
    case "$db_file" in /*) ;; *) echo "数据库路径必须绝对路径"; exit 1;; esac
    test -f "$db_file" || { echo "数据库缺失，阻断部署: $db_file"; exit 1; }
    backup_name=$(printf '%s' "$db_file" | sha256sum | cut -c1-16)
    sqlite3 "$db_file" ".backup '${BACKUP_DIR}/${backup_name}.sqlite3'" || exit 1
    printf '%s\t%s\n' "$db_file" "${backup_name}.sqlite3" >> "${BACKUP_DIR}/db-map.tsv"
done < "${SQLITE_DB_MANIFEST}"

# 3. 记录当前运行进程与提交哈希
netstat -tulpn | grep -E '8000|5216|5000' > "${BACKUP_DIR}/port_snapshot.txt"
git rev-parse HEAD > "${BACKUP_DIR}/commit_sha.txt"
echo "生产本地备份完成: ${BACKUP_DIR}"
```

---

## 3. 部署与服务重启流程 (Deployment & Restart)

### 3.1 锁定核准 SHA 与依赖同步
```bash
cd /opt/lite_agent

# 记录旧 SHA 以供故障快速回退
OLD_SHA=$(cat "${BACKUP_DIR}/commit_sha.txt")
echo "当前部署基线 SHA: ${OLD_SHA}"

# 明确检出已核准发布 commit SHA (严禁直接 pull origin main 避免未经复核代码上线)
: "${APPROVED_RELEASE_SHA:?必须显式设置最终验收核准的40位commit SHA，禁止使用阶段二基线代替}"
[[ "$APPROVED_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 1
git fetch origin
git checkout "${APPROVED_RELEASE_SHA}"

# 部署后校验 SHA 完全一致
DEPLOYED_SHA=$(git rev-parse HEAD)
if [ "${DEPLOYED_SHA}" != "${APPROVED_RELEASE_SHA}" ]; then
    echo "❌ 部署 SHA 不匹配！预期 ${APPROVED_RELEASE_SHA}，实际 ${DEPLOYED_SHA}"
    exit 1
fi

# 同步生产依赖
source venv/bin/activate
pip install -r requirements.txt
```

### 3.2 架构门禁二次快速复检
```bash
# 启动前在生产机快速执行静态架构依赖门禁
python3 scripts/check_architecture.py
# 期望：未发现未登记的架构依赖违规，门禁检查通过！
```

### 3.3 生产服务平滑重启
```bash
# 重新加载 systemd 并重启 agent 服务
sudo systemctl daemon-reload
sudo systemctl restart lite_agent.service

# 检查服务运行状态
sudo systemctl status lite_agent.service --no-pager
```

---

## 4. 生产健康检查与冒烟验证 (Verification & Smoke Testing)

### 4.1 免认证健康路由检查
```bash
curl -s -i http://127.0.0.1:8000/health
# 期望返回 HTTP 200，包含精确字段:
# {"status": "healthy", "mode": "full", "version": "1.0.0"}

curl -s -i http://127.0.0.1:8000/api/v1/health
# 期望返回 HTTP 200，包含:
# {"status": "healthy", "mode": "full", "version": "1.0.0"}
```

### 4.2 错误日志与后台守护进程核验
```bash
# 检查重启后 1 分钟内的日志，确认无任何反向依赖、未捕获异常或循环报错
sudo journalctl -u lite_agent.service --since "1 minute ago" --no-pager | grep -iE 'error|traceback|exception|fatal'
# 期望输出为空

# 确认 Socks5 Worker 代次守护进程正常启动并由主进程纳管
sudo journalctl -u lite_agent.service --since "1 minute ago" --no-pager | grep "Socks5 Auto-Failover Worker"
```

### 4.3 基础对话与核心能力冒烟
```bash
# 方式 A: 标准平台对话端点 (/api/v1/chat, 采用 session_id + text 结构)
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Authorization: Bearer ${LOCAL_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "smoke_check_session", "text": "ping"}'
# 期望返回 HTTP 200，并包含 status='completed' 或成功文本应答

# 方式 B: OpenAI 兼容端点 (/v1/chat/completions, 采用 messages 结构)
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer ${LOCAL_ADMIN_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "ping"}], "stream": false}'
# 期望返回 HTTP 200 与 OpenAI 兼容 JSON 响应
```

### 4.4 发送计数与单次投递核验
- 检查 `output_delivery` 归档记录：确认轻量/内部会话未向外部 SMTP 发起网络连接；
- 检查 IM 通道日志：确认无重复推送。

---

## 5. 应急回滚预案 (Emergency Rollback Runbook)

当且仅当发生以下严重故障且无法在 10 分钟内热修复时，立即触发回滚：
1. 服务进程频繁崩溃且无法稳定保持运行；
2. 基础对话、会话路由全量返回 500 异常；
3. 发现向外网非预期通道批量发送未授权消息。

### 5.1 回滚原则与数据安全红线
> [!IMPORTANT]
> **绝对禁止整库替换还原！**
> 生产运行期间，用户可能已经完成了重要的人工账单还款标记、审批了会议提案或创建了新的 Todo。如果粗暴将数据库还原回旧快照，会导致这部分真实业务数据丢失。
> **回滚操作只回滚代码版本与进程，数据库保留当前最新状态（由于本阶段完全无破坏性 DB schema 变更，旧版代码完全向下兼容读取当前数据库）。**

### 5.2 步骤一：停止故障进程
```bash
sudo systemctl stop lite_agent.service
```

### 5.3 步骤二：代码快速回滚到备份提交
```bash
cd /opt/lite_agent
PREV_SHA=$(cat "${BACKUP_DIR}/commit_sha.txt")
git checkout "${PREV_SHA}"
```

### 5.4 步骤三：重启服务并验证
```bash
sudo systemctl restart lite_agent.service
sudo systemctl status lite_agent.service --no-pager
curl -s -i http://127.0.0.1:8000/health
```

### 5.5 步骤四：回滚后状态排查与事故复盘
1. 提取故障时间段内的全量日志并保存至 `${BACKUP_DIR}/failure_journal.log`；
2. 保持生产机现场，向用户与复核方 Codex 提交事故最小复现证据包；
3. 待本地定位修复并经完整回归门禁通过后，重新发起发布流程。

## 6. 本地已验与生产待授权范围

本地 core smoke 使用 `python3.13 scripts/verify_clean_core_environment.py --report-json /tmp/kernel-core-proof.json`，仅假模型和回环端口；full行为在矩阵列出的真实测试节点中验证，不连接生产。生产发布前需审核服务实际配置优先级（CLI、配置、环境）与插件启用列表，确保 full 模式按预期加载。生产普通对话可能调用真实模型，上述curl步骤只有在授权后才运行。

生产业务验收必须单独确认测试账户/人工状态只读基线，并逐项核对账单权威快照、Todo与会议可读性、后台作业状态。投递验证使用获准的唯一测试标识：记录发送前后真实回执和发送次数，预期inline外发0次、一次外部投递仅1次；未经授权不试发真实IM或邮件。回滚先检出记录的旧SHA，再恢复已记录的相容依赖版本并验证，禁止恢复旧数据库覆盖后来人工状态。最终版本SHA尚未生成，文档不提供可误部署的默认SHA。
