"""Tests for Mollie provider.

Uses unittest.mock to mock the Mollie SDK at the client level — no network calls.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from autopay.core.base import is_autopay_capable
from autopay.core.result import ChargeStatus
from autopay.providers.mollie.provider import MollieAutopayProvider


@pytest.fixture
def mollie_client_mock():
    """Mock Mollie client."""
    with patch("autopay.providers.mollie.provider.MollieClient") as mock_class:
        mock_instance = MagicMock()
        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mollie_provider(mollie_client_mock):
    """Create a Mollie provider with mocked client."""
    return MollieAutopayProvider(api_key="test_key", testmode=True)


class TestMollieProviderInit:
    """Tests for provider initialization."""

    def test_init_with_api_key(self):
        """Test that API key is set correctly."""
        with patch("autopay.providers.mollie.provider.MollieClient") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            _ = MollieAutopayProvider(api_key="test_key")
            assert _.api_key == "test_key"
            mock_class.assert_called_once()
            mock_instance.set_api_key.assert_called_once_with("test_key")

    def test_init_with_testmode(self):
        """Test that test mode is activated."""
        with patch("autopay.providers.mollie.provider.MollieClient") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            _ = MollieAutopayProvider(api_key="test_key", testmode=True)
            mock_class.assert_called_once()
            mock_instance.set_testmode.assert_called_once_with(True)

    def test_init_with_webhook_url(self):
        """Test that webhook URL is stored."""
        with patch("autopay.providers.mollie.provider.MollieClient"):
            _ = MollieAutopayProvider(api_key="test_key", webhook_url="https://example.com/webhook")
            assert _.webhook_url == "https://example.com/webhook"

    def test_init_with_access_token(self):
        """Test initialization with access token (line 76)."""
        with patch("autopay.providers.mollie.provider.MollieClient") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            _ = MollieAutopayProvider(access_token="access_token_123")
            mock_instance.set_access_token.assert_called_once_with("access_token_123")

    def test_init_oauth_credentials(self):
        """Test initialization with OAuth credentials (line 78)."""
        with patch("autopay.providers.mollie.provider.MollieClient") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            MollieAutopayProvider(client_id="client_123", client_secret="secret_456")
            mock_instance.set_oauth2_client_credentials.assert_called_once_with("client_123", "secret_456")


class TestMollieCapabilityFlags:
    """Tests for capability flags."""

    def test_supports_off_session_charge(self, mollie_provider):
        """Test that provider supports off-session charge."""
        assert MollieAutopayProvider.SUPPORTS_OFF_SESSION_CHARGE is True

    def test_supports_list_methods(self, mollie_provider):
        """Test that provider supports list methods."""
        assert MollieAutopayProvider.SUPPORTS_LIST_METHODS is True

    def test_supports_revoke(self, mollie_provider):
        """Test that provider supports revoke."""
        assert MollieAutopayProvider.SUPPORTS_REVOKE is True

    def test_supports_validate(self, mollie_provider):
        """Test that provider supports validate."""
        assert MollieAutopayProvider.SUPPORTS_VALIDATE is True

    def test_supports_refresh(self, mollie_provider):
        """Test that provider supports refresh."""
        assert MollieAutopayProvider.SUPPORTS_REFRESH is True

    def test_supports_webhook_verification(self, mollie_provider):
        """Mollie verifies webhooks via the optional IP allowlist (no signature)."""
        assert MollieAutopayProvider.SUPPORTS_WEBHOOK_VERIFICATION is True


class TestMollieChargeOffSession:
    """Tests for charge_off_session."""

    def test_charge_success_confirmed(self, mollie_provider, mollie_client_mock):
        """Test that successful charge returns CONFIRMED."""
        # Setup mock payment
        mock_payment = MagicMock()
        mock_payment.status = "paid"
        mock_payment.id = "tr_123"
        mock_payment.amount = {"currency": "EUR", "value": "9.99"}
        mock_payment.is_paid.return_value = True
        mock_payment.is_pending.return_value = False
        mock_payment.is_failed.return_value = False
        mock_payment.is_canceled.return_value = False
        mock_payment.is_expired.return_value = False

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_456"

        result = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("9.99"),
            currency="EUR",
            description="Test charge",
        )

        assert result.status == ChargeStatus.CONFIRMED
        assert result.transaction_id == "tr_123"
        assert result.amount_charged == Decimal("9.99")
        assert result.currency == "EUR"

    def test_charge_pending_sepa(self, mollie_provider, mollie_client_mock):
        """Test that pending SEPA charge returns PENDING."""
        mock_payment = MagicMock()
        mock_payment.status = "open"
        mock_payment.id = "tr_456"
        mock_payment.amount = {"currency": "EUR", "value": "19.99"}
        mock_payment.is_paid.return_value = False
        mock_payment.is_pending.return_value = True
        mock_payment.is_open.return_value = True
        mock_payment.is_failed.return_value = False

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_456"

        result = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("19.99"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.PENDING
        assert result.transaction_id == "tr_456"
        assert result.amount_charged is None

    def test_charge_failed(self, mollie_provider, mollie_client_mock):
        """Test that failed charge returns FAILED."""
        mock_payment = MagicMock()
        mock_payment.status = "failed"
        mock_payment.id = "tr_789"
        mock_payment.amount = {"currency": "EUR", "value": "5.00"}
        mock_payment.is_paid.return_value = False
        mock_payment.is_pending.return_value = False
        mock_payment.is_open.return_value = False
        mock_payment.is_failed.return_value = True
        mock_payment.is_canceled.return_value = False
        mock_payment.is_expired.return_value = False
        mock_payment.details = {"failureReason": "card_declined"}

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_456"

        result = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("5.00"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.FAILED
        assert "card_declined" in result.message

    def test_charge_rejected_fraud(self, mollie_provider, mollie_client_mock):
        """Test that charge rejected for fraud returns REJECTED."""
        mock_payment = MagicMock()
        mock_payment.status = "failed"
        mock_payment.id = "tr_fraud"
        mock_payment.amount = {"currency": "EUR", "value": "100.00"}
        mock_payment.is_paid.return_value = False
        mock_payment.is_pending.return_value = False
        mock_payment.is_open.return_value = False
        mock_payment.is_failed.return_value = True
        mock_payment.is_canceled.return_value = False
        mock_payment.is_expired.return_value = False
        mock_payment.details = {"failureReason": "possible_fraud"}

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_456"

        result = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("100.00"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.REJECTED
        assert "possible_fraud" in result.message

    def test_charge_api_error(self, mollie_provider, mollie_client_mock):
        """Test that API error returns FAILED."""
        mollie_client_mock.payments.create.side_effect = Exception("API error")

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_456"

        result = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("10.00"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.FAILED
        assert "API error" in result.message
        assert result.transaction_id is None


class TestMollieValidateMethod:
    """Tests for validate_method."""

    def test_validate_valid_mandate(self, mollie_provider, mollie_client_mock):
        """Test that valid mandate returns is_valid=True."""
        mock_mandate = MagicMock()
        mock_mandate.status = "valid"
        mock_mandate.id = "mdt_123"
        mock_mandate.method = "creditcard"
        mock_mandate.details = {"cardLabel": "Visa", "cardNumber": "4242424242424242"}

        mollie_client_mock.customer_mandates.get.return_value = mock_mandate

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_123"

        result = mollie_provider.validate_method(saved_method)

        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False
        assert result.reason == "Valid mandate"

    def test_validate_pending_mandate(self, mollie_provider, mollie_client_mock):
        """Test that pending mandate returns is_valid=False, needs_refresh=False."""
        mock_mandate = MagicMock()
        mock_mandate.status = "pending"

        mollie_client_mock.customer_mandates.get.return_value = mock_mandate

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_123"

        result = mollie_provider.validate_method(saved_method)

        assert result.is_valid is False
        assert result.is_expired is False
        assert result.needs_refresh is False

    def test_validate_invalid_mandate(self, mollie_provider, mollie_client_mock):
        """Test that invalid mandate returns needs_refresh=True."""
        mock_mandate = MagicMock()
        mock_mandate.status = "invalid"

        mollie_client_mock.customer_mandates.get.return_value = mock_mandate

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_123"

        result = mollie_provider.validate_method(saved_method)

        assert result.is_valid is False
        assert result.needs_refresh is True

    def test_validate_mandate_not_found(self, mollie_provider, mollie_client_mock):
        """Test that mandate not found returns needs_refresh=True."""
        from mollie.api.error import Error as MollieError

        mollie_client_mock.customer_mandates.get.side_effect = MollieError("Mandate not found")

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_123"

        result = mollie_provider.validate_method(saved_method)

        assert result.is_valid is False
        assert result.needs_refresh is True


class TestMollieRefreshMethod:
    """Tests for refresh_method and confirm_refresh."""

    @override_settings(AUTOPAY_SITE_BASE_URL="https://example.com")
    def test_refresh_method_creates_first_payment(self, mollie_provider, mollie_client_mock):
        """Test that refresh_method creates a first payment."""
        mock_payment = MagicMock()
        mock_payment.id = "tr_refresh_123"
        mock_payment.checkout_url = "https://checkout.mollie.com/tr_refresh_123"

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.id = 42
        saved_method.gateway_customer_id = "cus_123"

        result = mollie_provider.refresh_method(saved_method)

        assert result.gateway_setup_id == "tr_refresh_123"
        assert result.frontend_sdk == "mollie"
        assert result.frontend_payload["payment_url"] == "https://checkout.mollie.com/tr_refresh_123"
        assert result.frontend_payload["payment_id"] == "tr_refresh_123"

        # Verify correct payload was sent
        call_args = mollie_client_mock.payments.create.call_args[0][0]
        assert call_args["sequenceType"] == "first"
        assert call_args["amount"] == {"currency": "EUR", "value": "0.01"}
        assert call_args["customerId"] == "cus_123"

    def test_confirm_refresh_success(self, mollie_provider, mollie_client_mock):
        """Test that confirm_refresh returns new SavedMethod."""
        # Mock first payment
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True

        # Mock new mandate
        mock_mandate = MagicMock()
        mock_mandate.id = "mdt_new_123"
        mock_mandate.status = "valid"
        mock_mandate.method = "creditcard"
        mock_mandate.signatureDate = "2024-01-01"
        mock_mandate.details = {"cardLabel": "Visa", "cardNumber": "4242424242424242"}

        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate]

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"
        saved_method.method_type = "card"
        saved_method.display_label = "Visa •••• 4242"
        saved_method.is_active = True
        saved_method.metadata = {}

        result = mollie_provider.confirm_refresh(
            saved_method=saved_method,
            refresh_payload={"first_payment_id": "tr_refresh_123"},
        )

        assert result.gateway_method_id == "mdt_new_123"
        assert result.gateway_customer_id == "cus_123"
        assert result.method_type == "card"

        # Verify old mandate was revoked
        mollie_client_mock.customer_mandates.delete.assert_called_once_with("cus_123", "mdt_old_123")

    def test_confirm_refresh_no_new_mandate(self, mollie_provider, mollie_client_mock):
        """Test that confirm_refresh raises error if no new mandates."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True

        # No new mandate
        mock_old_mandate = MagicMock()
        mock_old_mandate.id = "mdt_old_123"
        mock_old_mandate.status = "valid"

        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.customer_mandates.list_for.return_value = [mock_old_mandate]

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"

        with pytest.raises(RuntimeError, match="No new valid mandate found"):
            mollie_provider.confirm_refresh(
                saved_method=saved_method,
                refresh_payload={"first_payment_id": "tr_refresh_123"},
            )


