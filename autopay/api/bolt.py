"""Django-Bolt API endpoints for django-autopay.

Implements all REST endpoints for frontend SDK and server-to-server use.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone
from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication

from autopay.core.base import AutopayCapabilityError
from autopay.core.facade import Payments
from autopay.core.models import (
    ChargeRecord,
    RefreshAttempt,
    SavedPaymentMethod,
)

from .schemas import (
    ChargeIn,
    ChargeOut,
    ConfirmRefreshIn,
    ConfirmRefreshOut,
    CreateMethodIn,
    CreateMethodOut,
    DeleteMethodOut,
    RefreshMethodIn,
    RefreshMethodOut,
    RefreshStatusOut,
    SavedMethodOut,
    ValidationResultOut,
    WebhookReceiverOut,
)

api = BoltAPI()


def _to_method_out(sm: SavedPaymentMethod) -> SavedMethodOut:
    """Converts SavedPaymentMethod to SavedMethodOut schema."""
    return SavedMethodOut(
        id=sm.id,
        gateway=sm.gateway,
        method_type=sm.method_type,
        display_label=sm.display_label,
        is_default=sm.is_default,
        is_active=sm.is_active,
        metadata=sm.metadata or {},
        created_at=sm.created_at.isoformat(),
    )


@api.get("/autopay/api/methods/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def list_methods(request):
    """List saved methods for the authenticated user.

    Reads ONLY from local DB — zero gateway calls (plan §604-621).
    """
    methods = [
        method async for method in SavedPaymentMethod.objects.filter(
            user=request.user, is_active=True
        ).order_by("-is_default", "-created_at")
    ]
    return [_to_method_out(m) for m in methods]


@api.post("/autopay/api/methods/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def create_method(request, body: CreateMethodIn) -> CreateMethodOut:
    """Start onboarding a new method.

    The user must first do an on-session checkout (via django-payments).
    This endpoint accepts the payment.id of the successful checkout and saves the method.
    """
    from payments import get_payment_model

    Payment = get_payment_model()

    try:
        payment = await Payment.objects.aget(id=body.payment_token)
    except Payment.DoesNotExist:
        return CreateMethodOut(setup_id=0, frontend_sdk="", frontend_payload={}, return_url="")

    if payment.user_id != request.user.id:
        return CreateMethodOut(setup_id=0, frontend_sdk="", frontend_payload={}, return_url="")

    saved_method = await sync_to_async(Payments.save_method)(payment)
    if saved_method is None:
        return CreateMethodOut(setup_id=0, frontend_sdk="", frontend_payload={}, return_url="")

    return CreateMethodOut(
        setup_id=saved_method.id,
        frontend_sdk=saved_method.gateway,
        frontend_payload=saved_method.metadata or {},
        return_url=f"/autopay/api/methods/{saved_method.id}/",
    )


@api.get("/autopay/api/methods/{method_id}/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def get_method(request, method_id: int) -> SavedMethodOut:
    """Get method details."""
    sm = await SavedPaymentMethod.objects.aget(id=method_id, user=request.user)
    return _to_method_out(sm)


@api.delete("/autopay/api/methods/{method_id}/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def revoke_method(request, method_id: int) -> DeleteMethodOut:
    """Revoke method."""
    sm = await SavedPaymentMethod.objects.aget(id=method_id, user=request.user)
    await sync_to_async(Payments.revoke_method)(sm.as_saved_method())
    sm.is_active = False
    await sm.asave()
    return DeleteMethodOut(success=True, deleted_method_id=method_id)


@api.post("/autopay/api/methods/{method_id}/validate/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def validate_method(request, method_id: int) -> ValidationResultOut:
    """Verify token/mandate validity without charging.

    Automatic dispatch on provider based on sm.gateway.
    Updates last_validated_at / last_validation_is_valid / last_validation_reason
    on the SavedPaymentMethod.
    """
    sm = await SavedPaymentMethod.objects.aget(id=method_id, user=request.user)
    if not sm.is_active:
        return ValidationResultOut(
            is_valid=False,
            is_expired=False,
            needs_refresh=True,
            reason="Method no longer active",
            checked_at=timezone.now().isoformat(),
            method_id=method_id,
            gateway=sm.gateway,
        )

    result = await sync_to_async(Payments.validate_method)(sm.as_saved_method())

    # Persist for history + UI
    await SavedPaymentMethod.objects.filter(pk=method_id).aupdate(
        last_validated_at=result.checked_at,
        last_validation_is_valid=result.is_valid,
        last_validation_reason=result.reason[:255],
    )

    return ValidationResultOut(
        is_valid=result.is_valid,
        is_expired=result.is_expired,
        needs_refresh=result.needs_refresh,
        reason=result.reason,
        checked_at=result.checked_at.isoformat(),
        method_id=method_id,
        gateway=sm.gateway,
    )


@api.post("/autopay/api/methods/validate_all/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def validate_all_my_methods(request) -> list[ValidationResultOut]:
    """Validate all saved methods for the current user, for all gateways.

    Useful as "Update method status" endpoint in the user panel.
    """
    results = []
    async for sm in SavedPaymentMethod.objects.filter(user=request.user, is_active=True):
        result = await sync_to_async(Payments.validate_method)(sm.as_saved_method())
        await SavedPaymentMethod.objects.filter(pk=sm.pk).aupdate(
            last_validated_at=result.checked_at,
            last_validation_is_valid=result.is_valid,
            last_validation_reason=result.reason[:255],
        )
        results.append(ValidationResultOut(
            is_valid=result.is_valid,
            is_expired=result.is_expired,
            needs_refresh=result.needs_refresh,
            reason=result.reason,
            checked_at=result.checked_at.isoformat(),
            method_id=sm.pk,
            gateway=sm.gateway,
        ))
    return results


@api.post("/autopay/api/methods/{method_id}/refresh/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def refresh_method(request, method_id: int, body: RefreshMethodIn) -> RefreshMethodOut:
    """Start a new setup flow to regenerate the token. See section 9."""
    sm = await SavedPaymentMethod.objects.aget(id=method_id, user=request.user)
    if not sm.is_active:
        return RefreshMethodOut(setup_id=0, frontend_sdk="", frontend_payload={}, expires_at="", return_url="")

    setup = await sync_to_async(Payments.refresh_method)(sm.as_saved_method(), reason=body.reason)

    return RefreshMethodOut(
        setup_id=setup.setup_id,
        frontend_sdk=setup.frontend_sdk,
        frontend_payload=setup.frontend_payload,
        expires_at=setup.expires_at.isoformat(),
        return_url=setup.return_url,
    )


@api.get("/autopay/api/methods/{method_id}/refresh/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def get_refresh_setup(request, method_id: int, attempt: int) -> RefreshMethodOut:
    """Retrieve setup data for the frontend SDK (after the user clicked the email link)."""
    # IDOR guard: attempt must belong to authenticated user's saved method
    try:
        attempt_obj = await RefreshAttempt.objects.aget(
            id=attempt, saved_method_id=method_id, saved_method__user=request.user
        )
    except RefreshAttempt.DoesNotExist:
        return RefreshMethodOut(setup_id=0, frontend_sdk="", frontend_payload={}, expires_at="", return_url="")

    if attempt_obj.status != "pending":
        return RefreshMethodOut(setup_id=0, frontend_sdk="", frontend_payload={}, expires_at="", return_url="")

    return RefreshMethodOut(
        setup_id=attempt_obj.id,
        frontend_sdk=attempt_obj.frontend_sdk,
        frontend_payload=attempt_obj.frontend_payload,
        expires_at=attempt_obj.expires_at.isoformat(),
        return_url=f"/autopay/api/methods/{method_id}/refresh/confirm/?attempt={attempt_obj.id}",
    )


@api.post("/autopay/api/methods/{method_id}/refresh/confirm/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def confirm_refresh_post(request, method_id: int, body: ConfirmRefreshIn) -> ConfirmRefreshOut:
    """Confirm the refresh after the frontend SDK has completed the setup.

    Gateway-specific payload:
    - PayPal: {setup_token_id, payment_token_id}
    - Stripe: {setup_intent_id}
    - Mollie: (via query string) ?first_payment_id=xxx
    """
    sm = await SavedPaymentMethod.objects.aget(id=method_id, user=request.user)

    refresh_payload = {
        "setup_token_id": body.setup_token_id,
        "setup_intent_id": body.setup_intent_id,
        "first_payment_id": body.first_payment_id,
        "payment_token_id": body.payment_token_id,
    }
    refresh_payload = {k: v for k, v in refresh_payload.items() if v is not None}

    try:
        await sync_to_async(Payments.confirm_refresh)(sm.as_saved_method(), refresh_payload)
        sm = await SavedPaymentMethod.objects.aget(id=method_id)  # refresh from DB
        return ConfirmRefreshOut(success=True, method=_to_method_out(sm), error=None)
    except (SavedPaymentMethod.DoesNotExist, AutopayCapabilityError) as e:
        return ConfirmRefreshOut(success=False, method=None, error=str(e))


@api.get("/autopay/api/methods/{method_id}/refresh/confirm/", auth=None)
async def confirm_refresh_get(request, method_id: int, first_payment_id: str = "", attempt: int = 0, sig: str = ""):
    """GET endpoint for Mollie redirect (user returns with ?first_payment_id=xxx).

    Authenticates via signed attempt_id (HMAC) instead of JWT.
    """
    # Verify attempt signature (if configured)
    if attempt and not _verify_attempt_signature(attempt, method_id, sig):
        return RefreshStatusOut(success=False, method_id=None, error="invalid signature")

    try:
        sm = await SavedPaymentMethod.objects.aget(id=method_id)
    except SavedPaymentMethod.DoesNotExist:
        return RefreshStatusOut(success=False, method_id=None, error="method not found")

    refresh_payload = {"first_payment_id": first_payment_id} if first_payment_id else {}

    try:
        await sync_to_async(Payments.confirm_refresh)(sm.as_saved_method(), refresh_payload)
        return RefreshStatusOut(success=True, method_id=method_id, error=None)
    except (SavedPaymentMethod.DoesNotExist, AutopayCapabilityError) as e:
        return RefreshStatusOut(success=False, method_id=None, error=str(e))


@api.post("/autopay/api/charges/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def create_charge(request, body: ChargeIn) -> ChargeOut:
    """Create an off-session charge. Used by Celery / backend, not the browser.

    The JWT API key here is server-side: it is recommended to use a service account
    instead of the user token. See section 12.7 for security.
    """
    # IDOR guard: saved method must belong to authenticated user
    try:
        sm = await SavedPaymentMethod.objects.aget(id=body.saved_method_id, is_active=True, user=request.user)
    except SavedPaymentMethod.DoesNotExist:
        # Return 404 to avoid leaking whether the method exists
        return ChargeOut(
            status="failed",
            message="Payment method not found",
            transaction_id=None,
            amount_charged=None,
            currency=None,
            requires_action=None,
            created_at=timezone.now().isoformat(),
        )

    result = await sync_to_async(Payments.charge_off_session)(
        saved_method=sm.as_saved_method(),
        amount=body.amount,
        currency=body.currency,
        description=body.description,
        idempotency_key=body.idempotency_key,
        metadata=body.metadata,
    )

    # Persist as ChargeRecord
    await ChargeRecord.objects.acreate(
        saved_method=sm,
        amount=body.amount,
        currency=body.currency,
        description=body.description,
        transaction_id=result.transaction_id or "",
        status=result.status.value,
        message=result.message,
        raw_response=result.raw_response or {},
        idempotency_key=body.idempotency_key or "",
    )

    return ChargeOut(
        status=result.status.value,
        message=result.message,
        transaction_id=result.transaction_id,
        amount_charged=str(result.amount_charged) if result.amount_charged else None,
        currency=result.currency,
        requires_action=result.requires_action,
        created_at=result.created_at.isoformat(),
    )


@api.get("/autopay/api/charges/{charge_id}/", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
async def get_charge(request, charge_id: int) -> ChargeOut:
    """Status polling. Re-fetch from gateway if the state is still pending."""
    # IDOR guard: charge must belong to authenticated user's saved methods
    try:
        rec = await ChargeRecord.objects.select_related("saved_method").aget(
            id=charge_id, saved_method__user=request.user
        )
    except ChargeRecord.DoesNotExist:
        # Return 404 to avoid leaking whether the charge exists
        return ChargeOut(
            status="failed",
            message="Charge not found",
            transaction_id=None,
            amount_charged=None,
            currency=None,
            requires_action=None,
            created_at=timezone.now().isoformat(),
        )

    if rec.status == "pending":
        # Sync from gateway
        sm = rec.saved_method
        result = await sync_to_async(Payments.get_charge_status)(sm.gateway, rec.transaction_id)
        if result.status.value != rec.status:
            rec.status = result.status.value
            rec.message = result.message
            await rec.asave(update_fields=["status", "message", "updated_at"])

    return ChargeOut(
        status=rec.status,
        message=rec.message,
        transaction_id=rec.transaction_id,
        amount_charged=str(rec.amount) if rec.status == "confirmed" else None,
        currency=rec.currency,
        requires_action=None,
        created_at=rec.created_at.isoformat(),
    )


@api.post("/autopay/api/webhooks/{variant}/", auth=None)
async def webhook_receiver(request, variant: str):
    """Optional webhook receiver. Thin shell on facade process_webhook.

    Dedup, signature verification and dispatch live in Payments.process_webhook:
    this endpoint is just an HTTP adapter. django-bolt does not expose the
    client IP, so IP-allowlist verification (Mollie) gets no client_ip here —
    use the Django webhook view when AUTOPAY_MOLLIE_ALLOWED_IPS is set.
    """
    try:
        payload = json.loads(await request.body())
    except json.JSONDecodeError:
        return WebhookReceiverOut(processed=False, event_id=None)

    headers = {k: v for k, v in request.headers.items()}
    result = await sync_to_async(Payments.process_webhook)(variant, payload, headers)

    return WebhookReceiverOut(processed=result.processed, event_id=result.event_id)


def _verify_attempt_signature(attempt_id: int, method_id: int, signature: str) -> bool:
    """HMAC-SHA256 of "attempt_id:method_id" using settings.AUTOPAY_WEBHOOK_SECRET.

    No secret configured → verification disabled (dev/test setup).
    """
    secret = getattr(settings, "AUTOPAY_WEBHOOK_SECRET", "")
    if not secret:
        return True
    expected = hmac.new(
        secret.encode(), f"{attempt_id}:{method_id}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")