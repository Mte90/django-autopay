"""Stripe provider for django-autopay.

Implements PaymentIntent off-session charges + SetupIntent for token refresh.
Uses Stripe SDK v15+ with per-instance clients (no global stripe.api_key).
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, ClassVar

import stripe
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone as django_timezone
from payments.stripe.providers import StripeProviderV3

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


class StripeAutopayProvider(AutopayProvider, StripeProviderV3):
    """Stripe provider with autopay support (off-session charges).

    Uses PaymentIntent for off-session charge and SetupIntent for token refresh.
    For refresh, AUTOPAY_STRIPE_PUBLISHABLE_KEY is required in settings.
    """

    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_LIST_METHODS = True
    SUPPORTS_REVOKE = True
    SUPPORTS_VALIDATE = True
    SUPPORTS_REFRESH = True
    SUPPORTS_WEBHOOK_VERIFICATION = True

    # Zero-decimal currencies (1 minor unit = 1 whole unit)
    ZERO_DECIMAL_CURRENCIES: ClassVar[set[str]] = {
        "JPY",
        "KRW",
        "VND",
        "CLP",
        "XOF",
        "XAF",
        "XPF",
        "BIF",
        "DJF",
        "GNF",
        "KMF",
        "MGA",
        "PYG",
        "RWF",
        "UGX",
        "VUV",
    }

    def __init__(
        self,
        api_key: str,
        use_token: bool = True,
        endpoint_secret: str | None = None,
        timeout: int = 30,
        **kwargs: Any,
    ) -> None:
        """Initialize the Stripe provider.

        Args:
            api_key: Stripe secret key (sk_test_... or sk_live_...)
            use_token: If True, uses token for requests (default)
            endpoint_secret: Webhook signing secret for verify_webhook_request
            timeout: Timeout in seconds for HTTP requests
        """
        super().__init__(api_key=api_key, use_token=use_token)
        self.endpoint_secret = endpoint_secret
        self.timeout = timeout
        # Client per-istanza (stripe>=8): NON impostare stripe.api_key globale
        self.stripe_client = stripe.StripeClient(api_key)

    @property
    def publishable_key(self) -> str:
        """Publishable key for the frontend (client_secret flow for refresh).

        Do NOT derive from the secret key: sk_ → pk_ is not a valid
        transformation — the pk must be in separate settings.
        """
        from django.conf import settings

        key = getattr(settings, "AUTOPAY_STRIPE_PUBLISHABLE_KEY", "")
        if not key:
            raise ImproperlyConfigured(
                "AUTOPAY_STRIPE_PUBLISHABLE_KEY is required for Stripe refresh"
            )
        return key

    @classmethod
    def _minor_to_amount(cls, minor: int, currency: str) -> Decimal:
        """Converts Stripe minor units to Decimal in the currency."""
        if currency.upper() in cls.ZERO_DECIMAL_CURRENCIES:
            return Decimal(minor)
        return (Decimal(minor) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @classmethod
    def _to_minor_units(cls, amount: Decimal, currency: str) -> int:
        """Converts a Decimal in the currency to Stripe minor units.

        int(amount * 100) on float is forbidden: representation errors
        (e.g., 8.40 → 839). Always use Decimal + quantize.
        """
        if currency.upper() in cls.ZERO_DECIMAL_CURRENCIES:
            return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    def _pm_to_saved_method(self, customer_id: str, pm) -> SavedMethod:
        """Converte un PaymentMethod Stripe in SavedMethod."""
        if pm.type == "card":
            card = pm.card
            method_type = "card"
            display_label = f"{card.brand.title()} •••• {card.last4}"
            metadata = {
                "brand": card.brand,
                "last4": card.last4,
                "exp_month": card.exp_month,
                "exp_year": card.exp_year,
                "fingerprint": card.fingerprint,
            }
        elif pm.type == "sepa_debit":
            method_type = "sepa"
            display_label = f"IBAN •••• {pm.sepa_debit.last4}"
            metadata = {"last4": pm.sepa_debit.last4, "mandate": pm.sepa_debit.mandate_id}
        elif pm.type == "paypal":
            method_type = "paypal"
            payer_email = getattr(pm.paypal, "payer_email", "") or ""
            display_label = f"PayPal ({payer_email})"
            metadata = {"email": payer_email}
        else:
            method_type = pm.type
            display_label = pm.type
            metadata = {}

        return SavedMethod(
            gateway="stripe",
            gateway_customer_id=customer_id,
            gateway_method_id=pm.id,
            method_type=method_type,
            display_label=display_label,
            metadata=metadata,
        )

    def charge_off_session(
        self,
        saved_method: SavedMethod,
        amount: Decimal,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> ChargeResult:
        """Charge off-session via PaymentIntent.

        Args:
            saved_method: saved payment method
            amount: Amount to charge (Decimal)
            currency: currency (ISO 4217, e.g. EUR, USD, JPY)
            description: optional description (max 500 chars)
            idempotency_key: idempotency key to prevent double charging
            metadata: optional metadata to attach to the PaymentIntent

        Returns:
            ChargeResult with status CONFIRMED, REQUIRES_SCA, PENDING, FAILED, or REJECTED
        """
        try:
            params: dict[str, Any] = {
                "amount": self._to_minor_units(amount, currency),
                "currency": currency.lower(),
                "customer": saved_method.gateway_customer_id,
                "payment_method": saved_method.gateway_method_id,
                "off_session": True,
                "confirm": True,
            }
            if description:
                params["description"] = description[:500]
            if metadata:
                params["metadata"] = metadata

            intent = self.stripe_client.v1.payment_intents.create(
                params,
                # WITHOUT idempotency_key a task retry can double-charge
                options={"idempotency_key": idempotency_key} if idempotency_key else None,
            )
        except stripe.error.CardError as e:
            # card_declined is a hard rejection, not a transient failure
            if e.code == "card_declined":
                return ChargeResult(
                    status=ChargeStatus.REJECTED,
                    message=str(e),
                    transaction_id=None,
                    raw_response=e.json_body or {},
                    amount_charged=None,
                    currency=currency,
                    requires_action=None,
                    created_at=django_timezone.now(),
                )
            # authentication_required also needs SCA
            if e.code == "authentication_required":
                client_secret = None
                if hasattr(e, "payment_intent") and e.payment_intent:
                    client_secret = getattr(e.payment_intent, "client_secret", None)
                return ChargeResult(
                    status=ChargeStatus.REQUIRES_SCA,
                    message="3DS required",
                    transaction_id=None,
                    raw_response=e.json_body or {},
                    amount_charged=None,
                    currency=currency,
                    requires_action={"type": "3ds", "client_secret": client_secret} if client_secret else {"type": "3ds"},
                    created_at=django_timezone.now(),
                )
            return ChargeResult(
                status=ChargeStatus.FAILED,
                message=str(e),
                transaction_id=None,
                raw_response=e.json_body or {},
                amount_charged=None,
                currency=currency,
                requires_action=None,
                created_at=django_timezone.now(),
            )
        except stripe.error.InvalidRequestError as e:
            # SCA flow: requires_action / requires_source_action / authentication_required
            if e.code in ("requires_action", "requires_source_action", "authentication_required") or "requires_action" in str(e):
                # Try to get client_secret from the error or by retrieving the PaymentIntent
                client_secret = None
                if hasattr(e, "payment_intent") and e.payment_intent:
                    client_secret = getattr(e.payment_intent, "client_secret", None)
                return ChargeResult(
                    status=ChargeStatus.REQUIRES_SCA,
                    message="3DS required",
                    transaction_id=None,
                    raw_response=e.json_body or {},
                    amount_charged=None,
                    currency=currency,
                    requires_action={"type": "3ds", "client_secret": client_secret} if client_secret else {"type": "3ds"},
                    created_at=django_timezone.now(),
                )
            raise
        except stripe.error.AuthenticationError as e:
            # 401 / invalid key → FAILED, not REJECTED
            return ChargeResult(
                status=ChargeStatus.FAILED,
                message=f"Stripe auth error: {e}",
                transaction_id=None,
                raw_response=e.json_body or {},
                amount_charged=None,
                currency=currency,
                requires_action=None,
                created_at=django_timezone.now(),
            )
        except stripe.error.StripeError as e:
            # Altri errori gateway
            return ChargeResult(
                status=ChargeStatus.FAILED,
                message=f"Stripe error: {e}",
                transaction_id=None,
                raw_response=e.json_body or {},
                amount_charged=None,
                currency=currency,
                requires_action=None,
                created_at=django_timezone.now(),
            )

        return self._parse_intent(intent)

    def _parse_intent(self, intent) -> ChargeResult:
        """Parsa un PaymentIntent in ChargeResult."""
        now = django_timezone.now()

        if intent.status == "succeeded":
            return ChargeResult(
                status=ChargeStatus.CONFIRMED,
                message="Charged successfully",
                transaction_id=intent.id,
                raw_response=intent.to_dict(),
                amount_charged=self._minor_to_amount(intent.amount, intent.currency),
                currency=intent.currency.upper(),
                requires_action=None,
                created_at=now,
            )
        elif intent.status == "requires_action":
            return ChargeResult(
                status=ChargeStatus.REQUIRES_SCA,
                message="3DS required",
                transaction_id=intent.id,
                raw_response=intent.to_dict(),
                amount_charged=None,
                currency=intent.currency.upper(),
                requires_action={
                    "type": "3ds",
                    "client_secret": intent.client_secret,
                    "next_action": intent.next_action,
                },
                created_at=now,
            )
        elif intent.status == "processing":
            return ChargeResult(
                status=ChargeStatus.PENDING,
                message="Processing",
                transaction_id=intent.id,
                raw_response=intent.to_dict(),
                amount_charged=None,
                currency=intent.currency.upper(),
                requires_action=None,
                created_at=now,
            )
        elif intent.status == "requires_payment_method":
            # Payment method no longer valid → REJECTED (non retryable)
            return ChargeResult(
                status=ChargeStatus.REJECTED,
                message="Payment method no longer valid",
                transaction_id=intent.id,
                raw_response=intent.to_dict(),
                amount_charged=None,
                currency=intent.currency.upper(),
                requires_action=None,
                created_at=now,
            )
        else:
            return ChargeResult(
                status=ChargeStatus.PENDING,
                message=f"Stripe status={intent.status}",
                transaction_id=intent.id,
                raw_response=intent.to_dict(),
                amount_charged=None,
                currency=intent.currency.upper(),
                requires_action=None,
                created_at=now,
            )

    def get_charge_status(self, transaction_id: str) -> ChargeResult:
        """Retrieve the status of a PaymentIntent."""
        intent = self.stripe_client.v1.payment_intents.retrieve(transaction_id)
        return self._parse_intent(intent)

    def list_saved_methods(self, customer_id: str) -> list[SavedMethod]:
        """List all saved payment methods for a customer."""
        pms = self.stripe_client.v1.payment_methods.list(
            {"customer": customer_id, "type": "card"}
        ).data
        return [self._pm_to_saved_method(customer_id, pm) for pm in pms]

    def revoke_method(self, saved_method: SavedMethod) -> None:
        """Detach a PaymentMethod from the customer."""
        self.stripe_client.v1.payment_methods.detach(saved_method.gateway_method_id)

    def validate_method(self, saved_method: SavedMethod) -> ValidationResult:
        """Validate a saved payment method."""
        now = django_timezone.now()

        try:
            pm = self.stripe_client.v1.payment_methods.retrieve(saved_method.gateway_method_id)
        except stripe.error.InvalidRequestError as e:
            # Token not found or revoked
            return ValidationResult(
                is_valid=False,
                is_expired=False,
                needs_refresh=True,
                reason=f"Payment method not found: {e}",
                checked_at=now,
                raw_response=e.json_body or {},
            )
        except stripe.error.AuthenticationError as e:
            return ValidationResult(
                is_valid=False,
                is_expired=False,
                needs_refresh=False,
                reason=f"Stripe auth error: {e}",
                checked_at=now,
                raw_response=e.json_body or {},
            )

        # Check expiry per carte
        is_expired = False
        if pm.type == "card":
            card = pm.card
            exp_year = card.exp_year
            exp_month = card.exp_month
            if exp_year and exp_month:
                exp_date = datetime(exp_year, exp_month, 1, tzinfo=timezone.utc).date()
                if exp_date < django_timezone.now().date().replace(day=1):
                    is_expired = True

        # Check if the payment method is still associated with the customer
        if pm.customer is None:
            return ValidationResult(
                is_valid=False,
                is_expired=False,
                needs_refresh=True,
                reason="Payment method detached from customer",
                checked_at=now,
                raw_response=pm.to_dict(),
            )

        if is_expired:
            return ValidationResult(
                is_valid=False,
                is_expired=True,
                needs_refresh=True,
                reason=f"Card expired {exp_month}/{exp_year}",
                checked_at=now,
                raw_response=pm.to_dict(),
            )

        return ValidationResult(
            is_valid=True,
            is_expired=False,
            needs_refresh=False,
            reason="Payment method valid",
            checked_at=now,
            raw_response=pm.to_dict(),
        )

    def refresh_method(self, saved_method: SavedMethod, reason: str = "manual") -> RefreshSetupData:
        """Create a new SetupIntent for token refresh.

        Only gateway calls: persistence in facade.
        """
        from autopay.core.result import RefreshSetupData

        intent = self.stripe_client.v1.setup_intents.create(
            {
                "customer": saved_method.gateway_customer_id,
                "usage": "off_session",
                "payment_method_types": ["card"],
                "metadata": {"saved_method_id": str(saved_method.id)} if saved_method.id else {},
            },
        )

        return RefreshSetupData(
            gateway_setup_id=intent.id,
            frontend_sdk="stripe",
            frontend_payload={
                "client_secret": intent.client_secret,
                "publishable_key": self.publishable_key,
                "setup_intent_id": intent.id,
            },
            expires_at=django_timezone.now() + timedelta(hours=1),
        )

    def confirm_refresh(self, saved_method: SavedMethod, refresh_payload: dict) -> SavedMethod:
        """Retrieve the PaymentMethod created by the SetupIntent and return updated data.

        Persistence in facade.
        """
        setup_intent_id = refresh_payload["setup_intent_id"]
        intent = self.stripe_client.v1.setup_intents.retrieve(setup_intent_id)

        if intent.status != "succeeded":
            raise RuntimeError(f"SetupIntent not succeeded: {intent.status}")

        new_pm_id = intent.payment_method
        pm = self.stripe_client.v1.payment_methods.retrieve(new_pm_id)

        # Detach the old PM
        try:
            self.stripe_client.v1.payment_methods.detach(saved_method.gateway_method_id)
        except Exception:
            logger.debug("Old payment method already detached", exc_info=True)

        metadata = dict(saved_method.metadata or {})
        display_label = saved_method.display_label

        if pm.type == "card":
            card = pm.card
            metadata.update(
                {
                    "brand": card.brand,
                    "last4": card.last4,
                    "exp_month": card.exp_month,
                    "exp_year": card.exp_year,
                    "fingerprint": card.fingerprint,
                }
            )
            display_label = f"{card.brand.title()} •••• {card.last4}"

        return SavedMethod(
            gateway=saved_method.gateway,
            gateway_customer_id=saved_method.gateway_customer_id,
            gateway_method_id=new_pm_id,
            method_type=saved_method.method_type,
            display_label=display_label,
            is_active=saved_method.is_active,
            metadata=metadata,
            id=saved_method.id,
        )

    def save_method(self, payment) -> SavedMethod:
        """Save the method after a successful on-session PaymentIntent.

        Retrieves the PaymentIntent, extracts the charge, and returns the
        attached payment method as a SavedMethod.
        """
        if not payment.transaction_id:
            raise ValueError("payment.transaction_id required to save method")

        try:
            intent = self.stripe_client.v1.payment_intents.retrieve(payment.transaction_id)
        except stripe.error.InvalidRequestError as e:
            raise RuntimeError(f"Failed to retrieve PaymentIntent: {e}")

        # Get the charge from the PaymentIntent
        charge = None
        if hasattr(intent, "charges") and intent.charges and len(intent.charges.data) > 0:
            charge = intent.charges.data[0]
        elif hasattr(intent, "latest_charge") and intent.latest_charge:
            charge = intent.latest_charge

        if not charge:
            raise RuntimeError("No charge found on PaymentIntent")

        # Get the payment method from the charge
        pm_id = getattr(charge, "payment_method", None)
        if not pm_id:
            raise RuntimeError("Payment has no attached payment method")

        try:
            pm = self.stripe_client.v1.payment_methods.retrieve(pm_id)
        except stripe.error.InvalidRequestError as e:
            raise RuntimeError(f"Failed to retrieve payment method: {e}")

        # Determine customer ID
        customer_id = None
        if hasattr(pm, "customer") and pm.customer:
            customer_id = pm.customer
        elif hasattr(charge, "customer") and charge.customer:
            customer_id = charge.customer

        if not customer_id:
            raise RuntimeError("No customer ID found on payment method or charge")

        # Build display label and metadata based on payment method type
        if pm.type == "card":
            card = pm.card
            method_type = "card"
            display_label = f"{card.brand.title()} •••• {card.last4}"
            metadata = {
                "brand": card.brand,
                "last4": card.last4,
                "exp_month": card.exp_month,
                "exp_year": card.exp_year,
                "fingerprint": card.fingerprint,
            }
        elif pm.type == "sepa_debit":
            method_type = "sepa"
            display_label = f"IBAN •••• {pm.sepa_debit.last4}"
            metadata = {"last4": pm.sepa_debit.last4, "mandate": pm.sepa_debit.mandate_id}
        elif pm.type == "paypal":
            method_type = "paypal"
            payer_email = getattr(pm.paypal, "payer_email", "") or ""
            display_label = f"PayPal ({payer_email})"
            metadata = {"email": payer_email}
        else:
            method_type = pm.type
            display_label = pm.type
            metadata = {}

        return SavedMethod(
            gateway="stripe",
            gateway_customer_id=customer_id,
            gateway_method_id=pm.id,
            method_type=method_type,
            display_label=display_label,
            metadata=metadata,
            is_active=True,
        )

    def verify_webhook_request(self, raw_body: bytes, headers) -> bool:
        """Verify Stripe webhook signature.

        Args:
            raw_body: The exact bytes received in the request body.
            headers: Case-insensitive header mapping.
        """
        if not self.endpoint_secret:
            return False

        sig = headers.get("Stripe-Signature", "")
        try:
            stripe.Webhook.construct_event(raw_body.decode("utf-8"), sig, self.endpoint_secret)
            return True
        except (stripe.error.SignatureVerificationError, ValueError):
            return False

    def process_webhook_event(self, event_type: str, payload: dict) -> WebhookEventResult | None:
        """Handler for Stripe webhook events."""
        obj = payload.get("data", {}).get("object", {})

        if event_type == "payment_intent.succeeded":
            return WebhookEventResult(
                processed=True,
                action="status_update",
                transaction_id=obj.get("id"),
                new_status=ChargeStatus.CONFIRMED,
            )
        elif event_type == "payment_intent.payment_failed":
            return WebhookEventResult(
                processed=True,
                action="status_update",
                transaction_id=obj.get("id"),
                new_status=ChargeStatus.FAILED,
            )
        elif event_type == "charge.refunded":
            return WebhookEventResult(
                processed=True,
                action="refund",
                transaction_id=obj.get("payment_intent"),
            )
        elif event_type == "charge.dispute.created":
            return WebhookEventResult(
                processed=True,
                action="chargeback",
                transaction_id=obj.get("payment_intent"),
            )
        return WebhookEventResult(processed=False, action="ignored", transaction_id=None)