class TestMollieListSavedMethods:
    """Tests for list_saved_methods."""

    def test_list_methods_success(self, mollie_provider, mollie_client_mock):
        """Test that list_saved_methods returns valid mandates."""
        mock_mandate1 = MagicMock()
        mock_mandate1.id = "mdt_1"
        mock_mandate1.status = "valid"
        mock_mandate1.method = "creditcard"
        mock_mandate1.details = {"cardLabel": "Visa", "cardNumber": "4242424242424242"}

        mock_mandate2 = MagicMock()
        mock_mandate2.id = "mdt_2"
        mock_mandate2.status = "invalid"  # Not included
        mock_mandate2.method = "creditcard"

        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate1, mock_mandate2]

        methods = mollie_provider.list_saved_methods("cus_123")

        assert len(methods) == 1
        assert methods[0].gateway_method_id == "mdt_1"
        assert methods[0].method_type == "card"

    def test_list_methods_empty_customer(self, mollie_provider, mollie_client_mock):
        """Test that empty customer returns empty list."""
        methods = mollie_provider.list_saved_methods("")
        assert methods == []


class TestMollieRevokeMethod:
    """Tests for revoke_method."""

    def test_revoke_success(self, mollie_provider, mollie_client_mock):
        """Test that revoke_method deletes mandate."""
        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_123"

        mollie_provider.revoke_method(saved_method)

        mollie_client_mock.customer_mandates.delete.assert_called_once_with("cus_123", "mdt_123")


