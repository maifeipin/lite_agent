# WeChat iLink 个人微信通道接入方案 (v2.2)

> **任务**: 为 lite_agent 新增个人微信通道（channel='wechat'），基于腾讯官方 iLink Bot 协议，按 SiverKing/weixin-ClawBot-API 仓库的 Python 直译方案实现
> **读者**: 负责实施的 AI 编程助手。本文档自包含，读完即可开工，无需额外提问
> **当前门禁**: ✅ **Gate 0 已通过**（2026-08-16，评审记录见 §3 文末），当前允许进入 Phase 0B（客户端 + 契约测试）；后续 Phase 1 无新增门禁
> **日期**: 2026-08-16

**修订记录**

| 版本 | 日期 | 内容 |
|---|---|---|
| v1 | 2026-08-16 | 初版 |
| v2 | 2026-08-16 | 评审修订：P1×5（context_token 贯穿收发链路 / 登录模式二分，服务启动不阻塞 / 空轮询与网络错误区分 / 主动推送以有效上下文为前提 / Phase 0 拆分 + Gate 0 协议评审）；P2×4（凭据原子写+白名单落盘 / 降级 msg_id 改稳定哈希 / 修正日期 / 明确文档入库策略） |
| v2.1 | 2026-08-16 | Gate 0 评审修订：契约表下游一致性 ×4（context store 补 sends_used 配额计数 / ret=-14 改"保留凭据+隔离 1h+单次试探"，纠正 v2 的"清凭据" / 补游标持久化文件 cursor_file / §5.2 补 C7/C9/C14 实现要求清单）；C13 已定案，§5.5 锁定路径 B；§8 验收补 19-22 |
| v2.2 | 2026-08-16 | Phase 0B 实施对齐：`ILinkClient` 构造函数显式接收 `cursor_file`，且 `WeChatChannel` 示例透传该配置；不改变 Gate 0 已通过的协议契约。 |

---

## 0. 一页纸总结

- **做什么**: 新增 `channels/wechat.py`（通道适配）+ `channels/wechat_ilink.py`（iLink 协议客户端）+ golden fixture 契约测试，让 lite_agent 通过个人微信收发消息。
- **怎么做**: 复刻 SiverKing 仓库思路——纯 Python 直接实现微信官方 iLink Bot HTTP 协议（扫码登录 → 长轮询收消息 → 带 context_token 发消息），**不部署 OpenClaw，不引入 Node.js，不引入第三方 SDK**。
- **两个硬前提**（v2 评审确立）：
  1. **context_token 是收发链路的必备状态**：官方 sendmessage 要求 `to_user_id + context_token` 同时提供；无有效 context 的主动发送会"HTTP 200 但不送达"。
  2. **服务模式不扫码**：systemd 下无交互终端，凭据缺失/失效时通道进入 offline 并通知管理员，绝不阻塞其他通道启动。
- **工作量**: 2 个新文件 + 契约测试 + 4 处小改动，预计 500~600 行。

---

## 1. 背景

微信通过 `Tencent/openclaw-weixin` 插件首次官方开放了个人号 Bot API（底层 iLink 协议）：手机微信扫码登录后，Bot 获得一个 HTTP 长轮询通道收发个人微信消息。该协议**不绑定 OpenClaw**。

lite_agent 已有 5 条通道（feishu/dingtalk/wecom/telegram/api），通道层有成熟抽象（`BaseChannel`）与挂载模板（ARCHITECTURE.md 第 16 节）。本方案是该模板的标准应用，但 iLink 有两个区别于现有通道的协议特性：**发送依赖接收时获得的 context_token**、**登录依赖一次性二维码**——这两点构成 v2 的核心设计约束。

## 2. 方案选型与边界

| 候选 | 结论 | 理由 |
|---|---|---|
| 部署 OpenClaw 桥接 | ❌ | 多 Node 进程 + 双层转发，违背"零重型依赖" |
| 第三方 Python SDK（weilink/wx_channel 等） | ❌ | 协议灰度期 SDK 质量参差；关键通道自控协议层 |
| **按 SiverKing 仓库直译协议（本方案）** | ✅ | 纯 Python、零新重型依赖、有踩坑文档、与 telegram.py 长轮询同构 |

**Phase 1 边界（本期不做）**：
- 不处理图片/语音/文件消息（固定文案回复；图片→OCR 留 Phase 2）
- 不处理群聊消息（仅单聊；群聊 @ 留 Phase 2）
- 不做多账号/多 Bot 管理
- 不改 RequestSelector 的 DOMAIN_MAP（微信是新通道，不是新技能域）

## 3. 实施前必读资料与协议契约表（Phase 0A 交付物）

> iLink 的 endpoint、字段、错误码**以固定版本的官方实现为准**；§3 只固化 lite_agent Phase 1 所需的最小 wire contract，完整媒体协议仍引用固定 commit。
> **但"活跃仓库最新代码"不是永久事实源**——Phase 0A 必须把参考实现**固定到具体 commit/tag** 并记录在下表，实现与 golden fixture 都以该固定版本为准，防止上游漂移带动实现和测试同时漂移。

**必读顺序**：
0. **官方接口文档**: 微信开发者文档"clawbot 相关接口"（developers.weixin.qq.com）——术语与语义基准
1. **主参考（用户指定）**: `SiverKing/weixin-ClawBot-API`——先读 `weixin-openclaw-api-py-docs.md`（踩坑全记录），再读 Python 源码
2. **协议佐证**: `hao-ji-xing/openclaw-weixin` 的 `weixin-bot-api.md`
3. **交叉验证（仅冲突时）**: `Oaklight/weilink`、`manymore13/wx_channel`
4. **官方源头**: `Tencent/openclaw-weixin`（TS 实现）

**协议契约表（Phase 0A 回填，Gate 0 评审对象）**：

