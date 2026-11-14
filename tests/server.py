"""Launch one API process for multi-process tests.

Usage: python tests/server.py

Environment:
* ``PORT``               - listening port
* ``ENERGY_DB_PATH``      - shared SQLite file
* ``ENERGY_TEST_EXIT_FILE`` - when this file appears, exit immediately without
  running shutdown handlers (simulates a process crash for failure-safety tests)
"""

from __future__ import annotations

import os
import sys
import threading
import time

import uvicorn


def _watch_exit_file(path: str) -> None:
    while not os.path.exists(path):
        time.sleep(0.05)
    os._exit(9)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    exit_file = os.environ.get("ENERGY_TEST_EXIT_FILE")
    if exit_file:
        threading.Thread(
            target=_watch_exit_file, args=(exit_file,), daemon=True
        ).start()
    port = int(os.environ.get("PORT", "8101"))
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