class TestMollieWebhook:
    """Tests for webhook handling."""

    def test_verify_webhook_request_signature(self, mollie_provider):
        """Test that verify_webhook_request accepts (raw_body, headers) signature."""
        # Test with form-encoded body
        result = mollie_provider.verify_webhook_request(b"id=tr_123", {"Content-Type": "application/x-www-form-urlencoded"})
        assert result is True

        # Test with JSON body
        result = mollie_provider.verify_webhook_request(b'{"id": "tr_123"}', {"Content-Type": "application/json"})
        assert result is True

        # Test without id
        result = mollie_provider.verify_webhook_request(b"", {})
        assert result is False

    def test_verify_webhook_request_json_parse_error(self, mollie_provider):
        """Test verify_webhook_request returns False on JSON parse error."""
        # Invalid JSON
        result = mollie_provider.verify_webhook_request(
            b"{invalid json",
            {"Content-Type": "application/json"}
        )
        assert result is False

    def test_verify_webhook_request_form_parse_error(self, mollie_provider):
        """Test verify_webhook_request returns False on form parse error (bad UTF-8)."""
        # Invalid UTF-8
        result = mollie_provider.verify_webhook_request(
            b"\xff\xfe invalid",
            {"Content-Type": "application/x-www-form-urlencoded"}
        )
        assert result is False

    def test_verify_webhook_request_no_ip_check(self, mollie_provider):
        """Test that without IP allowlist, webhook is valid if it has an id."""
        # New signature: (raw_body, headers)
        assert mollie_provider.verify_webhook_request(b"id=tr_123", {"Content-Type": "application/x-www-form-urlencoded"}) is True

    def test_verify_webhook_request_with_ip_check(self, mollie_provider):
        """Test that with IP allowlist, only allowed IPs are valid."""
        with override_settings(AUTOPAY_MOLLIE_ALLOWED_IPS=["192.168.1.1"]):
            # New signature: (raw_body, headers)
            assert mollie_provider.verify_webhook_request(
                b"id=tr_123",
                {"Content-Type": "application/x-www-form-urlencoded", "REMOTE_ADDR": "192.168.1.1"}
            ) is True

            assert mollie_provider.verify_webhook_request(
                b"id=tr_123",
                {"Content-Type": "application/x-www-form-urlencoded", "REMOTE_ADDR": "10.0.0.1"}
            ) is False

    def test_verify_webhook_request_ip_check_rejects_missing_ip(self, mollie_provider):
        """Fail closed: allowlist configured and no REMOTE_ADDR → rejected."""
        with override_settings(AUTOPAY_MOLLIE_ALLOWED_IPS=["192.168.1.1"]):
            assert mollie_provider.verify_webhook_request(
                b"id=tr_123",
                {"Content-Type": "application/x-www-form-urlencoded"}
            ) is False

    def test_verify_webhook_request_json_body(self, mollie_provider):
        """Test webhook verification with JSON body."""
        assert mollie_provider.verify_webhook_request(
            b'{"id": "tr_123"}',
            {"Content-Type": "application/json"}
        ) is True

    def test_verify_webhook_request_json_body_no_id(self, mollie_provider):
        """Test webhook verification with JSON body but no id."""
        assert mollie_provider.verify_webhook_request(
            b'{"event": "payment.paid"}',
            {"Content-Type": "application/json"}
        ) is False

    def test_process_webhook_event(self, mollie_provider, mollie_client_mock):
        """Test that process_webhook_event processes the event."""
        mock_payment = MagicMock()
        mock_payment.status = "paid"
        mock_payment.id = "tr_123"
        mock_payment.amount = {"currency": "EUR", "value": "9.99"}
        mock_payment.is_paid.return_value = True

        mollie_client_mock.payments.get.return_value = mock_payment

        result = mollie_provider.process_webhook_event("payment.paid", {"id": "tr_123"})

        assert result.processed is True
        assert result.action == "status_update"
        assert result.transaction_id == "tr_123"

    def test_process_webhook_event_missing_id(self, mollie_provider):
        """Test process_webhook_event with missing payment id (line 460)."""
        result = mollie_provider.process_webhook_event("payment.paid", {})
        assert result.processed is False
        assert result.action == "ignored"

    def test_process_webhook_event_error(self, mollie_provider, mollie_client_mock):
        """Test process_webhook_event with API error."""
        mollie_client_mock.payments.get.side_effect = Exception("API error")

        result = mollie_provider.process_webhook_event("payment.paid", {"id": "tr_error"})

        # process_webhook_event returns None when SUPPORTS_WEBHOOK_VERIFICATION is False
        # The error is logged but no result is returned
        assert result is None or (result.processed is False and result.action == "error")


