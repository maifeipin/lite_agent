"""
PC Daemon 配置加载器 (daemon.config).
严格从环境变量或本地 JSON 配置文件读取，严禁硬编码密钥。
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from typing import List


@dataclass
class DaemonConfig:
    hub_ws_url: str
    daemon_token: str
    node_id: str
    display_name: str
    workspace_roots: List[str] = field(default_factory=list)
    codex_bin: str = "codex"
    agy_bin: str = "agy"
    extra_path: List[str] = field(default_factory=list)
    heartbeat_interval_sec: float = 15.0
    inactivity_timeout_sec: float = 1800.0
    turn_timeout_sec: float = 7200.0


def load_daemon_config(config_path: str = "") -> DaemonConfig:
    data = {}
    if config_path and os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}

    hostname = socket.gethostname()
    hub_ws_url = os.environ.get("PC_CLI_HUB_WS_URL") or data.get("hub_ws_url") or "ws://127.0.0.1:8889/ws/pc-cli/daemon"
    daemon_token = os.environ.get("PC_CLI_DAEMON_TOKEN") or data.get("daemon_token") or ""
    node_id = os.environ.get("PC_CLI_NODE_ID") or data.get("node_id") or hostname
    display_name = os.environ.get("PC_CLI_DISPLAY_NAME") or data.get("display_name") or node_id

    roots_env = os.environ.get("PC_CLI_WORKSPACE_ROOTS", "")
    if roots_env.strip():
        workspace_roots = [p.strip() for p in roots_env.split(os.pathsep) if p.strip()]
    else:
        workspace_roots = list(data.get("workspace_roots") or [os.path.expanduser("~")])

    extra_path = list(data.get("extra_path") or [])
    return DaemonConfig(
        hub_ws_url=hub_ws_url,
        daemon_token=daemon_token,
        node_id=node_id,
        display_name=display_name,
        workspace_roots=[os.path.realpath(os.path.expanduser(p)) for p in workspace_roots],
        codex_bin=os.environ.get("CODEX_EXECUTABLE") or data.get("codex_bin") or "codex",
        agy_bin=os.environ.get("ANTIGRAVITY_EXECUTABLE") or data.get("agy_bin") or "agy",
        extra_path=extra_path,
        heartbeat_interval_sec=float(data.get("heartbeat_interval_sec", 15.0)),
        inactivity_timeout_sec=float(data.get("inactivity_timeout_sec", 1800.0)),
        turn_timeout_sec=float(data.get("turn_timeout_sec", 7200.0)),
    )
