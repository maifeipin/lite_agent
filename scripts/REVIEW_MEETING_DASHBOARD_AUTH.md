# Dashboard 会议认证（C+ 修订版）

## 权限

`POST /agent/api/v1/auth` 校验既有 htpasswd 成功后返回：

```json
{"success":true,"review_token":"<随机短期会话>","review_token_expires_at":1790949000,"scope":"review-meetings:host"}
```

不返回全局 API 密钥。`review_host_sessions` 仅存 SHA-256、用户名、显式 `host` 角色、到期时间及撤销时间；TTL 为 3600 秒。该凭据仅被会议路由接受，允许列表、创建、更新邀请、advance、waive、主持人 comments。它不能调用聊天或系统 API，也没有 `decide` 权限；人工裁决继续使用 owner 密钥和确认串。

登录限制为每个实际 TCP 对端每分钟 10 次请求，429 拒绝超限；不信任任意客户端的 X-Forwarded-For。经本机 Nginx 转发时所有请求会共享该对端限额，适用于目前单管理员用途；多用户部署应在受控 Nginx 层按真实客户端 IP 另加限制。计数在进程重启时清空。

`POST /agent/api/v1/review-meetings/logout`，Bearer 为主持人会话，JSON 为 `{}`，只撤销自己的短期会话。前端统一封装提供 `ReviewMeetingAPI.logout()` 调用该入口并清除浏览器存储；本轮未新增页面控件。关标签页只清 sessionStorage，不撤销服务端凭据；服务端仍由 TTL 限制。

参会席位凭据仍仅限对应会议：有效期内允许读取待审批、修改、裁决、归档快照；评论和正式意见要求未归档的 open 状态。裁决/归档后允许离会，并撤销该席位凭据。过期、撤销、跨会议凭据仍被拒绝。复用邀请码再次 join 可能产生新席位，不是凭据恢复：保存已有 session_token，避免重复 join。

## 前端与部署

`meeting_api.js` 只向 `https://edge.maifeipin.com/agent/api/v1/review-meetings…` 发送会话。请求使用 `credentials: omit`，登录仍通过 mail 同源 `/agent/api/v1/auth`。受限 token 与到期时间保存在 sessionStorage；401/403 清理并回登录，直播 400/401/403/404/421 停止并提示。弹窗移除或切换详情会 abort 挂起请求。

必须同时发布后端与 `login.html`、`meeting_api.js`、`index.html`、`modules/meetings.js`、`modules/todos.js`。VPS 配置保留：

```json
{"review_meeting_base_url":"https://edge.maifeipin.com","review_dashboard_origin":"https://mail.maifeipin.com","review_meeting_allowed_hosts":[]}
```

务必从 allowed_hosts 撤销 mail：其旧代理会注入全局管理员凭据，不能只发布前端。会议与登录 CORS 只响应配置的 Dashboard origin 和会议入口 origin；不使用通配 origin。保留边缘入口对 Authorization 的真实透传。不要在日志、网页源码或邀请链接输出主持人会话。

## 验收

- 未认证 mail 会议列表：421；未认证 edge 列表：403。
- 席位详情（含终态/归档）：200；列表与管理操作：403；全局 API：拒绝。
- 主持人会话会议管理可用，全局 API 和 decide：拒绝；到期/撤销后拒绝。
- 浏览器 mail 登录→会议列表→创建→直播→插话可用，退出后旧会话不可用。
- 登录超限：429；其他 origin 不获会议/登录 CORS 授权。

本地验证：`python3 -m unittest discover -s tests -p test_review_meeting_api.py -q`；`node tests/test_review_meeting_frontend.js`。这不能代替 VPS/Nginx 和实际浏览器部署验收。
