# Models and Result Types

Reference documentation for django-autopay database models and result dataclasses.

## Database Models

### SavedPaymentMethod

Stores a vaulted payment method token. NO card data, only gateway-side references.

This model is the single source of truth for the app: it contains the reference to the gateway-side token/mandate. Provider routing (PayPal, Mollie, Stripe) happens by reading the `gateway` field — the app must never know which circuit it is using; it passes the object to the `Payments.*` facade, which dispatches on `provider_factory(saved_method.gateway)`.

| Field | Type | Description |
|-------|------|-------------|
| `user` | ForeignKey | User owning this payment method |
| `gateway` | CharField(32) | Gateway identifier: "paypal" \| "mollie" \| "stripe" |
| `gateway_customer_id` | CharField(128) | Gateway-side customer ID |
| `gateway_method_id` | CharField(128) | Gateway-side payment method ID (unique) |
| `method_type` | CharField(32) | Type: "card" \| "paypal" \| "sepa" \| "ideal" |
| `display_label` | CharField(128) | User-facing label for this method |
| `is_default` | BooleanField | True if this is the user's default payment method |
| `is_active` | BooleanField | True if method is active and usable |
| `metadata` | JSONField | Extra data: card brand, last4, expiry, fingerprint, etc. |
| `last_validated_at` | DateTimeField | Timestamp of last validation check |
| `last_validation_is_valid` | BooleanField | Result of last validation |
| `last_validation_reason` | CharField(255) | Human-readable reason from last validation |
| `last_refreshed_at` | DateTimeField | Timestamp of last refresh flow completion |
| `created_at` | DateTimeField | Record creation timestamp |
| `updated_at` | DateTimeField | Record update timestamp |

**Constraints:**
- Unique together: `(gateway, gateway_method_id)`
- Indexes: `(user, is_active, is_default)`, `(gateway, gateway_customer_id)`, `(is_active, last_validation_is_valid)`

**Methods:**
- `as_saved_method()` — Returns a `SavedMethod` protocol object for facade operations

**Properties:**
- `needs_validation` — True if never validated or last validation is older than 1 day

---

### ChargeRecord

Records a charge transaction for audit and reporting purposes.

| Field | Type | Description |
|-------|------|-------------|
| `saved_method` | ForeignKey | Payment method used for this charge |
| `amount` | DecimalField(9,2) | Charge amount |
| `currency` | CharField(3) | Currency code (default: "EUR") |
| `description` | CharField(255) | Human-readable charge description |
| `transaction_id` | CharField(128) | Gateway-side transaction ID |
| `status` | CharField(16) | Charge status (see `ChargeStatus` enum) |
| `message` | TextField | Human-readable status message |
| `raw_response` | JSONField | Full gateway response for audit |
| `idempotency_key` | CharField(128) | Idempotency key for retry safety |
| `created_at` | DateTimeField | Record creation timestamp |
| `updated_at` | DateTimeField | Record update timestamp |

**Constraints:**
- Index: `(status, created_at)`
- Foreign key to `SavedPaymentMethod` with `PROTECT` (prevents deletion if charges exist)

**Note:** Gateway payloads contain personal data (PayPal: name/email/address). Truncate/scrub unnecessary fields and plan retention (e.g., cleanup > 180 days via job) for GDPR compliance.

---

### RefreshAttempt

Tracks refresh flow attempts for payment method renewal.

| Field | Type | Description |
|-------|------|-------------|
| `saved_method` | ForeignKey | Payment method being refreshed |
| `gateway_setup_id` | CharField(128) | Setup token/intent ID from gateway |
| `frontend_sdk` | CharField(32) | SDK used: "paypal" \| "stripe" \| "mollie" |
| `frontend_payload` | JSONField | Gateway-specific frontend data |
| `status` | CharField(16) | Status: pending \| in_progress \| confirmed \| expired \| failed |
| `reason` | CharField(64) | Trigger: "expired_card" \| "revoked" \| "sca_required" \| "manual" |
| `initiated_by` | ForeignKey | User who initiated the refresh (nullable) |
| `created_at` | DateTimeField | Attempt creation timestamp |
| `expires_at` | DateTimeField | When this setup expires |
| `confirmed_at` | DateTimeField | Timestamp when refresh was confirmed (nullable) |

**Constraints:**
- Indexes: `(saved_method, status)`, `(status, expires_at)`
- Ordering: `-created_at` (newest first)

