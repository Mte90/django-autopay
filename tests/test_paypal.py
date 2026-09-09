"""Tests for PayPal provider.

Uses responses to mock HTTP calls. Zero real network.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from autopay.core.base import is_autopay_capable
from autopay.core.result import ChargeStatus
from autopay.core.saved_method import SavedMethod

try:
    from autopay.providers.paypal import PaypalAutopayProvider
except ImportError:
    pytest.skip("PayPal provider not installed", allow_module_level=True)


@pytest.fixture
def paypal_provider():
    """Crea un provider PayPal con settings di test."""
    # Clear cache before each test
    PaypalAutopayProvider._token_cache.clear()
    return PaypalAutopayProvider(
        client_id="test-client-id",
        secret="test-secret",
        endpoint="https://api-m.sandbox.paypal.com",
    )


@pytest.fixture
def saved_method():
    """Crea un SavedMethod per i test."""
    return SavedMethod(
        gateway="paypal",
        gateway_customer_id="cus_test123",
        gateway_method_id="vault_token_123",
        method_type="card",
        display_label="Visa •••• 4242",
        is_active=True,
        metadata={"brand": "visa", "last_digits": "4242", "expiry": "2027-12"},
    )


class TestPaypalAuth:
    """Test OAuth2 token fetching and caching."""

    @responses.activate
    def test_fetches_access_token(self, paypal_provider):
        """OAuth2 client-credentials fetch."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token_123", "expires_in": 32400},
            status=200,
        )

        token = paypal_provider._get_access_token()
        assert token == "test_token_123"
        assert len(responses.calls) == 1

    @responses.activate
    def test_caches_token_by_client_id(self, paypal_provider):
        """Token cache a livello di classe, keyed per client_id."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token_123", "expires_in": 32400},
            status=200,
        )

        # Prima chiamata: fetch
        token1 = paypal_provider._get_access_token()
        assert token1 == "test_token_123"

        # Second call with same client_id: cache hit
        token2 = paypal_provider._get_access_token()
        assert token2 == "test_token_123"

        # Only one HTTP call
        assert len(responses.calls) == 1

    @responses.activate
    def test_different_client_ids_have_separate_caches(self):
        """Client ID diversi hanno cache separate."""
        # Clear cache to avoid interference from previous tests
        PaypalAutopayProvider._token_cache.clear()
        
        # Use different URLs to ensure proper matching
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "token_a", "expires_in": 32400},
            status=200,
        )
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "token_b", "expires_in": 32400},
            status=200,
        )

        provider_a = PaypalAutopayProvider(
            client_id="client_a", secret="secret_a", endpoint="https://api-m.sandbox.paypal.com"
        )
        provider_b = PaypalAutopayProvider(
            client_id="client_b", secret="secret_b", endpoint="https://api-m.sandbox.paypal.com"
        )

        token_a = provider_a._get_access_token()
        token_b = provider_b._get_access_token()

        assert token_a == "token_a"
        assert token_b == "token_b"
        assert len(responses.calls) == 2


class TestChargeOffSession:
    """Test charge_off_session."""

    @responses.activate
    def test_charge_success(self, paypal_provider, saved_method):
        """Charge off-session con successo."""
        # Mock token
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        # Mock charge
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "id": "order_123",
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "payments": {
                            "captures": [
                                {"amount": {"value": "99.99", "currency_code": "EUR"}}
                            ]
                        }
                    }
                ],
            },
            status=201,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
            description="Test charge",
        )

        assert result.status == ChargeStatus.CONFIRMED
        assert result.transaction_id == "order_123"
        assert result.amount_charged == Decimal("99.99")
        assert result.currency == "EUR"

    @responses.activate
    def test_charge_declined(self, paypal_provider, saved_method):
        """Charge decline (422)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "error": {
                    "name": "PAYMENT_DENIED",
                    "message": "The payment was denied",
                }
            },
            status=422,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.FAILED
        assert "The payment was denied" in result.message

    @responses.activate
    def test_charge_401_retryable_failed(self, paypal_provider, saved_method):
        """401 OAuth error → FAILED (retryable), non REJECTED."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={"error": {"message": "Invalid token"}},
            status=401,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.FAILED
        assert "retryable" in result.message.lower()

    @responses.activate
    def test_charge_404_resource_revoked(self, paypal_provider, saved_method):
        """404 → REJECTED (token revocato)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={"error": {"message": "Resource not found"}},
            status=404,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.REJECTED
        assert "revoked" in result.message.lower() or "not found" in result.message.lower()

    @responses.activate
    def test_charge_requires_sca(self, paypal_provider, saved_method):
        """Charge con SCA richiesta."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "id": "order_123",
                "status": "PAYER_ACTION_REQUIRED",
                "purchase_units": [{"amount": {"currency_code": "EUR"}}],
                "payment_source": {
                    "card": {
                        "authentication_result": {"liability_shift": "POSSIBLE"},
                    }
                },
            },
            status=201,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.REQUIRES_SCA
        assert result.transaction_id == "order_123"

    @responses.activate
    def test_idempotency_header_passthrough(self, paypal_provider, saved_method):
        """Idempotency key passa nell'header PayPal-Request-Id."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        def request_callback(request):
            assert request.headers.get("PayPal-Request-Id") == "idempotency_key_123"
            return (201, {}, '{"id": "order_123", "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"amount": {"value": "99.99", "currency_code": "EUR"}}]}}]}')

        responses.add_callback(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            callback=request_callback,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
            idempotency_key="idempotency_key_123",
        )

        assert result.status == ChargeStatus.CONFIRMED

    @responses.activate
    def test_amount_formatting_decimal_string(self, paypal_provider, saved_method):
        """Amount formattato come stringa decimale, non centesimi."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        def request_callback(request):
            import json
            body = json.loads(request.body)
            amount_value = body["purchase_units"][0]["amount"]["value"]
            assert amount_value == "99.99"
            assert isinstance(amount_value, str)
            return (201, {}, '{"id": "order_123", "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"amount": {"value": "99.99", "currency_code": "EUR"}}]}}]}')

        responses.add_callback(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            callback=request_callback,
        )

        paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )


class TestLiabilityShift:
    """Test liability_shift parsing."""

    @responses.activate
    @pytest.mark.parametrize(
        "liability,expected",
        [
        ["YES", "YES"],
        ["NO", "NO"],
        ["POSSIBLE", "POSSIBLE"],
        ["UNKNOWN", "UNKNOWN"],
        ["INVALID_VALUE", "UNKNOWN"],
        [None, "UNKNOWN"],  # Absent defaults to UNKNOWN per spec
        ],
    )
    def test_liability_shift_parsing(self, paypal_provider, saved_method, liability, expected):
        """Normalizza liability_shift sui soli valori YES/NO/POSSIBLE/UNKNOWN."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        auth_result = {"liability_shift": liability} if liability is not None else {}
        # Always use APPROVED status to test liability_shift parsing
        status = "APPROVED"
        purchase_units = [{"amount": {"value": "99.99", "currency_code": "EUR"}}]
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "id": "order_123",
                "status": status,
                "purchase_units": purchase_units,
                "payment_source": {"card": {"authentication_result": auth_result}},
            },
            status=201,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        if liability is None:
            # Absent liability_shift still sets REQUIRES_SCA with UNKNOWN
            assert result.status == ChargeStatus.REQUIRES_SCA
            assert result.requires_action is not None
            assert result.requires_action["details"]["liability_shift"] == "UNKNOWN"
        else:
            assert result.status == ChargeStatus.REQUIRES_SCA
            assert result.requires_action is not None
            assert result.requires_action["details"]["liability_shift"] == expected


