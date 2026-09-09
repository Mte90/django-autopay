# Mollie Provider

Mollie integration for django-autopay with customers, mandates, and recurring payments.

## Installation

```bash
pip install "django-autopay[mollie]"
```

Or with uv:

```bash
uv add "django-autopay[mollie]"
```

## Setup

### 1. Create Mollie Account

1. Go to [Mollie Dashboard](https://www.mollie.com/dashboard/)
2. Get your API key (Developers → API keys)
3. Configure webhook endpoint in Mollie dashboard

### 2. Configure PAYMENT_VARIANTS

```python
# settings.py
PAYMENT_VARIANTS = {
    "mollie": (
        "autopay.providers.mollie.MollieAutopayProvider",
        {
            "api_key": os.environ["MOLLIE_API_KEY"],
            "access_token": None,  # OAuth2 access token (alternative to api_key)
            "client_id": None,  # OAuth2 client ID
            "client_secret": None,  # OAuth2 client secret
            "testmode": False,  # Enable test mode
            "webhook_url": None,  # Webhook URL for off-session flows
        },
    ),
}
```

**Constructor parameters:**
- `api_key` (optional): Mollie API key (testmode or live)
- `access_token` (optional): OAuth2 access token (alternative to api_key)
- `client_id` (optional): OAuth2 client ID
- `client_secret` (optional): OAuth2 client secret
- `testmode` (optional, default=False): Enable test mode
- `webhook_url` (optional): Webhook URL used by `charge_off_session()` and `refresh_method()` for redirect URLs

### 3. Configure Base URL

Required for refresh flow redirect URLs:

```python
# settings.py
AUTOPAY_SITE_BASE_URL = "https://yourdomain.com"
```

## Features

### Off-Session Charges

Mollie supports recurring payments via customer mandates.

```python
from autopay.core.facade import Payments
from decimal import Decimal

result = Payments.charge_off_session(
    saved_method=saved_method,
    amount=Decimal("25.00"),
    currency="EUR",
    description="Monthly subscription",
)
```

**Note:** A "first" payment with `sequenceType='first'` is required to create the mandate. Subsequent charges use `sequenceType='recurring'`.

### Save Method

After a successful first payment, call `save_method()` to extract the mandate:

```python
from autopay.core.facade import Payments

# After user completes on-session payment
saved_method = Payments.save_method(payment)

# Persist to your database
SavedPaymentMethod.objects.create(
    user=user,
    gateway="mollie",
    gateway_customer_id=saved_method.gateway_customer_id,
    gateway_method_id=saved_method.gateway_method_id,  # mandate ID
    method_type=saved_method.method_type,
    display_label=saved_method.display_label,
    metadata=saved_method.metadata,
)
```

### Refresh Flow

Mollie supports token refresh via a new first payment:

```python
from autopay.core.facade import Payments

# Start refresh
setup = Payments.refresh_method(saved_method, reason="expired_card")

# Frontend redirects to setup.frontend_payload["payment_url"]
# User completes payment, Mollie redirects to setup.return_url

# Confirm refresh (via GET with first_payment_id from redirect)
Payments.confirm_refresh(saved_method, {"first_payment_id": "tr_xxx"})
```

**Refresh Details:**
- Creates a new first payment (`sequenceType='first'`)
- Mollie creates a new mandate after successful payment
- Old mandate is automatically revoked
- New mandate replaces the old one in `SavedPaymentMethod`

### Validation

Mollie supports mandate validation via API:

```python
from autopay.core.facade import Payments

result = Payments.validate_method(saved_method)

# result.is_valid = True if mandate status is "valid"
# result.is_valid = False if mandate is "invalid" or "pending"
# result.needs_refresh = True if mandate needs attention
```

**Mandate Statuses:**
- `valid` — Mandate active, can charge
- `pending` — Awaiting first payment confirmation
- `invalid` — Revoked by customer or bank

## Webhooks

### Endpoint Wiring

Configure Mollie to send webhooks to:

```
POST /payments/webhook/mollie/
```

Or via the included URLs:

```python
# urls.py
from django.urls import include, path

urlpatterns = [
    path("", include("autopay.core.urls")),
]
```

### Content Type

Mollie sends **form-encoded** webhooks:

```
Content-Type: application/x-www-form-urlencoded

id=tr_xxx
```

Behind some proxies, the body may arrive as JSON. The provider's `verify_webhook_request()` handles both formats.

### Event Types

Mollie sends webhooks for payment status changes:

| Mollie Status | ChargeStatus |
|--------------|--------------|
| `paid` | CONFIRMED |
| `failed` | FAILED |
| `refunded` | REFUNDED |
| `canceled` | FAILED |

**Note:** Mollie does not use event types like Stripe. The webhook payload contains the current `status` field.

### Deduplication Strategy

Mollie sends a webhook for **every status change** of a payment:

```
payment.created → payment.paid → payment.refunded
```

The facade uses a **composite event ID** (`payment_id:status`) to ensure each state is processed independently.

### IP Allowlist

Mollie does not sign webhooks. Use IP filtering in production:

```python
# settings.py
AUTOPAY_MOLLIE_ALLOWED_IPS = [
    "185.55.224.10",  # Exact IP only
    "185.55.225.20",  # Exact IP only
]
```

**Important:** The provider uses **exact string matching** (not CIDR ranges). Each IP must be specified individually. Check for updates at https://docs.mollie.com/overview/network-ips

## Sandbox Testing

Use test mode for development:

```python
PAYMENT_VARIANTS = {
    "mollie": (
        "autopay.providers.mollie.MollieAutopayProvider",
        {
            "api_key": "test_YourTestKey",
        },
    ),
}
```

## Capability Flags

```python
SUPPORTS_OFF_SESSION_CHARGE = True
SUPPORTS_LIST_METHODS = True
SUPPORTS_REVOKE = True
SUPPORTS_VALIDATE = True  # Mandate validation via API
SUPPORTS_REFRESH = True   # New first payment flow
SUPPORTS_REFUND = True    # Full/partial refunds
SUPPORTS_WEBHOOK_VERIFICATION = False  # IP allowlist only
```

## Supported Payment Methods

Mollie mandates support:

- **Credit/Debit Cards** — `method_type = "card"`
- **SEPA Direct Debit** — `method_type = "sepa"`
- **PayPal** — `method_type = "paypal"`

## Limitations

- Webhook verification via IP allowlist only (no signature)
- Mandate validation requires API call (no local cache)
- Refresh requires new first payment (cannot extend existing mandate)