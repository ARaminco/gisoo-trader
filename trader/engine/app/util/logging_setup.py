import logging
import os
import sys


def setup_logging(name: str = "trader") -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "websockets.client", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name)