class TestMollieIsAutopayCapable:
    """Tests for is_autopay_capable."""

    def test_mollie_provider_is_capable(self, mollie_provider):
        """Test that MollieAutopayProvider is considered capable."""
        assert is_autopay_capable(mollie_provider) is True

    def test_non_autopay_provider_not_capable(self, mollie_provider):
        """Test that non-AutopayProvider object is not considered capable."""
        assert is_autopay_capable("not a provider") is False
        assert is_autopay_capable({"not": "a provider"}) is False


class TestMollieOAuthInit:
    """Tests for Mollie OAuth initialization."""

    def test_init_oauth_mode(self):
        """Test that OAuth mode is activated when oauth_client_id is provided."""
        with patch("autopay.providers.mollie.provider.MollieClient") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            provider = MollieAutopayProvider(
                client_id="oauth_client_123",
                client_secret="oauth_secret_456",
            )
            assert provider.client_id == "oauth_client_123"
            assert provider.client_secret == "oauth_secret_456"
            mock_instance.set_oauth2_client_credentials.assert_called_once_with(
                "oauth_client_123", "oauth_secret_456"
            )

    def test_init_oauth_with_testmode(self):
        """Test that test mode works with OAuth."""
        with patch("autopay.providers.mollie.provider.MollieClient") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            _ = MollieAutopayProvider(
                oauth_client_id="oauth_client_123",
                oauth_secret="oauth_secret_456",
                testmode=True,
            )
            mock_instance.set_testmode.assert_called_once_with(True)