class TestValidateMethod:
    """Test validate_method."""

    @responses.activate
    def test_validate_success(self, paypal_provider, saved_method):
        """Token valido."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "status": "OK",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {"year": 2027, "month": 12},
                    }
                },
            },
            status=200,
        )

        result = paypal_provider.validate_method(saved_method)

        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False
        assert result.reason == "Token valido"

    @responses.activate
    def test_validate_expired_card(self, paypal_provider):
        """Expired card."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        expired_method = SavedMethod(
            gateway="paypal",
            gateway_customer_id="cus_test123",
            gateway_method_id="vault_token_123",
            method_type="card",
            display_label="Visa •••• 4242",
            is_active=True,
            metadata={"brand": "visa", "last_digits": "4242", "expiry": "2020-12"},
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "status": "OK",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {"year": 2020, "month": 12},
                    }
                },
            },
            status=200,
        )

        result = paypal_provider.validate_method(expired_method)

        assert result.is_valid is False
        assert result.is_expired is True
        assert result.needs_refresh is True
        assert "expired" in result.reason.lower()

    @responses.activate
    def test_validate_token_expired_status(self, paypal_provider):
        """Token with EXPIRED status (lines 464-472)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "status": "EXPIRED",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {"year": 2020, "month": 12},  # Expired date
                    }
                },
            },
            status=200,
        )

        sm = SavedMethod(
            gateway="paypal",
            gateway_customer_id="cus_test123",
            gateway_method_id="vault_token_123",
            method_type="card",
            display_label="Visa •••• 4242",
        )
        result = paypal_provider.validate_method(sm)

        assert result.is_valid is False
        assert result.is_expired is True
        assert result.needs_refresh is True
        # Card expiry is checked first, so this will be "Card expired" not "token expired"
        assert "expired" in result.reason.lower()

    @responses.activate
    def test_validate_token_invalid_status(self, paypal_provider):
        """Token with INVALID status (lines 464-472)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "status": "INVALID",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {"year": 2027, "month": 12},  # Future date
                    }
                },
            },
            status=200,
        )

        sm = SavedMethod(
            gateway="paypal",
            gateway_customer_id="cus_test123",
            gateway_method_id="vault_token_123",
            method_type="card",
            display_label="Visa •••• 4242",
        )
        result = paypal_provider.validate_method(sm)

        assert result.is_valid is False
        assert result.is_expired is False
        assert result.needs_refresh is True
        assert "token invalid" in result.reason.lower()

    @responses.activate
    def test_validate_token_unknown_status(self, paypal_provider):
        """Token with unknown status assumes valid (lines 473-480)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "status": "UNKNOWN_STATUS",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {"year": 2027, "month": 12},
                    }
                },
            },
            status=200,
        )

        sm = SavedMethod(
            gateway="paypal",
            gateway_customer_id="cus_test123",
            gateway_method_id="vault_token_123",
            method_type="card",
            display_label="Visa •••• 4242",
        )
        result = paypal_provider.validate_method(sm)

        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False
        assert "assumed valid" in result.reason.lower()

    @responses.activate


    @responses.activate
    def test_validate_404_token_revoked(self, paypal_provider, saved_method):
        """Token not found (revoked)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={"error": {"message": "Resource not found"}},
            status=404,
        )

        result = paypal_provider.validate_method(saved_method)

        assert result.is_valid is False
        assert result.is_expired is False
        assert result.needs_refresh is True
        assert "revoked" in result.reason.lower() or "not found" in result.reason.lower()

    @responses.activate
    def test_validate_401_error(self, paypal_provider, saved_method):
        """Validate with 401 error returns gateway error (line 494)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={"error": {"message": "Unauthorized"}},
            status=401,
        )

        result = paypal_provider.validate_method(saved_method)

        assert result.is_valid is False
        assert result.needs_refresh is False
        assert "401" in result.reason


class TestRefreshMethod:
    """Test refresh_method e confirm_refresh."""

    @responses.activate
    def test_refresh_method_returns_refresh_setup_data(self, paypal_provider, saved_method):
        """refresh_method returns RefreshSetupData."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v3/vault/setup-tokens",
            json={
                "id": "setup_token_123",
                "status": "OK",
            },
            status=201,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/identity/generate-token",
            json={"client_token": "client_secret_123"},
            status=200,
        )

        from autopay.core.result import RefreshSetupData

        result = paypal_provider.refresh_method(saved_method)

        assert isinstance(result, RefreshSetupData)
        assert result.gateway_setup_id == "setup_token_123"
        assert result.frontend_sdk == "paypal"
        assert result.frontend_payload["setup_token_id"] == "setup_token_123"
        assert result.frontend_payload["client_token"] == "client_secret_123"
        assert result.frontend_payload["client_id"] == "test-client-id"

    @responses.activate
    def test_refresh_method_setup_token_creation_fails(self, paypal_provider, saved_method):
        """refresh_method raises error when setup token creation fails (line 520)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v3/vault/setup-tokens",
            json={"error": {"message": "Creation failed"}},
            status=400,
        )

        with pytest.raises(RuntimeError, match="setup token creation failed"):
            paypal_provider.refresh_method(saved_method)

    @responses.activate
    def test_confirm_refresh_exchanges_setup_token(self, paypal_provider, saved_method):
        """confirm_refresh scambia setup_token → payment_token."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        # Mock confirm_refresh
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens",
            json={
                "id": "new_vault_token_456",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {"year": 2026, "month": 12},
                    }
                },
            },
            status=201,
        )

        # Mock revoke old token
        responses.add(
            responses.DELETE,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={},
            status=204,
        )

        result = paypal_provider.confirm_refresh(
            saved_method, {"setup_token_id": "setup_token_123"}
        )

        assert result.gateway_method_id == "new_vault_token_456"
        assert result.metadata["brand"] == "visa"
        assert result.metadata["last_digits"] == "4242"
        assert result.metadata["expiry"] == "2026-12"

    @responses.activate
    def test_confirm_refresh_paypal_source(self, paypal_provider, saved_method):
        """confirm_refresh with PayPal source (lines 584-585)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens",
            json={
                "id": "new_vault_token_456",
                "payment_source": {
                    "paypal": {
                        "email": "user@example.com",
                    }
                },
            },
            status=201,
        )

        responses.add(
            responses.DELETE,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={},
            status=204,
        )

        result = paypal_provider.confirm_refresh(
            saved_method, {"setup_token_id": "setup_token_123"}
        )

        assert result.gateway_method_id == "new_vault_token_456"
        assert result.metadata["email"] == "user@example.com"
        assert result.display_label == "PayPal (user@example.com)"

    @responses.activate
    def test_confirm_refresh_payment_token_creation_fails(self, paypal_provider, saved_method):
        """confirm_refresh raises error when payment token creation fails (line 559)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens",
            json={"error": {"message": "Creation failed"}},
            status=400,
        )

        with pytest.raises(RuntimeError, match="Payment token creation failed"):
            paypal_provider.confirm_refresh(saved_method, {"setup_token_id": "setup_token_123"})


