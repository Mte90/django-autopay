"""Mollie provider for django-autopay.

Implements customers/mandates, recurring payments, and refresh flow.
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.utils import timezone as django_timezone
from mollie.api.client import Client as MollieClient

from autopay.core.base import AutopayProvider
from autopay.core.result import (
    ChargeResult,
    ChargeStatus,
    RefreshSetupData,
    ValidationResult,
    WebhookEventResult,
)
from autopay.core.saved_method import SavedMethod

logger = logging.getLogger(__name__)


class MollieAutopayProvider(AutopayProvider):
    """Mollie provider with customers/mandates for recurring payments.

    Extends MollieProvider (community) with customer/mandate/recurring.
    Uses official mollie-api-python SDK for API calls.
    """

    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_LIST_METHODS = True
    SUPPORTS_REVOKE = True
    SUPPORTS_VALIDATE = True
    SUPPORTS_REFRESH = True
    SUPPORTS_REFUND = True
    SUPPORTS_WEBHOOK_VERIFICATION = True  # no signature; verifies via optional IP allowlist

    def __init__(
        self,
        api_key: str = "",
        access_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        testmode: bool = False,
        webhook_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Mollie provider.

        Args:
            api_key: Mollie API key (testmode or live)
            access_token: OAuth2 access token (alternative to api_key)
            client_id: OAuth2 client ID (alternative to api_key)
            client_secret: OAuth2 client secret (alternative to api_key)
            testmode: If True, uses Mollie sandbox
            webhook_url: URL for Mollie webhooks (optional)
        """
        self.api_key = api_key
        self.access_token = access_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.testmode = testmode
        self.webhook_url = webhook_url

        # Initialize the Mollie API client
        self.client = MollieClient()
        if api_key:
            self.client.set_api_key(api_key)
        elif access_token:
            self.client.set_access_token(access_token)
        elif client_id and client_secret:
            self.client.set_oauth2_client_credentials(client_id, client_secret)

        if testmode:
            self.client.set_testmode(True)

    # -- capability: save_method ------------------------------------------

    def save_method(self, payment) -> SavedMethod:
        """Save the method after a successful 'first' payment.

        Precondition: payment.transaction_id must be a Mollie payment
        with sequenceType='first' and status='paid'. We retrieve the mandates.
        """
        if not payment.transaction_id:
            raise ValueError("payment.transaction_id required")

        mollie_payment = self.client.payments.get(payment.transaction_id)
        if not getattr(mollie_payment, "is_paid", lambda: False)():
            raise RuntimeError(f"Mollie payment {payment.transaction_id} is not paid")

        customer_id = getattr(mollie_payment, "customerId", None)
        if not customer_id:
            raise RuntimeError("Mollie payment has no customerId — first payment needs customerId")

        # Fetch mandates
        mandates = list(self.client.customer_mandates.list_for(customer_id))
        valid = [m for m in mandates if getattr(m, "status", "") == "valid"]

        if not valid:
            raise RuntimeError("No valid mandate after first payment")

        mandate = valid[0]
        return self._mandate_to_saved_method(customer_id, mandate)

    def _mandate_to_saved_method(self, customer_id: str, mandate) -> SavedMethod:
        """Convert a Mollie mandate to SavedMethod."""
        details = dict(getattr(mandate, "details", {}) or {})
        method = getattr(mandate, "method", "")

        if method == "creditcard":
            method_type = "card"
            card_label = details.get("cardLabel", "CARD")
            card_number = details.get("cardNumber", "")
            display_label = f"{card_label} •••• {card_number[-4:]}" if card_number else card_label
        elif method == "directdebit":
            method_type = "sepa"
            consumer_account = details.get("consumerAccount", "")
            display_label = f"IBAN •••• {consumer_account[-4:]}" if consumer_account else "SEPA"
        elif method == "paypal":
            method_type = "paypal"
            display_label = f"PayPal ({details.get('consumerAccount', '')})"
        else:
            method_type = method
            display_label = method

        return SavedMethod(
            gateway="mollie",
            gateway_customer_id=customer_id,
            gateway_method_id=getattr(mandate, "id", ""),
            method_type=method_type,
            display_label=display_label,
            metadata={
                "method": method,
                "signature_date": getattr(mandate, "signatureDate", None),
                "details": details,
            },
        )

    # -- capability: charge_off_session -----------------------------------

    def charge_off_session(
        self,
        saved_method: SavedMethod,
        amount: Decimal,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> ChargeResult:
        """POST /v2/payments with sequenceType='recurring'."""
        amount_str = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        payload = {
            "amount": {"currency": currency, "value": amount_str},
            "description": (description[:255] if description else "Recurring charge"),
            "sequenceType": "recurring",
            "customerId": saved_method.gateway_customer_id,
            "mandateId": saved_method.gateway_method_id,
            "metadata": metadata or {},
        }

        if self.webhook_url:
            payload["webhookUrl"] = self.webhook_url

        try:
            mollie_payment = self.client.payments.create(payload)
        except Exception as e:
            logger.exception("Mollie API error during charge")
            return ChargeResult(
                status=ChargeStatus.FAILED,
                message=f"Mollie API error: {e}",
                transaction_id=None,
                raw_response={"error": str(e)},
                amount_charged=None,
                currency=currency,
                requires_action=None,
                created_at=django_timezone.now(),
            )

        return self._parse_mollie_payment(mollie_payment)

    def _parse_mollie_payment(self, mollie_payment) -> ChargeResult:
        """Parse a Mollie payment response and return ChargeResult."""
        now = django_timezone.now()

        status = getattr(mollie_payment, "status", "")
        transaction_id = getattr(mollie_payment, "id", "")
        amount_data = getattr(mollie_payment, "amount", None)
        amount_value = amount_data.get("value") if amount_data else None
        currency = amount_data.get("currency") if amount_data else None

        # Helper methods check
        is_paid = getattr(mollie_payment, "is_paid", lambda: False)()
        is_pending = getattr(mollie_payment, "is_pending", lambda: False)()
        is_open = getattr(mollie_payment, "is_open", lambda: False)()
        is_failed = getattr(mollie_payment, "is_failed", lambda: False)()
        is_canceled = getattr(mollie_payment, "is_canceled", lambda: False)()
        is_expired = getattr(mollie_payment, "is_expired", lambda: False)()

        if is_paid:
            return ChargeResult(
                status=ChargeStatus.CONFIRMED,
                message="Charged successfully",
                transaction_id=transaction_id,
                raw_response=dict(mollie_payment),
                amount_charged=Decimal(amount_value) if amount_value else None,
                currency=currency,
                requires_action=None,
                created_at=now,
            )
        elif is_pending or is_open:
            return ChargeResult(
                status=ChargeStatus.PENDING,
                message=f"Mollie status={status}, processing",
                transaction_id=transaction_id,
                raw_response=dict(mollie_payment),
                amount_charged=None,
                currency=currency,
                requires_action=None,
                created_at=now,
            )
        elif is_failed:
            details = getattr(mollie_payment, "details", None) or {}
            reason = details.get("failureReason", "")

            if reason == "possible_fraud":
                return ChargeResult(
                    status=ChargeStatus.REJECTED,
                    message=f"Mollie rejected mandate: {reason}",
                    transaction_id=transaction_id,
                    raw_response=dict(mollie_payment),
                    amount_charged=None,
                    currency=currency,
                    requires_action=None,
                    created_at=now,
                )
            return ChargeResult(
                status=ChargeStatus.FAILED,
                message=f"Mollie failed: {reason}",
                transaction_id=transaction_id,
                raw_response=dict(mollie_payment),
                amount_charged=None,
                currency=currency,
                requires_action=None,
                created_at=now,
            )
        elif is_canceled or is_expired:
            return ChargeResult(
                status=ChargeStatus.FAILED,
                message=f"Mollie {status}",
                transaction_id=transaction_id,
                raw_response=dict(mollie_payment),
                amount_charged=None,
                currency=currency,
                requires_action=None,
                created_at=now,
            )
        else:
            return ChargeResult(
                status=ChargeStatus.PENDING,
                message=f"Mollie status={status}",
                transaction_id=transaction_id,
                raw_response=dict(mollie_payment),
                amount_charged=None,
                currency=currency,
                requires_action=None,
                created_at=now,
            )

    # -- capability: get_charge_status (polling) --------------------------

    def get_charge_status(self, transaction_id: str) -> ChargeResult:
        """GET /v2/payments/<id> to poll the status."""
        mollie_payment = self.client.payments.get(transaction_id)
        return self._parse_mollie_payment(mollie_payment)

    # -- capability: list_saved_methods -----------------------------------

    def list_saved_methods(self, customer_id: str) -> list[SavedMethod]:
        """GET /v2/customers/<id>/mandates."""
        if not customer_id:
            return []

        mandates = list(self.client.customer_mandates.list_for(customer_id))
        return [
            self._mandate_to_saved_method(customer_id, m)
            for m in mandates
            if getattr(m, "status", "") == "valid"
        ]

    # -- capability: revoke_method ----------------------------------------

    def revoke_method(self, saved_method: SavedMethod) -> None:
        """DELETE /v2/customers/<id>/mandates/<mandate_id>."""
        self.client.customer_mandates.delete(
            saved_method.gateway_customer_id,
            saved_method.gateway_method_id,
        )

    # -- capability: validate_method --------------------------------------

    def validate_method(self, saved_method: SavedMethod) -> ValidationResult:
        """Mollie: customer_mandates.get(customer_id, mandate_id). No charge."""
        from mollie.api.error import Error as MollieError

        now = datetime.now(timezone.utc)

        try:
            mandate = self.client.customer_mandates.get(
                saved_method.gateway_customer_id,
                saved_method.gateway_method_id,
            )
        except MollieError as e:
            logger.warning("Mollie mandate validation error: %s", e)
            return ValidationResult(
                is_valid=False,
                is_expired=False,
                needs_refresh=True,
                reason=f"Mandate not found: {e}",
                checked_at=now,
                raw_response={"error": str(e)},
            )

        raw = dict(mandate)
        status = getattr(mandate, "status", "")

        if status == "valid":
            return ValidationResult(
                is_valid=True,
                is_expired=False,
                needs_refresh=False,
                reason="Valid mandate",
                checked_at=now,
                raw_response=raw,
            )
        elif status == "pending":
            return ValidationResult(
                is_valid=False,
                is_expired=False,
                needs_refresh=False,
                reason="Mandate awaiting first payment confirmation",
                checked_at=now,
                raw_response=raw,
            )
        else:  # "invalid"
            return ValidationResult(
                is_valid=False,
                is_expired=False,
                needs_refresh=True,
                reason="Mandate invalid (revoked by customer or bank)",
                checked_at=now,
                raw_response=raw,
            )

    # -- capability: refresh_method + confirm_refresh ---------------------

    def refresh_method(self, saved_method: SavedMethod, currency: str = "EUR", description: str = "", amount: str = "0.01") -> RefreshSetupData:
        """Mollie: new first payment → new mandate.

        Args:
            saved_method: The saved method to refresh
            currency: Currency for the verification payment (default EUR)
            description: Description for the payment (default: method-specific)
            amount: Amount string for the payment (default "0.01" for non-card, "0.00" for card)

        For card mandates (sequenceType='first'), zero-amount authorizations are supported.
        For SEPA and other methods, use 0.01 to avoid rejection.

        Only gateway calls: persistence is in the facade.
        """
        from django.conf import settings

        base_url = getattr(settings, "AUTOPAY_SITE_BASE_URL", "http://localhost")

        # Default description if not provided
        if not description:
            description = "Mandate verification"

        payload = {
            "amount": {"currency": currency, "value": amount},
            "description": (description[:255] if description else "Mandate verification"),
            "redirectUrl": f"{base_url}/autopay/api/methods/{saved_method.id}/refresh/confirm/",
            "sequenceType": "first",
            "customerId": saved_method.gateway_customer_id,
        }

        if self.webhook_url:
            payload["webhookUrl"] = self.webhook_url

        mollie_payment = self.client.payments.create(payload)

        return RefreshSetupData(
            gateway_setup_id=getattr(mollie_payment, "id", ""),
            frontend_sdk="mollie",
            frontend_payload={
                "payment_url": getattr(mollie_payment, "checkout_url", ""),
                "payment_id": getattr(mollie_payment, "id", ""),
            },
            expires_at=django_timezone.now() + timedelta(hours=1),
        )

    def confirm_refresh(self, saved_method: SavedMethod, refresh_payload: dict) -> SavedMethod:
        """Confirm refresh after user completes the first payment.

        Resolves first_payment_id from:
        1. refresh_payload (first_payment_id or payment_id)
        2. Fallback: stored mandate data in saved_method

        Args:
            saved_method: The saved method being refreshed
            refresh_payload: Dict containing first_payment_id or payment_id

        Returns:
            Updated SavedMethod with new mandate data

        Raises:
            RuntimeError: If first_payment_id cannot be resolved
        """
        first_payment_id = refresh_payload.get("first_payment_id") or refresh_payload.get("payment_id")

        # Fallback: derive from saved_method if not in payload
        if not first_payment_id:
            # Check if saved_method has stored mandate/reference data
            metadata = getattr(saved_method, "metadata", {})
            if metadata and "first_payment_reference" in metadata:
                first_payment_id = metadata["first_payment_reference"]
            elif saved_method.gateway_method_id:
                # For Mollie, the gateway_method_id is the mandate ID
                # We need to fetch the customer's mandates to find the first payment
                customer_id = saved_method.gateway_customer_id
                if customer_id:
                    try:
                        mandates = list(self.client.customer_mandates.list_for(customer_id))
                        if mandates:
                            # Use the first valid mandate's ID as reference
                            first_valid = next((m for m in mandates if getattr(m, "status", "") == "valid"), None)
                            if first_valid:
                                first_payment_id = getattr(first_valid, "id", "")
                    except Exception:
                        logger.debug("Could not resolve first_payment_id from mandates", exc_info=True)

        if not first_payment_id:
            raise RuntimeError("Cannot resolve first payment id for Mollie refresh")

        mollie_payment = self.client.payments.get(first_payment_id)
        if not getattr(mollie_payment, "is_paid", lambda: False)():
            raise RuntimeError(f"First payment {first_payment_id} is not paid: {mollie_payment.status}")

        # Retrieve the new mandate (Mollie creates it on first payment)
        customer_id = saved_method.gateway_customer_id
        mandates = list(self.client.customer_mandates.list_for(customer_id))
        new_mandates = [
            m
            for m in mandates
            if getattr(m, "status", "") == "valid" and getattr(m, "id", "") != saved_method.gateway_method_id
        ]

        if not new_mandates:
            raise RuntimeError("No new valid mandate found after first payment")

        new_mandate = new_mandates[0]

        # Revoke the old mandate (ignore errors)
        try:
            self.client.customer_mandates.delete(customer_id, saved_method.gateway_method_id)
        except Exception:
            logger.debug("Old mandate already deleted or error during deletion", exc_info=True)

        return self._mandate_to_saved_method(customer_id, new_mandate)

    # -- webhook (optional, no signature) --------------------------------

    def verify_webhook_request(self, raw_body: bytes, headers: dict) -> bool:
        """Verify a Mollie webhook (no signature — optional IP allowlist).

        When AUTOPAY_MOLLIE_ALLOWED_IPS is configured, the sender IP must be
        present (REMOTE_ADDR header key) and listed, otherwise the request is
        rejected. With no allowlist configured, any request whose payload has
        an 'id' field is trusted.

        Args:
            raw_body: The raw bytes received in the request body
            headers: Case-insensitive header mapping (REMOTE_ADDR may be included)

        Returns:
            True if the request is trusted (IP allowed and payload has an id field)
        """
        from urllib.parse import parse_qs

        from django.conf import settings

        allowed_ips = getattr(settings, "AUTOPAY_MOLLIE_ALLOWED_IPS", [])
        if allowed_ips and headers.get("REMOTE_ADDR", "") not in allowed_ips:
            return False

        # Mollie POSTs form-encoded (id=tr_xxx); behind proxy may arrive as JSON.
        import json

        content_type = headers.get("Content-Type", headers.get("content-type", ""))
        if content_type.startswith("application/json"):
            try:
                data = json.loads(raw_body or b"{}")
            except (json.JSONDecodeError, ValueError):
                return False
        else:
            data = {k: v[0] for k, v in parse_qs(raw_body.decode("utf-8", "replace")).items()}

        return bool(data.get("id"))

    def process_webhook_event(self, event_type: str, payload: dict) -> WebhookEventResult | None:
        """Process Mollie webhook event.

        For Mollie, the event is a 'payment.id'. Fetch payment and parse status.
        """
        payment_id = payload.get("id")
        if not payment_id:
            return WebhookEventResult(processed=False, action="ignored", transaction_id=None)

        try:
            mollie_payment = self.client.payments.get(payment_id)
            result = self._parse_mollie_payment(mollie_payment)
            return WebhookEventResult(
                processed=True,
                action="status_update",
                transaction_id=payment_id,
                new_status=result.status,
                raw=result.raw_response,
            )
        except Exception as e:
            logger.exception("Mollie webhook processing error")
            return WebhookEventResult(
                processed=False,
                action="error",
                transaction_id=payment_id if payment_id else None,
                error=str(e),
            )

    def refund(self, transaction_id: str, amount: Decimal | None = None, reason: str = "") -> ChargeResult:
        """Refund a Mollie payment.

        Args:
            transaction_id: The payment ID to refund
            amount: Partial refund amount (None for full refund)
            reason: Refund reason (stored in metadata)

        Returns:
            ChargeResult with refund status
        """
        from django.utils import timezone as django_timezone

        try:
            payment = self.client.payments.get(transaction_id)
            amount_data = getattr(payment, "amount", None)
            payment_currency = amount_data.get("currency") if amount_data else "EUR"
            payment_amount = Decimal(amount_data.get("value", "0")) if amount_data else Decimal(0)

            # Determine refund amount
            refund_amount = amount if amount is not None else payment_amount
            refund_amount_str = str(refund_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

            # Create refund
            refund_payload = {
                "amount": {"currency": payment_currency, "value": refund_amount_str},
            }
            if reason:
                refund_payload["description"] = reason[:255]

            refund = self.client.payments_refunds.create(transaction_id, refund_payload)

            return ChargeResult(
                status=ChargeStatus.CONFIRMED,
                message="Refund processed successfully",
                transaction_id=getattr(refund, "id", ""),
                raw_response=dict(refund),
                amount_charged=refund_amount,
                currency=payment_currency,
                requires_action=False,
                created_at=django_timezone.now(),
            )
        except Exception as e:
            logger.exception("Mollie refund error")
            return ChargeResult(
                status=ChargeStatus.FAILED,
                message=f"Refund failed: {e}",
                transaction_id=transaction_id,
                raw_response={"error": str(e)},
                amount_charged=None,
                currency="",
                requires_action=False,
                created_at=django_timezone.now(),
            )