"""Saved method types for django-autopay."""

from dataclasses import dataclass
from typing import Protocol


class SavedMethodProtocol(Protocol):
    """Protocol that any 'saved method' model must implement."""

    gateway: str                  # "paypal" | "mollie" | "stripe"
    gateway_customer_id: str      # Customer ID on gateway
    gateway_method_id: str        # Token/mandate ID on gateway
    method_type: str              # "card" | "paypal" | "sepa" | "ideal"
    is_active: bool
    metadata: dict                # brand, last4, expiry, fingerprint, etc.


@dataclass
class SavedMethod:
    """Dataclass to pass to provider methods.

    Built from our SavedPaymentMethod model or from a custom user model.
    """

    gateway: str
    gateway_customer_id: str
    gateway_method_id: str
    method_type: str
    display_label: str = ""
    is_active: bool = True
    metadata: dict | None = None
    id: int | None = None  # PK del SavedPaymentMethod locale, opzionale