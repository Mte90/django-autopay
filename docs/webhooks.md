# Webhooks

Webhook handling, deduplication, and endpoint wiring.

## Facade Deduplication Contract

The facade `Payments.process_webhook()` handles deduplication automatically:

1. **Insert-then-catch** - Wrap insert in `transaction.atomic()`, catch `IntegrityError` on replay
2. **Dispatch before dedup for Mollie** - Mollie provider dispatched first (uses webhook payload for `new_status`), then dedup insert
3. **Dispatch after dedup for Stripe/PayPal** - Only dispatch after insert succeeds
4. **Return existing** - On replay, return success WITHOUT re-dispatching

**Note:** For Mollie, a replayed webhook calls the Mollie API once before returning noop — by design. Mollie payloads contain only the payment `id`, so the provider must fetch the payment to learn the current status; a replay cannot be told apart from a genuine transition without that fetch.

### Event ID Strategy by Provider

| Provider | Event ID Strategy |
|----------|------------------|
| Stripe | `event.id` from webhook payload |
| PayPal | `id` then `event_id` from payload (underscore) |
| Mollie | Composite key: `payment_id:status` (e.g., `tr_abc123:confirmed`) |

**Mollie Composite Key:** Mollie uses `payment_id:new_status` where `new_status` is the Charge enum VALUE (e.g., `tr_abc123:confirmed`, never `:paid`).

## Endpoint Wiring

### Option 1: Plain Django (Recommended)

The package provides a ready-made view and URL config:

```python
# urls.py
from django.urls import path, include

urlpatterns = [
    # ...
    path("", include("autopay.core.urls")),
]
```

This registers:
- `POST /payments/webhook/<variant>/` — Webhook receiver for any provider

Example provider-specific URLs:
- `POST /payments/webhook/stripe/`
- `POST /payments/webhook/paypal/`
- `POST /payments/webhook/mollie/`

### Option 2: Custom View

```python
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from autopay.core.facade import Payments
from autopay.core.views import autopay_webhook_view  # Reuse the built-in view

# Use the built-in view directly
urlpatterns = [
    path("payments/webhook/<slug:variant>/", autopay_webhook_view, name="autopay_webhook"),
]
```

## Content Types by Provider

| Provider | Content Type | Body Format |
|----------|-------------|-------------|
| Stripe | `application/json` | JSON |
| PayPal | `application/json` | JSON |
| Mollie | `application/x-www-form-urlencoded` | Form-encoded (may arrive as JSON behind proxy) |

The facade automatically parses the body based on `Content-Type`.

## Signature Verification

### Stripe

**Header:** `Stripe-Signature`

Configure the webhook signing secret in `PAYMENT_VARIANTS`:

```python
PAYMENT_VARIANTS = {
    "stripe": (
        "autopay.providers.stripe.StripeAutopayProvider",
        {
            "api_key": "sk_test_xxx",
            "endpoint_secret": "whsec_xxx",  # Required for signature verification
        },
    ),
}
```

- `SUPPORTS_WEBHOOK_VERIFICATION = True`
- Signature verified automatically using `endpoint_secret`

### PayPal

**Header:** `PAYPAL-TRANSMISSION-SIG`, `PAYPAL-CERT-URL`, `PAYPAL-TRANSMISSION-ID`, `PAYPAL-TRANSMISSION-TIME`, `PAYPAL-AUTH-ALGO`

Configure in `PAYMENT_VARIANTS`:

```python
PAYMENT_VARIANTS = {
    "paypal": (
        "autopay.providers.paypal.PaypalAutopayProvider",
        {
            "client_id": "xxx",
            "secret": "xxx",
            "webhook_id": "xxx",  # Recommended for signature verification
        },
    ),
}
```

- `SUPPORTS_WEBHOOK_VERIFICATION = True`
- Signature verified automatically using transmission headers + webhook_id

### Mollie

**No signature header** — Mollie does not sign webhooks by default.

```python
PAYMENT_VARIANTS = {
    "mollie": (
        "autopay.providers.mollie.MollieAutopayProvider",
        {
            "api_key": "test_xxx",
        },
    ),
}
```

- `SUPPORTS_WEBHOOK_VERIFICATION = True` — verified via the optional IP allowlist
- With no allowlist configured, any payload containing an `id` is trusted (see below)

### Mollie IP Allowlist

