"""Base provider class for django-autopay."""

from decimal import Decimal

from payments.core import BasicProvider

from .result import ChargeResult, RefreshSetupData, ValidationResult, WebhookEventResult
from .saved_method import SavedMethod


class AutopayCapabilityError(RuntimeError):
    """Raised when invoking a capability on a provider that does not support it.

    This is a configuration/programming error (e.g. dummy variant used for an
    off-session charge), NOT a payment outcome: it does not become ChargeResult
    FAILED, does not create ChargeRecord and should not enter automatic retries.
    """


def is_autopay_capable(provider: BasicProvider) -> bool:
    """True if the provider can do off-session charge."""
    return isinstance(provider, AutopayProvider) and provider.SUPPORTS_OFF_SESSION_CHARGE


class AutopayProvider(BasicProvider):
    """Extends BasicProvider with off-session charge capability.

    Providers that support charge-by-token override:
      - save_method(payment) -> SavedMethod
      - charge_off_session(saved_method, amount, ...) -> ChargeResult
      - refresh_method(saved_method) -> RefreshSetupData  (gateway only; persistence in facade)
      - confirm_refresh(saved_method, refresh_payload) -> SavedMethod  (gateway only; persistence in facade)
      - validate_method(saved_method) -> ValidationResult  (no test charge)
      - list_saved_methods(customer_id) -> list[SavedMethod]
      - revoke_method(saved_method) -> None
      - get_charge_status(transaction_id) -> ChargeResult  (for sync polling)

    NOTE ON ROUTING: dispatch between providers is handled entirely by the
    facade `Payments.*`, which reads `saved_method.gateway` (e.g. "paypal",
    "mollie", "stripe") and instantiates the corresponding provider via
    `provider_factory(saved_method.gateway)`. The user application MUST
    NOT know which gateway it is using — it passes a `SavedMethod` and
    the system figures out which provider to call. This applies to
    `charge_off_session`, `refresh_method`, `confirm_refresh`,
    `validate_method`, `list_saved_methods`, `revoke_method`.

    Providers that do not support auto-charge do not define them (so
    is_autopay_capable() returns False and the facade skips the provider).
    """

    # capability flag — override in subclasses
    SUPPORTS_OFF_SESSION_CHARGE = False
    SUPPORTS_LIST_METHODS = False
    SUPPORTS_REVOKE = False
    SUPPORTS_REFRESH = False
    SUPPORTS_VALIDATE = False              # validate_method() available
    SUPPORTS_REFUND = False                # refund() available (§20.0)
    SUPPORTS_WEBHOOK_VERIFICATION = False

    def save_method(self, payment) -> SavedMethod:
        """Save the payment method after a successful on-session flow.

        Call AFTER payment is in CONFIRMED status. Implementation
            by default raises NotImplementedError — autopay-capable providers
        implement it.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support saving methods")

    def charge_off_session(
        self,
        saved_method: SavedMethod,
        amount: Decimal,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> ChargeResult:
        """Charge an amount using the saved token/mandate. Sync.

        Returns ChargeResult with status:
          CONFIRMED → charge succeeded (no webhook needed)
          PENDING   → in progress, you must poll get_charge_status or wait for the webhook
          FAILED    → definitively failed, the method must be reviewed
          REQUIRES_SCA → needs 3DS, user must be on-session
           REJECTED  → mandate/token revoked, skip the method → call refresh_method
        """
        raise NotImplementedError(f"{type(self).__name__} does not support off-session charges")

    def refresh_method(self, saved_method: SavedMethod) -> RefreshSetupData:
        """Start a new setup flow to regenerate the token.

        Call when:
          - charge_off_session returns REJECTED (revoked token)
          - charge_off_session returns REQUIRES_SCA persistent (mandate no longer
            covered by exemption)
          - the gateway notifies (via optional webhook) VAULT.TOKEN.REVOKED
          - scheduled job: early refresh before card expiry

        Returns RefreshSetupData (gateway data only, no DB access):
          - gateway_setup_id: setup_token_id / setup_intent_id / payment_id on gateway
          - frontend_payload: dict with everything needed by the frontend SDK
            (e.g. PayPal: {client_token, setup_token_id};
                         Stripe: {client_secret};
                         Mollie: {payment_url, payment_id})
          - frontend_sdk: "paypal" | "stripe" | "mollie" → tells the frontend
            which SDK to load
          - expires_at

        Persistence (RefreshAttempt) and return_url are in the facade
         Payments.refresh_method, which returns RefreshSetup.
        """
        if not self.SUPPORTS_REFRESH:
            raise NotImplementedError(f"{type(self).__name__} does not support token refresh")
        raise NotImplementedError(
            f"{type(self).__name__} declares SUPPORTS_REFRESH but does not implement refresh_method"
        )

    def confirm_refresh(self, saved_method: SavedMethod, refresh_payload: dict) -> SavedMethod:
        """Confirm the refresh after the frontend has done the setup.

        - PayPal: receives {setup_token_id, payment_token_id} → updates saved_method
        - Stripe: receives {setup_intent_id} → retrieves PM, updates saved_method
        - Mollie: receives {first_payment_id} → fetches mandate, updates saved_method

        IMPORTANT: updates the existing SavedPaymentMethod IN-PLACE (no new record).
        The method ID stays the same; only gateway_method_id changes (and optionally
        metadata like brand/last4 if the card is different).
        """
        if not self.SUPPORTS_REFRESH:
            raise NotImplementedError(f"{type(self).__name__} does not support token refresh")
        raise NotImplementedError

    def validate_method(self, saved_method: SavedMethod) -> ValidationResult:
        """Check if the saved token/mandate is still valid, WITHOUT charging.

        Gateway-specific implementation, but typically:
          - PayPal:    GET /v3/vault/payment-tokens/<id> — 200 OK = valid, 404 = revoked
          - Stripe:    PaymentMethod.retrieve(id) — checks card.exp_year vs now
          - Mollie:    customer_mandates.get(customer_id, mandate_id) — status=="valid"

        Returns ValidationResult with:
          - is_valid: True if the token can be used for off-session charge
          - is_expired: True if the card is expired (for cards)
          - needs_refresh: True if the token is invalid and needs a refresh
          - reason: human-readable message
          - checked_at: UTC timestamp
          - raw_response: dict of gateway payload for audit

        IMPORTANT: this method does NOT do a test charge, does NOT do setup_intent,
        does NOT generate traffic to the issuer. It is a read-only GET
        on the gateway. Cost: 0. Latency: ~200ms.

        Call:
          - Periodically (e.g. daily via Celery) to mark methods
            expiring soon
          - Before a scheduled charge (optional: the app may decide
            to refresh in advance if validate_method returns needs_refresh)
          - On "token revoked" webhook to confirm locally
        """
        if not self.SUPPORTS_VALIDATE:
            raise NotImplementedError(f"{type(self).__name__} does not support validation")
        raise NotImplementedError

    def list_saved_methods(self, customer_id: str) -> list[SavedMethod]:
        """List saved methods for a customer_id on the gateway."""
        if not self.SUPPORTS_LIST_METHODS:
            raise NotImplementedError(f"{type(self).__name__} does not support listing methods")
        raise NotImplementedError

    def revoke_method(self, saved_method: SavedMethod) -> None:
        """Revoke the saved method on the gateway (DELETE /tokens/<id>)."""
        if not self.SUPPORTS_REVOKE:
            raise NotImplementedError(f"{type(self).__name__} does not support revoking methods")
        raise NotImplementedError

    def get_charge_status(self, transaction_id: str) -> ChargeResult:
        """Polling: returns the current transaction status on the gateway."""
        raise NotImplementedError(f"{type(self).__name__} does not support status polling")

    def refund(self, transaction_id: str, amount: Decimal | None = None, reason: str = "") -> ChargeResult:
        """Refund (full if amount=None, partial otherwise) a confirmed charge.

        Optional capability (SUPPORTS_REFUND): the three gateways support it,
        but remains optional in the package because the refund may also pass through
        from the gateway dashboard. Returns ChargeResult with status=CONFIRMED
        (refund executed) or PENDING (processing on gateway side).
        """
        if not self.SUPPORTS_REFUND:
            raise NotImplementedError(f"{type(self).__name__} does not support refunds")
        raise NotImplementedError

    def process_webhook_event(self, event_type: str, payload: dict) -> WebhookEventResult | None:
        """Optional webhook handler. Override if SUPPORTS_WEBHOOK_VERIFICATION."""
        if not self.SUPPORTS_WEBHOOK_VERIFICATION:
            return None
        raise NotImplementedError

    def verify_webhook_request(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Verify webhook request signature/authenticity.

        Args:
            raw_body: The exact bytes received in the request body.
            headers: Case-insensitive header mapping (use .get() with canonical
                    header names like "Stripe-Signature").
        """
        if not self.SUPPORTS_WEBHOOK_VERIFICATION:
            return False
        raise NotImplementedError