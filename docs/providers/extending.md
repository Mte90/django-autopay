# Extending the Provider System

This guide explains how to implement a custom payment provider for django-autopay.

## Provider Architecture

Providers are the gateway-specific implementation layer. The `Payments` facade handles cross-gateway dispatch:

1. **Facade** (`autopay.core.facade.Payments`) — static methods that dispatch to the correct provider
2. **Provider** — implements gateway-specific logic (e.g., `PaypalAutopayProvider`, `DummyAutopayProvider`)
3. **Routing** — `saved_method.gateway` determines which provider is instantiated via `provider_factory`

The user application never knows which gateway it is using. It passes a `SavedMethod` and the system routes to the correct provider.

## Capability Flags

All capability flags are class attributes on `AutopayProvider`. Override them in your subclass to declare support.

| Flag | Type | Description |
|------|------|-------------|
| `SUPPORTS_OFF_SESSION_CHARGE` | `bool` | Enables `charge_off_session()` — required for autopay-capable providers |
| `SUPPORTS_LIST_METHODS` | `bool` | Enables `list_saved_methods(customer_id)` — list all methods for a customer |
| `SUPPORTS_REVOKE` | `bool` | Enables `revoke_method(saved_method)` — delete a token on the gateway |
| `SUPPORTS_REFRESH` | `bool` | Enables `refresh_method()` and `confirm_refresh()` — regenerate expired/revoked tokens |
| `SUPPORTS_VALIDATE` | `bool` | Enables `validate_method(saved_method)` — check token validity without charging |
| `SUPPORTS_REFUND` | `bool` | Enables `refund(transaction_id, amount)` — refund a confirmed charge |
| `SUPPORTS_WEBHOOK_VERIFICATION` | `bool` | Enables `verify_webhook_request()` — verify webhook signature/authenticity |

Example:
```python
class MyProvider(AutopayProvider):
    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_LIST_METHODS = True
    SUPPORTS_REVOKE = True
    SUPPORTS_REFRESH = True
    SUPPORTS_VALIDATE = True
    SUPPORTS_REFUND = True
    SUPPORTS_WEBHOOK_VERIFICATION = True
```

## Required Methods

These abstract methods **must** be implemented by any provider that sets `SUPPORTS_OFF_SESSION_CHARGE = True`:

### `save_method(payment) -> SavedMethod`

Called after a successful on-session payment flow to persist the payment method.

```python
def save_method(self, payment) -> SavedMethod:
    """Save the payment method after a successful on-session flow."""
    # Extract vault token from payment.extra_data
    # Return SavedMethod with gateway, gateway_method_id, gateway_customer_id, etc.
```

### `charge_off_session(saved_method, amount, currency, description, idempotency_key, metadata) -> ChargeResult`

Charge an amount using the saved token/mandate. Synchronous.

```python
def charge_off_session(
    self,
    saved_method: SavedMethod,
    amount: Decimal,
    currency: str,
    description: str = "",
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> ChargeResult:
    """Charge using the saved token. Returns CONFIRMED, PENDING, FAILED, REQUIRES_SCA, or REJECTED."""
```

### `get_charge_status(transaction_id) -> ChargeResult`

Polling: retrieve the current status of a charge on the gateway.

```python
def get_charge_status(self, transaction_id: str) -> ChargeResult:
    """Poll the gateway for transaction status."""
```

## Optional Methods

These methods have default implementations that raise `NotImplementedError`. Implement them only if the corresponding capability flag is `True`.

### `list_saved_methods(customer_id) -> list[SavedMethod]`

List all saved payment methods for a customer on the gateway.

```python
def list_saved_methods(self, customer_id: str) -> list[SavedMethod]:
    """List saved methods for a customer_id on the gateway."""
```

**Enable with:** `SUPPORTS_LIST_METHODS = True`

### `revoke_method(saved_method) -> None`

Delete a payment method on the gateway.

```python
def revoke_method(self, saved_method: SavedMethod) -> None:
    """Revoke the saved method on the gateway (DELETE /tokens/<id>)."""
```

**Enable with:** `SUPPORTS_REVOKE = True`

### `refresh_method(saved_method) -> RefreshSetupData`

Start a new setup flow to regenerate an expired or revoked token.

```python
def refresh_method(self, saved_method: SavedMethod) -> RefreshSetupData:
    """Start a new setup flow. Returns gateway_setup_id, frontend_payload, frontend_sdk, expires_at."""
```

**Enable with:** `SUPPORTS_REFRESH = True`

### `confirm_refresh(saved_method, refresh_payload) -> SavedMethod`

Confirm the refresh after the frontend completes the setup flow. Updates the existing `SavedMethod` in-place.

```python
def confirm_refresh(self, saved_method: SavedMethod, refresh_payload: dict) -> SavedMethod:
    """Confirm refresh. Updates gateway_method_id and metadata. Returns updated SavedMethod."""
```

**Enable with:** `SUPPORTS_REFRESH = True`

### `validate_method(saved_method) -> ValidationResult`

