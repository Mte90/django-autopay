"""Django models for django-autopay core module."""
from django.conf import settings
from django.db import models


class SavedPaymentMethod(models.Model):
    """Saved token / mandate. NO card data, only gateway-side references.
    
    This model is the single source of truth for the app: it contains the reference to the 
    gateway-side token/mandate. Provider routing (PayPal, Mollie, Stripe) 
    happens by reading the `gateway` field of this table — the app must never 
    know which circuit it is using; it passes the object to the `Payments.*` facade, 
    which dispatches on `provider_factory(saved_method.gateway)`.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="saved_payment_methods",
    )
    gateway = models.CharField(max_length=32, db_index=True)  # "paypal" | "mollie" | "stripe"
    gateway_customer_id = models.CharField(max_length=128)
    gateway_method_id = models.CharField(max_length=128, unique=True)
    method_type = models.CharField(max_length=32)  # "card" | "paypal" | "sepa" | "ideal"
    display_label = models.CharField(max_length=128)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)  # brand, last4, expiry, fingerprint
    
    # campi di validazione (popolati da Payments.validate_method)
    last_validated_at = models.DateTimeField(null=True, blank=True)
    last_validation_is_valid = models.BooleanField(null=True, blank=True)
    last_validation_reason = models.CharField(max_length=255, blank=True, default="")
    last_refreshed_at = models.DateTimeField(null=True, blank=True)  # updated by confirm_refresh
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ("gateway", "gateway_method_id")
        indexes = [  # noqa: RUF012
            models.Index(fields=["user", "is_active", "is_default"]),
            models.Index(fields=["gateway", "gateway_customer_id"]),
            models.Index(fields=["is_active", "last_validation_is_valid"]),
        ]

    def as_saved_method(self):
        """Return a SavedMethod protocol object for facade operations.

        Converts this model instance into the gateway-agnostic SavedMethod
        value object used by the Payments facade and provider layer.
        """
        from .saved_method import SavedMethod
        return SavedMethod(
            gateway=self.gateway,
            gateway_customer_id=self.gateway_customer_id,
            gateway_method_id=self.gateway_method_id,
            method_type=self.method_type,
            display_label=self.display_label,
            is_active=self.is_active,
            metadata=self.metadata,
            id=self.pk,
        )

    @property
    def needs_validation(self) -> bool:
        """True if never validated or the last validation is old."""
        from datetime import timedelta

        from django.utils import timezone
        if not self.last_validated_at:
            return True
        return self.last_validated_at < timezone.now() - timedelta(days=1)


class ChargeRecord(models.Model):
    """Off-session charge history (optional, for audit/reporting).
    
    The app may choose to use it or use its own model.
    """
    saved_method = models.ForeignKey(
        SavedPaymentMethod, on_delete=models.PROTECT, related_name="charges"
    )
    amount = models.DecimalField(max_digits=9, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    description = models.CharField(max_length=255)
    transaction_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16)  # ChargeStatus values
    message = models.TextField(blank=True)
    # Retention/PII: gateway payloads contain personal data (PayPal: name/email/address).
    # Truncate/scrub unnecessary fields and plan retention (e.g., cleanup > 180 days via job) for GDPR.
    raw_response = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=128, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        indexes = [models.Index(fields=["status", "created_at"])]  # noqa: RUF012


class RefreshAttempt(models.Model):
    """History of refreshes for a SavedPaymentMethod. One per setup flow."""
    saved_method = models.ForeignKey(
        SavedPaymentMethod,
        on_delete=models.CASCADE,
        related_name="refresh_attempts",
    )
    gateway_setup_id = models.CharField(max_length=128)  # setup_token_id / setup_intent_id / first_payment_id
    frontend_sdk = models.CharField(max_length=32)        # "paypal" | "stripe" | "mollie"
    frontend_payload = models.JSONField(default=dict)     # what the provider returns for the frontend
    status = models.CharField(max_length=16, default="pending")  # pending | confirmed | expired | failed
    reason = models.CharField(max_length=64, blank=True)  # "expired_card" | "revoked" | "sca_required" | "manual"
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        indexes = [  # noqa: RUF012
            models.Index(fields=["saved_method", "status"]),
            models.Index(fields=["status", "expires_at"]),
        ]
        ordering = ["-created_at"]  # noqa: RUF012


class ProcessedWebhookEvent(models.Model):
    """For cross-gateway idempotency. Optional, only if using webhooks."""
    gateway = models.CharField(max_length=32)
    event_id = models.CharField(max_length=128)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ("gateway", "event_id")


def _get_saved_method_model():
    """Return the SavedPaymentMethod model to use.
    
    Respects the AUTOPAY_SAVED_METHOD_MODEL setting if configured,
    otherwise uses the default SavedPaymentMethod from this package.
    """
    from django.apps import apps
    from django.conf import settings
    label = getattr(settings, "AUTOPAY_SAVED_METHOD_MODEL", None)
    if label:
        return apps.get_model(label)
    return SavedPaymentMethod