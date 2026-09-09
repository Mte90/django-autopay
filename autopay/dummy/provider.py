"""Dummy provider for testing and development.

Simulates charge off-session, refresh, and webhook events without network calls.
Useful for local development and testing webhook flows.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from autopay.core.base import AutopayProvider
from autopay.core.result import (
    ChargeResult,
    ChargeStatus,
    RefreshSetupData,
    ValidationResult,
    WebhookEventResult,
)
from autopay.core.saved_method import SavedMethod


class DummyAutopayProvider(AutopayProvider):
    """Dummy provider for test/dev. Simulates charge, refresh, webhook without network."""

    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_LIST_METHODS = True
    SUPPORTS_REVOKE = True
    SUPPORTS_VALIDATE = True
    SUPPORTS_REFRESH = True
    SUPPORTS_WEBHOOK_VERIFICATION = False  # Dummy does not verify signatures

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self._methods: dict[str, dict] = {}
        self._charges: dict[str, dict] = {}

    def charge_off_session(
        self,
        saved_method: SavedMethod,
        amount: Decimal,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> ChargeResult:
        """Simulate off-session charge. Default: success."""
        transaction_id = f"dummy_charge_{saved_method.gateway_method_id}_{datetime.now(timezone.utc).timestamp()}"
        
        # Simulate different outcomes based on method
        if "fail" in saved_method.gateway_method_id:
            return ChargeResult(
                status=ChargeStatus.FAILED,
                transaction_id=transaction_id,
                amount_charged=amount,
                currency=currency,
                message="Simulated failure",
                raw_response={"error": "simulated_failure"},
                requires_action=None,
                created_at=datetime.now(timezone.utc),
            )
        
        if "decline" in saved_method.gateway_method_id:
            return ChargeResult(
                status=ChargeStatus.REJECTED,
                transaction_id=transaction_id,
                amount_charged=amount,
                currency=currency,
                message="Simulated decline",
                raw_response={"error": "card_declined"},
                requires_action=None,
                created_at=datetime.now(timezone.utc),
            )

        self._charges[transaction_id] = {
            "amount": str(amount),
            "currency": currency,
            "status": "succeeded",
        }

        return ChargeResult(
            status=ChargeStatus.CONFIRMED,
            transaction_id=transaction_id,
            amount_charged=amount,
            currency=currency,
            message="Charge succeeded (dummy)",
            raw_response={"id": transaction_id, "status": "succeeded"},
            requires_action=None,
            created_at=datetime.now(timezone.utc),
        )

    def list_saved_methods(self, customer_id: str) -> list[SavedMethod]:
        """List saved methods (dummy)."""
        methods = []
        for method_id, data in self._methods.items():
            if data.get("customer_id") == customer_id:
                methods.append(
                    SavedMethod(
                        gateway="dummy",
                        gateway_customer_id=customer_id,
                        gateway_method_id=method_id,
                        method_type=data.get("type", "card"),
                        display_label=data.get("label", "Dummy Method"),
                        is_active=True,
                        metadata=data.get("metadata", {}),
                    )
                )
        return methods

    def revoke_method(self, saved_method: SavedMethod) -> None:
        """Revoke method (dummy)."""
        if saved_method.gateway_method_id in self._methods:
            del self._methods[saved_method.gateway_method_id]

    def validate_method(self, saved_method: SavedMethod) -> ValidationResult:
        """Validate method (dummy: always valid)."""
        return ValidationResult(
            is_valid=True,
            is_expired=False,
            needs_refresh=False,
            checked_at=datetime.now(timezone.utc),
        )

    def refresh_method(self, saved_method: SavedMethod, reason: str = "manual") -> RefreshSetupData:
        """Init refresh (dummy)."""
        setup_id = f"dummy_refresh_{saved_method.gateway_method_id}_{datetime.now(timezone.utc).timestamp()}"
        
        return RefreshSetupData(
            gateway_setup_id=setup_id,
            frontend_sdk="dummy",
            frontend_payload={"url": f"https://dummy.example.com/refresh/{setup_id}"},
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )

    def confirm_refresh(self, saved_method: SavedMethod, refresh_payload: dict) -> SavedMethod:
        """Confirm refresh (dummy: always success)."""
        # Update metadata
        if saved_method.gateway_method_id in self._methods:
            self._methods[saved_method.gateway_method_id]["metadata"] = refresh_payload.get("metadata", {})
            self._methods[saved_method.gateway_method_id]["last_refreshed_at"] = datetime.now(timezone.utc).isoformat()
        
        return saved_method

    def process_webhook_event(self, event_type: str, payload: dict) -> WebhookEventResult | None:
        """Process webhook event (dummy)."""
        if event_type == "charge.succeeded":
            return WebhookEventResult(
                processed=True,
                action="status_update",
                transaction_id=payload.get("transaction_id"),
                new_status=ChargeStatus.CONFIRMED,
            )
        elif event_type == "charge.failed":
            return WebhookEventResult(
                processed=True,
                action="status_update",
                transaction_id=payload.get("transaction_id"),
                new_status=ChargeStatus.FAILED,
            )
        elif event_type == "method.revoked":
            return WebhookEventResult(
                processed=True,
                action="revoke_method",
                transaction_id=payload.get("method_id"),
            )
        elif event_type == "unknown.event":
            return WebhookEventResult(
                processed=False,
                action="ignored",
                transaction_id=None,
            )
        # Default: return a processed result with noop action
        return WebhookEventResult(
            processed=True,
            action="noop",
            transaction_id=payload.get("id"),
        )