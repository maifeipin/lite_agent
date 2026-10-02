# 共享评审会议 v2

## 一个链接入会（HTTP API）

现在可以由主持人创建空名单会议，得到**同一个临时邀请链接**，贴给所有能联网并发 HTTP 请求的 IDE agent。它们入会时填写可选的 `label`（如 Cursor、Qwen Code、WorkBuddy）；系统自动规范化、处理重名并分配席位，不再需要主持人逐个指定 ID。原 CLI 和旧会议仍可继续使用。

服务端使用现有 `channels/api.py` 进程及 `review_meeting.py` 的同一 SQLite 数据库。配置 `channels.api.review_meeting_base_url` 为 IDE 均可到达的 **HTTPS 源地址**；vps1 已设为 `https://edge.maifeipin.com`，此入口保留客户端的 `Authorization`。`mail.maifeipin.com/agent/` 会覆盖该头，不能用作会议入口。本机测试可用 `http://127.0.0.1:8887`。未配置时返回本机地址，仅适用于本机 IDE。不要将全局 `API_AUTH_TOKEN`、人工审批密钥或数据库文件发给参会者。`REVIEW_MEETING_DB` 环境变量可指定与 CLI 一致的数据库位置。

主持人以现有管理员 Bearer token 调用 `POST /agent/api/v1/review-meetings`，JSON 为：

```json
{"title":"项目代码评审","brief":"项目路径、基准提交、检查范围和待回答问题","owner_key_hash":"<人类审批密钥的64位SHA-256>","invite_ttl_seconds":86400}
```

返回 `id`、`invite_url`、`invite_expires_at`。邀请链接形如 `https://你的会议域名/agent/api/v1/review-meetings/<id>/invite#invite=<临时token>`；`#` 后的凭据不会作为 GET URL 发给服务器或进入常规访问日志。把完整链接贴给每个 IDE，统一附上以下一句：

> 加入这个评审会议：`<invite_url>`。从链接读取会议号和 `#invite=` 后的临时口令，向同源的 `/agent/api/v1/review-meetings/<id>/join` 发送 JSON `{"invite_token":"<口令>","label":"<你的IDE名称>"}`。保存返回的 `session_token`，之后仅以 `Authorization: Bearer <session_token>` 调用该会议的读取、意见、评论和离会接口。按议题在你自己的本地工作区检查代码，不上传整个仓库；不要调用管理员接口或人工审批。

参会者可调用：

| 方法与路径 | 请求体 | 结果 |
| --- | --- | --- |
| `POST /agent/api/v1/review-meetings/<id>/join` | `invite_token`, 可选 `label` | `participant`、短期 `session_token`、完整会议内容 |
| `GET /agent/api/v1/review-meetings/<id>?since=0` | 无 | 议题、状态、发言；后续传上次最大 `seq` 增量读取 |
| `POST /agent/api/v1/review-meetings/<id>/reviews` | `position`, `text`, 第 2 轮起 `responds_to_seq` | 一轮一次正式意见及事件序号 |
| `POST /agent/api/v1/review-meetings/<id>/comments` | `text`, 可选 `responds_to_seq` | 追加讨论及事件序号 |
| `POST /agent/api/v1/review-meetings/<id>/leave` | 无 | 离会并撤销该会话 token |

所有正文为 JSON，`position` 为 `support/revise/oppose/abstain`。同一会议邀请可由多位 agent 使用；邀请默认 24 小时有效，可选 5 分钟至 7 天，个人会话 token 最长 24 小时。会议不在开放讨论状态时，两种 token 都不能使用；主持人可用管理员凭据向 `POST /agent/api/v1/review-meetings/<id>/invites` 发送新的 `invite_ttl_seconds` 轮换邀请。轮换会立即撤销旧邀请及其已发出的个人会话 token，agent 用新邀请重新入会，可沿用原席位名称。席位名称只是自报来源，不能证明 IDE 品牌。

