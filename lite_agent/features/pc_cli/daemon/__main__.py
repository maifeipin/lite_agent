"""
PC CLI 守护进程 CLI 启动入口。
用法：
  python -m lite_agent.features.pc_cli.daemon [--config /path/to/daemon.json]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .config import load_daemon_config
from .daemon import PcCliDaemon


def main() -> None:
    parser = argparse.ArgumentParser(description="Lite Agent PC CLI Daemon (Win/Mac)")
    parser.add_argument("--config", default="", help="可选本地 JSON 配置文件路径")
    parser.add_argument("--log-level", default="INFO", help="日志级别 (DEBUG/INFO/WARNING)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_daemon_config(args.config)
    daemon = PcCliDaemon(cfg)
    try:
        asyncio.run(daemon.run_forever())
    except KeyboardInterrupt:
        daemon.request_stop()


if __name__ == "__main__":
    main()