class TestListAndRevoke:
    """Test list_saved_methods e revoke_method."""

    @responses.activate
    def test_list_saved_methods(self, paypal_provider):
        """Lista metodi salvati."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20",
            json={
                "payment_tokens": [
                    {
                        "id": "vault_123",
                        "payment_source": {
                            "card": {
                                "brand": "visa",
                                "last_digits": "4242",
                                "expiry": {"year": 2030, "month": 12},
                            }
                        },
                    },
                    {
                        "id": "vault_456",
                        "payment_source": {
                            "paypal": {
                                "email": "user@example.com",
                            }
                        },
                    },
                ]
            },
            status=200,
        )

        methods = paypal_provider.list_saved_methods("cus_test123")

        assert len(methods) == 2
        assert methods[0].method_type == "card"
        assert methods[0].display_label == "visa •••• 4242"
        assert methods[1].method_type == "paypal"
        assert methods[1].display_label == "PayPal (user@example.com)"

    def test_list_saved_methods_empty_customer(self, paypal_provider):
        """List methods with empty customer_id returns empty list (line 368)."""
        methods = paypal_provider.list_saved_methods("")
        assert methods == []

    def test_list_raises_on_other_4xx(self, paypal_provider):
        """Test _api_get_paginated helper raises HTTPError on other 4xx (not 401/403/429)."""
        import time

        import requests

        # Pre-populate token cache so OAuth POST is bypassed
        PaypalAutopayProvider._token_cache[paypal_provider.client_id] = (
            "test_token",
            time.time() + 32400,
        )

        # Mock a 418 response (I'm a teapot - any 4xx not 401/403/429)
        mock_response = MagicMock()
        mock_response.status_code = 418
        mock_response.raise_for_status.side_effect = requests.HTTPError("418 I'm a teapot")

        with (
            patch("autopay.providers.paypal.provider.requests.get", return_value=mock_response),
            pytest.raises(requests.HTTPError),
        ):
            paypal_provider._api_get_paginated("/v3/test/path")

    @responses.activate
    def test_list_saved_methods_api_error_raises_5xx(self, paypal_provider):
        """5xx errors propagate - do not return empty list (H4 fix)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20",
            json={"error": {"message": "API error"}},
            status=500,
        )

        # 5xx should raise, not return []
        with pytest.raises(requests.exceptions.HTTPError):
            paypal_provider.list_saved_methods("cus_test123")

    @responses.activate
    def test_revoke_method(self, paypal_provider, saved_method):
        """Revoca metodo."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.DELETE,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            body="",
            status=204,
        )

        paypal_provider.revoke_method(saved_method)
        # No error = success

    @responses.activate
    def test_revoke_method_error(self, paypal_provider, saved_method):
        """Revoke method raises error on non-200/204/404 status (line 416)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.DELETE,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={"error": {"message": "Delete failed"}},
            status=500,
        )

        with pytest.raises(RuntimeError, match="Failed to revoke"):
            paypal_provider.revoke_method(saved_method)