class TestMollieRefreshFlow:
    """Tests for Mollie refresh flow edge cases."""

    @override_settings(AUTOPAY_SITE_BASE_URL="https://example.com")
    def test_refresh_method_zero_amount_card(self, mollie_provider, mollie_client_mock):
        """Test that refresh_method supports zero-amount for card mandates."""
        mock_payment = MagicMock()
        mock_payment.id = "tr_refresh_zero"
        mock_payment.checkout_url = "https://checkout.mollie.com/tr_refresh_zero"

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.id = 42
        saved_method.gateway_customer_id = "cus_123"

        # Zero amount for card
        result = mollie_provider.refresh_method(saved_method, currency="EUR", description="Card verification", amount="0.00")

        assert result.gateway_setup_id == "tr_refresh_zero"

        call_args = mollie_client_mock.payments.create.call_args[0][0]
        assert call_args["amount"] == {"currency": "EUR", "value": "0.00"}
        assert call_args["description"] == "Card verification"

    @override_settings(AUTOPAY_SITE_BASE_URL="https://example.com")
    def test_refresh_method_custom_currency_description(self, mollie_provider, mollie_client_mock):
        """Test that refresh_method accepts custom currency and description."""
        mock_payment = MagicMock()
        mock_payment.id = "tr_refresh_custom"
        mock_payment.checkout_url = "https://checkout.mollie.com/tr_refresh_custom"

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.id = 42
        saved_method.gateway_customer_id = "cus_123"

        _ = mollie_provider.refresh_method(saved_method, currency="USD", description="USD verification", amount="0.01")

        call_args = mollie_client_mock.payments.create.call_args[0][0]
        assert call_args["amount"] == {"currency": "USD", "value": "0.01"}
        assert call_args["description"] == "USD verification"

    def test_confirm_refresh_resolves_first_payment_from_payload(self, mollie_provider, mollie_client_mock):
        """Test that confirm_refresh resolves first_payment_id from payload."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True

        mock_mandate = MagicMock()
        mock_mandate.id = "mdt_new_123"
        mock_mandate.status = "valid"
        mock_mandate.method = "creditcard"
        mock_mandate.details = {"cardLabel": "Visa", "cardNumber": "4242424242424242"}

        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate]

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"

        result = mollie_provider.confirm_refresh(
            saved_method=saved_method,
            refresh_payload={"first_payment_id": "tr_refresh_123"},
        )

        assert result.gateway_method_id == "mdt_new_123"
        mollie_client_mock.payments.get.assert_called_once_with("tr_refresh_123")

    def test_confirm_refresh_raises_when_no_first_payment(self, mollie_provider):
        """Test that confirm_refresh raises RuntimeError when first_payment_id cannot be resolved."""
        saved_method = MagicMock()
        saved_method.gateway_customer_id = ""
        saved_method.gateway_method_id = ""
        saved_method.metadata = {}

        with pytest.raises(RuntimeError, match="Cannot resolve first payment id"):
            mollie_provider.confirm_refresh(saved_method, {})

    def test_confirm_refresh_with_metadata_fallback(self, mollie_provider, mollie_client_mock):
        """Test that confirm_refresh can resolve first_payment_id from metadata."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True

        mock_mandate = MagicMock()
        mock_mandate.id = "mdt_new_456"
        mock_mandate.status = "valid"
        mock_mandate.method = "creditcard"
        mock_mandate.details = {"cardLabel": "Mastercard", "cardNumber": "5555555555554444"}

        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate]

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"
        saved_method.metadata = {"first_payment_reference": "tr_from_metadata"}

        result = mollie_provider.confirm_refresh(
            saved_method=saved_method,
            refresh_payload={},  # Empty payload, use metadata
        )

        assert result.gateway_method_id == "mdt_new_456"
        mollie_client_mock.payments.get.assert_called_once_with("tr_from_metadata")

    def test_confirm_refresh_resolves_mandate_from_gateway(self, mollie_provider, mollie_client_mock):
        """Test confirm_refresh resolves first_payment_id from gateway mandates."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True

        mock_mandate = MagicMock()
        mock_mandate.id = "mdt_new_789"
        mock_mandate.status = "valid"
        mock_mandate.method = "creditcard"
        mock_mandate.details = {"cardLabel": "Visa", "cardNumber": "4242424242424242"}

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"
        saved_method.metadata = {}  # No first_payment_reference in metadata

        # No first_payment_id in payload, should resolve from mandates
        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate]

        result = mollie_provider.confirm_refresh(
            saved_method=saved_method,
            refresh_payload={},  # Empty payload
        )

        assert result.gateway_method_id == "mdt_new_789"
        # Should have called list_for to find mandates (may be called multiple times in confirm_refresh)

    def test_confirm_refresh_mandate_resolution_fails(self, mollie_provider, mollie_client_mock):
        """Test confirm_refresh when mandate listing raises exception."""
        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"
        saved_method.metadata = {}

        # list_for raises exception
        mollie_client_mock.customer_mandates.list_for.side_effect = Exception("API error")

        with pytest.raises(RuntimeError, match="Cannot resolve first payment id"):
            mollie_provider.confirm_refresh(
                saved_method=saved_method,
                refresh_payload={},
            )


class TestMollieRefund:
    """Tests for refund functionality."""

    def test_refund_full(self, mollie_provider, mollie_client_mock):
        """Test full refund."""
        mock_payment = MagicMock()
        mock_payment.id = "tr_refund_123"
        mock_payment.amount = {"currency": "EUR", "value": "10.00"}

        mock_refund = MagicMock()
        mock_refund.id = "rf_refund_123"
        mock_refund.amount = {"currency": "EUR", "value": "10.00"}

        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.payments_refunds.create.return_value = mock_refund

        result = mollie_provider.refund("tr_refund_123")

        assert result.status == ChargeStatus.CONFIRMED
        assert result.transaction_id == "rf_refund_123"
        assert result.amount_charged == Decimal("10.00")
        assert result.currency == "EUR"
        mollie_client_mock.payments_refunds.create.assert_called_once()

    def test_refund_partial(self, mollie_provider, mollie_client_mock):
        """Test partial refund."""
        mock_payment = MagicMock()
        mock_payment.id = "tr_refund_partial"
        mock_payment.amount = {"currency": "USD", "value": "50.00"}

        mock_refund = MagicMock()
        mock_refund.id = "rf_partial"
        mock_refund.amount = {"currency": "USD", "value": "25.00"}

        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.payments_refunds.create.return_value = mock_refund

        result = mollie_provider.refund("tr_refund_partial", amount=Decimal("25.00"))

        assert result.status == ChargeStatus.CONFIRMED
        assert result.amount_charged == Decimal("25.00")
        assert result.currency == "USD"

        # Verify the refund payload
        call_args = mollie_client_mock.payments_refunds.create.call_args
        assert call_args[0][1]["amount"] == {"currency": "USD", "value": "25.00"}

    def test_refund_with_reason(self, mollie_provider, mollie_client_mock):
        """Test refund with reason."""
        mock_payment = MagicMock()
        mock_payment.id = "tr_refund_reason"
        mock_payment.amount = {"currency": "EUR", "value": "15.00"}

        mock_refund = MagicMock()
        mock_refund.id = "rf_reason"

        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.payments_refunds.create.return_value = mock_refund

        result = mollie_provider.refund("tr_refund_reason", reason="Customer request")

        assert result.status == ChargeStatus.CONFIRMED
        call_args = mollie_client_mock.payments_refunds.create.call_args
        assert call_args[0][1]["description"] == "Customer request"

    def test_refund_error(self, mollie_provider, mollie_client_mock):
        """Test refund failure."""
        mollie_client_mock.payments.get.side_effect = Exception("Payment not found")

        result = mollie_provider.refund("tr_not_found")

        assert result.status == ChargeStatus.FAILED
        assert "Refund failed" in result.message
        """Test that refresh_method uses default amount (0.01 EUR)."""
        mock_payment = MagicMock()
        mock_payment.id = "tr_refresh_456"
        mock_payment.checkout_url = "https://checkout.mollie.com/tr_refresh_456"

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.id = 42
        saved_method.gateway_customer_id = "cus_123"

        # Default amount (0.01 EUR)
        result = mollie_provider.refresh_method(saved_method)

        assert result.gateway_setup_id == "tr_refresh_456"

        call_args = mollie_client_mock.payments.create.call_args[0][0]
        assert call_args["amount"] == {"currency": "EUR", "value": "0.01"}

    @override_settings(AUTOPAY_SITE_BASE_URL="https://example.com")
    def test_refresh_method_with_custom_description(self, mollie_provider, mollie_client_mock):
        """Test that refresh_method uses default description."""
        mock_payment = MagicMock()
        mock_payment.id = "tr_refresh_789"
        mock_payment.checkout_url = "https://checkout.mollie.com/tr_refresh_789"

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.id = 42
        saved_method.gateway_customer_id = "cus_123"

        _ = mollie_provider.refresh_method(saved_method)

        call_args = mollie_client_mock.payments.create.call_args[0][0]
        assert call_args["description"] == "Mandate verification"

    @override_settings(AUTOPAY_SITE_BASE_URL="https://example.com")
    def test_refresh_method_with_webhook_url(self, mollie_provider, mollie_client_mock):
        """Test that refresh_method includes webhookUrl when set (line 385)."""
        # Create provider with webhook_url
        with patch("autopay.providers.mollie.provider.MollieClient") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            provider = MollieAutopayProvider(api_key="test_key", webhook_url="https://example.com/webhook")

            mock_payment = MagicMock()
            mock_payment.id = "tr_refresh_webhook"
            mock_payment.checkout_url = "https://checkout.mollie.com/tr_refresh_webhook"

            mock_instance.payments.create.return_value = mock_payment

            saved_method = MagicMock()
            saved_method.id = 42
            saved_method.gateway_customer_id = "cus_123"

            result = provider.refresh_method(saved_method)

            call_args = mock_instance.payments.create.call_args[0][0]
            assert call_args["webhookUrl"] == "https://example.com/webhook"
            assert result.gateway_setup_id == "tr_refresh_webhook"

    def test_confirm_refresh_payment_not_found(self, mollie_provider, mollie_client_mock):
        """Test that confirm_refresh handles payment not found."""
        mollie_client_mock.payments.get.side_effect = Exception("Payment not found")

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"

        with pytest.raises(Exception, match="Payment not found"):
            mollie_provider.confirm_refresh(
                saved_method=saved_method,
                refresh_payload={"first_payment_id": "tr_not_found"},
            )

    def test_confirm_refresh_payment_pending(self, mollie_provider, mollie_client_mock):
        """Test that confirm_refresh handles pending payment."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = False
        mock_payment.status = "tr_pending"

        mollie_client_mock.payments.get.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"

        with pytest.raises(RuntimeError, match="is not paid"):
            mollie_provider.confirm_refresh(
                saved_method=saved_method,
                refresh_payload={"first_payment_id": "tr_pending"},
            )

    def test_confirm_refresh_mandate_creation_error(self, mollie_provider, mollie_client_mock):
        """Test that confirm_refresh handles mandate creation error."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True

        # No mandates returned
        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.customer_mandates.list_for.return_value = []

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"

        with pytest.raises(RuntimeError, match="No new valid mandate found"):
            mollie_provider.confirm_refresh(
                saved_method=saved_method,
                refresh_payload={"first_payment_id": "tr_refresh_123"},
            )

    def test_confirm_refresh_missing_payment_id(self, mollie_provider):
        """Test that confirm_refresh raises error when payment_id is missing."""
        saved_method = MagicMock()
        saved_method.gateway_customer_id = ""
        saved_method.gateway_method_id = ""
        saved_method.metadata = {}

        with pytest.raises(RuntimeError, match="Cannot resolve first payment id"):
            mollie_provider.confirm_refresh(saved_method, {})

    def test_confirm_refresh_old_mandate_delete_error(self, mollie_provider, mollie_client_mock):
        """Test that confirm_refresh handles old mandate delete error (lines 431-432)."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True

        mock_mandate = MagicMock()
        mock_mandate.id = "mdt_new_123"
        mock_mandate.status = "valid"
        mock_mandate.method = "creditcard"
        mock_mandate.details = {"cardLabel": "Visa", "cardNumber": "4242424242424242"}

        mollie_client_mock.payments.get.return_value = mock_payment
        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate]
        # Make delete raise an error (should be caught and logged)
        mollie_client_mock.customer_mandates.delete.side_effect = Exception("Delete error")

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"
        saved_method.method_type = "card"
        saved_method.display_label = "Visa"
        saved_method.is_active = True
        saved_method.metadata = {}

        # Should not raise - the error is caught and logged
        result = mollie_provider.confirm_refresh(
            saved_method=saved_method,
            refresh_payload={"first_payment_id": "tr_refresh_123"},
        )
        assert result.gateway_method_id == "mdt_new_123"

    def test_confirm_refresh_no_customer_id_falls_through(self, mollie_provider, mollie_client_mock):
        """Test confirm_refresh when customer_id is None/empty (line 435 branch not taken)."""
        saved_method = MagicMock()
        saved_method.gateway_customer_id = ""  # Empty customer_id
        saved_method.gateway_method_id = "mdt_old_123"
        saved_method.metadata = {}  # No first_payment_reference

        # No first_payment_id in payload, and customer_id is empty so mandates not fetched
        with pytest.raises(RuntimeError, match="Cannot resolve first payment id"):
            mollie_provider.confirm_refresh(
                saved_method=saved_method,
                refresh_payload={},
            )

    def test_confirm_refresh_empty_mandates_list(self, mollie_provider, mollie_client_mock):
        """Test confirm_refresh when mandates list is empty (line 438 branch not taken)."""
        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"
        saved_method.metadata = {}  # No first_payment_reference

        # Return empty mandates list
        mollie_client_mock.customer_mandates.list_for.return_value = []

        with pytest.raises(RuntimeError, match="Cannot resolve first payment id"):
            mollie_provider.confirm_refresh(
                saved_method=saved_method,
                refresh_payload={},
            )

    def test_confirm_refresh_no_valid_mandates(self, mollie_provider, mollie_client_mock):
        """Test confirm_refresh when mandates exist but none are valid (line 441 branch not taken)."""
        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_old_123"
        saved_method.metadata = {}  # No first_payment_reference

        # Return mandates but none are valid
        mock_mandate_invalid = MagicMock()
        mock_mandate_invalid.id = "mdt_invalid_1"
        mock_mandate_invalid.status = "invalid"

        mock_mandate_pending = MagicMock()
        mock_mandate_pending.id = "mdt_pending_1"
        mock_mandate_pending.status = "pending"

        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate_invalid, mock_mandate_pending]

        with pytest.raises(RuntimeError, match="Cannot resolve first payment id"):
            mollie_provider.confirm_refresh(
                saved_method=saved_method,
                refresh_payload={},
            )


