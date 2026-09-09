# PayPal Provider

PayPal integration for django-autopay with OAuth2 authentication and vault handling.

## Installation

```bash
pip install django-autopay[paypal]
```

## Setup

### 1. Create PayPal Application

1. Go to [PayPal Developer Dashboard](https://developer.paypal.com/)
2. Create a REST API app
3. Note your Client ID and Secret

### 2. Configure PAYMENT_VARIANTS

```python
PAYMENT_VARIANTS = {
    "paypal": (
        "autopay.providers.paypal.PaypalAutopayProvider",
        {
            "client_id": os.environ["PAYPAL_CLIENT_ID"],
            "secret": os.environ["PAYPAL_SECRET"],
            "endpoint": "https://api-m.sandbox.paypal.com",  # Sandbox
            # "endpoint": "https://api.paypal.com",  # Production
            "capture": True,  # Capture payment immediately
            "webhook_id": os.environ["PAYPAL_WEBHOOK_ID"],  # REQUIRED for signature verification
            "timeout": 30,  # Request timeout in seconds
        },
    ),
}
```

**Constructor parameters:**
- `client_id` (required): PayPal client ID
- `secret` (required): PayPal secret
- `endpoint` (optional): API endpoint (sandbox or production)
- `capture` (optional, default=True): Capture payment immediately
- `webhook_id` (optional but REQUIRED for verification): PayPal webhook ID. Without this, `verify_webhook_request()` returns `False`
- `timeout` (optional, default=30): Request timeout in seconds

## Features

### Off-Session Charges

PayPal supports off-session charges via vaulted payment tokens.

```python
from autopay.core.facade import Payments

result = Payments.charge_off_session(
    saved_method=saved_method,
    amount=Decimal("25.00"),
    currency="EUR",
    description="Monthly subscription",
)
```

### Refresh Flow

When a token expires or is revoked:

```python
from autopay.core.facade import Payments

# Start refresh
setup = Payments.refresh_method(saved_method, reason="token_expired")

# Frontend redirects to setup.return_url
# User completes 3DS/SCA flow

# Confirm after redirect
new_method = Payments.confirm_refresh(saved_method, refresh_payload)
```

### Validation

Check if a payment method is still valid without charging:

```python
from autopay.core.facade import Payments

result = Payments.validate_method(saved_method)
if result.is_valid:
    # Can charge
    pass
elif result.needs_refresh:
    # Call refresh_method()
    pass
```

## Webhooks

### Endpoint Wiring

Configure PayPal to send webhooks to:

```
POST /autopay/api/webhooks/paypal/
```

Or for plain Django:

```
POST /payments/webhook/paypal/
```

### Event Types

PayPal sends these events:

- `PAYMENT.CAPTURE.COMPLETED` - Charge succeeded
- `VAULT.PAYMENT-TOKEN.DELETED` - Token revoked by user
- `PAYMENT.CAPTURE.REFUNDED` - Charge refunded
- `PAYMENT.CAPTURE.REVERSED` - Charge reversed

### Signature Verification

`SUPPORTS_WEBHOOK_VERIFICATION` is always `True`, but verification **fails unless `webhook_id` is configured**. The provider returns `False` from `verify_webhook_request()` without a valid `webhook_id`.

## Sandbox Testing

Use sandbox credentials for testing:

```python
PAYMENT_VARIANTS = {
    "paypal": (
        "autopay.providers.paypal.PaypalAutopayProvider",
        {
            "client_id": "sb",  # Sandbox client ID
            "secret": "sandbox_secret",
            "endpoint": "https://api-m.sandbox.paypal.com",
            "webhook_id": "sandbox_webhook_id",  # Required for verification
        },
    ),
}
```

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

| PayPal Status | ChargeStatus |
|--------------|--------------|
| COMPLETED | CONFIRMED |
| APPROVED / PAYER_ACTION_REQUIRED | REQUIRES_SCA |
| VOIDED | FAILED |
| HTTP 422 (AUTHENTICATION_FAILURE, TOKEN_EXPIRED, INVALID_RESOURCE_ID) | REJECTED |
| HTTP 422 (other) | FAILED |
| HTTP 404 | REJECTED |