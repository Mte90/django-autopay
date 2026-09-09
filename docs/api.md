# REST API (django-bolt)

Optional JSON API on top of the facade, served by django-bolt. Requires the `[api]` extra and Python >= 3.12.

## Installation

```bash
pip install "django-autopay[api]"
```

Or with uv:

```bash
uv add "django-autopay[api]"
```

The extra is marker-gated to Python >= 3.12 because django-bolt requires it; the core package has no such floor.

## Getting the API object

`autopay.api.bolt` exposes a ready-made `BoltAPI` instance with all routes registered:

```python
from autopay.api.bolt import api
```

Mount it in your ASGI application as described in the django-bolt documentation. All routes use absolute paths under `/autopay/api/`, so mounting at the root keeps the URLs stable. No entry in `INSTALLED_APPS` is required.

## Authentication

### JWT Authentication (Protected Endpoints)

Protected endpoints use django-bolt's `JWTAuthentication`. Include the Authorization header:

```
Authorization: Bearer <jwt-token>
```

### Signature Verification (Webhooks)

Webhook endpoints authenticate via gateway-specific signatures:
- **Stripe:** `Stripe-Signature` header
- **PayPal:** `PAYPAL-TRANSMISSION-SIG`, `PAYPAL-CERT-URL`, `PAYPAL-TRANSMISSION-ID`, `PAYPAL-TRANSMISSION-TIME` headers
- **Mollie:** IP allowlist (no signature)

## Endpoints

### Saved Payment Methods

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/autopay/api/methods/` | JWT | List saved methods |
| POST | `/autopay/api/methods/` | JWT | Create a saved method |
| GET | `/autopay/api/methods/{method_id}/` | JWT | Retrieve a method |
| DELETE | `/autopay/api/methods/{method_id}/` | JWT | Deactivate a method |
| POST | `/autopay/api/methods/{method_id}/validate/` | JWT | Validate one method |
| POST | `/autopay/api/methods/validate_all/` | JWT | Validate all methods |

### Refresh Flow

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/autopay/api/methods/{method_id}/refresh/` | JWT | Start a refresh flow |
| GET | `/autopay/api/methods/{method_id}/refresh/` | JWT | Retrieve refresh setup data (requires `attempt` query param) |
| POST | `/autopay/api/methods/{method_id}/refresh/confirm/` | JWT | Confirm a refresh attempt |
| GET | `/autopay/api/methods/{method_id}/refresh/confirm/` | Signed link | Cardholder confirm page (Mollie) |

### Charges

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/autopay/api/charges/` | JWT | Off-session charge |
| GET | `/autopay/api/charges/{charge_id}/` | JWT | Charge status |

### Webhooks

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/autopay/api/webhooks/{variant}/` | Gateway signature | Webhook receiver |

## Request/Response Schemas

### List Methods

**Request:** `GET /autopay/api/methods/`

**Response:** `list[SavedMethodOut]`

```json
[
  {
    "id": 1,
    "gateway": "stripe",
    "method_type": "card",
    "display_label": "Visa •••• 4242",
    "is_default": true,
    "is_active": true,
    "metadata": {"brand": "visa", "last4": "4242"},
    "created_at": "2024-01-15T10:30:00Z"
  }
]
```

### Create Method

**Request:** `POST /autopay/api/methods/`

Body: `CreateMethodIn`

```json
{
  "gateway": "stripe",
  "payment_token": "123"
}
```

  **Note:** `payment_token` is the `Payment` primary key as a string (`payment.id` from the checkout process), not a gateway-side token. The facade fetches `Payment.objects.aget(id=int(body.payment_token))` and extracts the gateway token from that payment object.

**Response:** `CreateMethodOut`

```json
{
  "setup_id": 1,
  "frontend_sdk": "stripe",
  "frontend_payload": {},
  "return_url": "/autopay/api/methods/1/"
}
```

### Validate Method

**Request:** `POST /autopay/api/methods/{method_id}/validate/`

**Response:** `ValidationResultOut`

```json
{
  "is_valid": true,
  "is_expired": false,
  "needs_refresh": false,
  "reason": "Valid mandate",
  "checked_at": "2024-01-15T10:30:00Z",
  "method_id": 1,
  "gateway": "stripe"
}
```

