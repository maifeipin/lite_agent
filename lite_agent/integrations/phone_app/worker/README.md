# Mac Worker 物理设备执行端部署与配置指南

本模块为部署在 Mac 本地的工作节点程序，负责直连物理手机（通过 USB / ADB / Handsets Daemon）并主动向中枢（VPS1）建立受控执行通道。

---

## 1. 架构拓扑 (VPS1 ↔ Mac 安全通道)

```
+------------------------------------+          +------------------------------------+
|            VPS1 (中枢端)            |          |           Mac 本地工作端            |
|                                    |  HTTPS   |                                    |
|  - PhoneAppService                 |  Long-   |  - MacWorkerDaemon (run_worker.py) |
|  - WorkerHub (会话管理/租约调度)     |  Poll    |  - HandsetsDriver (USB / TCP 9008) |
|  - REST Routes (/api/v1/phone-app) | <------- |  - JdAdapter (京东 App 业务适配器)   |
|  - SQLite 数据库与审计日志           | (Mac主动) |  - 物理手机: OnePlus 9 (c68a3cf2)   |
+------------------------------------+          +------------------------------------+
```

- **中枢 (VPS1)**：不直接运行 ADB 或触碰物理设备，所有受控设备状态由 Mac Worker 主动上报保活。
- **工作端 (Mac)**：启动守护进程主动向 VPS1 握手注册并周期心跳；采用单向主动连接避免 VPS 穿透内网。
- **传输安全与协议**：生产跨公网环境**强制要求 HTTPS/TLS 反向代理**，明文 HTTP 仅限本地 127.0.0.1 闭环联调；通道模式为鉴权 HTTPS 长轮询（附带独立后台心跳线程与取消通道通知）。

---

## 2. 鉴权令牌与版本锁定

- **协议版本**：`1.0.0`（严格版本锁定，主版本不一致直接拒绝握手）。
- **鉴权凭证**：禁止使用任何硬编码默认弱口令。启动时必须通过环境变量 `PHONE_WORKER_TOKEN` 或 `--token` 传入高强度随机密钥（例如使用 `openssl rand -hex 32` 生成）。
- **受控设备**：OnePlus 9（序列号 `c68a3cf2`，Android 14，已安装并联通 Handsets 守护进程）。

---

## 3. 启动指令

### 3.1 检查本地设备就绪
在 Mac 本地终端执行：
```bash
adb devices
# 输出中必须包含 c68a3cf2 device
hs --device c68a3cf2 dev ping
# 输出必须包含 pong
```

### 3.2 启动 Mac Worker 守护程序
```bash
# 生成并在中枢与本地配置相同的强密钥:
export PHONE_WORKER_TOKEN="<STRONG_RANDOM_TOKEN_FROM_CONFIG_OR_ENV>"

python3 -m lite_agent.integrations.phone_app.worker.run_worker \
  --hub-url https://<VPS1_DOMAIN_OR_LOCAL>:8000 \
  --token "$PHONE_WORKER_TOKEN" \
  --worker-id mac-worker-oneplus9 \
  --device-id c68a3cf2
```

环境变量支持：
- `PHONE_HUB_URL`: 中枢服务地址（默认 `http://127.0.0.1:8000`）
- `PHONE_WORKER_TOKEN`: 鉴权凭证（必填，无默认密码）
- `PHONE_WORKER_ID`: 工作端唯一标识
- `PHONE_DEVICE_ID`: 目标设备序列号

### 3.3 离线测试桩模式 (非真机)
```bash
PHONE_WORKER_TOKEN="test_token_123" python3 -m lite_agent.integrations.phone_app.worker.run_worker --fake
```

---

## 4. 日志与生命周期

- **握手与独立心跳**：程序启动后首先向 `/api/v1/phone-app/worker/handshake` 发起握手获取会话；随后启动独立后台线程每隔 10 秒发送一次心跳保活并同步真实设备状态，任务执行阻塞时不影响心跳。
- **任务领取与回执**：通过长轮询领取任务租约，执行后立即上报回执（含单调时钟实测耗时 `timings` 与设备复位状态 `reset_status`）。
- **实时取消响应**：后台心跳线程若收到中枢取消租约通知，立即向执行器触发 `cancel_event`，安全退出并在 finally 中复位设备。
- **安全退出**：支持 `SIGINT` (Ctrl+C) 与 `SIGTERM`，收到信号后完成当前任务并安全断开。