class TestCapabilityFlags:
    """Test capability flags."""

    def test_supports_off_session_charge(self, paypal_provider):
        """SUPPORTS_OFF_SESSION_CHARGE = True."""
        assert paypal_provider.SUPPORTS_OFF_SESSION_CHARGE is True

    def test_supports_list_methods(self, paypal_provider):
        """SUPPORTS_LIST_METHODS = True."""
        assert paypal_provider.SUPPORTS_LIST_METHODS is True

    def test_supports_revoke(self, paypal_provider):
        """SUPPORTS_REVOKE = True."""
        assert paypal_provider.SUPPORTS_REVOKE is True

    def test_supports_validate(self, paypal_provider):
        """SUPPORTS_VALIDATE = True."""
        assert paypal_provider.SUPPORTS_VALIDATE is True

    def test_supports_refresh(self, paypal_provider):
        """SUPPORTS_REFRESH = True."""
        assert paypal_provider.SUPPORTS_REFRESH is True

    def test_is_autopay_capable(self, paypal_provider):
        """is_autopay_capable returns True."""
        assert is_autopay_capable(paypal_provider) is True


class TestSaveMethod:
    """Test save_method for PayPal."""

    @responses.activate
    def test_save_method_success_card(self, paypal_provider):
        """Save method after successful checkout with card."""
        from unittest.mock import MagicMock

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {"year": 2027, "month": 12},
                    }
                },
            },
            status=200,
        )

        mock_payment = MagicMock()
        mock_payment.extra_data = '{"vault_payment_token_id": "vault_token_123", "vault_customer_id": "cus_test123"}'

        result = paypal_provider.save_method(mock_payment)

        assert result.gateway == "paypal"
        assert result.gateway_customer_id == "cus_test123"
        assert result.gateway_method_id == "vault_token_123"
        assert result.method_type == "card"
        assert result.display_label == "visa •••• 4242"

    @responses.activate
    def test_save_method_success_paypal(self, paypal_provider):
        """Save method after successful checkout with PayPal."""
        from unittest.mock import MagicMock

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "payment_source": {
                    "paypal": {
                        "email": "user@example.com",
                    }
                },
            },
            status=200,
        )

        mock_payment = MagicMock()
        mock_payment.extra_data = '{"vault_payment_token_id": "vault_token_123", "vault_customer_id": "cus_test123"}'

        result = paypal_provider.save_method(mock_payment)

        assert result.method_type == "paypal"
        assert result.display_label == "PayPal (user@example.com)"

    @responses.activate
    def test_save_method_paypal_source(self, paypal_provider):
        """Save method with PayPal source (lines 584-585)."""
        from unittest.mock import MagicMock

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "payment_source": {
                    "paypal": {
                        "email": "user@example.com",
                    }
                },
            },
            status=200,
        )

        mock_payment = MagicMock()
        mock_payment.extra_data = '{"vault_payment_token_id": "vault_token_123", "vault_customer_id": "cus_test123"}'

        result = paypal_provider.save_method(mock_payment)

        assert result.method_type == "paypal"
        assert result.display_label == "PayPal (user@example.com)"
        assert result.metadata["email"] == "user@example.com"

    @responses.activate
    def test_save_method_missing_token(self, paypal_provider):
        """Save method fails when vault_payment_token_id is missing."""
        from unittest.mock import MagicMock

        mock_payment = MagicMock()
        mock_payment.extra_data = '{"vault_customer_id": "cus_test123"}'

        with pytest.raises(ValueError, match="vault_payment_token_id is required"):
            paypal_provider.save_method(mock_payment)

    @responses.activate
    def test_save_method_unknown_source(self, paypal_provider):
        """Test save_method with unknown payment source type (lines 160-162)."""
        from unittest.mock import MagicMock

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "payment_source": {
                    "unknown_type": {"data": "test"},
                },
            },
            status=200,
        )

        mock_payment = MagicMock()
        mock_payment.extra_data = '{"vault_payment_token_id": "vault_token_123", "vault_customer_id": "cus_test123"}'

        result = paypal_provider.save_method(mock_payment)

        assert result.method_type == "unknown"
        assert result.display_label == "PayPal method"

    @responses.activate
    @responses.activate
    def test_save_method_api_error(self, paypal_provider):
        """Save method fails when token fetch API error."""
        from unittest.mock import MagicMock

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={"error": {"message": "Token not found"}},
            status=404,
        )

        mock_payment = MagicMock()
        mock_payment.extra_data = '{"vault_payment_token_id": "vault_token_123", "vault_customer_id": "cus_test123"}'

        with pytest.raises(RuntimeError, match="PayPal token fetch failed"):
            paypal_provider.save_method(mock_payment)

    @responses.activate
    def test_charge_error_body_error_not_dict(self, paypal_provider, saved_method):
        """Test charge when body.error is a string (not dict)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        # Response with error as string, not dict - 422 status
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={"error": "Payment failed"},  # error is string, not dict
            status=422,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status in (ChargeStatus.FAILED, ChargeStatus.REJECTED)
        # When error is not a dict, error_message should be str(body)
        assert "error" in result.message or "Payment failed" in result.message

    @responses.activate
    def test_non_2xx_error_body_error_not_dict(self, paypal_provider, saved_method):
        """Test non-2xx error when body.error is a string."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        # Response with error as string, not dict - 500 error (goes to else branch)
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={"error": "Internal server error"},  # error is string
            status=500,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.FAILED
        # When error is not a dict, structured_error name should be UNKNOWN
        structured = result.raw_response.get("structured_error", {})
        assert structured.get("name") == "UNKNOWN"


