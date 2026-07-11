# 邮件系统全链路测试用例

> 执行方式: 在 VPS 上通过 `debug_api.py` 或飞书 IM 下发指令。
> 检查手段: `journalctl -u lite-agent`, SQL 直接查 DB, 飞书看推送。

---

## 用例 1: 不依赖 LLM 的纯本地指令 (LLM 完全宕机也不影响)

### 1.1 `/mail` - 查看原始收件箱
```bash
# 飞书发: /mail 5 maifeipin@qq.com
# 或 debug_api:
python3 scripts/debug_api.py "/mail 5 maifeipin@qq.com"
```
**预期**: 返回最近 5 封邮件主题/发件人/UID（纯 POP3，不消耗 LLM）
**检查**: 结果含 UID、发件人、主题

### 1.2 `/search` - 搜索正文库
```bash
# 飞书发: /search 验证码 5
# 或 debug_api:
python3 scripts/debug_api.py "/search 验证码 5"
```
**预期**: 搜索 email_bodies 表，返回匹配邮件摘要（纯 SQL）
**检查**: 返回结果含发件人、摘录文本

### 1.3 `/search --hedgedoc` - 完整文档上传
```bash
# 飞书发: /search --hedgedoc 验证码
# 或 debug_api:
python3 scripts/debug_api.py "/search --hedgedoc 验证码"
```
**预期**: 结果含 HedgeDoc 链接 + 前3封摘要
**检查**: 链接可访问，文档含完整正文

### 1.4 `/mailstats` - 统计概览
```bash
python3 scripts/debug_api.py "/mailstats"
```
**预期**: 返回分类/重要性/状态/成功率 统计
**检查**: 数字和 SQL `select count(*)...group by...` 一致

### 1.5 `/headers` `/missed` - 已入库邮件查看
```bash
python3 scripts/debug_api.py "/headers 10"
python3 scripts/debug_api.py "/missed 10"
```
**预期**: 返回处理过的邮件标题列表（纯 SQL）
**检查**: 列表数 <= 10

---

## 用例 2: cron 定时拉取+推送

### 2.1 20 分钟 cron 推送验证
```bash
# 1. 查未推送的高优邮件
python3 -c "
import sqlite3
conn = sqlite3.connect('/home/liteagent/mail-statement-parser/statements.db')
r = conn.execute(\"SELECT count(*) FROM email_summaries WHERE importance='high' AND pushed=0\").fetchone()
print(f'未推送高优: {r[0]}')
"
# 2. 若 > 0, 等下次整20分钟 (或手动触发 cron)
#    飞书发: /cron, 找 [邮件同步_每20分钟] 的序号, 发 /cron <序号>
# 3. 再次查询 pushed 数量
```
**预期**: pushed 数量增加，飞书收到红卡推送
**检查**: DB `pushed=1` 增量 = cron 执行次数 × 新增高优邮件数

### 2.2 新邮件到达 → 自动推送
```bash
# 1. 从 163 发一封测试邮件到 maifeipin@qq.com, 主题含"紧急""验证码"等
# 2. 等最多 20 分钟后检查 journal
journalctl -u lite-agent --since=-25m | grep -E "push_alert|高优邮件推送|卡片已发送"
# 3. 查 DB 是否有新入库 + pushed=1
```
**预期**: 邮件入库 → importance=high → pushed=1 → 飞书红卡
**检查**: journal 含 `push_alert`, DB `pushed=1`

### 2.3 20分钟 cron 无新邮件时不浪费资源
```bash
journalctl -u lite-agent --since=-30m | grep "邮件同步_每20分钟"
```
**预期**: 执行时间 < 10 秒（无新邮件时 consecutive_skip 快速退出）
**不需要 check**: 如果所有最近邮件都已处理

---

## 用例 3: LLM 宕机场景

### 3.1 LLM 不可用时拉取仍成功
- 停用 LLM (改 API key)
- 发 `/cmd fs 1`
```bash
python3 scripts/debug_api.py "/cmd fs 1"
```
**预期**: POP3 拉取成功，正文存入 email_bodies，状态 = failed
**检查**: DB `select count(*) from email_summaries where status='failed'` 增加

### 3.2 LLM 恢复后自动重试
- 修复 LLM 配置
- 等下个 20 分钟周期
**预期**: failed 邮件被重新 LLM 处理 → importance 填充 → 高优推送
**检查**: 原 failed 记录的 status 变为 processed，importance != NULL

### 3.3 LLM 宕机时手动查询仍可用
- LLM 停用状态下
```bash
python3 scripts/debug_api.py "/mail 5"        # POP3 拉
python3 scripts/debug_api.py "/search 安全"   # 查正文库
python3 scripts/debug_api.py "/headers 20"     # 看已入库
```
**预期**: 三个都正常返回结果。不需要 LLM
**检查**: 均有有效输出

---

## 用例 4: POP3 UID 回收防误判

### 4.1 验证 subject 校验生效
```bash
# 1. 找一条 processed 邮件 (id=N)
# 2. 手动改其 subject 为"XXXXXX_TEST_UID_RECYCLE"
sqlite3 statements.db "UPDATE email_summaries SET subject='XXXXXX_TEST_UID_RECYCLE' WHERE id=N"
# 3. 下个周期应触发重新处理
```
**预期**: journal 出现 `UID=X 已被回收 (新主题=...)`
**检查**: 邮件重新入 LLM 处理，subject 更新

---

## 用例 5: 银行账单拉取 (修复后的 tuple 解包)

### 5.1 `/cmd fetch` 不再报 tuple 错误
```bash
python3 scripts/debug_api.py "/cmd fetch 1"
```
**预期**: 正常输出 "开始同步账户" / "扫描完成"，无 `int() argument must be a string, not 'tuple'`
**检查**: 无 TypeError

---

## 用例 6: 多账户同步 & Outlook OAuth

### 6.1 Outlook 不再报权限错误
```bash
journalctl -u lite-agent | grep -i "outlook.*permission"
```
**预期**: 无 `Permission denied: 'token_outlook.json'`
**检查**: token 文件属主为 liteagent

### 6.2 所有账户扫描完成
```bash
python3 scripts/debug_api.py "/cmd fs 1"
```
**预期**: 输出含所有 4 个账户(chenli_mail@163.com / maifeipin@qq.com / samsmath288@gmail.com / ms4ai@outlook.com)
**检查**: 每个账户有"扫描完成"行

---

## 验证清单

| # | 测试项 | 关键 SQL / 命令 |
|---|--------|----------------|
| 1 | 仓库对齐 | `git log --oneline -1` 本地=VPS |
| 2 | lite-agent 服务运行 | `systemctl is-active lite-agent` |
| 3 | cron 任务注册 | 飞书 `/cron` 查看含"邮件同步_每20分钟"+"智能邮件助手同步" |
| 4 | pushed 列存在 | `PRAGMA table_info(email_summaries)` 含 pushed |
| 5 | email_bodies 有数据 | `select count(*) from email_bodies` > 0 |
| 6 | API 通道可访问 | `curl -s http://127.0.0.1:8887/api/v1/chat` 返回非错误 |