**Status Values:**
- `pending` — Refresh attempt created, awaiting user completion
- `in_progress` — Attempt claimed by user (transition from `pending`)
- `confirmed` — User completed the flow successfully (transition from `in_progress`)
- `failed` — Refresh flow failed (transition from `in_progress`)
- `expired` — Written by `confirm_refresh` when the attempt is already past `expires_at`; the write happens before the `RuntimeError` is raised

**Transition Flow:**
```
pending (created) → in_progress (claim, facade.py:210) → confirmed (facade.py:256) or failed (facade.py:229)
```

Note: When a refresh attempt is already past `expires_at`, `confirm_refresh` writes `status='expired'` and then raises `RuntimeError`. Line numbers in this page reference the source at documentation time — verify against the current source when relying on them.

---

### ProcessedWebhookEvent

Deduplication table for webhook events across gateways.

| Field | Type | Description |
|-------|------|-------------|
| `gateway` | CharField(32) | Gateway identifier |
| `event_id` | CharField(128) | Gateway-side event ID |
| `payload` | JSONField | Raw webhook payload |
| `received_at` | DateTimeField | When the event was received |

**Constraints:**
- Unique together: `(gateway, event_id)` — ensures idempotency

**Note:** Optional model, only needed if using webhooks. The facade `Payments.process_webhook()` handles deduplication automatically.

---

## Result Types

### ChargeResult

Result of an off-session charge operation.

| Field | Type | Description |
|-------|------|-------------|
| `status` | ChargeStatus | Current status (see enum below) |
| `message` | str | Human-readable status message |
| `transaction_id` | str \| None | Gateway-side transaction ID |
| `raw_response` | dict | Full gateway response for audit |
| `amount_charged` | Decimal \| None | Actual amount charged (if confirmed) |
| `currency` | str \| None | Currency code |
| `requires_action` | dict \| None | 3DS action info (if REQUIRES_SCA) |
| `created_at` | datetime | When this result was created |

#### ChargeStatus Enum

| Value | Description |
|-------|-------------|
| `CONFIRMED` | Charge succeeded |
| `PENDING` | In progress, must sync later |
| `FAILED` | Definitively failed, no retry needed |
| `REQUIRES_SCA` | Needs 3DS, user must be on-session |
| `REJECTED` | Mandate/token revoked, skip the method |

---

### RefreshSetup

Result of `Payments.refresh_method()` — gateway data for frontend integration.

| Field | Type | Description |
|-------|------|-------------|
| `setup_id` | int | PK of `RefreshAttempt` (0 if models not installed) |
| `frontend_sdk` | Literal | SDK to use: "paypal" \| "stripe" \| "mollie" \| "adyen" \| ... |
| `frontend_payload` | dict | Gateway-specific data for frontend SDK |
| `expires_at` | datetime | When this setup expires |
| `return_url` | str | URL where frontend redirects after flow |

**Note:** This is returned by the facade. The underlying `RefreshSetupData` dataclass contains only gateway data without DB access.

---

### ValidationResult

Result of `provider.validate_method(saved_method)` — verifies token/mandate status without charging.

| Field | Type | Description |
|-------|------|-------------|
| `is_valid` | bool | True if method can be used for off-session charges |
| `is_expired` | bool | True if card has expired (cards only) |
| `needs_refresh` | bool | True if method is invalid and needs refresh flow |
| `reason` | str | Human-readable explanation ("OK", "Card expired 11/2024", "Token revoked", etc.) |
| `checked_at` | datetime \| None | UTC timestamp of verification |
| `raw_response` | dict \| None | Gateway payload for audit |

**Usage:** All 3 supported gateways (PayPal, Stripe, Mollie) expose a GET API that returns the current status of the payment token/mandate.

---

### WebhookEventResult

Result of webhook event processing via `Payments.process_webhook()`.

| Field | Type | Description |
|-------|------|-------------|
| `processed` | bool | True if event was handled (or already seen) |
| `action` | str | Action taken (see values below) |
| `transaction_id` | str \| None | Related transaction ID |
| `event_id` | str \| None | Gateway event ID |
| `new_status` | ChargeStatus \| None | New charge status (if status_update) |
| `raw` | dict \| None | Original webhook payload |
| `error` | str \| None | Error message (if action=error) |

#### Action Values

| Value | Description |
|-------|-------------|
| `noop` | No action needed |
| `revoke_method` | Deactivate saved payment method |
| `refund` | Process refund |
| `status_update` | Update charge status |
| `ignored` | Unknown event type, recorded but not processed |
| `error` | Processing failed |

---

## See Also

- [Refresh Flow](refresh.md) — How to use `RefreshAttempt` and `RefreshSetup`
- [Webhooks](webhooks.md) — How `ProcessedWebhookEvent` and `WebhookEventResult` work together