Configure allowed IPs in settings:

```python
# settings.py
AUTOPAY_MOLLIE_ALLOWED_IPS = [
    "185.55.224.1",  # Exact match only
    "185.55.225.42",
]
```

**Note:** Exact string matching only — no CIDR support.

**Note:** Enforcement is strict: when the allowlist is set, the webhook is rejected unless the client IP (`REMOTE_ADDR`) is present and listed. The Django webhook view supplies `REMOTE_ADDR` automatically; the optional bolt webhook route cannot — django-bolt does not expose the client IP.

## Event Handling

### Stripe Events

| Event | Action |
|-------|--------|
| `payment_intent.succeeded` | `status_update` — Update charge status |
| `payment_intent.payment_failed` | `status_update` — Mark charge as failed |
| `charge.refunded` | `refund` — Process refund |
| `charge.dispute.created` | `chargeback` — Initiate chargeback handling |

Example:

```python
# Stripe webhook payload
result = Payments.process_webhook("stripe", raw_body, headers, "application/json")

# result.action = "status_update" or "refund" or "chargeback"
# result.processed = True
```

### PayPal Events

| Event | Action |
|-------|--------|
| `PAYMENT.CAPTURE.COMPLETED` | `noop` — Ignored (no action needed) |
| `PAYMENT.CAPTURE.REFUNDED` | `refund` — Process refund |
| `PAYMENT.CAPTURE.REVERSED` | `refund` — Process refund |
| `VAULT.PAYMENT-TOKEN.DELETED` | `revoke_method` — Deactivate saved method |

Example:

```python
# PayPal webhook payload
result = Payments.process_webhook("paypal", raw_body, headers, "application/json")
```

### Mollie Events

Mollie sends form-encoded webhooks with status in the payload:

| Status | Action |
|--------|--------|
| `paid` | `status_update` — Charge succeeded |
| `pending` | `status_update` — Awaiting confirmation |
| `open` | `status_update` — Payment started (Mandate) |
| `failed` | `status_update` — Charge failed |
| `canceled` | `status_update` — Charge canceled |
| `expired` | `status_update` — Charge expired |

Example:

```python
# Mollie webhook payload (form-encoded)
result = Payments.process_webhook("mollie", raw_body, headers, "application/x-www-form-urlencoded")
```

## Error Handling

### Signature Verification Failed

```python
result = Payments.process_webhook("stripe", raw_body, headers, "application/json")

# result.processed = False
# result.action = "error"
# result.error = "Webhook signature verification failed"
# HTTP 400 returned to gateway (will retry)
```

### Unknown Event Type

```python
result = Payments.process_webhook("stripe", raw_body, headers, "application/json")

# result.processed = False
# result.action = "ignored"
# Event recorded but not processed
```

### Invalid Body

```python
result = Payments.process_webhook("mollie", b"invalid", headers, "application/x-www-form-urlencoded")

# result.processed = False
# result.action = "error"
# result.error = "Invalid body"
```

## Best Practices

### 1. Never Trust Payload for Money

Always re-read charge details from the gateway API:

```python
# Don't trust webhook amount directly
amount = payload["amount"]

# Re-read from API
payment = stripe.PaymentIntent.retrieve(payload["data"]["object"]["id"])
amount = payment.amount_received
```

### 2. Idempotency

The facade handles idempotency via `ProcessedWebhookEvent`. Your handlers should be idempotent too.

### 3. Retry Logic

Gateways retry failed webhooks. The deduplication ensures you only process once.

### 4. Logging

Log all webhook events for debugging:

```python
from autopay.core.models import ProcessedWebhookEvent

event = ProcessedWebhookEvent.objects.get(gateway="stripe", event_id="evt_xxx")
print(event.received_at, event.payload)
```

## Testing Webhooks

### Local Development

Use a tunneling service (ngrok, localtunnel) to expose your local server:

```bash
ngrok http 8000
```

Configure the webhook URL in your provider dashboard to point to:
```
https://<ngrok-id>.ngrok.io/payments/webhook/stripe/
```

### Test Events

**Stripe:**
```bash
stripe trigger payment_intent.succeeded
```

**PayPal:** Use the PayPal Dashboard → Webhooks → Simulate webhook.

**Mollie:** Use the Mollie Dashboard → Developers → Webhooks → Test webhook.