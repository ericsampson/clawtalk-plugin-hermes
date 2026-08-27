"""ClawTalk REST SDK - public surface.

Usage::

    from .sdk import ClawTalkClient, ApiError
"""

from .client import ClawTalkClient
from .endpoints import (
    ENDPOINTS,
    IMPLEMENTED_ENDPOINTS,
    READ_ENDPOINTS,
    UNIMPLEMENTED_ENDPOINTS,
    Endpoint,
    resolve,
)
from .errors import ApiError

__all__ = [
    "ApiError",
    "ClawTalkClient",
    "ENDPOINTS",
    "Endpoint",
    "IMPLEMENTED_ENDPOINTS",
    "READ_ENDPOINTS",
    "UNIMPLEMENTED_ENDPOINTS",
    "resolve",
]
