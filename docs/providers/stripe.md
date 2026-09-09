# Stripe Provider

Stripe integration for django-autopay with PaymentIntents and SetupIntents.

## Installation

```bash
pip install django-autopay[stripe]
```

## Setup

### 1. Create Stripe Account

1. Go to [Stripe Dashboard](https://dashboard.stripe.com/)
2. Get your API keys (Developers → API keys)
3. Configure webhook endpoint

### 2. Configure PAYMENT_VARIANTS

```python
PAYMENT_VARIANTS = {
    "stripe": (
        "autopay.providers.stripe.StripeAutopayProvider",
        {
            "api_key": os.environ["STRIPE_SECRET_KEY"],
            "use_token": True,  # Use token-based auth (default)
            "endpoint_secret": os.environ.get("STRIPE_WEBHOOK_SECRET", ""),  # Optional
            "timeout": 30,  # Request timeout in seconds
        },
    ),
}
```

**Constructor parameters:**
- `api_key` (required): Stripe secret key
- `use_token` (optional, default=True): Use token-based authentication
- `endpoint_secret` (optional): Webhook signing secret
- `timeout` (optional, default=30): Request timeout in seconds

## Features

### Off-Session Charges

Stripe PaymentIntents support off-session charges with saved payment methods.

```python
from autopay.core.facade import Payments

result = Payments.charge_off_session(
    saved_method=saved_method,
    amount=Decimal("25.00"),
    currency="EUR",
    description="Monthly subscription",
    idempotency_key=f"charge_{user.id}_{date.today()}",  # Prevent duplicates
)
```

### Zero-Decimal Currencies

The provider automatically handles zero-decimal currencies (16 total: JPY, KRW, VND, CLP, XOF, XAF, XPF, BIF, DJF, GNF, KMF, MGA, PYG, RWF, UGX, VUV):

```python
# EUR - 2 decimals
Payments.charge_off_session(..., amount=Decimal("25.00"), currency="EUR")

# JPY - 0 decimals (automatic)
Payments.charge_off_session(..., amount=Decimal("1000"), currency="JPY")
```

### Refresh Flow

**Note:** `AUTOPAY_STRIPE_PUBLISHABLE_KEY` is REQUIRED for `refresh_method`. The provider raises `ImproperlyConfigured` without it (see [configuration.md](../configuration.md)).

When a payment method expires or fails:

```python
from autopay.core.facade import Payments

# Start refresh with SetupIntent
setup = Payments.refresh_method(saved_method, reason="card_expired")

# Frontend uses Stripe.js with setup.client_secret
# User enters new card details

# Confirm after redirect
new_method = Payments.confirm_refresh(saved_method, {"setup_intent_id": "seti_xxx"})
```

### Validation

Check payment method status without charging:

```python
from autopay.core.facade import Payments

result = Payments.validate_method(saved_method)
if result.is_valid:
    # Card is valid
    pass
elif result.is_expired:
    # Card expired, needs refresh
    pass
```

## Webhooks

### Endpoint Wiring

Configure Stripe to send webhooks to:

```
POST /autopay/api/webhooks/stripe/
```

Or for plain Django:

```
POST /payments/webhook/stripe/
```

### Event Types

Stripe sends these events:

- `payment_intent.succeeded` - Charge succeeded
- `payment_intent.payment_failed` - Charge failed
- `charge.refunded` - Charge refunded
- `charge.dispute.created` - Dispute created

### Signature Verification

Enable webhook verification:

```python
PAYMENT_VARIANTS = {
    "stripe": (
        "autopay.providers.stripe.StripeAutopayProvider",
        {
            "api_key": os.environ["STRIPE_SECRET_KEY"],
            "endpoint_secret": os.environ["STRIPE_WEBHOOK_SECRET"],
        },
    ),
}
```

## Sandbox Testing

Use test keys for testing:

```python
PAYMENT_VARIANTS = {
    "stripe": (
        "autopay.providers.stripe.StripeAutopayProvider",
        {
            "api_key": "sk_test_your-test-key",
        },
    ),
}
```

Test card numbers:
- `4242 4242 4242 4242` - Success
- `4000 0000 0000 9995` - Declined
- `4000 0025 0000 3155` - Requires SCA

## Capability Flags

```python
SUPPORTS_OFF_SESSION_CHARGE = True
SUPPORTS_LIST_METHODS = True
SUPPORTS_REVOKE = True
SUPPORTS_VALIDATE = True
SUPPORTS_REFRESH = True
SUPPORTS_WEBHOOK_VERIFICATION = True
```

## Error Mapping

| Stripe Error | ChargeStatus |
|-------------|--------------|
| `succeeded` | CONFIRMED |
| `requires_action` | REQUIRES_SCA |
| `card_declined` | REJECTED |
| `authentication_required` | REQUIRES_SCA |
| `invalid_api_key` | FAILED (retryable) |