class TestGetChargeStatus:
    """Test get_charge_status for PayPal."""

    @responses.activate
    def test_get_charge_status_confirmed(self, paypal_provider):
        """Polling returns confirmed status."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders/order_123",
            json={
                "id": "order_123",
                "status": "COMPLETED",
                "purchase_units": [
                    {
                        "payments": {
                            "captures": [
                                {"amount": {"value": "99.99", "currency_code": "EUR"}}
                            ]
                        }
                    }
                ],
            },
            status=200,
        )

        result = paypal_provider.get_charge_status("order_123")

        assert result.status == ChargeStatus.CONFIRMED
        assert result.transaction_id == "order_123"

    @responses.activate
    def test_get_charge_status_pending(self, paypal_provider):
        """Polling returns pending status."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders/order_123",
            json={
                "id": "order_123",
                "status": "CREATED",
                "purchase_units": [{"amount": {"value": "99.99", "currency_code": "EUR"}}],
            },
            status=200,
        )

        result = paypal_provider.get_charge_status("order_123")

        assert result.status == ChargeStatus.PENDING


class TestPayPalWebhook:
    """Test PayPal webhook handlers."""

    def test_verify_webhook_request_success(self, paypal_provider):
        """Webhook signature verification succeeds."""
        import json

        paypal_provider.webhook_id = "wh_test_123"

        raw_body = json.dumps({"id": "evt_123"}).encode("utf-8")
        headers = {
            "PAYPAL-TRANSMISSION-ID": "trans_id_123",
            "PAYPAL-TRANSMISSION-TIME": "2024-01-01T00:00:00Z",
            "PAYPAL-CERT-URL": "https://cert.paypal.com",
            "PAYPAL-AUTH-ALGO": "SHA256withRSA",
            "PAYPAL-TRANSMISSION-SIG": "signature_123",
        }

        with responses.RequestsMock() as rsps:
            # Mock OAuth token endpoint
            rsps.add(
                responses.POST,
                "https://api-m.sandbox.paypal.com/v1/oauth2/token",
                json={"access_token": "mock_token", "expires_in": 3600},
                status=200,
            )
            # Mock webhook verification endpoint
            rsps.add(
                responses.POST,
                "https://api-m.sandbox.paypal.com/v1/notifications/verify-webhook-signature",
                json={"verification_status": "SUCCESS"},
                status=200,
            )

            result = paypal_provider.verify_webhook_request(raw_body, headers)
            assert result is True

    def test_verify_webhook_request_failure(self, paypal_provider):
        """Webhook signature verification fails."""
        import json

        paypal_provider.webhook_id = "wh_test_123"

        raw_body = json.dumps({"id": "evt_123"}).encode("utf-8")
        headers = {
            "PAYPAL-TRANSMISSION-ID": "trans_id_123",
            "PAYPAL-TRANSMISSION-TIME": "2024-01-01T00:00:00Z",
            "PAYPAL-CERT-URL": "https://cert.paypal.com",
            "PAYPAL-AUTH-ALGO": "SHA256withRSA",
            "PAYPAL-TRANSMISSION-SIG": "signature_123",
        }

        with responses.RequestsMock() as rsps:
            # Mock OAuth token endpoint
            rsps.add(
                responses.POST,
                "https://api-m.sandbox.paypal.com/v1/oauth2/token",
                json={"access_token": "mock_token", "expires_in": 3600},
                status=200,
            )
            # Mock webhook verification endpoint
            rsps.add(
                responses.POST,
                "https://api-m.sandbox.paypal.com/v1/notifications/verify-webhook-signature",
                json={"verification_status": "FAILURE"},
                status=200,
            )

            result = paypal_provider.verify_webhook_request(raw_body, headers)
            assert result is False

    def test_verify_webhook_request_no_webhook_id(self, paypal_provider):
        """Webhook verification fails when webhook_id not configured."""
        paypal_provider.webhook_id = None

        raw_body = b'{"id": "evt_123"}'
        headers = {}
        result = paypal_provider.verify_webhook_request(raw_body, headers)
        assert result is False

    def test_verify_webhook_request_500_error(self, paypal_provider):
        """Webhook verification fails with 500 error."""
        import json

        paypal_provider.webhook_id = "wh_test_123"

        raw_body = json.dumps({"id": "evt_123"}).encode("utf-8")
        headers = {
            "PAYPAL-TRANSMISSION-ID": "trans_id_123",
            "PAYPAL-TRANSMISSION-TIME": "2024-01-01T00:00:00Z",
            "PAYPAL-CERT-URL": "https://cert.paypal.com",
            "PAYPAL-AUTH-ALGO": "SHA256withRSA",
            "PAYPAL-TRANSMISSION-SIG": "signature_123",
        }

        with responses.RequestsMock() as rsps:
            # Mock OAuth token endpoint
            rsps.add(
                responses.POST,
                "https://api-m.sandbox.paypal.com/v1/oauth2/token",
                json={"access_token": "mock_token", "expires_in": 3600},
                status=200,
            )
            # Mock webhook verification endpoint with 500 error
            rsps.add(
                responses.POST,
                "https://api-m.sandbox.paypal.com/v1/notifications/verify-webhook-signature",
                json={"error": {"message": "Server error"}},
                status=500,
            )

            result = paypal_provider.verify_webhook_request(raw_body, headers)
            assert result is False

    def test_process_webhook_event_payment_succeeded(self, paypal_provider):
        """Process PAYMENT.CAPTURE.COMPLETED event."""
        payload = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "sale_123"},
        }

        result = paypal_provider.process_webhook_event("PAYMENT.CAPTURE.COMPLETED", payload)

        assert result.processed is True
        assert result.action == "noop"
        assert result.transaction_id == "sale_123"

    def test_process_webhook_event_token_revoked(self, paypal_provider):
        """Process VAULT.PAYMENT-TOKEN.DELETED event."""
        payload = {
            "event_type": "VAULT.PAYMENT-TOKEN.DELETED",
            "resource": {"id": "vault_token_123"},
        }

        result = paypal_provider.process_webhook_event("VAULT.PAYMENT-TOKEN.DELETED", payload)

        assert result.processed is True
        assert result.action == "revoke_method"
        assert result.transaction_id == "vault_token_123"

    def test_process_webhook_event_auth_reversed(self, paypal_provider):
        """Process PAYMENT.CAPTURE.REFUNDED event."""
        payload = {
            "event_type": "PAYMENT.CAPTURE.REFUNDED",
            "resource": {"id": "auth_123"},
        }

        result = paypal_provider.process_webhook_event("PAYMENT.CAPTURE.REFUNDED", payload)

        assert result.processed is True
        assert result.action == "refund"
        assert result.transaction_id == "auth_123"

    def test_process_webhook_event_capture_reversed(self, paypal_provider):
        """Process PAYMENT.CAPTURE.REVERSED event."""
        payload = {
            "event_type": "PAYMENT.CAPTURE.REVERSED",
            "resource": {"id": "capture_123"},
        }

        result = paypal_provider.process_webhook_event("PAYMENT.CAPTURE.REVERSED", payload)

        assert result.processed is True
        assert result.action == "refund"
        assert result.transaction_id == "capture_123"

    def test_process_webhook_event_ignored(self, paypal_provider):
        """Process unknown event type."""
        payload = {"event_type": "UNKNOWN.EVENT"}

        result = paypal_provider.process_webhook_event("UNKNOWN.EVENT", payload)

        assert result.processed is False
        assert result.action == "ignored"
        assert result.transaction_id is None

    @responses.activate
    def test_confirm_refresh_unknown_source_type(self, paypal_provider, saved_method):
        """confirm_refresh with unknown source type keeps display_label (lines 583-587)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens",
            json={
                "id": "new_vault_token_456",
                "payment_source": {
                    "unknown_type": {},
                },
            },
            status=201,
        )

        responses.add(
            responses.DELETE,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={},
            status=204,
        )

        result = paypal_provider.confirm_refresh(
            saved_method, {"setup_token_id": "setup_token_123"}
        )

        assert result.gateway_method_id == "new_vault_token_456"
        assert result.display_label == saved_method.display_label


class TestChargeEdgeCases:
    """Test additional charge edge cases."""

    @responses.activate
    def test_charge_voided_status(self, paypal_provider, saved_method):
        """Charge with VOIDED status returns FAILED."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "id": "order_123",
                "status": "VOIDED",
                "purchase_units": [{"amount": {"value": "99.99", "currency_code": "EUR"}}],
            },
            status=201,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.FAILED
        assert "voided" in result.message.lower()

    @responses.activate
    def test_charge_created_status(self, paypal_provider, saved_method):
        """Charge with CREATED status returns PENDING."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "id": "order_123",
                "status": "CREATED",
                "purchase_units": [{"amount": {"value": "99.99", "currency_code": "EUR"}}],
            },
            status=201,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.PENDING

    @responses.activate
    def test_charge_422_auth_failure(self, paypal_provider, saved_method):
        """422 with AUTHENTICATION_FAILURE returns REJECTED."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "error": {
                    "name": "AUTHENTICATION_FAILURE",
                    "message": "Token authentication failed",
                }
            },
            status=422,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.REJECTED

    @responses.activate
    def test_charge_500_server_error(self, paypal_provider, saved_method):
        """500 server error returns FAILED."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={"error": {"message": "Internal server error"}},
            status=500,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.FAILED
        assert "Internal server error" in result.message

    @responses.activate
    def test_charge_unexpected_status(self, paypal_provider, saved_method):
        """Charge with unexpected status returns PENDING (line 285)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "id": "order_123",
                "status": "UNEXPECTED_STATUS",
                "purchase_units": [{"amount": {"value": "99.99", "currency_code": "EUR"}}],
            },
            status=201,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.PENDING
        assert "Unexpected PayPal status" in result.message