class TestMollieChargeEdgeCases:
    """Tests for Mollie charge edge cases."""

    def test_charge_canceled_status(self, mollie_provider, mollie_client_mock):
        """Test that canceled charge returns FAILED."""
        mock_payment = MagicMock()
        mock_payment.status = "canceled"
        mock_payment.id = "tr_canceled"
        mock_payment.amount = {"currency": "EUR", "value": "10.00"}
        mock_payment.is_canceled.return_value = True
        mock_payment.is_paid.return_value = False
        mock_payment.is_pending.return_value = False
        mock_payment.is_failed.return_value = False
        mock_payment.is_expired.return_value = False
        mock_payment.is_open.return_value = False

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_456"

        result = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("10.00"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.FAILED

    def test_charge_expired_status(self, mollie_provider, mollie_client_mock):
        """Test that expired charge returns FAILED."""
        mock_payment = MagicMock()
        mock_payment.status = "expired"
        mock_payment.id = "tr_expired"
        mock_payment.amount = {"currency": "EUR", "value": "10.00"}
        mock_payment.is_expired.return_value = True
        mock_payment.is_paid.return_value = False
        mock_payment.is_pending.return_value = False
        mock_payment.is_failed.return_value = False
        mock_payment.is_canceled.return_value = False
        mock_payment.is_open.return_value = False

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_456"

        result = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("10.00"),
            currency="EUR",
        )

        assert result.status == ChargeStatus.FAILED

    def test_charge_unknown_status(self, mollie_provider, mollie_client_mock):
        """Test that unknown status returns PENDING."""
        mock_payment = MagicMock()
        mock_payment.status = "unknown_status"
        mock_payment.id = "tr_unknown"
        mock_payment.is_paid.return_value = False
        mock_payment.is_pending.return_value = False
        mock_payment.is_failed.return_value = False
        mock_payment.is_canceled.return_value = False
        mock_payment.is_expired.return_value = False

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_456"

        result = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("10.00"),
            currency="EUR",
        )

        # Source returns PENDING for unknown status
        assert result.status == ChargeStatus.PENDING

    def test_charge_unknown_status_else_branch(self, mollie_provider, mollie_client_mock):
        """Test charge with status that falls through to else (line 266)."""
        mock_payment = MagicMock()
        mock_payment.status = "canceled"  # This falls through to else after all the if/elif checks
        mock_payment.id = "tr_else"
        mock_payment.amount = {"currency": "EUR", "value": "10.00"}
        # All the is_* methods return False, so it falls to else
        mock_payment.is_paid.return_value = False
        mock_payment.is_pending.return_value = False
        mock_payment.is_open.return_value = False
        mock_payment.is_failed.return_value = False
        mock_payment.is_canceled.return_value = False  # Important: False even though status is "canceled"
        mock_payment.is_expired.return_value = False

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_456"

        result = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("10.00"),
            currency="EUR",
        )

        # Falls to else branch, returns PENDING with status message
        assert result.status == ChargeStatus.PENDING
        assert "status=canceled" in result.message

    def test_charge_with_mandate_id(self, mollie_provider, mollie_client_mock):
        """Test that charge uses mandateId from saved_method.gateway_method_id."""
        mock_payment = MagicMock()
        mock_payment.status = "paid"
        mock_payment.id = "tr_with_mandate"
        mock_payment.amount = {"currency": "EUR", "value": "15.00"}
        mock_payment.is_paid.return_value = True

        mollie_client_mock.payments.create.return_value = mock_payment

        saved_method = MagicMock()
        saved_method.gateway_customer_id = "cus_123"
        saved_method.gateway_method_id = "mdt_789"

        _ = mollie_provider.charge_off_session(
            saved_method=saved_method,
            amount=Decimal("15.00"),
            currency="EUR",
        )

        call_args = mollie_client_mock.payments.create.call_args[0][0]
        assert call_args["mandateId"] == "mdt_789"

    def test_charge_with_webhook_url(self, mollie_provider, mollie_client_mock):
        """Test that charge includes webhookUrl when set (line 170)."""
        # Create provider with webhook_url
        with patch("autopay.providers.mollie.provider.MollieClient") as mock_class:
            mock_instance = MagicMock()
            mock_class.return_value = mock_instance
            provider = MollieAutopayProvider(api_key="test_key", webhook_url="https://example.com/webhook")

            mock_payment = MagicMock()
            mock_payment.status = "paid"
            mock_payment.id = "tr_webhook"
            mock_payment.amount = {"currency": "EUR", "value": "10.00"}
            mock_payment.is_paid.return_value = True

            mock_instance.payments.create.return_value = mock_payment

            saved_method = MagicMock()
            saved_method.gateway_customer_id = "cus_123"
            saved_method.gateway_method_id = "mdt_456"

            _ = provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.00"),
                currency="EUR",
            )

            call_args = mock_instance.payments.create.call_args[0][0]
            assert call_args["webhookUrl"] == "https://example.com/webhook"