### Refresh Method

**Request:** `POST /autopay/api/methods/{method_id}/refresh/`

Body: `RefreshMethodIn`

```json
{
  "reason": "expired_card"
}
```

**Response:** `RefreshMethodOut`

```json
{
  "setup_id": 42,
  "frontend_sdk": "stripe",
  "frontend_payload": {"client_secret": "seti_..."},
  "expires_at": "2024-01-15T11:30:00Z",
  "return_url": "/autopay/api/methods/1/refresh/confirm/?attempt=42"
}
```

### Confirm Refresh

**Request:** `POST /autopay/api/methods/{method_id}/refresh/confirm/`

Body: `ConfirmRefreshIn`

```json
{
  "setup_intent_id": "seti_123456",
  "setup_token_id": null,
  "payment_token_id": null,
  "first_payment_id": null
}
```

**Response:** `ConfirmRefreshOut`

```json
{
  "success": true,
  "method": {
    "id": 1,
    "gateway": "stripe",
    "method_type": "card",
    "display_label": "Visa •••• 1234",
    "is_default": false,
    "is_active": true,
    "metadata": {},
    "created_at": "2024-01-15T10:30:00Z"
  },
  "error": null
}
```

### Refresh Status (GET confirm endpoint)

**Request:** `GET /autopay/api/methods/{method_id}/refresh/confirm/`

**Response:** `RefreshStatusOut`

```json
{
  "success": true,
  "method_id": 1,
  "error": null
}
```

**Note:** On signature failure, returns `success=False` with `error="invalid signature"` — HTTP 200, not 403.

### Delete Method

**Request:** `DELETE /autopay/api/methods/{method_id}/`

**Response:** `DeleteMethodOut`

```json
{
  "success": true,
  "deleted_method_id": 1
}
```

### Off-Session Charge

**Request:** `POST /autopay/api/charges/`

Body: `ChargeIn`

```json
{
  "saved_method_id": 1,
  "amount": "25.00",
  "currency": "EUR",
  "description": "Monthly subscription",
  "idempotency_key": "unique-key-123",
  "metadata": {"subscription_id": "sub_123"}
}
```

**Response:** `ChargeOut`

```json
{
  "status": "confirmed",
  "message": "Charged successfully",
  "transaction_id": "pi_123456",
  "amount_charged": "25.00",
  "currency": "EUR",
  "requires_action": null,
  "created_at": "2024-01-15T10:30:00Z"
}
```

### Charge Status

**Request:** `GET /autopay/api/charges/{charge_id}/`

**Response:** `ChargeOut`

**Behavior:** Re-fetches a pending charge from the gateway and persists the updated status/message. If the charge is still pending, this endpoint queries the gateway API to get the current state, updates the local `ChargeRecord`, and returns the refreshed data (bolt.py:333-370).

```json
{
  "status": "confirmed",
  "message": "Charged successfully",
  "transaction_id": "pi_123456",
  "amount_charged": "25.00",
  "currency": "EUR",
  "requires_action": null,
  "created_at": "2024-01-15T10:30:00Z"
}
```

### Webhook Receiver

**Request:** `POST /autopay/api/webhooks/{variant}/`

Body: Gateway-specific webhook payload (JSON or form-encoded; the facade parses by content type). Use the Django webhook view in `autopay/core/views.py` instead when `AUTOPAY_MOLLIE_ALLOWED_IPS` is set — this endpoint cannot supply the client IP required by the fail-closed allowlist.

**Response:** `WebhookReceiverOut`

```json
{
  "processed": true,
  "event_id": "evt_123456"
}
```

- `processed: true` — Event was handled or already seen (replay)
- `processed: false` — Event rejected (invalid JSON, signature failure, unknown variant)

## Refresh Confirmation Links

The cardholder-facing confirm endpoint (`GET .../refresh/confirm/`) is issued without authentication. When `AUTOPAY_WEBHOOK_SECRET` is set, links carry:

```
/autopay/api/methods/{method_id}/refresh/confirm/?attempt={attempt_id}&sig={hmac}&first_payment_id={id}
```

