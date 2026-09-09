"""django-autopay: recurring / off-session charge extension for django-payments.

Public exports are added as the core is implemented (design doc:
``piano-implementazione-django-autopay.md``).
"""
from autopay.core.base import AutopayCapabilityError, is_autopay_capable
from autopay.core.facade import Payments
from autopay.core.result import ChargeResult, ChargeStatus

__all__ = [
    "AutopayCapabilityError",
    "ChargeResult",
    "ChargeStatus",
    "Payments",
    "is_autopay_capable",
]