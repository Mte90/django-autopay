"""Django-Bolt REST API for django-autopay.

Exposes REST endpoints for frontend SDKs and server-to-server calls.
Requires Python >= 3.12 (django-bolt floor).
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 12):  # pragma: no cover
    raise ImportError(
        "autopay.api requires Python >= 3.12 (django-bolt floor). "
        "Install without [api] extra or upgrade Python."
    )

from .bolt import api
from .schemas import (
    ChargeIn,
    ChargeOut,
    ConfirmRefreshIn,
    ConfirmRefreshOut,
    CreateMethodIn,
    CreateMethodOut,
    RefreshMethodIn,
    RefreshMethodOut,
    SavedMethodOut,
    ValidationResultOut,
    WebhookReceiverOut,
)

__all__ = [
    "ChargeIn",
    "ChargeOut",
    "ConfirmRefreshIn",
    "ConfirmRefreshOut",
    "CreateMethodIn",
    "CreateMethodOut",
    "RefreshMethodIn",
    "RefreshMethodOut",
    "SavedMethodOut",
    "ValidationResultOut",
    "WebhookReceiverOut",
    "api",
]