The `sig` parameter is an HMAC-SHA256 of `"{attempt_id}:{method_id}"` using `AUTOPAY_WEBHOOK_SECRET`. Mismatching signatures return `200` with `RefreshStatusOut(success=False, method_id=None, error="invalid signature")` — **not HTTP 403**.

The `first_payment_id` query parameter is optional (used for Mollie redirects to initiate the first payment after refresh confirmation).

Without the secret configured (development only), the check is skipped.

## Testing

The `TestClient` from `django_bolt.testing` drops the Authorization header at the Rust layer, causing 401 errors despite valid JWT. Test bolt handlers directly with a `FakeRequest` object:

```python
import asyncio
from autopay.api.bolt import api


class FakeRequest:
    """Minimal async request for testing bolt handlers."""
    
    def __init__(self, user, headers: dict[str, str] | None = None):
        self.user = user
        self._headers = headers or {}
    
    async def body(self) -> bytes:
        return b""
    
    @property
    def headers(self) -> dict[str, str]:
        return self._headers


async def test_list_methods():
    handler = api.find("GET", "/autopay/api/methods/")
    req = FakeRequest(
        user=FakeUser(),
        headers={"Authorization": "Bearer test-token"}
    )
    response = await handler(req)
    assert response.status == 200


class FakeUser:
    """Minimal user for testing."""
    is_authenticated = True
    pk = 1
```

See `tests/test_api_http.py` for working examples against every endpoint.

## Error Responses

**Important:** This API does NOT use HTTP 403 or 404 for error conditions. All responses return HTTP 200 with a failure body containing error details.

| Status | Meaning |
|--------|---------|
| 200 | Success or logical error (see response body) |
| 200 (failure body) | Resource not found, signature failure, validation error |
| 400 | Bad request (malformed JSON, missing required fields) |
| 401 | Unauthorized (missing/invalid JWT on protected endpoints) |
| 500 | Internal server error |

**Failure response bodies:**

- **Resource not found:** `ChargeOut(status="failed", message="<resource> not found")` or `RefreshStatusOut(success=False, method_id=None, error="<reason>")`
- **Signature failure (GET refresh confirm):** `RefreshStatusOut(success=False, method_id=None, error="invalid signature")`
- **Webhook signature failure:** `WebhookReceiverOut(processed=False, ...)`
- **Validation error:** `ChargeOut(status="failed", message="<validation error>")`

Error responses include a machine-readable body with details.

## Facade API Reference

The `Payments` facade provides static methods for cross-gateway payment operations. All methods dispatch on `saved_method.gateway` via `provider_factory`.

### save_method(payment)

Extracts and saves a payment method token from a completed on-session payment.

**Signature:**
```python
@staticmethod
def save_method(payment) -> SavedMethod | None
```

**Parameters:**
- `payment`: A completed payment object from the gateway

**Returns:** `SavedMethod` value object (not the ORM instance) with the extracted vault/token, or `None` if:
1. Gateway is not autopay-capable
2. `payment.user` is `None`
3. The provider's `save_method` returned `None`

**Important:** The return is a value object. Persist `saved_method.id` on FK fields (e.g., `SavedPaymentMethod.gateway_method_id = saved_method.id`).

**Example:**
```python
from autopay.core.facade import Payments

# After processing an on-session payment
saved_method = Payments.save_method(payment)
if saved_method:
    # Store for future off-session charges
    print(f"Saved {saved_method.gateway_method_id} for {saved_method.gateway}")
```

### sync_saved_methods(user)

Synchronizes saved payment methods from the gateway for a user.

**Signature:**
```python
@staticmethod
def sync_saved_methods(user) -> list[SavedMethodProtocol]
```

**Parameters:**
- `user`: Django user object

**Returns:** List of `SavedMethodProtocol` objects representing active methods

**Behavior:** Reconciles local `SavedPaymentMethod` records with the gateway. Marks methods as `is_active=False` if they no longer exist on the gateway. One API call per configured gateway.

**Example:**
```python
from autopay.core.facade import Payments

# Run from a scheduled job (e.g., Celery beat)
methods = Payments.sync_saved_methods(request.user)
print(f"Synced {len(methods)} methods")
```

### revoke_method(saved_method)

Revokes a saved payment method at the gateway.

**Signature:**
```python
@staticmethod
def revoke_method(saved_method: SavedMethodProtocol) -> None
```

