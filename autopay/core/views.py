"""Django views for django-autopay core module."""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .facade import Payments


@csrf_exempt
@require_POST
def autopay_webhook_view(request, variant: str):
    """Django view to receive gateway webhooks.

    Thin wrapper that:
    1. Pass raw body and headers to facade
    2. Call facade.Payments.process_webhook
    3. Return JSON 200/400

    The real webhook logic (signature, dedup, dispatch) is in the facade.
    """
    # Pass raw body and headers directly to facade
    raw_body = request.body
    content_type = request.content_type
    headers = request.headers  # case-insensitive HttpHeaders

    result = Payments.process_webhook(
        variant,
        raw_body,
        headers,
        content_type,
        client_ip=request.META.get("REMOTE_ADDR", ""),
    )

    # Return appropriate response
    if result.processed or result.action == "noop":
        return JsonResponse({"status": "ok", "action": result.action})
    else:
        # Error response - gateway will retry
        return JsonResponse(
            {"status": "error", "action": result.action, "error": result.error or "unknown"},
            status=400,
        )