"""
Logging for the marking path (D1.1).

One logger, "exammanager.marking", used by the marker, the queue and the key
rotator. Messages go to the console (same text the old print() calls
produced) and to a rotating file at logs/marking.log (5 MB x 5 files).
Idempotent: calling setup_marking_logging() more than once is harmless.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "exammanager.marking"
_LOG_DIR = Path(__file__).parent / "logs"

_configured = False


def setup_marking_logging(log_dir: Path = None) -> logging.Logger:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger
    _configured = True

    logger.setLevel(logging.INFO)
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    try:
        log_dir = Path(log_dir or _LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "marking.log", maxBytes=5 * 1024 * 1024,
                                 backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
        logger.addHandler(fh)
    except OSError as e:
        logger.warning(f"Could not open logs/marking.log ({e}) — logging to console only.")

    return logger


def get_marking_logger() -> logging.Logger:
    return setup_marking_logging()
