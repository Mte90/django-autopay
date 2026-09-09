"""Request/Response schemas for the django-bolt API.

Uses msgspec.Struct for type-safe request/response validation.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import msgspec


class SavedMethodOut(msgspec.Struct):
    """Response for SavedPaymentMethod."""

    id: int
    gateway: str  # "paypal" | "mollie" | "stripe"
    method_type: str  # "card" | "paypal" | "sepa"
    display_label: str
    is_default: bool
    is_active: bool
    metadata: dict[str, Any]
    created_at: str  # ISO 8601


class CreateMethodIn(msgspec.Struct):
    """Request to create a new saved method."""

    gateway: str  # "paypal" | "mollie" | "stripe"
    payment_token: str | None = None  # payment.id from django-payments checkout


class CreateMethodOut(msgspec.Struct):
    """Response for the frontend SDK: what to load."""

    setup_id: int
    frontend_sdk: str  # "paypal" | "stripe" | "mollie"
    frontend_payload: dict[str, Any]  # gateway-specific (client_secret, client_token, payment_url, ...)
    return_url: str


class RefreshMethodIn(msgspec.Struct):
    """Request to start a token refresh."""

    reason: str = "manual"  # "expired_card" | "revoked" | "sca_required" | "manual"


class RefreshMethodOut(msgspec.Struct):
    """Refresh response: data for the frontend SDK."""

    setup_id: int
    frontend_sdk: str
    frontend_payload: dict[str, Any]
    expires_at: str  # ISO 8601
    return_url: str


class ConfirmRefreshIn(msgspec.Struct):
    """Request to confirm a refresh.

    Varies by gateway:
    - PayPal: {setup_token_id, payment_token_id}
    - Stripe: {setup_intent_id}
    - Mollie: {first_payment_id} (via query string)
    """

    setup_token_id: str | None = None
    payment_token_id: str | None = None
    setup_intent_id: str | None = None
    first_payment_id: str | None = None


class ConfirmRefreshOut(msgspec.Struct):
    """Refresh confirmation response."""

    success: bool
    method: SavedMethodOut | None = None
    error: str | None = None


class ChargeIn(msgspec.Struct):
    """Request to create an off-session charge."""

    saved_method_id: int
    amount: Decimal
    currency: str = "EUR"
    description: str = ""
    idempotency_key: str | None = None
    metadata: dict[str, Any] | None = None


class ChargeOut(msgspec.Struct):
    """Charge response."""

    status: str  # "confirmed" | "pending" | "failed" | "requires_sca" | "rejected"
    message: str
    transaction_id: str | None
    amount_charged: str | None  # Decimal as string for precision
    currency: str | None
    requires_action: dict[str, Any] | None  # 3DS frontend payload
    created_at: str


class WebhookReceiverOut(msgspec.Struct):
    """Webhook receiver response."""

    processed: bool
    event_id: str | None


class ValidationResultOut(msgspec.Struct):
    """Response of POST /autopay/api/methods/{id}/validate/."""

    is_valid: bool
    is_expired: bool
    needs_refresh: bool
    reason: str
    checked_at: str  # ISO 8601
    method_id: int
    gateway: str  # echo of gateway dispatch (useful for UI debug)


class DeleteMethodOut(msgspec.Struct):
    """Response for DELETE /autopay/api/methods/{id}/."""

    success: bool
    deleted_method_id: int


class RefreshStatusOut(msgspec.Struct):
    """Response for GET /autopay/api/methods/{id}/refresh/confirm/."""

    success: bool
    method_id: int | None = None
    error: str | None = None