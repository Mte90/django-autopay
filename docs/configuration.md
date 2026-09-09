# Configuration

django-autopay settings and provider configuration.

## Required Settings

### PAYMENT_VARIANTS

Standard django-payments setting. Maps gateway names to provider classes and their constructor arguments.

```python
PAYMENT_VARIANTS = {
    "paypal": (
        "autopay.providers.paypal.PaypalAutopayProvider",
        {
            "client_id": "your-paypal-client-id",
            "secret": "your-paypal-secret",
            "endpoint": "https://api-m.sandbox.paypal.com",  # Optional, defaults to live
        },
    ),
    "stripe": (
        "autopay.providers.stripe.StripeAutopayProvider",
        {
            "api_key": "sk_test_your-stripe-secret-key",
            "endpoint_secret": "whsec_your-webhook-secret",  # Optional, for signature verification
        },
    ),
    "mollie": (
        "autopay.providers.mollie.MollieAutopayProvider",
        {
            "api_key": "test_your-mollie-api-key",
        },
    ),
    "dummy": (
        "autopay.dummy.DummyAutopayProvider",
        {},
    ),
}
```

## Optional Settings

### AUTOPAY_SITE_BASE_URL

Base URL for constructing redirect URLs in refresh flows. Used by the facade to build return URLs.

**Required for refresh flows.** There is no default: `Payments.refresh_method()` reads it directly and raises `AttributeError` when it is not set.

```python
# settings.py
AUTOPAY_SITE_BASE_URL = "https://yourdomain.com"
```

It must be a publicly reachable base URL so the cardholder's browser can open the refresh confirmation link after completing the frontend SDK setup.

### AUTOPAY_WEBHOOK_SECRET

HMAC secret for signing refresh confirmation links. When set, the GET confirm endpoint verifies signatures over `attempt_id:method_id`.

**Default:** `""` (verification disabled)

```python
# settings.py
AUTOPAY_WEBHOOK_SECRET = "your-secret-key"
```

**Note:** This is NOT a provider webhook secret. It is used only for signing the cardholder-facing refresh confirmation links (`GET /autopay/api/methods/{id}/refresh/confirm/?attempt=X&sig=...`).

Leave unset for development. Each provider has its own signature verification mechanism (Stripe-Signature, PayPal transmission headers).

### AUTOPAY_STRIPE_PUBLISHABLE_KEY

Publishable key for Stripe refresh flow frontend SDK. Required when using Stripe's SetupIntent for token refresh.

**Default:** `""` (required for Stripe refresh)

```python
# settings.py
AUTOPAY_STRIPE_PUBLISHABLE_KEY = "pk_test_your-publishable-key"
```

**Note:** Do NOT derive this from the secret key. It must be configured separately as it is a different key type.

### AUTOPAY_MOLLIE_ALLOWED_IPS

List of IP addresses allowed to send Mollie webhooks. Matching is exact string comparison — CIDR notation is not supported.

**Default:** `[]` (no IP filtering)

```python
# settings.py
AUTOPAY_MOLLIE_ALLOWED_IPS = [
    "185.55.224.12",
    "185.55.224.13",
]
```

> **Note:** enforcement is strict — when the list is non-empty, the webhook is rejected unless the sender IP is present and listed. The client IP is read from `REMOTE_ADDR`: the Django webhook view passes it automatically, while the optional bolt webhook route does not expose the client IP and cannot enforce the allowlist.

Check for updates at https://docs.mollie.com/overview/network-ips

### AUTOPAY_CUSTOMER_LOOKUP

Optional callable path for custom customer ID lookup. Defaults to searching `SavedPaymentMethod`.

**Default:** `None` (use default lookup)

```python
# settings.py
AUTOPAY_CUSTOMER_LOOKUP = "myapp.payments.get_customer_id"

# The callable must accept (user, variant_name) and return str | None
def get_customer_id(user, variant_name):
    # Custom logic to find gateway customer ID
    return CustomCustomer.objects.get(user=user, gateway=variant_name).gateway_id
```

### AUTOPAY_SAVED_METHOD_MODEL

Optional override for the model used to persist saved payment methods. Value is a Django model label (`"app.ModelName"`).

**Default:** `None` (uses the built-in `autopay.core.models.SavedPaymentMethod`)

```python
# settings.py
AUTOPAY_SAVED_METHOD_MODEL = "myapp.CustomSavedMethod"
```

A custom model must provide the same fields the facade and the API rely on (user, gateway, gateway method id, token data — see [Models](models.md)) and the `as_saved_method()` method returning the `SavedMethod` value object — every facade path and API handler calls it. Most projects should leave this unset.

## Django-payments Settings

django-autopay requires these django-payments settings:

```python
PAYMENT_HOST = "yourdomain.com"  # Required for payment URLs
```

## Provider-Specific Settings

### PayPal

Configure via `PAYMENT_VARIANTS`:
- `client_id`: PayPal OAuth2 client ID
- `secret`: PayPal OAuth2 secret
- `endpoint`: API endpoint (`https://api-m.sandbox.paypal.com` for sandbox, `https://api-m.paypal.com` for live)
- `webhook_id`: PayPal webhook ID (optional, for signature verification)

### Stripe

Configure via `PAYMENT_VARIANTS`:
- `api_key`: Stripe secret key (sk_*)
- `endpoint_secret`: Webhook signing secret (whsec_*, optional, for signature verification)

Additionally, set `AUTOPAY_STRIPE_PUBLISHABLE_KEY` for refresh flow.

### Mollie

Configure via `PAYMENT_VARIANTS`:
- `api_key`: Mollie API key (test_* or live_*)

Optionally, set `AUTOPAY_MOLLIE_ALLOWED_IPS` for IP filtering.

### Dummy

No configuration required. Use for development and testing.

## Environment Variables

For security, load secrets from environment variables:

```python
import os

PAYMENT_VARIANTS = {
    "stripe": (
        "autopay.providers.stripe.StripeAutopayProvider",
        {
            "api_key": os.environ["STRIPE_SECRET_KEY"],
        },
    ),
    "paypal": (
        "autopay.providers.paypal.PaypalAutopayProvider",
        {
            "client_id": os.environ["PAYPAL_CLIENT_ID"],
            "secret": os.environ["PAYPAL_SECRET"],
        },
    ),
}

AUTOPAY_SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "http://localhost")
AUTOPAY_WEBHOOK_SECRET = os.environ.get("AUTOPAY_WEBHOOK_SECRET", "")
AUTOPAY_STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
```