**Parameters:**
- `saved_method`: The `SavedMethodProtocol` to revoke

**Raises:** `AutopayCapabilityError` if the gateway does not support revocation

**Example:**
```python
from autopay.core.facade import Payments
from .models import SavedPaymentMethod

method = SavedPaymentMethod.objects.get(pk=method_id, user=user)
Payments.revoke_method(method.as_saved_method())
# Method is revoked on the gateway. Facade-only callers must set `is_active=False` on the `SavedPaymentMethod` themselves.
```

### get_charge_status(gateway, transaction_id)

Polls the gateway for the current transaction status.

**Signature:**
```python
@staticmethod
def get_charge_status(gateway: str, transaction_id: str) -> ChargeResult
```

**Parameters:**
- `gateway`: The provider variant (e.g., "stripe", "paypal", "mollie")
- `transaction_id`: The transaction ID from the initial charge

**Returns:** `ChargeResult` with the current status

**Example:**
```python
from autopay.core.facade import Payments

# Check status of a pending charge
result = Payments.get_charge_status("stripe", "pi_123456")
print(f"Status: {result.status}")
```

### refund(gateway, transaction_id, amount=None, reason="")

Refunds a confirmed charge. Full refund if `amount=None`, partial otherwise.

**Signature:**
```python
@staticmethod
def refund(
    gateway: str,
    transaction_id: str,
    amount: Decimal | None = None,
    reason: str = ""
) -> ChargeResult
```

**Parameters:**
- `gateway`: The provider variant
- `transaction_id`: The original charge transaction ID
- `amount`: Refund amount (optional). `None` for full refund
- `reason`: Optional refund reason

**Returns:** `ChargeResult` with the refund status

**Raises:** `AutopayCapabilityError` if the gateway does not support refunds (checks `SUPPORTS_REFUND` capability flag)

**Example:**
```python
from decimal import Decimal
from autopay.core.facade import Payments

# Full refund
result = Payments.refund("stripe", "pi_123456")

# Partial refund
result = Payments.refund("stripe", "pi_123456", amount=Decimal("10.00"), reason="Customer request")
```

### validate_all_user_methods(user)

Validates all active saved methods for a user across all gateways.

**Signature:**
```python
@staticmethod
def validate_all_user_methods(user) -> list[tuple[int, ValidationResult]]
```

**Parameters:**
- `user`: Django user object

**Returns:** List of tuples `(saved_method_id, ValidationResult)` for each active method

**Behavior:** Calls `validate_method()` on each method, updates `last_validated_at` in the database, and populates validation fields (`last_validation_is_valid`, `last_validation_reason`).

**Example:**
```python
from autopay.core.facade import Payments

results = Payments.validate_all_user_methods(request.user)
for method_id, result in results:
    if result.needs_refresh:
        print(f"Method {method_id} needs refresh: {result.reason}")
```

### is_autopay_capable(provider)

Checks if a provider supports off-session charges.

**Signature:**
```python
@staticmethod
def is_autopay_capable(provider: AutopayProvider) -> bool
```

**Parameters:**
- `provider`: An `AutopayProvider` instance

**Returns:** `True` if the provider supports off-session charges

**Example:**
```python
from autopay.core.facade import Payments
from payments.core import provider_factory

provider = provider_factory("stripe")
if Payments.is_autopay_capable(provider):
    print("Gateway supports autopay")
```

### charge_off_session(saved_method, amount, currency="EUR", description="", idempotency_key=None, metadata=None)

Executes an off-session charge using a saved payment method.

**Signature:**
```python
@staticmethod
def charge_off_session(
    saved_method: SavedMethod,
    amount: Decimal,
    currency: str,
    description: str = "",
    idempotency_key: str | None = None,
    metadata: dict | None = None
) -> ChargeResult
```

**Parameters:**
- `saved_method`: The `SavedMethod` value object to charge
- `amount`: Charge amount
- `currency`: Currency code (default: "EUR")
- `description`: Human-readable charge description
- `idempotency_key`: Idempotency key for retry safety (optional)
- `metadata`: Extra data to attach to the charge (optional)

**Returns:** `ChargeResult` with the charge status