Check if a saved token is still valid without charging. Read-only gateway call.

```python
def validate_method(self, saved_method: SavedMethod) -> ValidationResult:
    """Check token validity. Returns is_valid, is_expired, needs_refresh, reason, checked_at, raw_response."""
```

**Enable with:** `SUPPORTS_VALIDATE = True`

### `refund(transaction_id, amount, reason) -> ChargeResult`

Refund a confirmed charge (full if `amount=None`, partial otherwise).

```python
def refund(self, transaction_id: str, amount: Decimal | None = None, reason: str = "") -> ChargeResult:
    """Refund a confirmed charge. Returns CONFIRMED or PENDING."""
```

**Enable with:** `SUPPORTS_REFUND = True`

### `verify_webhook_request(raw_body, headers) -> bool`

Verify the authenticity of a webhook request (signature, HMAC, etc.).

```python
def verify_webhook_request(self, raw_body: bytes, headers) -> bool:
    """Verify webhook signature. Returns True if valid."""
```

**Enable with:** `SUPPORTS_WEBHOOK_VERIFICATION = True`

### `is_autopay_capable(provider) -> bool`

Helper function to check if a provider supports off-session charges. Used by the facade to gate dispatch.

```python
from autopay.core.base import is_autopay_capable

if is_autopay_capable(provider):
    # Safe to call charge_off_session()
    pass
```

Returns `True` if `isinstance(provider, AutopayProvider)` and `provider.SUPPORTS_OFF_SESSION_CHARGE`.

### Exceptions

#### `AutopayCapabilityError`

**Raised by the facade** (not `NotImplementedError`) when invoking a capability on a provider that does not support it. This is a configuration error, not a payment outcome.

```python
from autopay.core.base import AutopayCapabilityError

try:
    Payments.charge_off_session(saved_method, amount, currency)
except AutopayCapabilityError as e:
    # Provider lacks SUPPORTS_OFF_SESSION_CHARGE
    pass
```

Raised at:
- `charge_off_session()` - when provider is not an `AutopayProvider` or `SUPPORTS_OFF_SESSION_CHARGE=False`
- `list_saved_methods()`, `revoke_method()`, `refresh_method()`, `validate_method()` - when the corresponding capability flag is `False`

### `process_webhook_event(event_type, payload) -> WebhookEventResult | None`

Handle a specific webhook event type. Called after signature verification.

```python
def process_webhook_event(self, event_type: str, payload: dict) -> WebhookEventResult | None:
    """Process webhook event. Return action (revoke_method, status_update, etc.) or None to ignore."""
```

**Enable with:** `SUPPORTS_WEBHOOK_VERIFICATION = True` (required for webhook processing)

## Implementing a Custom Provider

### Step 1: Create the Provider Class

Subclass `AutopayProvider` and set your capability flags:

```python
from autopay.core.base import AutopayProvider
from autopay.core.result import ChargeResult, ChargeStatus
from autopay.core.saved_method import SavedMethod
from decimal import Decimal


class MyProvider(AutopayProvider):
    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_LIST_METHODS = True
    SUPPORTS_REVOKE = True
    SUPPORTS_REFRESH = False
    SUPPORTS_VALIDATE = True
    SUPPORTS_REFUND = False
    SUPPORTS_WEBHOOK_VERIFICATION = False
```

### Step 2: Implement Required Methods

```python
    def save_method(self, payment) -> SavedMethod:
        """Save the payment method after checkout."""
        # Extract token from payment.extra_data
        # Return SavedMethod instance
        return SavedMethod(
            gateway="myprovider",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Visa •••• 4242",
            metadata={"brand": "visa", "last4": "4242"},
            is_active=True,
        )

    def charge_off_session(
        self,
        saved_method: SavedMethod,
        amount: Decimal,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> ChargeResult:
        """Charge using the saved token."""
        # Call your gateway API
        # Return ChargeResult with status CONFIRMED, PENDING, FAILED, REQUIRES_SCA, or REJECTED
        return ChargeResult(
            status=ChargeStatus.CONFIRMED,
            transaction_id="txn_123",
            amount_charged=amount,
            currency=currency,
            message="Charged successfully",
            raw_response={},
            requires_action=None,
            created_at=datetime.now(timezone.utc),
        )

    def get_charge_status(self, transaction_id: str) -> ChargeResult:
        """Poll for charge status."""
        # Call gateway API to check status
        pass
```

### Step 3: Implement Optional Methods (as needed)

```python
    def validate_method(self, saved_method: SavedMethod) -> ValidationResult:
        """Check if token is still valid."""
        # Call gateway API (GET request, no charge)
        return ValidationResult(
            is_valid=True,
            is_expired=False,
            needs_refresh=False,
            reason="Token is valid",
            checked_at=datetime.now(timezone.utc),
            raw_response={},
        )

    def list_saved_methods(self, customer_id: str) -> list[SavedMethod]:
        """List all methods for a customer."""
        # Call gateway API to list payment methods
        return []

    def revoke_method(self, saved_method: SavedMethod) -> None:
        """Delete the token on the gateway."""
        # Call gateway DELETE endpoint
        pass
```