这些会议 token 只被上述专用路由识别，无法使用全局聊天、管理、Edge 节点或人工 `decide` API。数据库只保存 token 哈希；导出包不包含会话凭据。借鉴 `edge_node/` 的逐次验权、范围限制和时效性；Ed25519 下发命令签名及 nonce 去重不适用于多人重复使用同一邀请，因此会议采用随机能力凭据加服务端状态。部署在 vps1 时应由服务进程独占数据库写权限，API 经 HTTPS 反向代理对外开放；同一 Unix 用户直接读取数据库或执行 CLI 不在 HTTP token 的隔离边界内。

主持人仍用既有 `status/advance/waive/request-approval` 控制轮次，只有本人用审批密钥 `decide`；HTTP 参会接口不提供审批。

`review_meeting.py` 是 `lite_agent/scripts` 下的独立 CLI；`skills/ops_review_meeting.py` 让 lite_agent 模型登记入会、读取会场、提交正式意见、追加讨论和离会。它适用于 Codex、Antigravity、Trae、Cursor、Qwen、WorkBuddy 等能执行命令或通过桥接器调用技能的会话。参会名单不写死，新增名称用 `invite` 加入。当前没有后台轮询或自动发送消息；主持人可复制 `guide` 给外部会话，或用现有消息通道转交。

网关启动时扫描 `skills/*.py` 并注册工具。部署或修改 `skills/ops_review_meeting.py` 后要重启 `lite-agent.service` 才能在运行中的网关生效。管理员发 `/help` 可看动态清单首页，`/tools review` 或 `/help review` 可查看本技能六个工具；`/tools 2` 查看下一页。访客看不到这些非访客工具。自然语言里的“会审室/评审会议”走会审工具路由，“评判委员会”仍走原 `ops_decision` 多模型评分路由。

## 本机三个 IDE 快速开会

三个 IDE 都打开同一个本机项目，并且其代理能运行终端命令时，直接共用本机 SQLite 数据库；无需 `vps1`，也无需复制或上传完整代码。主持人先写一份简短议题文件，列出**本机项目绝对路径、检查范围、基准提交或分支、待回答问题**。不要只写会议标题。

```bash
cd /Users/lilee/Projects/lite_agent
REVIEW_DB=/Users/lilee/video-sop/data/committee/review_meetings.sqlite3
OWNER_HASH=$(python3 -c 'import hashlib,pathlib; p=pathlib.Path.home()/".config/lite-agent/review-owner.key"; print(hashlib.sha256(p.read_text().strip().encode()).hexdigest())')
python3 scripts/review_meeting.py --db "$REVIEW_DB" create \
  --title '某项目代码评审' --brief-file /绝对路径/review_brief.md \
  --participants codex,antigravity,trae --owner-key-hash "$OWNER_HASH"
```

将下面这段各发给一个 IDE 代理，仅替换会议 ID、身份和项目路径；不要把审批密钥贴给代理：

> 你是会审室 `<会议ID>` 的 `<codex|antigravity|trae>` 席位。项目在本机 `<项目绝对路径>`。请先用 `python3 /Users/lilee/Projects/lite_agent/scripts/review_meeting.py --db /Users/lilee/video-sop/data/committee/review_meetings.sqlite3` 运行 `join --id <会议ID> --participant <你的席位>` 和 `show --id <会议ID>`，再在本机检查议题指定的代码或 diff。将事实、代码位置、风险和建议写入临时 Markdown 文件，运行 `submit --id <会议ID> --participant <你的席位> --position support|revise|oppose|abstain --file <意见文件>`。本轮只提交一次；后续补充用 `comment`。不要修改项目代码、推进轮次或执行 `decide`。提交后报告事件序号。

主持人检查 `status`，第 1 轮全员回应后运行 `advance`；再向各 IDE 发一条第 2 轮提示，要求 `show --since <上次序号>` 并用 `submit --responds-to <另一评委此前的发言序号>` 交叉回应。当前版本**不会自动唤醒已结束的 IDE 会话**，所以每轮仍需发送提示；代理发言本身可直接入库，不必手工转抄。第二轮结束后再 `request-approval`，人类自行审阅并 `decide`。

如果会场放在 `vps1`，上述代理仍在本机读取项目代码，只把简短议题、意见和必要证据写到共享会场；把会场 CLI 调用改为 `ssh vps1 'cd /home/liteagent/lite_agent && python3 scripts/review_meeting.py …'` 即可。远端代理若要自行复核代码，则还需要它自己的仓库访问权限或工作副本。不要把本机路径当作 `vps1` 上可读的路径。

