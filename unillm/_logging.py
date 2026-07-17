"""
Logging configuration for UniLLM
"""

import logging
import sys

# Create loggers
verbose_logger = logging.getLogger("unillm")
verbose_proxy_logger = logging.getLogger("unillm.proxy")

# Default configuration
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)

# Attach the handler only to the parent "unillm" logger. "unillm.proxy" propagates
# to it; giving the child its own handler printed every proxy log line twice.
verbose_logger.addHandler(_handler)

# Set default level to WARNING
verbose_logger.setLevel(logging.WARNING)
verbose_proxy_logger.setLevel(logging.WARNING)


def set_verbose(enable: bool = True):
    """Enable or disable verbose logging"""
    level = logging.DEBUG if enable else logging.WARNING
    verbose_logger.setLevel(level)
    verbose_proxy_logger.setLevel(level)
