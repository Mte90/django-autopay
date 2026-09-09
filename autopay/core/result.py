"""Core result types for django-autopay."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal


class ChargeStatus(str, Enum):
    """Status of an off-session charge."""

    CONFIRMED = "confirmed"        # charge succeeded
    PENDING = "pending"            # in progress, you must sync later
    FAILED = "failed"              # definitively failed, no retry needed
    REQUIRES_SCA = "requires_sca"  # needs 3DS, user must be on-session
    REJECTED = "rejected"          # mandate/token revoked, skip the method


@dataclass
class ChargeResult:
    """Result of an off-session charge."""

    status: ChargeStatus          # CONFIRMED, PENDING, FAILED, REQUIRES_SCA, REJECTED
    message: str                   # human-readable
    transaction_id: str | None     # gateway-side ID (e.g. "PAYID-xxx", "tr_xxx", "pi_xxx")
    raw_response: dict             # full dump for audit/debug
    amount_charged: Decimal | None  # if CONFIRMED, the amount actually charged
    currency: str | None
    requires_action: dict | None   # if REQUIRES_SCA, info for the 3DS frontend
    created_at: datetime


@dataclass
class RefreshSetupData:
    """Returned by PROVIDER.refresh_method: gateway data only, no DB access.

    Persistence (RefreshAttempt) is in the facade Payments.refresh_method (§9.5bis)."""

    gateway_setup_id: str                  # setup_token_id / setup_intent_id / payment_id on gateway
    frontend_sdk: Literal["paypal", "stripe", "mollie", "adyen", ...]
    frontend_payload: dict                 # gateway-specific
    expires_at: datetime


@dataclass
class RefreshSetup:
    """Returned by FACADE Payments.refresh_method (§9.5bis)."""

    setup_id: int                          # PK of RefreshAttempt (0 if models not installed)
    frontend_sdk: Literal["paypal", "stripe", "mollie", "adyen", ...]
    frontend_payload: dict                 # gateway-specifico
    expires_at: datetime
    return_url: str                        # URL where the frontend returns after the flow


@dataclass
class ValidationResult:
    """Result of provider.validate_method(saved_method).

    Verify token/mandate status on the gateway WITHOUT charging.
    All 3 supported gateways expose a GET API that returns the current
    status of the payment token / mandate.
    """

    is_valid: bool                          # True = can be used for off-session charge
    is_expired: bool = False                # True = card expired (cards only)
    needs_refresh: bool = False             # True = invalid, needs refresh_method
    reason: str = ""                        # human-readable: "OK", "Card expired 11/2024", "Token revoked", ...
    checked_at: datetime | None = None      # UTC timestamp of verification
    raw_response: dict | None = None        # payload gateway per audit


@dataclass
class WebhookEventResult:
    """Result of webhook event processing."""

    processed: bool
    action: str  # "noop" | "revoke_method" | "refund" | "status_update" | "ignored" | "error"
    transaction_id: str | None
    event_id: str | None = None
    new_status: ChargeStatus | None = None
    raw: dict | None = None
    error: str | None = None