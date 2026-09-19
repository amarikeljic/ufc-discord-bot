"""Refuse to start a second copy of the bot against the same database.

Two copies sharing a token both edit the same board messages, which trips
Discord's rate limits, and both post every live update twice.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO

# Windows can take a moment to release a dead process's lock, so a quick
# restart retries briefly before concluding another copy is running.
ATTEMPTS = 6
RETRY_DELAY = 0.5


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        last_error: OSError | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                time.sleep(RETRY_DELAY)
            try:
                self._handle = self._try_lock()
                return  # the OS releases the lock when the process exits, however it exits
            except OSError as exc:
                last_error = exc
        raise AlreadyRunning(
            f"Another copy of the bot is already running (lock held on {self.path}). "
            "Stop it before starting a new one."
        ) from last_error

    def _try_lock(self) -> IO:
        # noqa below: the handle must NOT be closed. It is the lock; a context
        # manager would release it and let a second copy of the bot start.
        handle = open(self.path, "a+")  # noqa: SIM115
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise
        return handle
