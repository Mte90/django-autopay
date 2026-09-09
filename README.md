# django-autopay

Recurring / off-session charge extension for
[django-payments](https://github.com/django-payments/django-payments): save a
payment method once, then charge it later without the user present — across
PayPal, Mollie and Stripe with one uniform API.

**Status: work in progress (0.1.0).** APIs are not stable yet.

## Quick Start

### Installation

**Supported versions:** Python >= 3.10, Django >= 5.2 (tested with 5.2/6.1).

```bash
pip install "django-autopay[all]"
```

Or with uv:

```bash
uv add "django-autopay[all]"
```

**Note:** The `[api]` extra (django-bolt) requires Python >= 3.12. On Python 3.10/3.11, installing `[all]` will skip django-bolt.

### Install Extras

| Extra | Description | Python |
|-------|-------------|--------|
| `[stripe]` | Stripe provider only | >= 3.10 |
| `[paypal]` | PayPal provider only | >= 3.10 |
| `[mollie]` | Mollie provider only | >= 3.10 |
| `[api]` | REST API (django-bolt) | >= 3.12 ⚠️ |
| `[all]` | All providers + API | >= 3.12 ⚠️ |
| `[dev]` | Development dependencies | >= 3.10 |

### Install from GitHub

The package installs straight from the git repository — useful before the first PyPI release or to track `main`:

```bash
uv add "django-autopay @ git+https://github.com/Mte90/django-autopay"
```

With all providers and the REST API:

```bash
uv add "django-autopay[all] @ git+https://github.com/Mte90/django-autopay"
```

Pin a branch, tag or commit by appending `@<ref>`:

```bash
uv add "django-autopay @ git+https://github.com/Mte90/django-autopay@v0.1.0"
```

### Add to INSTALLED_APPS

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "autopay",
]
```

**Note:** Use only `"autopay"` — do NOT add `"autopay.core"`. The app config in `autopay/apps.py` has `label = "autopay"`, and adding both entries causes migration conflicts.

### Configure a Provider

```python
# settings.py
PAYMENT_VARIANTS = {
    "stripe": (
        "autopay.providers.stripe.StripeAutopayProvider",
        {"api_key": "sk_test_xxx"},
    ),
}

AUTOPAY_SITE_BASE_URL = "https://yourdomain.com"
```

### Configure URLs

```python
# urls.py
from django.urls import path, include

urlpatterns = [
    # ...
    path("", include("autopay.core.urls")),  # Webhooks: /payments/webhook/<variant>/
]
```

### Charge a Saved Method

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
print(result.transaction_id)  # "pi_123456"
```

See [docs/quickstart.md](docs/quickstart.md) for a 5-minute onboarding guide.

## Documentation

- [Quick Start](docs/quickstart.md) - Install and first charge
- [Configuration](docs/configuration.md) - Settings and PAYMENT_VARIANTS
- [REST API](docs/api.md) - Optional django-bolt JSON API
- [Providers](docs/providers/) - PayPal, Stripe, Mollie guides
- [Webhooks](docs/webhooks.md) - Deduplication and endpoint wiring
- [Refresh Flow](docs/refresh.md) - Token/mandate refresh guide
- [Examples](docs/examples/) - SaaS, utility, pay-as-you-go patterns

## Demo App

`example/` contains a runnable demo app. Bootstrap it — this creates the SQLite DB (gitignored, never committed) and a `demo` / `demo` superuser:

```bash
./example/setup.sh
uv run python example/manage.py runserver
```

Log in at http://localhost:8000/accounts/login/.

## License

MIT