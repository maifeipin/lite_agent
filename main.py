"""
Lite Agent 生产主入口 (兼容薄委托)。
规范实现位于 lite_agent.cli。
"""

import signal
import time
from lite_agent.cli import (
    Agent,
    parse_args,
    load_config,
    session_cleanup_task,
    _register_configured_cron_jobs,
    main,
)

if __name__ == "__main__":
    main()