class TestSyncSavedMethodsSafety:
    """Test sync_saved_methods error handling (H4)."""

    @responses.activate
    def test_list_saved_methods_5xx_raises_exception(self, paypal_provider):
        """5xx errors propagate - do not return empty list."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20",
            json={"error": {"message": "Internal server error"}},
            status=500,
        )

        # Should raise, not return []
        with pytest.raises(requests.exceptions.HTTPError):
            paypal_provider.list_saved_methods("cus_test123")

    @responses.activate
    def test_list_saved_methods_401_returns_empty(self, paypal_provider):
        """401 returns empty list (token revoked - acceptable)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20",
            json={"error": {"message": "Unauthorized"}},
            status=401,
        )

        # 401/403 returns empty (token revoked)
        methods = paypal_provider.list_saved_methods("cus_test123")
        assert methods == []

    @responses.activate
    def test_list_saved_methods_403_returns_empty(self, paypal_provider):
        """403 returns empty list (forbidden - acceptable)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20",
            json={"error": {"message": "Forbidden"}},
            status=403,
        )

        methods = paypal_provider.list_saved_methods("cus_test123")
        assert methods == []


class TestErrorFormat:
    """Test error format preservation (H9)."""

    @responses.activate
    def test_error_format_preserves_name_details_message(self, paypal_provider, saved_method):
        """PayPal error structure {name, details, message} is preserved."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "error": {
                    "name": "INVALID_REQUEST",
                    "message": "The request is invalid",
                    "details": [
                        {"field": "amount", "issue": "Invalid amount format"},
                        {"field": "currency", "issue": "Unsupported currency"},
                    ],
                }
            },
            status=400,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        # Check structured_error in raw_response
        assert "structured_error" in result.raw_response
        structured = result.raw_response["structured_error"]
        assert structured["name"] == "INVALID_REQUEST"
        assert structured["message"] == "The request is invalid"
        assert len(structured["details"]) == 2
        assert structured["details"][0]["field"] == "amount"

    @responses.activate
    def test_error_format_defaults_when_absent(self, paypal_provider, saved_method):
        """Error format defaults to UNKNOWN/[] when fields absent."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={"error": {}},  # Empty error object
            status=400,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        structured = result.raw_response["structured_error"]
        assert structured["name"] == "UNKNOWN"
        assert structured["details"] == []
        assert "HTTP 400" in structured["message"] or structured["message"] == "UNKNOWN"


class TestLiabilityShiftEnum:
    """Test liability_shift enum mapping with UNKNOWN fallback."""

    @responses.activate
    def test_liability_shift_possible(self, paypal_provider, saved_method):
        """POSSIBLE maps to LiabilityShift.POSSIBLE."""
        from autopay.providers.paypal.provider import LiabilityShift

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "id": "order_123",
                "status": "APPROVED",
                "purchase_units": [{"amount": {"currency_code": "EUR"}}],
                "payment_source": {"card": {"authentication_result": {"liability_shift": "POSSIBLE"}}},
            },
            status=201,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.REQUIRES_SCA
        assert result.requires_action["details"]["liability_shift"] == LiabilityShift.POSSIBLE.value

    @responses.activate
    def test_liability_shift_unknown_value_maps_to_unknown(self, paypal_provider, saved_method):
        """Unrecognized value maps to LiabilityShift.UNKNOWN."""
        from autopay.providers.paypal.provider import LiabilityShift

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "id": "order_123",
                "status": "APPROVED",
                "purchase_units": [{"amount": {"currency_code": "EUR"}}],
                "payment_source": {"card": {"authentication_result": {"liability_shift": "GARBAGE_VALUE"}}},
            },
            status=201,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.REQUIRES_SCA
        assert result.requires_action["details"]["liability_shift"] == LiabilityShift.UNKNOWN.value

    @responses.activate
    def test_liability_shift_absent_defaults_to_unknown(self, paypal_provider, saved_method):
        """Absent liability_shift defaults to LiabilityShift.UNKNOWN."""
        from autopay.providers.paypal.provider import LiabilityShift

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v2/checkout/orders",
            json={
                "id": "order_123",
                "status": "APPROVED",
                "purchase_units": [{"amount": {"currency_code": "EUR"}}],
                "payment_source": {"card": {"authentication_result": {}}},
            },
            status=201,
        )

        result = paypal_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("99.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.REQUIRES_SCA
        assert result.requires_action["details"]["liability_shift"] == LiabilityShift.UNKNOWN.value


class TestPaginationBranches:
    """Test pagination branch coverage."""

    @responses.activate
    def test_pagination_stops_when_no_next_rel(self, paypal_provider):
        """Pagination stops when links exist but none have rel='next' (line 158-169)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        # Page 1 with links that have rel != "next" (e.g., rel="self")
        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20",
            json={
                "payment_tokens": [
                    {"id": "token_1", "payment_source": {"card": {"brand": "visa", "last_digits": "1111", "expiry": {"year": 2025, "month": 12}}}}
                ],
                "links": [
                    {"rel": "self", "href": "/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20"},
                    {"rel": "prev", "href": "/v3/vault/payment-tokens?customer_id=cus_test123&page=0"},
                ],
            },
            status=200,
        )

        methods = paypal_provider.list_saved_methods("cus_test123")

        # Should stop after first page - no next link found
        assert len(methods) == 1
        assert methods[0].gateway_method_id == "token_1"
        # Verify only one API call was made (no pagination)
        # Count GET calls to payment-tokens endpoint (excluding OAuth)
        get_calls = [c for c in responses.calls if "payment-tokens" in c.request.url and "oauth" not in c.request.url]
        assert len(get_calls) == 1


