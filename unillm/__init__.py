"""
UniLLM - Unified LLM Proxy

A minimal LLM proxy supporting Vertex AI Gemini via application default credentials.
"""

__version__ = "0.1.0"

# Core settings
drop_params: bool = True
request_timeout: float = 600.0

# Logging
from unillm._logging import verbose_logger, verbose_proxy_logger

__all__ = [
    "__version__",
    "drop_params",
    "request_timeout",
    "verbose_logger",
    "verbose_proxy_logger",
]
