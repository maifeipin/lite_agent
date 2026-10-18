#!/usr/bin/env python3
"""Review room v2 CLI wrapper.

Thin CLI entrypoint that delegates domain logic and database operations
to features.review_meeting.room.
"""
from __future__ import annotations

import sys
import os
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from lite_agent.features.review_meeting import room
from lite_agent.features.review_meeting.room import *  # noqa: F401, F403 - backwards compatibility

if __name__ == "__main__":
    raise SystemExit(room.main())