class TestPagination:
    """Test HATEOAS pagination (M14)."""

    @responses.activate
    def test_pagination_follows_next_link(self, paypal_provider):
        """Pagination follows next links to accumulate all items."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        # Page 1
        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20",
            json={
                "payment_tokens": [{"id": "token_1", "payment_source": {"card": {"brand": "visa", "last_digits": "1111", "expiry": {"year": 2025, "month": 12}}}}],
                "links": [
                    {"rel": "next", "href": "/v3/vault/payment-tokens?customer_id=cus_test123&page=2"}
                ],
            },
            status=200,
        )

        # Page 2
        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page=2",
            json={
                "payment_tokens": [{"id": "token_2", "payment_source": {"card": {"brand": "mastercard", "last_digits": "2222", "expiry": {"year": 2026, "month": 12}}}}],
                "links": [],  # No more pages
            },
            status=200,
        )

        methods = paypal_provider.list_saved_methods("cus_test123")

        assert len(methods) == 2
        assert methods[0].gateway_method_id == "token_1"
        assert methods[1].gateway_method_id == "token_2"

    @responses.activate
    def test_pagination_capped_at_10_pages(self, paypal_provider):
        """Pagination stops at 10 pages (safety cap)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        # Create 15 pages with next links
        for page in range(1, 16):
            has_next = page < 15
            next_href = f"https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page={page+1}" if has_next else ""
            links = [{"rel": "next", "href": next_href}] if has_next else []
            url = f"https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page={page}" if page > 1 else "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20"
            responses.add(
                responses.GET,
                url,
                json={
                    "payment_tokens": [{"id": f"token_{page}", "payment_source": {"card": {"brand": "visa", "last_digits": "1111", "expiry": {"year": 2025, "month": 12}}}}],
                    "links": links,
                },
                status=200,
            )

        methods = paypal_provider.list_saved_methods("cus_test123")

        # Should be capped at 10 pages
        assert len(methods) == 10
        assert methods[0].gateway_method_id == "token_1"
        assert methods[9].gateway_method_id == "token_10"


