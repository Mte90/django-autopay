"""Payments facade for django-autopay.

Cross-gateway dispatch layer. All methods are static and dispatch on
saved_method.gateway via provider_factory.
"""
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, unquote

from django.conf import settings
from payments.core import provider_factory

from .base import AutopayCapabilityError, AutopayProvider, is_autopay_capable
from .result import (
    ChargeResult,
    ChargeStatus,
    RefreshSetup,
    RefreshSetupData,
    ValidationResult,
    WebhookEventResult,
)
from .saved_method import SavedMethodProtocol


class Payments:
    """Cross-gateway facade. All methods are static."""

    @staticmethod
    def charge_off_session(
        saved_method: SavedMethodProtocol,
        amount: Decimal,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> ChargeResult:
        """Charge using the saved token. Dispatch on gateway."""
        provider = provider_factory(saved_method.gateway)
        if not is_autopay_capable(provider):
            raise AutopayCapabilityError(
                f"Gateway '{saved_method.gateway}' does not support off-session charges"
            )
        return provider.charge_off_session(
            saved_method=saved_method,
            amount=amount,
            currency=currency,
            description=description,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    @staticmethod
    def save_method(payment):  # type: ignore[return-type]
        """Save the method after a successful on-session payment.

        Persists a SavedPaymentMethod row so callers that use the facade
        directly (without going through the bolt route) still get a DB row.
        Returns the SavedMethod value object, or None if the provider is
        not autopay-capable or the payment has no associated user.
        """
        from .models import _get_saved_method_model

        SavedPaymentMethod = _get_saved_method_model()

        if payment.user_id is None:
            return None

        provider = provider_factory(payment.variant)
        if not is_autopay_capable(provider):
            return None
        saved = provider.save_method(payment)
        if saved is None:
            return None
        sm = SavedPaymentMethod.objects.create(
            user=payment.user,
            gateway=saved.gateway,
            gateway_customer_id=saved.gateway_customer_id,
            gateway_method_id=saved.gateway_method_id,
            method_type=saved.method_type,
            display_label=saved.display_label,
            metadata=saved.metadata or {},
        )
        return sm.as_saved_method()

    @staticmethod
    def list_saved_methods(user) -> list[SavedMethodProtocol]:
        """List saved methods of a user READING THE LOCAL DB (zero gateway calls).

        The source of truth for the UI is SavedPaymentMethod. To discover methods
        revoked on the gateway use `Payments.sync_saved_methods(user)` from job
        periodically — never from a sync view (one API call per gateway).
        """
        from .models import _get_saved_method_model

        model = _get_saved_method_model()
        return [sm.as_saved_method() for sm in model.objects.filter(user=user, is_active=True)]

    @staticmethod
    def sync_saved_methods(user) -> list[SavedMethodProtocol]:
        """Reconcile local methods with the gateway: is_active=False for tokens
        that the gateway no longer exposes (revoked by the user on the gateway).
        One API call per configured gateway: from scheduled job (Celery).
        """
        from .models import _get_saved_method_model

        SavedPaymentMethod = _get_saved_method_model()

        methods: list[SavedMethodProtocol] = []
        for variant_name in getattr(settings, "PAYMENT_VARIANTS", {}):
            provider = provider_factory(variant_name)
            if not is_autopay_capable(provider) or not provider.SUPPORTS_LIST_METHODS:
                continue
            customer_id = _get_customer_id_for(user, variant_name)
            if not customer_id:
                continue
            try:
                methods.extend(provider.list_saved_methods(customer_id))
            except NotImplementedError:
                continue
            remote_ids = {m.gateway_method_id for m in methods}
            SavedPaymentMethod.objects.filter(
                user=user, gateway=variant_name, is_active=True
            ).exclude(gateway_method_id__in=remote_ids).update(is_active=False)
        return methods

    @staticmethod
    def revoke_method(saved_method: SavedMethodProtocol) -> None:
        """Revoke the saved method on the gateway."""
        provider = provider_factory(saved_method.gateway)
        if not is_autopay_capable(provider):
            raise AutopayCapabilityError(
                f"Gateway '{saved_method.gateway}' does not support method revocation"
            )
        provider.revoke_method(saved_method)

    @staticmethod
    def get_charge_status(gateway: str, transaction_id: str) -> ChargeResult:
        """Polling: returns the current transaction status on the gateway."""
        provider = provider_factory(gateway)
        if not is_autopay_capable(provider):
            raise AutopayCapabilityError(
                f"Gateway '{gateway}' does not support off-session charges"
            )
        return provider.get_charge_status(transaction_id)

    @staticmethod
    def refund(gateway: str, transaction_id: str, amount: Decimal | None = None, reason: str = "") -> ChargeResult:
        """Refund (full if amount=None, partial otherwise) a confirmed charge."""
        provider = provider_factory(gateway)
        if not is_autopay_capable(provider) or not provider.SUPPORTS_REFUND:
            raise AutopayCapabilityError(f"Gateway '{gateway}' does not support refunds")
        return provider.refund(transaction_id, amount=amount, reason=reason)

    @staticmethod
    def refresh_method(saved_method: SavedMethodProtocol, reason: str = "manual") -> RefreshSetup:
        """Start a refresh of the token/mandate. Persistence in facade."""
        provider = provider_factory(saved_method.gateway)
        if not is_autopay_capable(provider):
            raise AutopayCapabilityError(
                f"Gateway '{saved_method.gateway}' does not support token refresh"
            )
        data: RefreshSetupData = provider.refresh_method(saved_method)
        base_url = settings.AUTOPAY_SITE_BASE_URL
        attempt_id = 0
        try:
            from .models import RefreshAttempt

            attempt = RefreshAttempt.objects.create(
                saved_method_id=saved_method.id,
                gateway_setup_id=data.gateway_setup_id,
                frontend_sdk=data.frontend_sdk,
                frontend_payload=data.frontend_payload,
                reason=reason,
                expires_at=data.expires_at,
            )
            attempt_id = attempt.pk
        except ImportError:
            pass  # models not installed: refresh works, without history
        return RefreshSetup(
            setup_id=attempt_id,
            frontend_sdk=data.frontend_sdk,
            frontend_payload=data.frontend_payload,
            expires_at=data.expires_at,
            return_url=f"{base_url}/autopay/api/methods/{saved_method.id}/refresh/confirm/?attempt={attempt_id}",
        )

    @staticmethod
    def confirm_refresh(saved_method: SavedMethodProtocol, refresh_payload: dict) -> SavedMethodProtocol:
        """Confirm the refresh after the frontend has done the setup.

        Anti-concurrency guard: atomic pending→in_progress→confirmed transition.
        Two concurrent confirms: only the first gets claimed=1, the second
        stops BEFORE touching the gateway.
        """
        provider = provider_factory(saved_method.gateway)

        try:
            from django.utils import timezone as tz

            from .models import RefreshAttempt

            attempt = RefreshAttempt.objects.filter(
                saved_method_id=saved_method.id, status="pending"
            ).order_by("-created_at").first()
            if attempt:
                # Check expiry
                if attempt.expires_at and tz.now() > attempt.expires_at:
                    RefreshAttempt.objects.filter(
                        pk=attempt.pk, status="pending"
                    ).update(status="expired")
                    raise RuntimeError("Refresh attempt has expired")
                
                # Claim the attempt atomically, set to in_progress
                claimed = RefreshAttempt.objects.filter(
                    pk=attempt.pk, status="pending"
                ).update(status="in_progress")
                if not claimed:
                    raise RuntimeError(
                        "Refresh attempt already confirmed by a concurrent request"
                    )
        except ImportError:
            pass  # without DB there is no shared state to protect

        try:
            updated = provider.confirm_refresh(saved_method, refresh_payload)
        except Exception:
            # Mark as failed if provider call fails
            try:
                from django.utils import timezone as tz

                from .models import RefreshAttempt

                RefreshAttempt.objects.filter(
                    saved_method_id=saved_method.id, status="in_progress"
                ).update(status="failed")
            except ImportError:
                pass
            raise

        # IN-PLACE persistence of the method (same PK)
        try:
            from django.utils import timezone as tz

            from .models import _get_saved_method_model

            SavedPaymentMethod = _get_saved_method_model()

            SavedPaymentMethod.objects.filter(pk=updated.id).update(
                gateway_method_id=updated.gateway_method_id,
                display_label=updated.display_label,
                metadata=updated.metadata,
                last_refreshed_at=tz.now(),
            )
        except ImportError:
            pass
        
        # Mark as confirmed only after success
        try:
            from django.utils import timezone as tz

            from .models import RefreshAttempt

            RefreshAttempt.objects.filter(
                saved_method_id=saved_method.id, status="in_progress"
            ).update(status="confirmed", confirmed_at=tz.now())
        except ImportError:
            pass
        
        return updated

    @staticmethod
    def validate_method(saved_method: SavedMethodProtocol) -> ValidationResult:
        """Check token validity without charging.

        Automatic dispatch on provider based on saved_method.gateway.
        The app must not know which gateway it is validating.
        """
        provider = provider_factory(saved_method.gateway)
        if not is_autopay_capable(provider) or not provider.SUPPORTS_VALIDATE:
            return ValidationResult(
                is_valid=False, is_expired=False, needs_refresh=False,
                reason=f"Gateway '{saved_method.gateway}' does not support validate_method",
                checked_at=datetime.now(timezone.utc),
                raw_response={},
            )
        return provider.validate_method(saved_method)

    @staticmethod
    def validate_all_user_methods(user) -> list[tuple[int, ValidationResult]]:
        """Validate all active saved methods of a user, for all gateways.

        Useful for periodic jobs that update the health status of methods.
        Returns a list of tuples (saved_method_id, ValidationResult).
        """
        from .models import _get_saved_method_model

        SavedPaymentMethod = _get_saved_method_model()

        results: list[tuple[int, ValidationResult]] = []
        for sm in SavedPaymentMethod.objects.filter(user=user, is_active=True):
            result = Payments.validate_method(sm.as_saved_method())
            # update last_validated_at
            SavedPaymentMethod.objects.filter(pk=sm.pk).update(
                last_validated_at=result.checked_at,
                # optional: save the last result to show in UI
                last_validation_is_valid=result.is_valid,
                last_validation_reason=result.reason,
            )
            if result.needs_refresh:
                # mark as is_active=False? No — the user might want to see the
                # invalid method in the user area to understand it needs refresh.
                # Better: add a separate is_valid field.
                pass
            results.append((sm.pk, result))
        return results

    @staticmethod
    def is_autopay_capable(provider: AutopayProvider) -> bool:
        """True if the provider can do off-session charge."""
        return is_autopay_capable(provider)

    @staticmethod
    def process_webhook(
        variant: str,
        raw_body: bytes,
        headers: dict[str, str] | None = None,
        content_type: str = "",
        client_ip: str = "",
    ) -> "WebhookEventResult":
        """Process a gateway webhook. Single webhook logic point.

        1. Resolve the provider for variant
        2. Parse raw_body based on content_type
        3. Verify signature ONLY if SUPPORTS_WEBHOOK_VERIFICATION
        4. Deduplicate via ProcessedWebhookEvent (insert-or-get; replay = success without re-dispatch)
        5. Dispatch a provider.process_webhook_event
        6. Persist the processed event

        For Mollie (no event_id): uses composite key payment_id+status to avoid
        swallowing of multiple states (pending → paid).
        
        Args:
            variant: The payment provider variant (e.g., "stripe", "paypal", "mollie")
            raw_body: The exact bytes received in the HTTP request body
            headers: Case-insensitive header mapping (use .get() with canonical
                    header names like "Stripe-Signature")
            content_type: The Content-Type header value for parsing the body
            client_ip: Client IP of the sender (e.g. request.META["REMOTE_ADDR"]).
                Used by IP-allowlist verification (Mollie)
        """
        from django.db import IntegrityError, transaction

        from .models import ProcessedWebhookEvent

        # Parse the body based on content_type
        payload = _parse_webhook_body(raw_body, content_type)
        if payload is None:
            return WebhookEventResult(
                processed=False,
                action="error",
                transaction_id=None,
                error="Invalid body",
            )

        try:
            provider = provider_factory(variant)
        except ValueError:
            # Unknown variant
            return WebhookEventResult(
                processed=False,
                action="error",
                transaction_id=None,
                error=f"Provider '{variant}' not found or not capable",
            )
        
        if not is_autopay_capable(provider):
            return WebhookEventResult(
                processed=False,
                action="error",
                transaction_id=None,
                error=f"Provider '{variant}' not found or not capable",
            )

        # Step 2: optional signature verification
        if provider.SUPPORTS_WEBHOOK_VERIFICATION:
            verify_headers = dict(headers) if headers else {}
            if client_ip:
                verify_headers.setdefault("REMOTE_ADDR", client_ip)
            try:
                if not provider.verify_webhook_request(raw_body, verify_headers):
                    return WebhookEventResult(
                        processed=False,
                        action="error",
                        transaction_id=None,
                        error="Webhook signature verification failed",
                    )
            except (NotImplementedError, ValueError):
                # Provider declares SUPPORTS_WEBHOOK_VERIFICATION but does not implement it
                return WebhookEventResult(
                    processed=False,
                    action="error",
                    transaction_id=None,
                    error="Webhook signature verification failed",
                )

        # Step 3: build dedup key.
        # Mollie webhooks only contain {"id": "..."}. The real status is only
        # known after fetching the payment from the API, so we dispatch the
        # provider *before* dedup and build a composite key payment_id:new_status.
        result = None
        if variant == "mollie":
            event_type = _extract_webhook_event_type(variant, payload)
            result = provider.process_webhook_event(event_type, payload)
            payment_id = payload.get("id", "")
            new_status = result.new_status.value if result and result.new_status else ""
            event_id = f"{payment_id}:{new_status}" if payment_id else ""
        else:
            event_id = _extract_webhook_event_id(variant, payload)

        # Step 4: deduplicate (insert-or-get) and apply actions atomically
        try:
            with transaction.atomic():
                ProcessedWebhookEvent.objects.create(
                    gateway=variant,
                    event_id=event_id,
                    payload=payload,
                )
                # Dispatch inside the same atomic block (skip for Mollie,
                # already dispatched above to build the composite key)
                if result is None:
                    event_type = _extract_webhook_event_type(variant, payload)
                    result = provider.process_webhook_event(event_type, payload)

                if result and result.processed:
                    _apply_webhook_action(result)

                if result is not None:
                    result.event_id = event_id

                final_result = result or WebhookEventResult(
                    processed=False,
                    action="ignored",
                    transaction_id=None,
                    event_id=event_id,
                )

            # Emit the webhook_processed signal outside the atomic block so
            # receiver failures cannot roll back the persisted webhook and
            # receiver DB writes are independent from the facade transaction.
            if final_result.processed:
                from .signals import webhook_processed

                webhook_processed.send_robust(
                    sender=variant,
                    event_type=event_type,
                    payload=payload,
                    result=final_result,
                )
            return final_result
        except IntegrityError:
            # Already processed — return success without re-dispatch
            return WebhookEventResult(
                processed=True,
                action="noop",
                transaction_id=None,
                event_id=event_id,
            )


def _parse_webhook_body(raw_body: bytes, content_type: str) -> dict | None:
    """Parse webhook body based on content type.
    
    Args:
        raw_body: The raw bytes from the request body
        content_type: The Content-Type header value
        
    Returns:
        A flat dict with the parsed payload, or None if parsing fails
    """
    if not raw_body:
        return None
    
    # Check content type for form data
    if "application/x-www-form-urlencoded" in content_type:
        try:
            # Parse form data and flatten single-value lists to scalars
            parsed = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
            # Flatten: single-item lists become scalars, multi-item stay as lists
            result = {}
            for key, values in parsed.items():
                if len(values) == 1:
                    result[key] = unquote(values[0])
                else:
                    result[key] = [unquote(v) for v in values]
            return result
        except (UnicodeDecodeError, ValueError):
            return None
    else:
        # Parse as JSON
        try:
            import json
            return json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None


def _get_customer_id_for(user, variant_name: str) -> str | None:
    """Helper: returns the gateway-side customer_id for this user.

    Search in our SavedPaymentMethod (if installed) or in a custom model
    configured via AUTOPAY_CUSTOMER_LOOKUP.
    """
    lookup = getattr(settings, "AUTOPAY_CUSTOMER_LOOKUP", None)
    if lookup:
        # string path to callable: e.g. "myapp.payments.get_customer_id"
        from django.utils.module_loading import import_string

        return import_string(lookup)(user, variant_name)
    # default: search in SavedPaymentMethod of the package
    try:
        from .models import _get_saved_method_model

        m = _get_saved_method_model().objects.filter(
            user=user, gateway=variant_name, is_active=True
        ).first()
        return m.gateway_customer_id if m else None
    except ImportError:
        return None


def _extract_webhook_event_id(variant: str, payload: dict) -> str:
    """Extract event_id from the gateway payload.

    Mollie is handled in process_webhook directly, where the composite key
    payment_id:new_status is built after fetching the payment from the API
    (Mollie webhooks only contain the payment id, not the status).
    """
    if variant == "paypal":
        return payload.get("id", "") or payload.get("event_id", "")
    elif variant == "stripe":
        return payload.get("id", "")
    else:
        return payload.get("id", "")


def _extract_webhook_event_type(variant: str, payload: dict) -> str:
    """Extract event_type from the gateway payload."""
    if variant == "paypal":
        return payload.get("event_type", "")
    elif variant == "stripe":
        return payload.get("type", "")
    elif variant == "mollie":
        return "payment.update"  # Mollie sends only payment.id
    elif variant == "dummy":
        return payload.get("type", "")
    return ""


def _apply_webhook_action(result: "WebhookEventResult") -> None:
    """Apply the resulting actions from the webhook (optional).

    The provider may have already applied the actions in process_webhook_event,
    this is a hook for additional actions at the facade level.
    """
    from .models import ChargeRecord, _get_saved_method_model

    SavedPaymentMethod = _get_saved_method_model()

    if result.action == "revoke_method" and result.transaction_id:
        # Revoke the saved method
        SavedPaymentMethod.objects.filter(
            gateway_method_id=result.transaction_id
        ).update(is_active=False, last_validated_at=None, last_validation_is_valid=False)
    elif result.action == "status_update" and result.transaction_id and result.new_status:
        # Update the charge status
        ChargeRecord.objects.filter(
            transaction_id=result.transaction_id
        ).update(status=result.new_status.value, updated_at=datetime.now(timezone.utc))
    elif result.action == "refund" and result.transaction_id:
        # Mark the original charge as refunded
        ChargeRecord.objects.filter(
            transaction_id=result.transaction_id
        ).update(status=ChargeStatus.REFUNDED.value, updated_at=datetime.now(timezone.utc))