**Example:**
```python
from decimal import Decimal
from autopay.core.facade import Payments

result = Payments.charge_off_session(
    saved_method=saved_method,
    amount=Decimal("25.00"),
    currency="EUR",
    description="Monthly subscription"
)
if result.status == ChargeStatus.CONFIRMED:
    print(f"Charged {result.amount_charged} {result.currency}")
```

### list_saved_methods(user)

Lists all saved payment methods for a user.

**Signature:**
```python
@staticmethod
def list_saved_methods(user) -> list[SavedMethod]
```

**Parameters:**
- `user`: Django user object

**Returns:** List of `SavedMethod` value objects for active methods

**Example:**
```python
from autopay.core.facade import Payments

methods = Payments.list_saved_methods(request.user)
for method in methods:
    print(f"{method.display_label} ({method.gateway})")
```

### refresh_method(saved_method, reason="manual")

Starts a refresh flow for a saved payment method.

**Signature:**
```python
@staticmethod
def refresh_method(
    saved_method: SavedMethod,
    reason: str = "manual"
) -> RefreshSetup
```

**Parameters:**
- `saved_method`: The `SavedMethod` value object to refresh
- `reason`: Trigger reason ("expired_card", "revoked", "sca_required", "manual")

**Returns:** `RefreshSetup` with gateway data for frontend SDK integration

**Example:**
```python
from autopay.core.facade import Payments

setup = Payments.refresh_method(saved_method, reason="expired_card")
print(f"Use {setup.frontend_sdk} with payload {setup.frontend_payload}")
```

### confirm_refresh(saved_method, refresh_payload)

Confirms a refresh attempt with gateway credentials.

**Signature:**
```python
@staticmethod
def confirm_refresh(
    saved_method: SavedMethodProtocol,
    refresh_payload: dict
) -> SavedMethodProtocol
```

**Parameters:**
- `saved_method`: The `SavedMethod` value object being refreshed
- `refresh_payload`: Provider-specific credentials dict. Keys depend on gateway:
  - Stripe: `{"setup_intent_id": "seti_..."}`
  - PayPal: `{"setup_token_id": "token_...", "first_payment_id": "pay_..."}`
  - Mollie: `{"first_payment_id": "tr_..."}`

**Returns:** `ConfirmRefreshOut` with success status and updated method data

**Example:**
```python
from autopay.core.facade import Payments

result = Payments.confirm_refresh(
    method_id=1,
    attempt_id=42,
    setup_intent_id="seti_123456"
)
if result.success:
    print(f"Refreshed method {result.method_id}")
else:
    print(f"Failed: {result.error}")
```

### validate_method(saved_method)

Validates a saved payment method without charging.

**Signature:**
```python
@staticmethod
def validate_method(saved_method: SavedMethod) -> ValidationResult
```

**Parameters:**
- `saved_method`: The `SavedMethod` value object to validate

**Returns:** `ValidationResult` with validation status

**Example:**
```python
from autopay.core.facade import Payments

result = Payments.validate_method(saved_method)
if result.needs_refresh:
    print(f"Method needs refresh: {result.reason}")
```

### process_webhook(variant, raw_body, headers=None, content_type="", client_ip="")

Processes a webhook event from a gateway.

**Signature:**
```python
@staticmethod
def process_webhook(
    variant: str,
    raw_body: bytes,
    headers: dict[str, str] | None = None,
    content_type: str = "",
    client_ip: str = ""
) -> WebhookEventResult
```

**Parameters:**
- `variant`: Gateway variant ("stripe", "paypal", "mollie")
- `raw_body`: Raw webhook payload bytes
- `headers`: Request headers (for signature verification)
- `content_type`: Content-Type header value (optional)
- `client_ip`: Client IP address (from `request.META["REMOTE_ADDR"]`). Used by Mollie IP allowlist verification.

**Returns:** `WebhookEventResult` with processing result

**Example:**
```python
from autopay.core.facade import Payments

result = Payments.process_webhook(
    variant="stripe",
    raw_body=request.body,
    headers=request.headers
)
if result.processed:
    print(f"Event handled: {result.action}")
else:
    print(f"Event rejected: {result.error}")
```

---

**See also:** `autopay/core/base.py` for `AutopayProvider` protocol and capability flags.