### Minimal Example (Dummy Provider)

The `DummyAutopayProvider` shows a complete minimal implementation:

```python
from autopay.core.base import AutopayProvider
from autopay.core.result import ChargeResult, ChargeStatus
from autopay.core.saved_method import SavedMethod
from datetime import datetime, timezone


class DummyAutopayProvider(AutopayProvider):
    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_LIST_METHODS = True
    SUPPORTS_REVOKE = True
    SUPPORTS_VALIDATE = True
    SUPPORTS_REFRESH = True
    SUPPORTS_WEBHOOK_VERIFICATION = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._methods: dict[str, dict] = {}  # Storage for saved methods
        self._charges: dict[str, dict] = {}  # Storage for charges

    def charge_off_session(self, saved_method, amount, currency, description="", **kwargs):
        """Simulate off-session charge with fail/decline support."""
        # Transaction ID includes timestamp for uniqueness
        transaction_id = f"dummy_charge_{saved_method.gateway_method_id}_{datetime.now(timezone.utc).timestamp()}"
        
        # Simulate failure if method ID contains "fail"
        if "fail" in saved_method.gateway_method_id:
            return ChargeResult(
                status=ChargeStatus.FAILED,
                transaction_id=transaction_id,
                amount_charged=amount,
                currency=currency,
                message="Simulated failure",
                raw_response={"error": "simulated_failure"},
                requires_action=None,
                created_at=datetime.now(timezone.utc),
            )
        
        # Simulate decline if method ID contains "decline"
        if "decline" in saved_method.gateway_method_id:
            return ChargeResult(
                status=ChargeStatus.REJECTED,
                transaction_id=transaction_id,
                amount_charged=amount,
                currency=currency,
                message="Simulated decline",
                raw_response={"error": "card_declined"},
                requires_action=None,
                created_at=datetime.now(timezone.utc),
            )

        # Success case
        self._charges[transaction_id] = {
            "amount": str(amount),
            "currency": currency,
            "status": "succeeded",
        }

        return ChargeResult(
            status=ChargeStatus.CONFIRMED,
            transaction_id=transaction_id,
            amount_charged=amount,
            currency=currency,
            message="Charge succeeded (dummy)",
            raw_response={"id": transaction_id, "status": "succeeded"},
            requires_action=None,
            created_at=datetime.now(timezone.utc),
        )

    def list_saved_methods(self, customer_id: str) -> list[SavedMethod]:
        """List saved methods from internal _methods dict."""
        methods = []
        for method_id, data in self._methods.items():
            if data.get("customer_id") == customer_id:
                methods.append(
                    SavedMethod(
                        gateway="dummy",
                        gateway_customer_id=customer_id,
                        gateway_method_id=method_id,
                        method_type=data.get("type", "card"),
                        display_label=data.get("label", "Dummy Method"),
                        is_active=True,
                        metadata=data.get("metadata", {}),
                    )
                )
        return methods

    def revoke_method(self, saved_method: SavedMethod) -> None:
        """Remove method from _methods dict."""
        if saved_method.gateway_method_id in self._methods:
            del self._methods[saved_method.gateway_method_id]

    def validate_method(self, saved_method: SavedMethod) -> ValidationResult:
        """Always returns valid (no reason field)."""
        return ValidationResult(
            is_valid=True,
            is_expired=False,
            needs_refresh=False,
            checked_at=datetime.now(timezone.utc),
        )
```

**Key behaviors:**
- `charge_off_session()`: Generates `dummy_charge_{id}_{timestamp}` transaction IDs; simulates failure/decline based on method ID
- `list_saved_methods()`: Returns methods from internal `_methods` dict
- `validate_method()`: Returns `ValidationResult` without `reason` field

## Registering a Provider

Add your provider to `PAYMENT_VARIANTS` in `settings.py`:

```python
PAYMENT_VARIANTS = {
    "myprovider": (
        "myapp.providers.myprovider.MyProvider",  # Import path
        {
            "api_key": os.environ["MYPROVIDER_API_KEY"],
            "endpoint": "https://api.myprovider.com",
        },
    ),
}
```

The tuple format is: `(class_path, kwargs)`

- `class_path`: String path to your provider class
- `kwargs`: Dictionary passed to `__init__` (in addition to standard `merchant` kwargs)

## Facade Integration

Once registered, your provider is automatically available through the `Payments` facade:

```python
from autopay.core.facade import Payments

# Charge off-session
result = Payments.charge_off_session(
    saved_method=saved_method,
    amount=Decimal("25.00"),
    currency="EUR",
    description="Subscription",
)

# Validate a method
validation = Payments.validate_method(saved_method)

# List all methods for a user
methods = Payments.list_saved_methods(user)

# Revoke a method
Payments.revoke_method(saved_method)
```

The facade handles routing based on `saved_method.gateway`. Your application code does not need to know which provider is being used.