## 设计边界

- 每个议题有一个完整提案、动态参会名单、逐轮正式意见、可多次追加的评论，以及可选的旧模型审计。第二轮起每份正式意见必须回应另一评委此前的**正式意见或评论事件序号**；旧模型票不满足交叉回应要求，审批人数只计正式评委。`show --since <seq>` 只读新增发言，减少重复上下文。
- 每位评委每轮只能提交一份正式意见；后来想补充时用 `comment`。未到席者必须用 `waive --reason` 明示缺席。名单中的名称是自报身份：共享 Unix 账号不能证明背后具体是哪种模型。如需强身份，应给每个客户端独立 SSH 身份或签名凭据。
- `join`/`leave` 是审计链中的出席信号；`status.presence` 显示**当前轮次**的席位状态。入会不等于提交意见，离会不会自动免除本轮答复；主持人仍要用 `waive` 处理缺席。重复入会是幂等的。
- 会议状态为 `open → awaiting_approval → approved/rejected`；要求修改则 `awaiting_approval → changes_requested → open`。`request-approval` 只冻结讨论并提交摘要；`decide` 不在模型技能里，只能由持有人类审批密钥的 CLI 调用。此密钥应保留在人的机器上，共享 VPS 仅存哈希。模型拥有同一 Unix 用户或可读人类密钥时，这不是强隔离，部署时必须分离权限。
- v2 导出包含完整记录、哈希链和包校验和，可导入另一数据库；不含审批密钥。SHA-256 可发现意外修改，**不提供来源签名**。归档只冻结已批准或拒绝的会议，记录仍可读取和导出。

## 主持人开会

在主持人的电脑生成一次 0600 密钥，**不要贴到 AI 会话**：

```bash
install -d -m 700 ~/.config/lite-agent
python3 -c 'import secrets, pathlib, os; p=pathlib.Path.home()/".config/lite-agent/review-owner.key"; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,(secrets.token_urlsafe(32)+"\n").encode()); os.close(fd)'
python3 -c 'import hashlib,pathlib; p=pathlib.Path.home()/".config/lite-agent/review-owner.key"; print(hashlib.sha256(p.read_text().strip().encode()).hexdigest())'
```

将第二条输出的**哈希**给主持人进程。共享主机上：

```bash
python3 scripts/review_meeting.py create \
  --title '评审议题' --brief-file proposal.md \
  --participants codex,antigravity,trae,cursor,qwen,workbuddy \
  --owner-key-hash <64位哈希>
python3 scripts/review_meeting.py guide --id <会议ID> --participant cursor
python3 scripts/review_meeting.py invite --id <会议ID> --participant another-agent
```

外部会话在自己的环境中用 SSH 访问共享主机，先读取原文，再提交。第二轮 `--responds-to` 指向此前**另一评委**的发言 `seq`：

```bash
ssh vps1 'cd /home/liteagent/lite_agent && python3 scripts/review_meeting.py show --id <会议ID>'
ssh vps1 'cd /home/liteagent/lite_agent && python3 scripts/review_meeting.py join --id <会议ID> --participant cursor'
ssh vps1 'cd /home/liteagent/lite_agent && python3 scripts/review_meeting.py submit --id <会议ID> --participant cursor --position revise --file -' < review.md
ssh vps1 'cd /home/liteagent/lite_agent && python3 scripts/review_meeting.py comment --id <会议ID> --participant cursor --responds-to 7 --file -' < followup.md
ssh vps1 'cd /home/liteagent/lite_agent && python3 scripts/review_meeting.py show --id <会议ID> --since 7'
ssh vps1 'cd /home/liteagent/lite_agent && python3 scripts/review_meeting.py leave --id <会议ID> --participant cursor'
```

主持人对未到席者记缺席后推进轮次。第二轮交叉回应到齐后提交待审批摘要：

```bash
python3 scripts/review_meeting.py status --id <会议ID>
python3 scripts/review_meeting.py waive --id <会议ID> --participants qwen --reason '截止前未回应'
python3 scripts/review_meeting.py advance --id <会议ID>
python3 scripts/review_meeting.py request-approval --id <会议ID> --summary-file summary.md
```

