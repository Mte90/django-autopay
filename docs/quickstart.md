# Quick Start Guide

Install django-autopay and make your first off-session charge in 5 minutes.

## Installation

```bash
pip install "django-autopay[all]"
```

Or with uv:

```bash
uv add "django-autopay[all]"
```

### Install Extras

| Extra | Description | Python |
|-------|-------------|--------|
| `[stripe]` | Stripe provider only | >= 3.10 |
| `[paypal]` | PayPal provider only | >= 3.10 |
| `[mollie]` | Mollie provider only | >= 3.10 |
| `[api]` | REST API (django-bolt) | >= 3.12 ⚠️ |
| `[all]` | All providers + API | >= 3.12 ⚠️ |
| `[dev]` | Development dependencies | >= 3.10 |

## Add to INSTALLED_APPS

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "autopay",
]
```

**Note:** Use only `"autopay"` — do NOT add `"autopay.core"`. The app config in `autopay/apps.py` has `label = "autopay"`, and adding both entries causes migration conflicts.

## Configure a Provider

For development, use the DummyAutopayProvider (no network calls):

```python
# settings.py
PAYMENT_VARIANTS = {
    "dummy": ("autopay.dummy.DummyAutopayProvider", {}),
}
```

For production, see provider-specific guides:
- [PayPal](providers/paypal.md)
- [Stripe](providers/stripe.md)
- [Mollie](providers/mollie.md)

## Configure URLs

Add the autopay URL patterns to handle webhooks:

```python
# urls.py
from django.urls import path, include

urlpatterns = [
    # ...
    path("", include("autopay.core.urls")),  # Webhook endpoint: /payments/webhook/<variant>/
]
```

## First Charge

```python
from autopay.core.facade import Payments
from autopay.core.models import SavedPaymentMethod
from decimal import Decimal

# Get a saved payment method
method = SavedPaymentMethod.objects.filter(user=user, is_active=True).first()

# Charge off-session
result = Payments.charge_off_session(
    saved_method=method,
    amount=Decimal("10.00"),
    currency="EUR",
    description="Monthly subscription",
)

print(result.status)  # ChargeStatus.CONFIRMED
print(result.transaction_id)  # "txn_123"
```

## Save the Payment Method

After a successful on-session payment (e.g., during checkout), extract the vault token and persist it for future off-session charges:

```python
from autopay.core.facade import Payments

# After completing a checkout with a payment provider
payment = process_checkout(user, payment_token)  # Your checkout logic

# Save the payment method for recurring use
saved_method = Payments.save_method(payment)

if saved_method:
    print(f"Saved method ID: {saved_method.id}")
    print(f"Gateway: {saved_method.gateway}")
    print(f"Display label: {saved_method.display_label}")  # e.g., "Visa •••• 4242"
```

The `save_method()` call:
1. Extracts the vault token from the completed `Payment` object
2. Creates a `SavedPaymentMethod` row in your database
3. Returns a `SavedMethod` value object containing the gateway method ID

To store the saved method on your model's ForeignKey, use `saved_method.id`:

```python
# Example: assign to a OneToOneField or ForeignKey
subscription.saved_method_id = saved_method.id
subscription.save()
```

## Understanding the Result

```python
from autopay.core.result import ChargeStatus

if result.status == ChargeStatus.CONFIRMED:
    # Charge succeeded
    pass
elif result.status == ChargeStatus.PENDING:
    # Awaiting webhook confirmation
    pass
elif result.status == ChargeStatus.FAILED:
    # Permanent failure, retry won't help
    pass
elif result.status == ChargeStatus.REQUIRES_SCA:
    # 3DS required, user must be on-session
    pass
elif result.status == ChargeStatus.REJECTED:
    # Token revoked, call refresh_method()
    pass
```

## Next Steps

- Read [Configuration](configuration.md) for all settings
- See the `example/` directory in the repository for a working demo app
- Set up [Webhooks](webhooks.md) for async notifications