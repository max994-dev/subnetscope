"""Rotating-file + console logging."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .config import LoggingCfg


def setup_logging(cfg: LoggingCfg) -> None:
    log_path = Path(cfg.file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(cfg.level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)

    file_h = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=cfg.rotate_max_bytes, backupCount=cfg.rotate_backups,
        encoding="utf-8",
    )
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    # Stderr console handler — keeps stdout clean for table/json/csv pipes.
    import sys
    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(fmt)
    console.setLevel(logging.WARNING)
    root.addHandler(console)