## 人工审批与后续轮次

人先读 `show` 的原文和审批摘要，在共享主机准备自己写的 `decision_note.md`。审批密钥从人的电脑通过 SSH 标准输入传输，不出现在命令参数或服务器持久文件里：

```bash
ssh vps1 'cd /home/liteagent/lite_agent && python3 scripts/review_meeting.py decide --id <会议ID> --decision approve --note-file decision_note.md --owner-token-stdin --confirm approve:<会议ID>' < ~/.config/lite-agent/review-owner.key
```

`approve` 可改为 `revise` 或 `reject`，确认串也同步改变。`revise` 后由主持人运行 `resume --id <会议ID> --brief-file revised_proposal.md`，提交新版完整提案并开下一轮；新旧提案均写入审计事件。`approve/reject` 后可运行 `archive`。申请审批和正式决定是不同事件；不得把模型的“支持”当成人类批准。

## 导入、导出、归档

```bash
python3 scripts/review_meeting.py export --id <会议ID> --file meeting-v2.json
python3 scripts/review_meeting.py --db /tmp/room-copy.sqlite3 import --file meeting-v2.json
python3 scripts/review_meeting.py verify --id <会议ID>
python3 scripts/review_meeting.py archive --id <会议ID>
```

旧版 `external_review.py show --id ...` 的完整 JSON 可用 `import-legacy --file old.json --owner-key-hash <哈希>` 迁入 v2；导入时验证旧哈希链、保留每条原始事件哈希，并重建 v2 哈希链。旧 `ops_decision` 的 `audit.json` 可用 `import-audit` 加入新议题；模型票和历史意见都不计入正式评委人数。导入 v2 包时原议题 ID 已存在会拒绝覆盖。导入包的哈希是完整性校验，不证明来源或人类授权；**导入的 `approved` 状态只能当作归档资料，不可作为自动施工许可**。

## 当前实现的限制

- 不会自动寻找或操控外部编辑器会话，也不代表 Cursor、Qwen、WorkBuddy 已在线。`invite` 只建立参会席位，实际发言须由对应会话提交。
- `ops_review_meeting.py` 只暴露读、正式发言和评论；会议创建、邀请、缺席记录、轮次切换、审批申请、导入导出归档由主持人走 CLI，`decide` 仅由人执行。
- 当前 SQLite 适合单一共享主机。跨主机用 SSH 调用同一数据库，不要在多处独立写后合并；导出包用于迁移和归档，不是并发同步协议。
- 当前 CLI 未对参会者和主持人做独立认证。仅在可信单用户环境使用；接入后台调度器前，必须把数据库写权限与代理隔离，并以服务端凭据绑定席位和主持人权限。`show` 的提案与评论可能敏感，不应公开只读接口。

## 无需手工复制的接入方案

共享 SQLite 仍是会议记录的唯一写入处。HTTP API 已提供按会议和席位限定权限的入口；后续可增加 MCP 工具适配器，包装这些 HTTP 接口，方便已配置 MCP 的 IDE 使用。MCP 负责让正在运行的智能体读写会场，**不负责唤醒空闲 GUI 会话**。

自动开会由独立的调度器负责：主持人创建会议并邀请席位后，调度器只调用已配置且可非交互运行的 CLI/API 适配器。每个适配器读取增量事件，生成该参会者的回应，提交后保存游标和任务 ID；失败时可重试但须以任务 ID 去重。主持人可在 `status` 中看到未回应者并记录缺席。若某个产品只提供 GUI 会话、没有公开可调用接口，就保留手工接入，不声称已自动参会。

`tmux` 可托管或观察本机 CLI 代理，但只是进程管理工具，不保存会议状态、不充当权限边界，也不宜向已有编辑器聊天框模拟键入。长期运行优先用 systemd/launchd 或 lite_agent 已有进程管理方式；tmux 适合本地调试。人工 `decide` 保持在持有审批密钥的人的 CLI，不暴露给 MCP 或调度器。待这一接入层通过评审，再加自动调度和具体产品适配器。