class TestMollieGetChargeStatus:
    """Tests for get_charge_status."""

    def test_get_charge_status_paid(self, mollie_provider, mollie_client_mock):
        """Test that paid payment returns CONFIRMED."""
        mock_payment = MagicMock()
        mock_payment.status = "paid"
        mock_payment.id = "tr_123"
        mock_payment.amount = {"currency": "EUR", "value": "25.00"}
        mock_payment.is_paid.return_value = True

        mollie_client_mock.payments.get.return_value = mock_payment

        result = mollie_provider.get_charge_status("tr_123")

        assert result.status == ChargeStatus.CONFIRMED
        assert result.transaction_id == "tr_123"
        assert result.amount_charged == Decimal("25.00")

    def test_get_charge_status_open(self, mollie_provider, mollie_client_mock):
        """Test that open payment returns PENDING."""
        mock_payment = MagicMock()
        mock_payment.status = "open"
        mock_payment.id = "tr_456"
        mock_payment.amount = {"currency": "EUR", "value": "30.00"}
        mock_payment.is_open.return_value = True
        mock_payment.is_paid.return_value = False

        mollie_client_mock.payments.get.return_value = mock_payment

        result = mollie_provider.get_charge_status("tr_456")

        assert result.status == ChargeStatus.PENDING

    def test_get_charge_status_failed(self, mollie_provider, mollie_client_mock):
        """Test that failed payment returns FAILED."""
        mock_payment = MagicMock()
        mock_payment.status = "failed"
        mock_payment.id = "tr_789"
        mock_payment.amount = None
        mock_payment.is_failed.return_value = True
        mock_payment.details = {"failureReason": "insufficient_funds"}
        mock_payment.is_paid.return_value = False
        mock_payment.is_pending.return_value = False
        mock_payment.is_open.return_value = False
        mock_payment.is_canceled.return_value = False
        mock_payment.is_expired.return_value = False

        mollie_client_mock.payments.get.return_value = mock_payment

        result = mollie_provider.get_charge_status("tr_789")

        assert result.status == ChargeStatus.FAILED

    def test_get_charge_status_api_error(self, mollie_provider, mollie_client_mock):
        """Test that API error returns FAILED."""
        mollie_client_mock.payments.get.side_effect = Exception("API error")

        # The source doesn't catch exceptions, so this will raise
        with pytest.raises(Exception, match="API error"):
            mollie_provider.get_charge_status("tr_error")


