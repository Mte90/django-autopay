# Refresh Flow

Token renewal and SCA handling for expired or revoked payment methods.

## When to Refresh

Call `refresh_method()` when:

1. `charge_off_session()` returns `REJECTED` — Token revoked
2. `charge_off_session()` returns `REQUIRES_SCA` persistently — Mandate no longer covered
3. Webhook `VAULT.PAYMENT-TOKEN.DELETED` received — Deactivates saved method (action: `revoke_method`)
4. Scheduled job — Proactive refresh before card expiry

## The Refresh Flow

### Step 1: Start Refresh

```python
from autopay.core.facade import Payments

setup = Payments.refresh_method(
    saved_method,
    reason="expired_card",  # or "revoked", "sca_required", "manual"
)

# setup contains:
# - setup_id: RefreshAttempt PK (for tracking)
# - frontend_sdk: "paypal" | "stripe" | "mollie"
# - frontend_payload: SDK-specific data (e.g., client_secret)
# - expires_at: When the setup expires
# - return_url: URL to redirect after flow
```

### Step 2: Frontend Integration

Each provider has different frontend requirements:

#### Stripe

```javascript
const stripe = Stripe('pk_test_...');
const { error } = await stripe.confirmCardSetup(
    setup.frontend_payload.client_secret,
    { payment_method: { card: cardElement } }
);
```

#### PayPal

```javascript
paypal.Buttons({
    onClick: () => {
        // Open PayPal flow with setup token
    }
}).render('#paypal-button');
```

### Step 3: Handle Redirect

After user completes the flow, they redirect to `setup.return_url`.

### Step 4: Confirm Refresh

Confirm via GET endpoint with HMAC signature:

```python
from autopay.core.facade import Payments

# Endpoint: GET /autopay/api/methods/{method_id}/refresh/confirm/?attempt={id}&sig={hmac}
# Optional: first_payment_id for Mollie
# HMAC verification: when AUTOPAY_WEBHOOK_SECRET is set, verifies SHA256 of "attempt_id:method_id"
# When unset, verification is disabled; mismatch returns 200 success=False
result = Payments.confirm_refresh(
    saved_method,
    refresh_payload={},  # Provider-specific keys (see below)
)
```

Provider-specific payload keys:
- **Stripe:** `setup_intent_id`
- **PayPal:** `setup_token_id`
- **Mollie:** `first_payment_id`

```python
# Example: Stripe
result = Payments.confirm_refresh(
    saved_method,
    refresh_payload={"setup_intent_id": "seti_xxx"},
)
```

## Race Guard

The facade implements a race guard for concurrent refresh attempts:

1. Create `RefreshAttempt` with status `pending`
2. User completes flow
3. Claim attempt: `pending → in_progress` (only one can succeed)
4. On provider success: `in_progress → confirmed`
5. On failure: `in_progress → failed`
6. **Expiry writes `expired`, then raises `RuntimeError`** — the attempt is marked before the error propagates

## Best Practices

### 1. Track Refresh Attempts

```python
from autopay.core.models import RefreshAttempt

attempts = RefreshAttempt.objects.filter(
    saved_method=saved_method,
    status="pending"
)
```

### 2. Set Expiry

Setup intents expire. Set reasonable timeouts:

```python
# Stripe SetupIntents expire after 1 hour
# PayPal setup tokens expire after 3 days
```

### 3. Handle Failures

```python
try:
    new_method = Payments.confirm_refresh(saved_method, payload)
except Exception as e:
    # Log failure, notify user
    pass
```

## Provider Differences

| Provider | Refresh Support | Notes |
|----------|----------------|-------|
| PayPal | Yes | Setup tokens, 3DS flow |
| Stripe | Yes | SetupIntents, 1h expiry |
| Mollie | Yes | New first payment creates new mandate |

## When Refresh Fails

If `confirm_refresh()` fails:

1. Mark `RefreshAttempt` as `failed`
2. Saved method stays active
3. Notify user to complete refresh or add new method