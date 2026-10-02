# 会议与频道权限变更部署说明

本变更不自动部署，也不授予模型最终裁决权限。

## 频道

- API 必须配置认证；访客仅能聊天和读取自身任务流，不能读管理记录、修改资源或指定跨频道通知。访客会话采用独立 `guest/` 命名空间；客户端仍提交原 session_id。OpenAI 兼容 user 字段也按角色隔离，原有兼容 API 会话名称会改变。
- Telegram 仅本人私聊发送者 `from.id` 匹配 `admin_user_id` 时为管理员。旧的正数 `admin_chat_id` 可作本人 user ID 的兼容值，群 ID 永不授管理员权限。
- 企微接收桥接器必须配置 `channels.wecom.bridge_secret`（建议 `${WECOM_BRIDGE_SECRET}`），上游每次携带相同 `X-Bridge-Secret`。缺配置返回 503，错误密钥返回 401。只监听本机不代替认证。先同步桥接器及配置，再发布此版本，否则会中断接收；不得将密钥写入日志或 Git。

## 可信直邀与共管

新会议默认返回单席位邀请，完整邀请仅交给一个 Agent。主持人向 `POST /agent/api/v1/review-meetings/<id>/invites` 提交 `{ "label": "codex-sol-session-a" }` 为每位讨论者另外签发邀请；发新邀请不会踢出现有参会者。
`join` 的自报 label 不能覆盖授权 label。可附 `metadata: {client,model,session_label}`，全部为自报信息；服务端返回不可复用 participant_id。邀请再次领取返回 409；同名加入获得独立名称，不能复用离场者身份。
入会前客户端生成 32 字节以上随机 recovery_secret，并随 join 提交 SHA-256 `recovery_hash`，自行保管明文。如果入会响应丢失，使用邀请签发时的 participant_id 调 `POST .../recover`，提交 participant_id/recovery_secret；有效恢复会轮换原会话，最多每小时 5 次，初次领取 24 小时后不能恢复。leave/撤销后不可恢复。恢复请求不要并行：乱序返回的旧 token 可能已失效。未设置恢复密钥者需联系主持人撤销旧席位并新发邀请。

主持人 `POST .../cohosts`（空 JSON）签发单会议一小时共管 token。共管可读本会议、评论、发席位邀请、撤销席位、advance/waive/request-approval。不能创建其他会议、列全局会议、签发或撤销共管、最终 decide/cancel 或归档。每个管理操作记录独立 cohost 操作者，不冒充人类。
主持人 `POST .../revoke-cohost` 提交 operator_id；主持人或共管 `POST .../revoke-seat` 提交 participant_id。撤销后挂起读取在返回前复验。撤销本身不自动豁免该席位本轮未提交，推进前用 waive 写明原因。
`POST .../advance` 必须提交整数 expected_round，旧轮次返回 409，避免共管并发跳轮。`POST .../request-approval` 提交 summary，仅冻结讨论并通知本人，不批准实施。

旧共享邀请默认拒绝新加入，已有有效会话可继续。短期需要旧协议时，可显式设置 `channels.api.review_allow_legacy_invites: true`；这会保留多人重复入会风险，迁移完成后关闭。新默认安全协议不需该开关。旧会议 CLI cancel 同时撤销新席位及共管授权。

快照每页最多 100 个事件，通常至多 256KB；用 next_since/has_more 继续读取。单条巨大旧事件不截断，以保留审计完整性，旧异常数据需另行处理。哈希链验证和导出始终使用完整记录。评论按席位每分钟 10 条、会议记录约 2MB 配额；长轮询全进程最多 64 个。API 按连接来源 IP 每分钟最多 120 次，反向代理共用 IP 时会聚合，部署还应在代理层按真实可信来源配置限制。

## 待裁決通知

所有经过 `room.execute(request-approval)` 的入口与 outbox 在同一事务入队。服务主进程消费，无需给 CLI IM 密钥。通知只含会议 ID、轮次和查看提醒，不发送私密全文、公开链接或 token。

在 config.json 显式启用并设置已确认属于本人的私聊通道顺序：

```json
"review_meeting_notifications": {
  "enabled": true,
  "owner_channels": ["feishu", "dingtalk", "wechat"]
}
```

按这些频道已有管理员绑定私信；拒绝飞书群 ID、Telegram 群 ID、企微 @all/多目标。首个成功后停止，失败按显式顺序回退；微信仍受上下文限制。最多 5 次投递轮次，发送超时可能重复，不能保证跨平台 exactly-once。撤回/过期摘要发送前复验，投递状态留在 meeting_notification_outbox。默认关闭以避免未经配置向历史会议发送通知。上线前验证收件人和钉钉应用单聊权限。

## 本人频道命令

本人私聊发送 `/meeting help` 查看命令。微信、飞书、钉钉、企微、Telegram 沿用各自本人绑定；飞书和钉钉仅限私聊。命令由服务端直接处理，不经过模型，不作为技能暴露。API 聊天不能代替本人频道身份。

```
/meeting create 安全评审 | 检查邀请权限与频道裁决方案
/meeting invite <会议ID> local-codex-sol-a
/meeting invite <会议ID> external-codex-astra
/meeting cohost <会议ID>
/meeting revoke-cohost <会议ID> <operator_id>
/meeting revoke-seat <会议ID> <participant_id>
/meeting status <会议ID>
/meeting decide <会议ID> approve
/meeting confirm <服务端返回的确认码>
/meeting cancel <会议ID>
```

create 返回第一份邀请，invite 每次返回一份新的独立链接。cohost 返回单会议限权凭据，需私下交给指定主持 Agent，不能公开发布。共管通过既有 API 推进/申请审批，仍不能裁决。

decide 支持 approve/revise/reject，cancel 用于终止尚未裁决的会议，两者都要本人再次 confirm。确认码随机生成、五分钟有效、一次性，绑定原频道与本人、最新事件序号；任何会议变化都会阻止旧确认。频道确认不需要把 owner.key 交给模型。裁决仅记录决定，不自动执行代码或生产变更。跨频道确认不允许，需在新频道重新申请。

命令回复直接返回，绕过自动长文上传以保护邀请/确认凭据。当前使用明确命令，不让自然语言的歧义触发裁决。旧 CLI 审批仍可用。