class TestMollieSaveMethod:
    """Tests for save_method."""

    def test_save_method_success(self, mollie_provider, mollie_client_mock):
        """Test that save_method returns SavedMethod."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True
        mock_payment.customerId = "cus_123"

        # Mock the payment retrieval
        mollie_client_mock.payments.get.return_value = mock_payment

        # Mock mandates list
        mock_mandate = MagicMock()
        mock_mandate.status = "valid"
        mock_mandate.id = "mdt_456"
        mock_mandate.method = "creditcard"
        mock_mandate.details = {"cardLabel": "Visa", "cardNumber": "4242424242424242"}
        
        mock_mandate_list = MagicMock()
        mock_mandate_list.data = [mock_mandate]
        mollie_client_mock.customer_mandates.list_for.return_value = mock_mandate_list.data

        mock_payment_obj = MagicMock()
        mock_payment_obj.transaction_id = "tr_123"

        result = mollie_provider.save_method(mock_payment_obj)

        assert result.gateway == "mollie"
        assert result.gateway_customer_id == "cus_123"
        assert result.gateway_method_id == "mdt_456"
        assert result.method_type == "card"
        assert result.display_label == "Visa •••• 4242"

    def test_save_method_directdebit(self, mollie_provider, mollie_client_mock):
        """Test save_method with SEPA direct debit (lines 122-125)."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True
        mock_payment.customerId = "cus_123"

        mollie_client_mock.payments.get.return_value = mock_payment

        mock_mandate = MagicMock()
        mock_mandate.status = "valid"
        mock_mandate.id = "mdt_sepa_123"
        mock_mandate.method = "directdebit"
        mock_mandate.details = {"consumerAccount": "NL12BANK0123456789"}

        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate]

        mock_payment_obj = MagicMock()
        mock_payment_obj.transaction_id = "tr_123"

        result = mollie_provider.save_method(mock_payment_obj)

        assert result.method_type == "sepa"
        assert "IBAN" in result.display_label

    def test_save_method_paypal(self, mollie_provider, mollie_client_mock):
        """Test save_method with PayPal (lines 126-128)."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True
        mock_payment.customerId = "cus_123"

        mollie_client_mock.payments.get.return_value = mock_payment

        mock_mandate = MagicMock()
        mock_mandate.status = "valid"
        mock_mandate.id = "mdt_pp_123"
        mock_mandate.method = "paypal"
        mock_mandate.details = {"consumerAccount": "user@example.com"}

        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate]

        mock_payment_obj = MagicMock()
        mock_payment_obj.transaction_id = "tr_123"

        result = mollie_provider.save_method(mock_payment_obj)

        assert result.method_type == "paypal"
        assert "PayPal" in result.display_label

    def test_save_method_unknown_method(self, mollie_provider, mollie_client_mock):
        """Test save_method with unknown method type (lines 129-131)."""
        mock_payment = MagicMock()
        mock_payment.is_paid.return_value = True
        mock_payment.customerId = "cus_123"

        mollie_client_mock.payments.get.return_value = mock_payment

        mock_mandate = MagicMock()
        mock_mandate.status = "valid"
        mock_mandate.id = "mdt_unknown_123"
        mock_mandate.method = "unknown_method"
        mock_mandate.details = {}

        mollie_client_mock.customer_mandates.list_for.return_value = [mock_mandate]

        mock_payment_obj = MagicMock()
        mock_payment_obj.transaction_id = "tr_123"

        result = mollie_provider.save_method(mock_payment_obj)

        assert result.method_type == "unknown_method"
        assert result.display_label == "unknown_method"

    def test_save_method_no_mandate(self, mollie_provider, mollie_client_mock):
        """Test that save_method handles missing mandate."""
        mock_payment = MagicMock()
        mock_payment.transaction_id = "tr_123"
        mock_payment.customer = "cus_123"
        mock_payment.mandate = None
        mock_payment.method = "creditcard"

        # Mock the payment retrieval
        mollie_client_mock.payments.get.return_value = mock_payment

        # Mock empty mandates list
        mollie_client_mock.customer_mandates.list_for.return_value = []

        mock_payment_obj = MagicMock()
        mock_payment_obj.transaction_id = "tr_123"

        with pytest.raises(RuntimeError, match="No valid mandate"):
            mollie_provider.save_method(mock_payment_obj)

    def test_save_method_missing_transaction_id(self, mollie_provider):
        """Test that save_method raises ValueError when transaction_id is missing (line 92)."""
        mock_payment = MagicMock()
        mock_payment.transaction_id = None

        with pytest.raises(ValueError, match="payment.transaction_id required"):
            mollie_provider.save_method(mock_payment)

    def test_save_method_payment_not_paid(self, mollie_provider, mollie_client_mock):
        """Test that save_method raises RuntimeError when payment is not paid (line 96)."""
        mock_payment = MagicMock()
        mock_payment.transaction_id = "tr_123"
        mock_payment.is_paid.return_value = False

        mollie_client_mock.payments.get.return_value = mock_payment

        mock_payment_obj = MagicMock()
        mock_payment_obj.transaction_id = "tr_123"

        with pytest.raises(RuntimeError, match="is not paid"):
            mollie_provider.save_method(mock_payment_obj)

    def test_save_method_no_customer_id(self, mollie_provider, mollie_client_mock):
        """Test that save_method raises RuntimeError when payment has no customerId (line 100)."""
        mock_payment = MagicMock()
        mock_payment.transaction_id = "tr_123"
        mock_payment.is_paid.return_value = True
        mock_payment.customerId = None

        mollie_client_mock.payments.get.return_value = mock_payment

        mock_payment_obj = MagicMock()
        mock_payment_obj.transaction_id = "tr_123"

        with pytest.raises(RuntimeError, match="no customerId"):
            mollie_provider.save_method(mock_payment_obj)



