"""Centralized logging configuration for ml-source-bridge.

All components (`log()` calls, plus any `logging.getLogger(...)` users)
funnel through this. Output goes to BOTH:

  - the configured log file (default: /tmp/ml-source-bridge.log) -- so
    you can `tail -f` it from anywhere, survives Ctrl-C of the foreground
    process, and lives in tmpfs so doesn't wear the SD card
  - stderr -- so systemd / journald captures the same lines

The log file is opened in append mode and is safe to truncate / delete /
rotate while the process is running (Python's FileHandler reopens on the
next emit thanks to the underlying OS file descriptor semantics).

Format includes wall-clock time with millisecond precision plus the
calling logger's name, so per-component prefixing is automatic when
modules use `getLogger(__name__)`.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional


# Shared log path for the whole mdtv2 stack (broker + bridge etc.) so a
# single `tail -F /tmp/mdt.log` shows everything in chronological order.
# Lives in /tmp (tmpfs) -- doesn't wear the SD card and self-clears on
# reboot.
DEFAULT_LOG_FILE = "/tmp/mdt.log"
# Tag every line with the component so multi-process output is
# distinguishable when several daemons share the file.
DEFAULT_FORMAT   = "%(asctime)s.%(msecs)03d %(levelname)-5s [bridge] %(message)s"
DEFAULT_DATEFMT  = "%Y-%m-%d %H:%M:%S"


def setup(log_file: Optional[str] = DEFAULT_LOG_FILE,
          level: int = logging.INFO) -> None:
    """Configure the root logger.

    Idempotent -- safe to call multiple times (each call replaces existing
    handlers). Pass `log_file=None` to disable file logging entirely (only
    stderr).
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Drop existing handlers from any prior call.
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    fmt = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATEFMT)

    if log_file:
        try:
            fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            print(f"[logging] couldn't open {log_file!r}: {e}",
                  file=sys.stderr)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