class TestListSavedMethodsBranches:
    """Test list_saved_methods branch coverage."""

    @responses.activate
    def test_list_saved_methods_paypal_source(self, paypal_provider):
        """list_saved_methods handles paypal payment_source (line 502-513)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20",
            json={
                "payment_tokens": [
                    {
                        "id": "paypal_token_123",
                        "payment_source": {
                            "paypal": {
                                "email": "test@example.com",
                            }
                        },
                    }
                ]
            },
            status=200,
        )

        methods = paypal_provider.list_saved_methods("cus_test123")

        assert len(methods) == 1
        assert methods[0].method_type == "paypal"
        assert methods[0].display_label == "PayPal (test@example.com)"
        assert methods[0].metadata == {"email": "test@example.com"}
        assert methods[0].gateway_customer_id == "cus_test123"
        assert methods[0].gateway_method_id == "paypal_token_123"


class TestValidateMethodBranches:
    """Test validate_method branch coverage for expiry edge cases."""

    @responses.activate
    def test_validate_method_empty_expiry_dict(self, paypal_provider):
        """validate_method with empty expiry dict - is_expired remains False (line 543-546)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "status": "OK",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {},  # Empty expiry dict
                    }
                },
            },
            status=200,
        )

        sm = SavedMethod(
            gateway="paypal",
            gateway_customer_id="cus_test123",
            gateway_method_id="vault_token_123",
            method_type="card",
            display_label="Visa •••• 4242",
        )
        result = paypal_provider.validate_method(sm)

        # is_expired should remain False when expiry is empty
        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False
        assert result.reason == "Token valido"

    @responses.activate
    def test_validate_method_missing_exp_month(self, paypal_provider):
        """validate_method with only exp_year present - is_expired remains False (line 546)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "status": "OK",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {"year": 2025},  # month missing
                    }
                },
            },
            status=200,
        )

        sm = SavedMethod(
            gateway="paypal",
            gateway_customer_id="cus_test123",
            gateway_method_id="vault_token_123",
            method_type="card",
            display_label="Visa •••• 4242",
        )
        result = paypal_provider.validate_method(sm)

        # is_expired should remain False when exp_month is missing
        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False
        assert result.reason == "Token valido"

    @responses.activate
    def test_validate_method_missing_exp_year(self, paypal_provider):
        """validate_method with only exp_month present - is_expired remains False (line 546)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "status": "OK",
                "payment_source": {
                    "card": {
                        "brand": "visa",
                        "last_digits": "4242",
                        "expiry": {"month": 12},  # year missing
                    }
                },
            },
            status=200,
        )

        sm = SavedMethod(
            gateway="paypal",
            gateway_customer_id="cus_test123",
            gateway_method_id="vault_token_123",
            method_type="card",
            display_label="Visa •••• 4242",
        )
        result = paypal_provider.validate_method(sm)

        # is_expired should remain False when exp_year is missing
        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False
        assert result.reason == "Token valido"




class TestListAndValidateBranches:
    """Branch coverage for list_saved_methods and validate_method."""

    @responses.activate
    def test_list_saved_methods_unknown_source(self, paypal_provider):
        """list_saved_methods with token having neither card nor paypal (branch 502->484)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens?customer_id=cus_test123&page_size=20",
            json={
                "payment_tokens": [
                    {
                        "id": "unknown_token_123",
                        "payment_source": {"bank": {"account_number": "1234"}},
                    }
                ]
            },
            status=200,
        )

        methods = paypal_provider.list_saved_methods("cus_test123")
        assert methods == []

    @responses.activate
    def test_validate_method_no_card_source(self, paypal_provider):
        """validate_method with token having paypal source, not card (branch 541->551)."""
        responses.add(
            responses.POST,
            "https://api-m.sandbox.paypal.com/v1/oauth2/token",
            json={"access_token": "test_token", "expires_in": 32400},
            status=200,
        )

        responses.add(
            responses.GET,
            "https://api-m.sandbox.paypal.com/v3/vault/payment-tokens/vault_token_123",
            json={
                "id": "vault_token_123",
                "status": "OK",
                "payment_source": {
                    "paypal": {"email": "test@example.com"},
                },
            },
            status=200,
        )

        sm = SavedMethod(
            gateway="paypal",
            gateway_customer_id="cus_test123",
            gateway_method_id="vault_token_123",
            method_type="paypal",
            display_label="PayPal (test@example.com)",
        )
        result = paypal_provider.validate_method(sm)

        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False
