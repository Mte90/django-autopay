"""PayPal provider for django-autopay.

Implements Vault v3 + Orders v2 for off-session charges.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, ClassVar

import requests
from django.utils import timezone as django_timezone

from autopay.core.base import AutopayProvider
from autopay.core.result import (
    ChargeResult,
    ChargeStatus,
    RefreshSetupData,
    ValidationResult,
    WebhookEventResult,
)
from autopay.core.saved_method import SavedMethod

logger = logging.getLogger(__name__)


class LiabilityShift(Enum):
    """PayPal liability shift status for 3DS authentication."""

    POSSIBLE = "POSSIBLE"
    NO = "NO"
    YES = "YES"
    UNKNOWN = "UNKNOWN"


# Mapping from PayPal API values to LiabilityShift enum. Any unrecognized value maps to UNKNOWN.
_LIABILITY_SHIFT_MAP: dict[str, LiabilityShift] = {
    "POSSIBLE": LiabilityShift.POSSIBLE,
    "NO": LiabilityShift.NO,
    "YES": LiabilityShift.YES,
    "UNKNOWN": LiabilityShift.UNKNOWN,
}


class PaypalAutopayProvider(AutopayProvider):
    """PayPal provider with Vault v3 + off-session charge.

    Uses PayPal Orders v2 + Vault v3 for off-session charges.
    """

    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_LIST_METHODS = True
    SUPPORTS_REVOKE = True
    SUPPORTS_VALIDATE = True
    SUPPORTS_REFRESH = True
    SUPPORTS_WEBHOOK_VERIFICATION = True

    # Shared OAuth cache at class/process level, keyed by client_id
    _token_cache: ClassVar[dict[str, tuple[str, float]]] = {}

    def __init__(
        self,
        client_id: str,
        secret: str,
        endpoint: str = "https://api-m.paypal.com",
        capture: bool = True,
        webhook_id: str | None = None,
        timeout: int = 30,
        **kwargs: Any,
    ) -> None:
        """Initialize the PayPal provider.

        Args:
            client_id: PayPal client ID (sandbox or production)
            secret: PayPal secret
            endpoint: PayPal API endpoint (default: production)
            capture: If True, immediately capture the order
            webhook_id: PayPal webhook ID (optional, for signature verification)
            timeout: Timeout in seconds for HTTP requests
        """
        self.client_id = client_id
        self.secret = secret
        self.endpoint = endpoint.rstrip("/")
        self.capture = capture
        self.webhook_id = webhook_id
        self.timeout = timeout

    # -- HTTP helpers -------------------------------------------------------

    def _get_access_token(self) -> str:
        """OAuth2 client-credentials with class-level cache."""
        import time

        cached = self._token_cache.get(self.client_id)
        if cached and cached[1] > time.time() + 60:
            return cached[0]

        url = f"{self.endpoint}/v1/oauth2/token"
        r = requests.post(
            url,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.secret),
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        self._token_cache[self.client_id] = (data["access_token"], time.time() + data["expires_in"])
        return data["access_token"]

    def _api_headers(self) -> dict[str, str]:
        """Returns headers for API requests."""
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
        }

    def _api_post(self, path: str, body: dict, idempotency_key: str | None = None) -> dict:
        """Executes a POST request to the PayPal API."""
        headers = self._api_headers()
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key

        r = requests.post(f"{self.endpoint}{path}", json=body, headers=headers, timeout=self.timeout)
        return {"status_code": r.status_code, "body": r.json() if r.content else {}}

    def _api_get_paginated(self, path: str, max_pages: int = 10) -> list[dict]:
        """Follow HATEOAS pagination for list endpoints.

        Args:
            path: Initial API path (e.g., "/v3/vault/payment-tokens?customer_id=...")
            max_pages: Maximum number of pages to fetch (default 10, safety cap)

        Returns:
            Accumulated list of items from all pages

        Raises:
            requests.RequestError: Network errors propagate (do not swallow)
            requests.HTTPError: 5xx errors propagate; 4xx handled per status
        """
        items: list[dict] = []
        current_path = path
        pages_fetched = 0

        while current_path and pages_fetched < max_pages:
            pages_fetched += 1
            r = requests.get(f"{self.endpoint}{current_path}", headers=self._api_headers(), timeout=self.timeout)

            if r.status_code == 200:
                body = r.json() if r.content else {}
                # PayPal returns items in "payment_tokens" for vault endpoints
                page_items = body.get("payment_tokens", []) or body.get("items", []) or []
                items.extend(page_items)

                # Look for next link in HATEOAS links array
                next_link = None
                for link in body.get("links", []):
                    if link.get("rel") == "next":
                        next_link = link.get("href")
                        break

                if next_link:
                    # Extract path from full URL (handle absolute URLs)
                    if next_link.startswith(self.endpoint):
                        current_path = next_link[len(self.endpoint):]
                    else:
                        current_path = next_link
                else:
                    current_path = ""
            elif r.status_code >= 500:
                # 5xx: propagate error, do not return partial data
                r.raise_for_status()
            elif r.status_code == 401 or r.status_code == 403:
                # 401/403: revoked token, return what we have (empty is acceptable)
                break
            else:
                # Other 4xx: propagate
                r.raise_for_status()

        return items

    def _api_get(self, path: str) -> dict:
        """Executes a GET request to the PayPal API."""
        r = requests.get(f"{self.endpoint}{path}", headers=self._api_headers(), timeout=self.timeout)
        return {"status_code": r.status_code, "body": r.json() if r.content else {}}

    def _api_delete(self, path: str) -> dict:
        """Executes a DELETE request to the PayPal API."""
        r = requests.delete(f"{self.endpoint}{path}", headers=self._api_headers(), timeout=self.timeout)
        # 204 No Content has no body
        return {"status_code": r.status_code, "body": r.json() if r.content else {}}

    # -- capability: save_method ------------------------------------------

    def save_method(self, payment) -> SavedMethod:
        """Save the method after a successful PayPal checkout with vault.

        Precondition: payment.extra_data must contain "vault_payment_token_id"
        and "vault_customer_id", populated by the frontend during on-session checkout.
        """
        extra = json.loads(payment.extra_data or "{}")
        token_id = extra.get("vault_payment_token_id")
        customer_id = extra.get("vault_customer_id")

        if not token_id:
            raise ValueError("payment.extra_data.vault_payment_token_id is required to save method")

        # Fetch token details (brand, last4, expiry)
        resp = self._api_get(f"/v3/vault/payment-tokens/{token_id}")
        if resp["status_code"] != 200:
            raise RuntimeError(f"PayPal token fetch failed: {resp['body']}")

        token_data = resp["body"]
        source = token_data.get("payment_source", {})

        if "card" in source:
            card = source["card"]
            method_type = "card"
            display_label = f"{card.get('brand', 'CARD')} •••• {card.get('last_digits', '????')}"
            metadata = {
                "brand": card.get("brand"),
                "last_digits": card.get("last_digits"),
                "expiry": f"{card.get('expiry', {}).get('year')}-{card.get('expiry', {}).get('month')}",
            }
        elif "paypal" in source:
            method_type = "paypal"
            display_label = f"PayPal ({source['paypal'].get('email', '')})"
            metadata = {"email": source["paypal"].get("email")}
        else:
            method_type = "unknown"
            display_label = "PayPal method"
            metadata = {}

        return SavedMethod(
            gateway="paypal",
            gateway_customer_id=customer_id or "",
            gateway_method_id=token_id,
            method_type=method_type,
            display_label=display_label,
            metadata=metadata,
            is_active=True,
        )

    # -- capability: charge_off_session -----------------------------------

    def charge_off_session(
        self,
        saved_method: SavedMethod,
        amount: Decimal,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> ChargeResult:
        """POST /v2/checkout/orders with payment_source.token.id saved. Sync."""
        amount_str = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {"currency_code": currency, "value": amount_str},
                    "description": description[:127] if description else None,
                    "custom_id": (metadata or {}).get("custom_id", ""),
                }
            ],
            "payment_source": {
                "token": {
                    "id": saved_method.gateway_method_id,
                    "type": "PAYMENT_METHOD_TOKEN",
                },
                "attributes": {"vault": {"store_in_vault": "ON_SUCCESS"}},
            },
        }
        # Remove None values
        body["purchase_units"][0] = {k: v for k, v in body["purchase_units"][0].items() if v is not None}

        resp = self._api_post("/v2/checkout/orders", body, idempotency_key=idempotency_key)
        return self._parse_charge_response(resp)

    def _parse_charge_response(self, resp: dict) -> ChargeResult:
        """Parse PayPal response and return ChargeResult."""
        sc = resp["status_code"]
        body = resp["body"]
        now = django_timezone.now()

        if 200 <= sc < 300:
            status = body.get("status", "")
            transaction_id = body.get("id", "")

            # Extract liability_shift se presente
            auth_result = (
                body.get("payment_source", {}).get("card", {}).get("authentication_result", {})
            )

            if status == "COMPLETED":
                pus = body.get("purchase_units") or [{}]
                captures = (pus[0].get("payments") or {}).get("captures") or []
                capture = captures[0] if captures else {}
                amount_value = (capture.get("amount") or {}).get("value")
                currency_code = (
                    (capture.get("amount") or {}).get("currency_code")
                    or (pus[0].get("amount") or {}).get("currency_code")
                )
                return ChargeResult(
                    status=ChargeStatus.CONFIRMED,
                    message="Charged successfully",
                    transaction_id=transaction_id,
                    raw_response=body,
                    amount_charged=Decimal(amount_value) if amount_value else None,
                    currency=currency_code,
                    requires_action=None,
                    created_at=now,
                )
            elif status in ("APPROVED", "PAYER_ACTION_REQUIRED"):
                # Map liability_shift to enum with UNKNOWN fallback
                liability_raw = auth_result.get("liability_shift")
                details = dict(auth_result)
                if liability_raw is not None:
                    liability = _LIABILITY_SHIFT_MAP.get(liability_raw, LiabilityShift.UNKNOWN)
                    details["liability_shift"] = liability.value
                else:
                    # Absent field defaults to UNKNOWN per spec
                    details["liability_shift"] = LiabilityShift.UNKNOWN.value
                return ChargeResult(
                    status=ChargeStatus.REQUIRES_SCA,
                    message="3D Secure authentication required",
                    transaction_id=transaction_id,
                    raw_response=body,
                    amount_charged=None,
                    currency=body.get("purchase_units", [{}])[0].get("amount", {}).get("currency_code"),
                    requires_action={"type": "3ds", "details": details},
                    created_at=now,
                )
            elif status == "VOIDED":
                return ChargeResult(
                    status=ChargeStatus.FAILED,
                    message="Order voided",
                    transaction_id=transaction_id,
                    raw_response=body,
                    amount_charged=None,
                    currency=None,
                    requires_action=None,
                    created_at=now,
                )
            elif status in ("CREATED", "SAVE"):
                return ChargeResult(
                    status=ChargeStatus.PENDING,
                    message=f"PayPal status={status}, action required",
                    transaction_id=transaction_id,
                    raw_response=body,
                    amount_charged=None,
                    currency=None,
                    requires_action=None,
                    created_at=now,
                )
            else:
                return ChargeResult(
                    status=ChargeStatus.PENDING,
                    message=f"Unexpected PayPal status: {status}",
                    transaction_id=transaction_id,
                    raw_response=body,
                    amount_charged=None,
                    currency=None,
                    requires_action=None,
                    created_at=now,
                )

        elif sc == 401:
            # 401 = OAuth token expired/invalid: transient error, NOT REJECTED
            return ChargeResult(
                status=ChargeStatus.FAILED,
                message="PayPal API OAuth authentication failed — retryable",
                transaction_id=None,
                raw_response=body,
                amount_charged=None,
                currency=None,
                requires_action=None,
                created_at=now,
            )
        elif sc == 422:
            # Preserve PayPal error structure: {name, details, message}
            error = body.get("error", {}) if isinstance(body, dict) else {}
            if isinstance(error, dict):
                error_name = error.get("name", "UNKNOWN")
                error_details = error.get("details", []) or []
                error_message = error.get("message", error_name)
            else:
                error_name = "UNKNOWN"
                error_details = []
                error_message = str(body)

            # Store structured error in raw_response for debugging
            structured_error = {
                "name": error_name,
                "details": error_details,
                "message": error_message,
            }

            if error_name in ("AUTHENTICATION_FAILURE", "TOKEN_EXPIRED", "INVALID_RESOURCE_ID"):
                return ChargeResult(
                    status=ChargeStatus.REJECTED,
                    message=error_message,
                    transaction_id=None,
                    raw_response={**body, "structured_error": structured_error},
                    amount_charged=None,
                    currency=None,
                    requires_action=None,
                    created_at=now,
                )
            return ChargeResult(
                status=ChargeStatus.FAILED,
                message=error_message,
                transaction_id=None,
                raw_response={**body, "structured_error": structured_error},
                amount_charged=None,
                currency=None,
                requires_action=None,
                created_at=now,
            )
        elif sc == 404:
            # Resource not found: token revoked/deleted
            return ChargeResult(
                status=ChargeStatus.REJECTED,
                message="PayPal resource not found — token revoked or deleted",
                transaction_id=None,
                raw_response=body,
                amount_charged=None,
                currency=None,
                requires_action=None,
                created_at=now,
            )
        else:
            # Preserve error structure for all non-2xx responses
            error_body = body.get("error", {}) if isinstance(body, dict) else {}
            if isinstance(error_body, dict):
                error_name = error_body.get("name", "UNKNOWN")
                error_details = error_body.get("details", []) or []
                error_message = error_body.get("message", f"HTTP {sc}")
            else:
                error_name = "UNKNOWN"
                error_details = []
                error_message = str(body) if body else f"HTTP {sc}"

            structured_error = {
                "name": error_name,
                "details": error_details,
                "message": error_message,
            }

            return ChargeResult(
                status=ChargeStatus.FAILED,
                message=error_message,
                transaction_id=None,
                raw_response={**body, "structured_error": structured_error},
                amount_charged=None,
                currency=None,
                requires_action=None,
                created_at=now,
            )

    # -- capability: get_charge_status (polling) --------------------------

    def get_charge_status(self, transaction_id: str) -> ChargeResult:
        """GET /v2/checkout/orders/<id> to poll the status."""
        resp = self._api_get(f"/v2/checkout/orders/{transaction_id}")
        return self._parse_charge_response(resp)

    # -- capability: list_saved_methods -----------------------------------

    def list_saved_methods(self, customer_id: str) -> list[SavedMethod]:
        """GET /v3/vault/payment-tokens?customer_id=<id> with pagination.

        Follows HATEOAS next links to accumulate all payment tokens.
        Capped at 10 pages (200 items) to prevent infinite loops.
        """
        if not customer_id:
            return []

        # Use paginated fetch; errors propagate (do not return empty on 5xx)
        tokens = self._api_get_paginated(f"/v3/vault/payment-tokens?customer_id={customer_id}&page_size=20")
        methods: list[SavedMethod] = []

        for t in tokens:
            source = t.get("payment_source", {})
            if "card" in source:
                card = source["card"]
                methods.append(
                    SavedMethod(
                        gateway="paypal",
                        gateway_customer_id=customer_id,
                        gateway_method_id=t["id"],
                        method_type="card",
                        display_label=f"{card.get('brand', 'CARD')} •••• {card.get('last_digits', '????')}",
                        metadata={
                            "brand": card.get("brand"),
                            "last_digits": card.get("last_digits"),
                            "expiry": f"{card.get('expiry', {}).get('year')}-{card.get('expiry', {}).get('month')}",
                        },
                    )
                )
            elif "paypal" in source:
                pp = source["paypal"]
                methods.append(
                    SavedMethod(
                        gateway="paypal",
                        gateway_customer_id=customer_id,
                        gateway_method_id=t["id"],
                        method_type="paypal",
                        display_label=f"PayPal ({pp.get('email', '')})",
                        metadata={"email": pp.get("email")},
                    )
                )

        return methods

    # -- capability: revoke_method ----------------------------------------

    def revoke_method(self, saved_method: SavedMethod) -> None:
        """DELETE /v3/vault/payment-tokens/<id>."""
        resp = self._api_delete(f"/v3/vault/payment-tokens/{saved_method.gateway_method_id}")
        if resp["status_code"] not in (200, 204, 404):
            raise RuntimeError(f"Failed to revoke PayPal token: {resp}")

    # -- capability: validate_method --------------------------------------

    def validate_method(self, saved_method: SavedMethod) -> ValidationResult:
        """PayPal: GET /v3/vault/payment-tokens/<id>. No charge."""
        now = datetime.now(timezone.utc)

        resp = self._api_get(f"/v3/vault/payment-tokens/{saved_method.gateway_method_id}")
        sc = resp["status_code"]
        body = resp["body"]

        if sc == 200:
            status = body.get("status", "")
            source = body.get("payment_source", {})

            # Check card expiry
            is_expired = False
            if "card" in source:
                card = source["card"]
                expiry = card.get("expiry", {})
                exp_year = expiry.get("year")
                exp_month = expiry.get("month")
                if exp_year and exp_month:
                    exp_date = datetime(exp_year, exp_month, 1, tzinfo=timezone.utc).date()
                    if exp_date < django_timezone.now().date().replace(day=1):
                        is_expired = True

            if status == "OK" and not is_expired:
                return ValidationResult(
                    is_valid=True,
                    is_expired=False,
                    needs_refresh=False,
                    reason="Token valido",
                    checked_at=now,
                    raw_response=body,
                )
            elif is_expired:
                exp_month_str = source["card"].get("expiry", {}).get("month")
                exp_year_str = source["card"].get("expiry", {}).get("year")
                return ValidationResult(
                    is_valid=False,
                    is_expired=True,
                    needs_refresh=True,
                    reason=f"Card expired {exp_month_str}/{exp_year_str}",
                    checked_at=now,
                    raw_response=body,
                )
            elif status in ("EXPIRED", "INVALID"):
                return ValidationResult(
                    is_valid=False,
                    is_expired=(status == "EXPIRED"),
                    needs_refresh=True,
                    reason=f"Token {status.lower()}",
                    checked_at=now,
                    raw_response=body,
                )
            else:
                # Unknown status, assume valid
                return ValidationResult(
                    is_valid=True,
                    is_expired=False,
                    needs_refresh=False,
                    reason=f"Token status={status} (assumed valid)",
                    checked_at=now,
                    raw_response=body,
                )
        elif sc == 404:
            return ValidationResult(
                is_valid=False,
                is_expired=False,
                needs_refresh=True,
                    reason="Token not found (revoked)",
                checked_at=now,
                raw_response={"status_code": sc, "body": body},
            )
        else:
            # 401, 5xx → gateway error
            return ValidationResult(
                is_valid=False,
                is_expired=False,
                needs_refresh=False,
                reason=f"PayPal HTTP {sc}: {body.get('message', '')}",
                checked_at=now,
                raw_response={"status_code": sc, "body": body},
            )

    # -- capability: refresh_method + confirm_refresh ---------------------

    def refresh_method(self, saved_method: SavedMethod) -> RefreshSetupData:
        """PayPal: new setup_token → user approves → new payment_token.

        Only gateway calls: persistence is in the facade (§9.5bis).
        """
        # 1. Create setup token on PayPal
        body = {
            "payment_source": {"card": {}},
            "customer": {
                "id": saved_method.gateway_customer_id,
                "merchant_customer_id": f"user-{saved_method.id or 'unknown'}",
            },
        }
        resp = self._api_post("/v3/vault/setup-tokens", body)
        if resp["status_code"] != 201:
            raise RuntimeError(f"PayPal setup token creation failed: {resp['body']}")

        setup_token = resp["body"]

        # 2. Generate client_token for frontend SDK
        client_token_resp = self._api_post(
            "/v1/identity/generate-token", {"customer_id": saved_method.gateway_customer_id}
        )
        client_token = (
            client_token_resp["body"].get("client_token", "")
            if client_token_resp["status_code"] == 200
            else ""
        )

        return RefreshSetupData(
            gateway_setup_id=setup_token["id"],
            frontend_sdk="paypal",
            frontend_payload={
                "client_token": client_token,
                "setup_token_id": setup_token["id"],
                "client_id": self.client_id,
            },
            expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        )

    def confirm_refresh(self, saved_method: SavedMethod, refresh_payload: dict) -> SavedMethod:
        """Frontend returns {setup_token_id, payment_token_id}.

        Only gateway calls + data mapping: persistence is in the facade.
        """
        setup_token_id = refresh_payload["setup_token_id"]

        # Exchange setup token → permanent payment token
        body = {
            "payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}},
            "customer": {"id": saved_method.gateway_customer_id},
        }
        resp = self._api_post("/v3/vault/payment-tokens", body)
        if resp["status_code"] != 201:
            raise RuntimeError(f"Payment token creation failed: {resp['body']}")

        new_token = resp["body"]

        # Optional: revoke the old token
        try:
            self._api_delete(f"/v3/vault/payment-tokens/{saved_method.gateway_method_id}")
        except OSError:  # Network error, ignore
            logger.debug("Old token already revoked or network error", exc_info=True)

        new_method_id = new_token["id"]
        source = new_token.get("payment_source", {})
        metadata = dict(saved_method.metadata or {})

        if "card" in source:
            card = source["card"]
            metadata.update(
                {
                    "brand": card.get("brand"),
                    "last_digits": card.get("last_digits"),
                    "expiry": f"{card.get('expiry', {}).get('year')}-{card.get('expiry', {}).get('month')}",
                }
            )
            display_label = f"{card.get('brand', 'CARD')} •••• {card.get('last_digits', '????')}"
        elif "paypal" in source:
            metadata["email"] = source["paypal"].get("email")
            display_label = f"PayPal ({source['paypal'].get('email', '')})"
        else:
            display_label = saved_method.display_label

        return SavedMethod(
            gateway=saved_method.gateway,
            gateway_customer_id=saved_method.gateway_customer_id,
            gateway_method_id=new_method_id,
            method_type=saved_method.method_type,
            display_label=display_label,
            is_active=saved_method.is_active,
            metadata=metadata,
            id=saved_method.id,
        )

    # -- webhook (optional) ---------------------------------------------

    def verify_webhook_request(self, raw_body: bytes, headers) -> bool:
        """Verify PayPal webhook signature (RSA-SHA256).

        Args:
            raw_body: The exact bytes received in the request body
            headers: Case-insensitive header mapping
        """
        if not self.webhook_id:
            return False  # Webhook not configured

        # Case-insensitive header access per spec
        headers_map = {k.upper(): v for k, v in (headers or {}).items()}
        transmission_headers = {
            "transmission_id": headers_map.get("PAYPAL-TRANSMISSION-ID", ""),
            "transmission_time": headers_map.get("PAYPAL-TRANSMISSION-TIME", ""),
            "cert_url": headers_map.get("PAYPAL-CERT-URL", ""),
            "auth_algo": headers_map.get("PAYPAL-AUTH-ALGO", ""),
            "transmission_sig": headers_map.get("PAYPAL-TRANSMISSION-SIG", ""),
        }
        body = {
            "auth_algo": transmission_headers["auth_algo"],
            "cert_url": transmission_headers["cert_url"],
            "transmission_id": transmission_headers["transmission_id"],
            "transmission_sig": transmission_headers["transmission_sig"],
            "transmission_time": transmission_headers["transmission_time"],
            "webhook_id": self.webhook_id,
            "webhook_event": json.loads(raw_body.decode("utf-8")),
        }
        resp = requests.post(
            f"{self.endpoint}/v1/notifications/verify-webhook-signature",
            json=body,
            headers=self._api_headers(),
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            return False
        return resp.json().get("verification_status") == "SUCCESS"

    def process_webhook_event(self, event_type: str, payload: dict) -> WebhookEventResult | None:
        """Handler for PayPal webhook events."""
        if event_type == "PAYMENT.CAPTURE.COMPLETED":
            return WebhookEventResult(
                processed=True, action="noop", transaction_id=payload.get("resource", {}).get("id")
            )
        elif event_type == "VAULT.PAYMENT-TOKEN.DELETED":
            token_id = payload.get("resource", {}).get("id")
            return WebhookEventResult(processed=True, action="revoke_method", transaction_id=token_id)
        elif event_type in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
            return WebhookEventResult(
                processed=True, action="refund", transaction_id=payload.get("resource", {}).get("id")
            )
        return WebhookEventResult(processed=False, action="ignored", transaction_id=None)