| # | 契约项 | 回填内容 |
|---|---|---|
| C1 | 参考实现固定的 commit/tag + 官方文档抓取日期 | **抓取/核验日期：2026-08-16。**官方语义基准：[Tencent/openclaw-weixin@`cef0bfc390393f716903e16d50408118047f87e0`](https://github.com/Tencent/openclaw-weixin/tree/cef0bfc390393f716903e16d50408118047f87e0)（`package.json`=`2.4.6`，commit 日期 2026-06-25）；主参考：[SiverKing/weixin-ClawBot-API@`13613ec90aac7be5075d2d000cfbacab361834cc`](https://github.com/SiverKing/weixin-ClawBot-API/tree/13613ec90aac7be5075d2d000cfbacab361834cc)；协议佐证：[hao-ji-xing/openclaw-weixin@`4a853693e63c63e987302b939487c0edac100caf`](https://github.com/hao-ji-xing/openclaw-weixin/tree/4a853693e63c63e987302b939487c0edac100caf)；C8/C12 实证参考：[Oaklight/weilink@`82bf4c844b4a6c02d7e0196589f295ddf938382a`](https://github.com/Oaklight/weilink/tree/82bf4c844b4a6c02d7e0196589f295ddf938382a)；冲突交叉验证：[manymore13/wx_channel@`ee5009e2a7bcd602f9ac2404dd274934b403b09d`](https://github.com/manymore13/wx_channel/tree/ee5009e2a7bcd602f9ac2404dd274934b403b09d)。未检索到可公开访问、可固定版本的 developers.weixin.qq.com wire-format 页面；因此以腾讯官方仓库该 commit 的 README + `src/api/{api,types}.ts` 为官方可复现证据。 |
| C2 | 登录：获取二维码 endpoint + 请求/响应字段 | 基座 `https://ilinkai.weixin.qq.com`。2.4.6 主路径：`POST /ilink/bot/get_bot_qrcode?bot_type=3`，JSON body `{"local_token_list": [<本地已有 bot_token，最多 10 个>]}`，首次为空数组；响应 `{ "qrcode": string, "qrcode_img_content": string }`。`qrcode` 是 C3 查询键；`qrcode_img_content` 是 `https://liteapp.weixin.qq.com/q/...` URL，不是 PNG/base64。旧版 `GET` 仅作兼容回退，Phase 0B 不应作为首选。 |
| C3 | 登录：扫码状态轮询 endpoint + 状态枚举（等待/已扫/已确认/过期） | `GET /ilink/bot/get_qrcode_status?qrcode=<URL-encode>`；需要数字配对码时追加 `&verify_code=<URL-encode>`；单次长轮询客户端上限 35s，轮次间约 1s。状态全集：`wait`（等待）、`scaned`（已扫待确认）、`confirmed`（成功）、`expired`（过期）、`scaned_but_redirect`（按 `redirect_host` 切换轮询基座）、`binded_redirect`（已绑定，无新凭据）、`need_verifycode`、`verify_code_blocked`。官方客户端内存二维码 TTL=5min、整体等待默认 480s、最多刷新 3 次；这些是 2.4.6 客户端策略，不是服务端 SLA。 |
| C4 | 登录成功返回的凭据字段（白名单落盘范围以此为准）+ 有效期语义 | `confirmed` 响应字段：`bot_token`、`ilink_bot_id`、`baseurl`、`ilink_user_id`。落盘白名单固定为 `{token, bot_id, base_url, user_id, saved_at}`；`saved_at` 为本地生成，二维码字段、原响应体、`local_token_list` 均不落盘。服务端没有返回 `expires_at/ttl`；凭据有效期只能由 C10 的 HTTP 鉴权失败或 `ret/errcode=-14` 判定。每次重新扫码的 `ilink_bot_id` 可能变化。 |
| C5 | 收消息：长轮询 endpoint、timeout 参数、**游标/offset 规则**（含重启后游标恢复语义） | `POST {baseurl}/ilink/bot/getupdates`，body `{"get_updates_buf":"<opaque cursor>","base_info":{...}}`；首次传空字符串。响应含 `ret/errcode/errmsg/msgs/get_updates_buf/longpolling_timeout_ms`。游标是不透明字符串：仅在成功响应返回非空新值时替换，并单独原子持久化；重启从该值续拉，不能自行解析/递增。`sync_buf` 仅为 deprecated 兼容字段。服务端建议值通常 35000ms，HTTP read timeout 应比它多约 5–10s。`-14` 时清空游标和 context store、转 offline；bot 凭据按 C10 先隔离观察，不立即删除。 |
| C6 | 收消息：单条消息字段 schema（消息 id、会话 id、发送者 id、**context_token**、类型、文本、时间戳） | `WeixinMessage`：`seq?: number`、`message_id?: number`、`from_user_id?: string`、`to_user_id?: string`、`client_id?: string`、`create_time_ms/update_time_ms/delete_time_ms?: number`、`session_id?: string`、`group_id?: string`、`message_type?: 1(USER)\|2(BOT)`、`message_state?: 0(NEW)\|1(GENERATING)\|2(FINISH)`、`item_list?: MessageItem[]`、`context_token?: string`、`run_id?: string`。文本取首个/全部 `item_list[type=1].text_item.text`；Phase 1 只处理 `message_type=1`、`message_state=2` 的 direct 文本。每条 inbound 提供的 token 都视为该用户最新上下文候选。 |
| C7 | **发消息：完整请求体**（to_user_id + context_token + 文本的精确字段名与嵌套结构） | `POST {baseurl}/ilink/bot/sendmessage`，C9 headers，完整文本体：`{"msg":{"from_user_id":"","to_user_id":"<wxid>","client_id":"openclaw-weixin-<全局唯一值>","message_type":2,"message_state":2,"item_list":[{"type":1,"text_item":{"text":"<UTF-8 text>"}}],"context_token":"<该用户最新 inbound token>"},"base_info":{"channel_version":"2.4.6","bot_agent":"lite_agent/<version> (python)"}}`。字段顺序无语义；`run_id` 是 2.4.6 可选字段，Phase 1 省略。官方 README 的三字段示例是简版，官方 `src/messaging/send.ts` 与 SiverKing 实测均补齐上述字段。响应为 JSON：HTTP 2xx 之后仍必须检查 `ret`，缺省/0 才按协议成功，非 0 为语义失败；SiverKing 已实证不完整 payload 可出现 HTTP 200/`{}` 但终端不送达，因此 smoke test 必须同时验证微信端实际收件。`getconfig/sendtyping` 是输入状态 UX 流程，不属于 sendmessage 完整请求体的必要字段。 |
| C8 | **context_token 生命周期**：有效期/会话窗口（社区说法约 24h，**必须实测确认**，不得照抄）、是否每条消息刷新、失效错误特征 | **官方确定项**：token 随 inbound `getupdates` 消息返回，回复时原样回传；官方 2.4.6 未公布 TTL，源码持久化时也不记录到期时间。**外部实证**：Oaklight 固定 commit 明确实现“用户最后入站后 24h”保守窗口、每个 context token 最多 10 次出站，新 inbound token 与旧值不同时发送计数归零；[腾讯仓库 issue #225](https://github.com/Tencent/openclaw-weixin/issues/225) 在 2.4.3/2.4.6 上观察到约 1–2 天后 `sendmessage` 返回 `ret=-2, errmsg=""`，并明确说明真实 TTL/失败语义未被官方文档定义。**Phase 0A 提议（待 Gate 0 书面确认）**：24h 是客户端安全 TTL，不是协议保证；每条 inbound 都覆盖 `updated_at` 与最新 token，仅当 token 值变化才重置本地 10 次计数；发送前同时要求 `age<24h && sends_used<10`。`ret=-2` 归类为“该用户 context 不可用/额度耗尽”（立即失效该用户 token，等待新 inbound），不得清 bot 凭据、不得当 C10 鉴权失效；Phase 1 禁止无 token 试发。 |
| C9 | 鉴权方式（token 在 Header/Query/Body 的位置） | 已登录业务 POST 的 headers：`Content-Type: application/json`、`AuthorizationType: ilink_bot_token`、`Authorization: Bearer <bot_token>`、`X-WECHAT-UIN: base64(decimal(random uint32))`（每请求重生）、`iLink-App-Id: bot`、`iLink-App-ClientVersion: 132102`（2.4.6 编码 `(2<<16)\|(4<<8)\|6`）。token 不进 Query/Body；Body 另带 `base_info.channel_version=2.4.6` 与仅供观测的 `bot_agent`（ASCII，≤256 bytes）。QR 登录请求无 Bearer token。 |
| C10 | 鉴权失效的错误码/响应特征（触发 offline 的判定） | `getupdates` 响应 `ret==-14` **或** `errcode==-14`：官方 2.4.6 命名为 stale/expired bot token，但其实现是暂停该账号全部请求 1h，**并不立即删除凭据**。Phase 0A 提议采用同等保守策略：转 offline、清游标/context、保留 credential 并隔离 1h；隔离后同 token 试探一次，仍为 `-14` 才要求 CLI 重登录。HTTP 401/403 可直接判永久鉴权失败并清凭据。HTTP 5xx、连接/DNS/TLS/客户端超时属于网络错误，不清凭据。`sendmessage ret=-2` 按 C8 只失效单用户 context；其他非零 `ret/errcode` 作为语义 API 错误记录脱敏码并返回失败，不能仅凭 HTTP 200 判成功。 |
| C11 | 服务端重复推送场景与去重字段；**server 消息 id 的字符集**（决定 5.4 msg_id 是否需要哈希降级） | 官方 schema 的 `message_id`、`seq` 都是可选 `number`；存在时转十进制字符串，字符集为 `^[0-9]+$`，不会含下划线。协议未承诺 exactly-once；长轮询请求重试、游标持久化前崩溃或首次空游标均可能再次看到消息，故以 `message_id` 为首选持久去重键。缺失时降级为 `sha256(from_user_id\|to_user_id\|session_id\|create_time_ms\|canonical(item_list))[:24]`，不得只用截断文本/时间桶。`seq` 仅辅助诊断，不单独作为跨会话去重键。 |
| C12 | 发送长度/频率限制（_send_segmented 参数依据） | 腾讯 2.4.6 README/类型未公开文本或 QPS 上限。Oaklight 固定 commit 的外部实证常量为 **16384 UTF-8 bytes/条**（超出服务端拒绝）和 **每个 context token 10 次出站**。Phase 0A 提议采用保守契约：按 UTF-8 bytes 分段且每段 `<=16384`，每段消耗一次 context 计数；`max_msg_len=2000` 只是更保守的产品默认值，不宣称协议上限。超过剩余额度时截断/返回失败，等待新 inbound；HTTP 429/非零 ret 不盲重试。未发现可固定的官方 QPS 数值。 |
| C13 | 发消息是否存在 markdown/富文本 msgtype（决定 5.5 格式策略走 A/B） | **不存在已公开的 markdown/富文本 msgtype。**官方 `MessageItemType` 只有 `0 NONE / 1 TEXT / 2 IMAGE / 3 VOICE / 4 FILE / 5 VIDEO / 11 TOOL_CALL_START / 12 TOOL_CALL_RESULT`；文本只有 `text_item.text`。官方插件另有客户端 Markdown filter，反证服务端不负责 Markdown 渲染。Phase 1 固定走 5.5 **路径 B**：作为纯文本发送，MD 符号是否可见由微信文本展示决定，不声明渲染能力。 |
| C14 | SiverKing 踩坑文档全部坑位清单 | 固定 commit 文档明确 3 个坑：① `qrcode_img_content` 是 HTTPS URL，不是图片/base64；② 成功响应可能是 `application/octet-stream`，应先读 raw text 再 JSON 解码，不能强依赖响应 MIME；③ 不完整 sendmessage body/复用旧 context 可出现 HTTP 200、`{}` 但不送达，必须使用当前 inbound token、补齐 C7 字段并做端侧闭环验证。其 2.x 增量注意项一并纳入：二维码优先 POST+`local_token_list`；处理 redirect/verify/binded 全状态；补 `iLink-App-*` headers 与 `base_info.bot_agent`；`group_id` 只用于识别，不宣称群聊；媒体需 CDN AES-128-ECB。SiverKing 将 `getconfig→sendtyping` 写为完整 UX 流程，但官方 2.4.6 sendmessage 实现未把它们设为发送前置条件，故 Phase 1 可不实现 typing。 |

**Gate 0 通过判据**：C1-C14 全部非空；C7 具备腾讯 2.4.6 源码 + SiverKing 端侧实测双证据；C8 明确区分官方确定项、外部实证和保守客户端提议，并由评审人书面确认该提议（精确 TTL 仍无官方保证）。

**Gate 0 评审记录（2026-08-16，v2.1）**：

- **结论：通过**，附条件已随 v2.1 修订落实。C1-C14 全部非空且固定到 commit；C7 具备腾讯 2.4.6 `src/messaging/send.ts` + SiverKing 端侧实测双证据；C8 三层区分清晰。
- **对 C8 提议的书面确认**：`age<24h && sends_used<10`、每条 inbound 刷新 `updated_at`、仅 token 值变化才重置计数、`ret=-2` 仅失效单用户 context——确认为 Phase 1 客户端策略。两个常数必须走配置（`context_ttl_hours` / `context_max_sends`），官方语义明确后调配置即可，不改代码。
- **评审发现的 4 处下游一致性缺口及处置**：
  1. §5.4 context store 只有 `(token, ts)`，C8/C12 的"每 token 10 次出站"无处可查 → v2.1 已补 `sends_used` 计数与配额检查
  2. §5.4/§6 的 -14 处理原为"清凭据"，与 C10"官方暂停 1h、不立即删除凭据"直接矛盾 → v2.1 已改为隔离 1h + 同 token 单次试探，连续 -14 才清凭据
  3. C5 要求游标"单独原子持久化"，但 §5.2/配置中没有对应文件 → v2.1 已补 `cursor_file`（默认 `data/wechat_cursor.json`）
  4. C7/C9/C14 的实现级约束（固定 headers、client_id 生成、octet-stream 容忍解码、ret 语义判定）未落入 §5.2 → v2.1 已补"实现要求"清单，作为 Phase 0B 契约测试依据

## 4. lite_agent 现状关键事实（实施锚点，已逐行核对）

### 4.1 通道接口（`channels/base.py`）

```python
class BaseChannel(ABC):
    def __init__(self, name: str, config: dict, agent)
    def start(self) / stop(self)                            # 抽象
    def send_response(self, message_id: str, response) -> bool
    def send_progress(self, *args) -> bool                  # 可选，处理中回显
    def push_result(self, msg, response) -> bool            # 可选，DAG 异步结果
    def push_progress(self, msg, text) -> bool              # 可选，DAG 进度
    def broadcast(self, response) -> bool                   # 可选
```

### 4.2 消息契约（`agent.py:61-93`）

```python
IncomingMessage(channel, user_id, chat_id, message_id, text,
                notify_channels=None, is_guest=False, sync_mode=False,
                channel_payload=None)
# session_key = f"{channel}:{user_id}"
AgentResponse(text, title="", color="blue", task_id="", logs=None)
```

### 4.3 同构参考实现与既有模式

- **长轮询**: `channels/telegram.py`——`ThreadPoolExecutor(max_workers=5)` + `executor.submit(poll_loop)`，**严禁在轮询线程同步调 agent.handle()**。
- **防重放**: `agent.session_mgr.is_message_processed(msg_id)`（session.py:707，SQLite 持久化 + 24h 清理）。
- **访客 fail-closed**: telegram.py:66-74 / wecom.py:143-151——未配 admin id 时全员访客 + 警告日志，禁止 fail-open。
- **项目根路径解析先例**: session.py:66-69 用 `os.path.dirname(os.path.abspath(__file__))` 锚定，**不依赖 CWD**（systemd 下 CWD 不可靠）。本方案两个数据文件同此约定：相对路径一律相对项目根解析 + `os.makedirs(..., exist_ok=True)`。
- **原子写先例**: config_loader.py:391-394——`tempfile.mkstemp(dir=目标同目录)`（mkstemp 默认 0600）→ 写入并 `os.fsync` → `os.replace(tmp, target)`。本方案凭据/上下文文件**必须**复刻此模式。

### 4.4 启动时序的硬约束（v2 评审确认）

- `agent.channels = channels` 在 **main.py:214**、所有通道 `start()` **之后**才赋值 → 通道 `start()` 内**不能**依赖 `agent.channels` 做任何跨通道通知。
- `_send_card` 是 **`_register_cron_jobs` 的闭包局部函数**（main.py:63）→ WeChatChannel **无法也不应**调用它。跨通道通知一律走 5.4 的 **notifier 注入**模式。
- systemd 下无交互终端 → **服务模式永远不进入扫码流程**（见 5.3 模式二分）。

### 4.5 配置与安全约定

- 敏感值 `${VAR}` 占位符进 config.example.json，真值在 `.env`。
- `data/` 已 gitignore（.gitignore:10）——凭据与上下文文件放这里。
- ⚠️ **`docs/` 也在 .gitignore（第 31 行）**：本文档默认不入库。但它与 §3 回填表是实施与复审依据，**交付时由用户决定是否 `git add -f docs/wechat-ilink-channel-design.md`**（实施 AI 不得自行提交，见 §9）。
- VPS1 为 **Python 3.9**：`Optional[List[...]]` 风格注解，禁 PEP 604/585 裸写。
- HTTP 客户端用 **httpx**（requirements.txt:8 已声明）；iLink 为境内腾讯服务，**直连，不走代理**。
- 除 httpx 外不新增第三方依赖；终端 ASCII 二维码可选 try import `qrcode`，不作为依赖。

## 5. 详细设计

### 5.1 模块划分

| 文件 | 职责 | 依赖 |
|---|---|---|
| `channels/wechat_ilink.py`（新增） | iLink 协议客户端 `ILinkClient`：登录（仅 CLI）、凭据加载/原子持久化、长轮询、带 context_token 的发送。**不 import agent/channels**，可独立测试 | httpx、标准库 |
| `channels/wechat.py`（新增） | `WeChatChannel(BaseChannel)`：消息映射、context_token 存储、防重放、访客判定、回复路由、offline 状态机 | wechat_ilink、channels.base、agent |
| `tests/wechat_ilink/`（新增） | golden fixture（`fixtures/*.json`，自 C1 固定版本的参考实现样例截取）+ 解析契约测试 | pytest |

### 5.2 ILinkClient 接口契约

```python
# channels/wechat_ilink.py
from typing import Optional, List, Dict, Callable

class ILinkAuthError(Exception):
    """鉴权失效。判定特征 = 契约表 C10。
    属性 permanent: bool —— HTTP 401/403 为 True（清凭据）；
    getupdates ret/errcode=-14 为 False（官方语义=暂停 1h，保留凭据隔离观察）"""

class ILinkNetworkError(Exception):
    """网络层失败（连接错误/客户端超时/5xx）。与'正常空轮询'严格区分"""

class ILinkContextError(Exception):
    """sendmessage ret=-2：该用户 context 不可用/出站额度耗尽（C8/C10）。
    仅失效该用户 token 等待新 inbound；不清 bot 凭据、不触发网络退避"""

class ILinkClient:
    def __init__(self, session_file: str, poll_timeout: int = 35,
                 cursor_file: str = 'data/wechat_cursor.json'):
        """session_file/cursor_file 相对路径按项目根解析（4.3 先例）。
        httpx.Client(timeout=poll_timeout + 10) —— 客户端超时必须大于服务端长轮询窗口"""

    def load_credential(self) -> bool:
        """服务模式唯一入口：读本地凭据，成功 True。
        JSON 损坏（历史半写残留）-> 警告日志（不含文件内容）+ 返回 False，不抛异常"""

    def ensure_login_cli(self, on_qrcode: Callable[[str], None],
                         on_status: Callable[[str], None] = print) -> None:
        """⚠️ 仅 __main__ CLI 调用（交互终端）。服务模式禁止调用。
        取二维码 -> on_qrcode(链接或ASCII) -> 轮询扫码 -> 成功 _save_session()"""

    def get_updates(self) -> List[Dict]:
        """三态契约（v2 P1 修正）：
        - 正常返回消息列表（可为空 [] = 长轮询窗口内无消息，调用方立即重 poll，不退避）
        - 网络/超时/5xx -> 抛 ILinkNetworkError（调用方指数退避的唯一依据）
        - 鉴权失效 -> 抛 ILinkAuthError（调用方转 offline）
        游标维护与重启恢复语义 = 契约表 C5"""

    def send_text(self, target_id: str, context_token: str, text: str) -> bool:
        """⚠️ context_token 为协议必填（契约表 C7），无 token 不得调用本方法。
        ret=-2 -> 抛 ILinkContextError；鉴权失效抛 ILinkAuthError；
        其他非零 ret 记脱敏错误码返回 False；仅凭 HTTP 200 判成功 = 契约测试不通过"""

    def logout_cleanup(self) -> None:
        """删除本地凭据文件（不触碰 contexts 文件）"""

    # ---- 内部 ----
    def _save_session(self, payload: Dict) -> None:
        """v2 P2 修正：
        1. 白名单落盘——只保存契约表 C4 确认的字段（token/bot id/base_url/saved_at 等），
           禁止原样落盘整个登录响应
        2. 原子写（config_loader.py:391 先例）：mkstemp(dir=同目录, 0600)
           -> write -> flush+os.fsync -> os.replace"""
```

**实现要求（v2.1 自契约表下游落位，Phase 0B 契约测试逐条对应）**：

1. **请求构造（C7/C9）**：业务请求 headers 固定含 `AuthorizationType: ilink_bot_token`、`Authorization: Bearer <bot_token>`、`X-WECHAT-UIN`（base64(decimal(random uint32))，**每请求重生成**）、`iLink-App-Id: bot`、`iLink-App-ClientVersion: 132102`；body 带 `base_info.channel_version="2.4.6"` 与 `bot_agent="lite_agent/<version> (python)"`（ASCII ≤256B）。`send_text` 内部补齐 `from_user_id=""`、`message_type=2`、`message_state=2`，并生成每条唯一的 `client_id="lite_agent-wechat-<uuid4>"`。
2. **响应解码（C14②）**：先读 raw text 再做 JSON 解析，**不得依赖响应 Content-Type**（成功响应可能是 `application/octet-stream`）。
3. **成功判定（C7/C10）**：HTTP 2xx 后必须检查 `ret`/`errcode`，仅缺省/0 视为协议成功；`getupdates` 遇 `ret/errcode=-14` 抛 `ILinkAuthError(permanent=False)`；`send_text` 遇 `ret=-2` 抛 `ILinkContextError`；HTTP 401/403 抛 `ILinkAuthError(permanent=True)`；其余非零 ret 记脱敏错误码后返回失败，不盲重试。
4. **游标持久化（C5）**：游标独立存 `cursor_file`（默认 `data/wechat_cursor.json`，0600 原子写，同 5.2 模式）；仅当成功响应返回**非空新值**时替换并落盘；启动时从文件续拉；`ILinkAuthError` 路径清空游标文件与 context store（C5），**是否清凭据取决于 permanent 标志（C10）**。
5. **QR 字段语义（C2/C3/C14①）**：CLI 展示的是响应里的 `qrcode_img_content`（`https://liteapp.weixin.qq.com/q/...` URL，不是图片字节）；`qrcode` 字段仅作 C3 状态查询键，不展示、不落盘。

**独立登录 CLI**（凭据获取/续期的唯一途径）：

```bash
python3 -m channels.wechat_ilink --login --session-file data/wechat_session.json
# 交互终端打印二维码（有 qrcode 库则 ASCII，否则链接）；成功 exit 0，超时/放弃 exit 2
```

### 5.3 登录模式二分与凭据安全语义（v2 P1 重写）

**两种模式写死，禁止混合**：

| | CLI 登录模式 | 服务模式（systemd） |
|---|---|---|
| 入口 | `python3 -m channels.wechat_ilink --login` | `WeChatChannel.start()` |
| 终端 | 交互终端，可打印二维码 | 无终端，**永不生成二维码** |
| 无凭据/失效 | 走扫码流程 | 通道进入 **offline**：工作线程每 60s 重试 `load_credential()`（操作员在服务运行中执行 CLI 登录后**免重启自愈**）；并通过 notifier 通知管理员（仅状态翻转时通知一次，不刷屏） |
| 阻塞性 | 允许阻塞（人就在终端前） | **绝不阻塞**：`start()` 只 submit 工作线程即返回，其余通道正常启动 |

**凭据分级安全语义**（v2 修正 v1 的自相矛盾）：

| 凭据 | 级别 | 日志策略 |
|---|---|---|
| 长期 token / 登录响应体 / context_token | 高 | **永不入任何日志**（含 journal）；只落 0600 文件；验收 #12 grep 验证 |
| 二维码链接 | 一次性登录凭据（短 TTL，以 C3 为准） | 仅允许出现在 **CLI 模式的 stdout**；服务模式不产生，journal 中不应出现（验收 #12b） |
| 凭据/上下文文件 | 高 | `data/` 下、0600、原子写；内容不出现于日志、异常 traceback、send_progress 文案 |

### 5.4 WeChatChannel 通道适配

```python
# channels/wechat.py
from typing import Dict, Optional, Tuple
from channels.base import BaseChannel
from channels.wechat_ilink import ILinkClient, ILinkAuthError, ILinkNetworkError

class WeChatChannel(BaseChannel):
    """个人微信通道 — iLink 长轮询（与 telegram.py 同构）+ context_token 状态"""

    def __init__(self, config: dict, agent):
        super().__init__('wechat', config, agent)
        self.admin_wxid = config.get('admin_wxid', '')
        self.max_msg_len = int(config.get('max_msg_len', 2000))
        self.context_ttl = float(config.get('context_ttl_hours', 24)) * 3600  # Gate 0 确认的客户端安全 TTL（C8）
        self.context_max_sends = int(config.get('context_max_sends', 10))     # C8/C12 外部实证：每 token 出站配额
        self.client = ILinkClient(session_file=config.get('session_file', 'data/wechat_session.json'),
                                  poll_timeout=int(config.get('poll_timeout', 35)),
                                  cursor_file=config.get('cursor_file', 'data/wechat_cursor.json'))
        self.contexts_file = config.get('contexts_file', 'data/wechat_contexts.json')
        # wxid -> {"context_token": str, "updated_at": float, "sends_used": int}，启动时从 contexts_file 载入
        self._contexts: Dict[str, Dict] = {}
        self._notifier = None        # main.py 启动后注入（见 5.6），形式 fn(text, title)
        self._online = False
        # ThreadPoolExecutor(max_workers=5, thread_name_prefix="WeChatWorker")

    def set_admin_notifier(self, fn) -> None:
        """服务模式唯一跨通道通知途径。由 main.py 在 agent.channels 赋值后注入"""
```

**context_token 存储（v2 P1 核心，v2.1 补配额）**：
- 每条入站消息（含访客）→ 提取 `context_token`（字段名以 C6 为准）→ 更新 `self._contexts[wxid]`：`updated_at=now`、`context_token=最新值`；**仅当 token 值变化时才 `sends_used=0`**（C8 确认语义）；随后**原子写 contexts_file**（同 5.2 白名单原子写模式；白名单 = `{wxid: {"context_token","updated_at","sends_used"}}`）
- `_valid_token(wxid) -> Optional[str]`：同时满足 `now - updated_at < context_ttl` **且** `sends_used < context_max_sends` 才返回 token，否则 None
- **配额消耗**：每成功发送**一段**消息即 `sends_used += 1` 并落盘（C12：分段逐段消耗出站配额）；`_send_segmented` 发送前检查 `sends_used + 计划段数 <= context_max_sends`，配额不足时截断到剩余段数，剩余为 0 直接返回 False
- `ILinkContextError`（ret=-2）→ 立即将该用户 `sends_used` 置为 `context_max_sends`（等效失效，等待新 inbound 恢复）；**不清 bot 凭据、不触发网络退避**
- **所有发送路径必须先取有效 token；取不到即返回 False**，让调用方（回退链/broadcast）继续后续通道——绝不无 token 发送（C7/C8：会"200 但不送达"）

**收消息映射**：

```python
# msg_id 路由编码（v2 P2 修正）：'wechat_{talker}_{seq}'
#   seq = server 消息 id（仅当其匹配 ^[A-Za-z0-9-]+$ 无下划线，字符集以 C11 为准）
#         否则降级 seq = md5(f'{talker}|{svr_ts}|{full_text}').hexdigest()[:16]
#         —— 哈希输入用完整稳定字段，禁止 v1 的"5 分钟窗口 + text[:80]"（会误去重合法重复消息）
# wxid 可含下划线 => 解析时 rpartition('_') 只剥 seq 段，talker 完整保留
IncomingMessage(
    channel='wechat',
    user_id=talker_wxid,
    chat_id=conversation_id,       # 单聊 = talker_wxid
    message_id=msg_id,
    text=text,
    is_guest=(talker_wxid != self.admin_wxid),   # admin_wxid 未配 -> 恒 True + 警告（fail-closed）
    channel_payload={'talker': talker_wxid, 'context_token': context_token},  # DAG 异步推送用原始 token
)
```

**轮询主循环三态**（对照 telegram.py `_poll_loop` 结构）：
1. `get_updates()` 返回列表（含空）→ 正常逐条处理，**立即下一轮，无退避**
2. `ILinkNetworkError` → 退避 1s→2s→…→30s 封顶重试
3. `ILinkAuthError` → 按 `permanent` 分流（v2.1 按 C10 修正，**不再无条件清凭据**）：
   - `permanent=False`（ret/errcode=-14，官方语义=暂停 1h）→ **保留凭据**、清游标文件与 context store、转 offline（notifier 通知一次）→ 隔离 1h 后用同 token 试探一次；仍为 -14 才 `logout_cleanup()` 并要求 CLI 重登录
   - `permanent=True`（HTTP 401/403）→ `logout_cleanup()` + 转 offline（notifier 一次）→ 60s 重试 `load_credential()`（等待 CLI 重登录后自愈）

消息处理骨架：非文本 → 固定文案回复；群聊（会话类型字段以 C6 为准）→ 单行调试日志跳过；`is_message_processed(msg_id)` → 跳过；`send_progress` 回显 → `executor.submit(agent.handle)` → `send_response`。

**发消息（全部经 context store）**：

```python
def send_response(self, message_id: str, response) -> bool:
    text = f"**{response.title}**\n\n{response.text}" if response.title else response.text
    talker = self._parse_talker(message_id)
    if not talker:
        print(f"  ❌ [WeChat] send_response 无法解析 talker from {message_id!r}")
        return False
    token = self._valid_token(talker)      # 入站消息刚刷新过 store，正常必有
    if not token:
        print(f"  ⚠️ [WeChat] {talker} 无有效上下文，放弃发送（回退链继续）")
        return False
    return self._send_segmented(talker, token, text)

def send_to(self, open_id: str, response) -> bool:
    """cron/主动推送。v2 P1：仅凭 admin_wxid 不能发送——必须有近期有效上下文"""
    token = self._valid_token(open_id)
    if not open_id or not token:
        return False                       # 回退链继续后续通道
    text = f"**{response.title}**\n\n{response.text}" if response.title else response.text
    return self._send_segmented(open_id, token, text)
```

- `push_result(msg, response)` / `push_progress(msg, text)`：**优先用 `msg.channel_payload['context_token']`**（原消息上下文），缺失再查 store；都没有 → False 走默认回退
- `broadcast(response)`：查 `sessions WHERE session_key LIKE 'wechat:%'` 后**逐用户过滤 `_valid_token`**，无有效上下文的跳过并在结果日志注明跳过数
- `send_progress`：照 telegram.py:149-163 模式，反解 talker + 查 store，异常只打日志
- `format_card` 不覆盖（基类返回原文）

### 5.5 消息格式对齐策略

接收：单聊消息天然纯文本，用户输入的 MD 语法原样透传进 `agent.handle()`，与其他通道一致，无需处理。

发送：对齐现有通道"Markdown 优先、纯文本降级"哲学——

| 通道 | 行为 |
|---|---|
| telegram | `parse_mode='Markdown'` 优先，失败退纯文本（MD 源码可见） |
| wecom | 有 title 时 `msgtype=markdown`，否则纯文本 |
| **wechat** | **路径 B（C13 已定案）**：官方无 markdown/富文本 msgtype，纯文本原样发送（= telegram 降级输出），不剥离 MD 语法。title 模板 `**{title}**\n\n{text}` 与 telegram.py:143/wecom.py:34 逐字节一致 |

### 5.6 main.py / 配置文件改动清单

**`main.py`**（3 处）：

1. 通道实例化段（API 通道段之前）：
```python
# -- 个人微信通道 (iLink) --
wx_channel = None
wx_cfg = config.get('channels', {}).get('wechat', {})
if wx_cfg.get('enabled'):
    from channels.wechat import WeChatChannel
    wx_channel = WeChatChannel(wx_cfg, agent)
    wx_channel.start()          # 非阻塞：无凭据也只是 offline，不影响其他通道
    channels.append(wx_channel)
```

2. **`agent.channels = channels`（main.py:214）之后**注入 notifier（v2 P1：_send_card 是闭包局部函数不可复用，且时序上必须晚于通道装配）：
```python
if wx_channel is not None:
    def _wx_notify(text, title='⚠️ 微信通道'):
        for ch in agent.channels:
            if ch.name == 'wechat' or not hasattr(ch, 'send_to'):
                continue
            uid = (config['channels'].get(ch.name, {}).get('admin_chat_id')
                   or config['channels'].get(ch.name, {}).get('admin_userid')
                   or config['channels'].get('feishu', {}).get('admin_open_id'))
            if uid and ch.send_to(uid, AgentResponse(text, title=title, color='red')):
                return True
        return False
    wx_channel.set_admin_notifier(_wx_notify)
```

3. `_send_card` 回退链（main.py:63-72）精确 diff：
```python
# before
for ch_name in ('feishu', 'dingtalk', 'wecom', 'telegram'):
# after
wx_admin = config.get('channels', {}).get('wechat', {}).get('admin_wxid', '')
for ch_name in ('feishu', 'dingtalk', 'wecom', 'telegram', 'wechat'):
#     uid 取值分支补充：
#     uid = tg_chat_id if ch_name == 'telegram' else (wx_admin if ch_name == 'wechat' else admin_open_id)
```
微信排在**末尾**（最小侵入，不改变现有优先级）；`send_to` 无有效上下文返回 False 时链自然继续。

**`config.example.json`** channels 段新增：
```json
"wechat": {
    "enabled": false,
    "admin_wxid": "",
    "session_file": "data/wechat_session.json",
    "contexts_file": "data/wechat_contexts.json",
    "cursor_file": "data/wechat_cursor.json",
    "context_ttl_hours": 24,
    "context_max_sends": 10,
    "poll_timeout": 35,
    "max_msg_len": 2000
}
```
（`context_ttl_hours=24` / `context_max_sends=10` 为 Gate 0 确认的客户端保守策略（C8/C12 外部实证），非官方协议保证；官方语义明确后改配置即可，不动代码。）

**`.env.example`**：无需新增变量；追加一行注释说明 iLink 凭据运行时扫码获取、落 `data/` 文件。

### 5.7 文档同步（Phase 1 收尾）

- `ARCHITECTURE.md`：3.4 表加微信行（iLink 长轮询 / iLink HTTP API / 直连）；第 14 节状态表加行
- `README.md`：多通道一句补微信
- 提醒用户决定是否 `git add -f` 本文档（4.5）

## 6. 异常与边界处理矩阵

| 场景 | 行为 |
|---|---|
| 空轮询（窗口内无消息） | 返回 []，立即重 poll，**不退避** |
| 网络错误/超时/5xx | ILinkNetworkError → 退避 1s→30s 指数 |
| getupdates ret/errcode=-14（官方语义=暂停 1h） | ILinkAuthError(permanent=False)：清游标+context store、**保留凭据**、offline 隔离 1h → 同 token 单次试探；再 -14 才清凭据要求 CLI 重登录 |
| HTTP 401/403 | ILinkAuthError(permanent=True)：清凭据 → offline + notifier 一次 → 60s 重试加载等待 CLI 重登录 |
| sendmessage ret=-2 | ILinkContextError：仅失效该用户 context（sends_used 置上限），等待新 inbound；不清凭据、不计网络退避 |
| 其他非零 ret/errcode | 记脱敏错误码，本次返回 False；不盲重试、不清凭据 |
| 服务模式无凭据启动 | offline，不阻塞其他通道；操作员 CLI 登录后 60s 内自愈 |
| 二维码过期（仅 CLI） | 自动重取 ≤3 次，超时 exit 2 |
| 凭据 JSON 损坏 | load_credential 返回 False（视为无凭据），警告日志不含内容 |
| 原子写中途断电 | os.replace 保证要么旧版要么新版；无半写文件 |
| 服务端重复推送 | is_message_processed 去重（msg_id 含服务端 id 或稳定哈希） |
| 无有效 context 的主动发送 | send_to/broadcast 返回 False / 跳过，回退链继续，**不无 token 发送** |
| 非文本消息 | 固定文案回复，不进 agent |
| 群聊消息 | 单行调试日志跳过（Phase 1） |
| 回复超长 | _send_segmented 按 UTF-8 bytes 顺序分段（段上限 16384，C12；产品默认 max_msg_len=2000 更保守），逐段消耗出站配额，配额不足截断，>5 段强制截断 |
| admin_wxid 未配置 | 全员访客 + 警告日志（fail-closed） |

## 7. 实施步骤（含强制门禁）

### Phase 0A — 协议提取（唯一允许的当前动作）
1. 读 §3 资料，**固定参考实现 commit/tag 并记录 C1**
2. 回填契约表 C1-C14；C8（context_token 窗口）与 C7（完整发送体）须有实测或官方文档佐证
3. 产出 golden fixture 原始素材（从固定版本参考实现的消息样例截取 JSON）

### Gate 0 — 协议评审（强制）
4. 契约表 + fixture 素材交评审（用户/另一个 AI）；**书面通过前禁止写任何客户端代码**

### Phase 0B — 客户端与契约测试
5. 实现 `channels/wechat_ilink.py`（含 CLI）+ `tests/wechat_ilink/` 契约测试（fixture 驱动）
6. 项目外 scratch 目录一次性验证：CLI 登录 → 收消息打印字段 → 带回声 context_token 回复。验证后删脚本

### Phase 1 — 通道接入
7. 实现 `channels/wechat.py`；改 main.py ×3、config.example.json、.env.example
8. 过 §8 验收；同步 ARCHITECTURE.md / README.md

### Phase 2 — 后续增强（另立任务）
- 图片 → `OCR_ENDPOINT`（参照 telegram.py `_process_photo`）；群聊 @；二维码经 notifier 推送

## 8. 验收标准

| # | 验收项 | 通过判据 |
|---|---|---|
| 1 | Python 3.9 编译 | `python3.9 -m py_compile channels/wechat_ilink.py channels/wechat.py` 无错 |
| 2 | CLI 登录 | 交互终端执行登录命令：打印二维码，扫码确认后 session 文件生成、权限 0600；超时 exit 2 |
| 3 | 服务模式不阻塞 | enabled=true 且无凭据启动：其余通道正常就绪、主进程完成启动；微信通道 offline 日志一行；admin 经其他通道收到一条失效通知 |
| 4 | 离线自愈 | 服务运行中执行 CLI 登录 → 60s 内微信通道自动上线，无需 restart |
| 5 | 管理员收发闭环 | admin 发"你好" → agent.handle 完整路径 → 微信收到回复（发送带有效 context_token） |
| 6 | 访客隔离 | 非管理员发消息 is_guest=True，guest 工具过滤生效（如 `/check` 被拒） |
| 7 | fail-closed | 不配 admin_wxid：全员访客 + 警告日志 |
| 8 | 防重放 | 同一 server 消息 id 重复推送只处理一次 |
| 9 | msg_id 路由 | 含下划线 wxid + 含下划线 server id（哈希降级）两用例，send_response 均正确反解 talker |
| 10 | 主动推送前提 | 前置：admin 24h 内向 bot 发过消息建立上下文 → 其余通道全关时 cron 推送经微信送达。**负例**：无有效 context 时 send_to 返回 False、链继续、不发送 |
| 11 | 空轮询区分 | 空返回不触发退避（日志可见连续 poll 无 sleep）；拔网触发 ILinkNetworkError 退避序列 |
| 12a | 凭据零泄漏 | `journalctl -u lite-agent --since today` grep token/context_token 无凭据**值** |
| 12b | 服务模式无二维码 | 服务模式 journal 不出现二维码链接（服务模式不生成二维码） |
| 13 | 原子写 | 写凭据/上下文文件中 kill -9：重启后文件要么旧版要么新版，无半写；损坏文件降级为未登录不崩溃 |
| 14 | 重启续期 | 凭据有效期内 restart 免扫码直接上线 |
| 15 | 群聊忽略 | 群消息仅一行调试日志 |
| 16 | 配置模板 | config.example.json 含 wechat 段（enabled=false），无真实凭据 |
| 17 | 格式对齐 | title 模板与 telegram/wecom 逐字节一致；MD 表格源码可见不丢结构；入站 MD 原样进 agent |
| 18 | 契约测试 | golden fixture 测试全绿；fixture 与 C1 固定 commit 对应 |
| 19 | ret=-2 处置 | 模拟 sendmessage ret=-2：该用户 token 立即失效（后续 send_to False），bot 凭据保留，新 inbound 后恢复 |
| 20 | -14 隔离语义 | 模拟 getupdates ret=-14：凭据文件**保留**、游标与 contexts 清空、offline 隔离 1h 后单次试探；连续 -14 才要求重登录 |
| 21 | 游标续拉 | 重启后首轮 get_updates 使用 cursor_file 持久化游标；-14 后游标文件被清空 |
| 22 | 出站配额 | sends_used 达 context_max_sends 后 send_to 返回 False（即使 age<24h）；token 值不变的新 inbound 不重置计数，token 变化才重置 |
| 23 | 发送体契约 | golden body 测试：构造的 sendmessage 请求体与 C7 字段集逐键一致（client_id 除外），含固定 headers 与 base_info |

## 9. 工作流约束（来自 .agents/AGENTS.md，必须遵守）

1. **凭据零硬编码**；新增配置同步 config.example.json 占位符
2. **禁止擅自提交/部署**：未经用户明确确认，不得 `git commit`/`git push`/部署；`git add -f docs/...` 同样需用户决定
3. **交付自动生成修改总结**（核心说明 + Git Diff 对照），供另一个 AI 复核
4. **临时文件不进项目目录**：scratch/tempfile，验证后删除（golden fixture 属正式交付物，放 `tests/wechat_ilink/fixtures/`，不在此限）
5. **用词规范**：禁用非正式网络俗称；iLink 为境内服务直连
6. **Python 3.9 兼容**：`Optional[List[...]]` 风格注解
