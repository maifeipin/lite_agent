# VPS1 Python 3.13 多版本并存与运行基线说明

本文档记录 `vps1` 生产环境引入 Python 3.13 独立解释器、依赖锁定与外部项目调用边界的配置标准。

---

## 1. 解释器构建来源与部署位置

- **发行渠道**: Astral / Python-Build-Standalone (PBS) 官方发行版
- **版本**: CPython 3.13.18 (`20261026` 构建)
- **下载来源**:
  `https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.13.18%2B20261026-x86_64-unknown-linux-gnu-install_only.tar.gz`
- **系统安装路径**: `/opt/python-3.13`
- **核心组件**:
  - Python: `3.13.18`
  - OpenSSL: `3.5.9`
  - SQLite: `3.53.1` (原生满足 ChromaDB `>=3.35.0` 需求，无需猴子补丁)
- **隔离保证**:
  - 绝对不触碰 Debian 宿主系统的 `/usr/bin/python3` 与 `/usr/bin/python3.9`；
  - 宿主系统自带的 `fail2ban`、`certbot`、微信推送服务（`/root/down/wx/`）及 root crontab 监控脚本保持原状运行。

---

## 2. 外部项目调用边界与解释器路由

| 项目 / 模块 | 宿主位置 | 运行环境 / 解释器 | 调用方式 | 解释器选择逻辑 |
| :--- | :--- | :--- | :--- | :--- |
| **Lite Agent 主服务** | `/home/liteagent/lite_agent` | `/home/liteagent/lite_agent/.venv313` | systemd 守护进程 | 独立 Python 3.13 虚拟环境 |
| **Mail Statement Parser** | `/home/liteagent/mail-statement-parser` | `/usr/bin/python3` (宿主 Python 3.9) | `subprocess` CLI 接口 | 统一通过 `get_parser_python_executable()`：显式配置优先（校验存在性与执行权限，失败直接阻断）；未配置则自动发现 `.venv` 或受控回退至 `/usr/bin/python3`，绝不继承 Agent 主进程当前解释器 |
| **Billing 领域服务** | `lite_agent/features/billing/service.py` | 随 parser 独立环境路由 | `subprocess` CLI 接口 | 统一通过 `get_parser_python_executable(script_dir)` 获取外部解释器 |
| **WeWork 推送服务** | `/root/down/wx/` | `/usr/bin/python3` (系统 3.9) | HTTP 本地回环 (`:6969`) | 独立后台进程 |
| **Meilisearch / Docker** | 容器环境 | 独立容器镜像 | HTTP 本地回环 (`:7700`) | 完全隔离 |

---

## 3. 依赖锁定文件

本次验证通过的完整依赖清单已保存在：
`dev/requirements/vps1-python313-lock.txt`
包含 111 个精确锁定的 Python 3.13 x86_64 wheel 版本（含 `chromadb==1.5.9`、`openai==2.38.0`、`google-genai==1.47.0`、`pydantic==2.14.2`、`httpx[socks]==0.28.1` 等）。

---

## 4. 生产服务切换与回滚步骤

### 当前生产配置
`/etc/systemd/system/lite-agent.service`：
```ini
ExecStart=/home/liteagent/lite_agent/.venv313/bin/python /home/liteagent/lite_agent/main.py
```

### 回滚步骤 (已实测验证)
1. 还原 systemd 配置至已备份的 Python 3.9 配置：
   ```bash
   sudo cp /etc/systemd/system/lite-agent.service.bak.py39 /etc/systemd/system/lite-agent.service
   sudo systemctl daemon-reload
   sudo systemctl restart lite-agent.service
   ```
2. 数据兼容性范围界定 (实测验证):
   - **本次验证范围**: 限定于当前依赖与数据组合（Python 3.13.18 + Chroma 1.5.9 与宿主 Python 3.9.2 环境）；
   - **实测结果**: 在当前数据快照下，Python 3.13 写入的 SQLite/Chroma 向量集合经由 `safe_chromadb_sqlite_env` 包装后可被 Python 3.9 正常读取并解析，因此恢复服务配置即可直接回读数据，无需格式迁移；
   - **边界限制**: 该结论不可扩大为未来任意升级均兼容的长期保证。若后续升级 ChromaDB 大版本或底层存储 Schema 发生变更，必须在升级前另行实测双向兼容性并保留数据冷快照。
