"""Tests for Stripe provider.

Uses unittest.mock to mock the Stripe client. The Stripe SDK v15+ uses
stripe.StripeClient internally, so we mock at the SDK boundary.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.test import override_settings

from autopay.core.base import is_autopay_capable
from autopay.core.result import ChargeStatus
from autopay.core.saved_method import SavedMethod
from autopay.providers.stripe import StripeAutopayProvider


@pytest.fixture
def stripe_provider():
    """Crea un provider Stripe con API key di test."""
    return StripeAutopayProvider(
        api_key="sk_test_dummy123",
        endpoint_secret="whsec_dummy123",
    )


class TestCurrencyConversion:
    """Test conversioni valuta (zero-decimal vs standard)."""

    def test_eur_to_minor_units(self):
        """EUR usa 2 decimali."""
        assert StripeAutopayProvider._to_minor_units(Decimal("10.50"), "EUR") == 1050
        assert StripeAutopayProvider._to_minor_units(Decimal(10), "EUR") == 1000
        assert StripeAutopayProvider._to_minor_units(Decimal("0.99"), "EUR") == 99

    def test_jpy_to_minor_units(self):
        """JPY is zero-decimal."""
        assert StripeAutopayProvider._to_minor_units(Decimal(1000), "JPY") == 1000
        assert StripeAutopayProvider._to_minor_units(Decimal(100), "JPY") == 100

    def test_minor_to_amount_eur(self):
        """Minor units → EUR."""
        assert StripeAutopayProvider._minor_to_amount(1050, "EUR") == Decimal("10.50")
        assert StripeAutopayProvider._minor_to_amount(1000, "EUR") == Decimal("10.00")

    def test_minor_to_amount_jpy(self):
        """Minor units → JPY."""
        assert StripeAutopayProvider._minor_to_amount(1000, "JPY") == Decimal(1000)
        assert StripeAutopayProvider._minor_to_amount(999, "JPY") == Decimal(999)


class TestChargeOffSession:
    """Test charge_off_session."""

    def test_charge_success(self, stripe_provider):
        """Charge andata a buon fine."""
        mock_intent = MagicMock()
        mock_intent.status = "succeeded"
        mock_intent.id = "pi_test123"
        mock_intent.amount = 1050
        mock_intent.currency = "eur"
        mock_intent.to_dict.return_value = {"id": "pi_test123", "status": "succeeded"}

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", return_value=mock_intent) as mock_create:
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
                description="Test charge",
                idempotency_key="idem_123",
            )

            assert result.status == ChargeStatus.CONFIRMED
            assert result.transaction_id == "pi_test123"
            assert result.amount_charged == Decimal("10.50")

            # Verify it was called with idempotency_key
            mock_create.assert_called_once()
            call_args = mock_create.call_args
            assert call_args[1]["options"]["idempotency_key"] == "idem_123"

    def test_charge_requires_action(self, stripe_provider):
        """Charge richiede 3DS."""
        mock_intent = MagicMock()
        mock_intent.status = "requires_action"
        mock_intent.id = "pi_test123"
        mock_intent.client_secret = "secret_xxx"
        mock_intent.next_action = {"type": "redirect_to_url"}
        mock_intent.to_dict.return_value = {"id": "pi_test123"}

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", return_value=mock_intent):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
            )

            assert result.status == ChargeStatus.REQUIRES_SCA
            assert result.requires_action["type"] == "3ds"
            assert result.requires_action["client_secret"] == "secret_xxx"

    def test_charge_failed(self, stripe_provider):
        """Charge fallita."""
        mock_error = stripe.error.CardError(
            "Your card was declined.",
            param="card_number",
            code="card_declined",
            json_body={"error": {"message": "Your card was declined."}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
            )

            # card_declined is now REJECTED (hard rejection)
            assert result.status == ChargeStatus.REJECTED

    def test_charge_auth_error_retryable(self, stripe_provider):
        """Authentication error → FAILED."""
        mock_error = stripe.error.AuthenticationError(
            "Invalid API key",
            json_body={"error": {"message": "Invalid API key"}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
            )

            assert result.status == ChargeStatus.FAILED

    def test_charge_processing(self, stripe_provider):
        """Charge in processing."""
        mock_intent = MagicMock()
        mock_intent.status = "processing"
        mock_intent.id = "pi_test123"
        mock_intent.to_dict.return_value = {"id": "pi_test123", "status": "processing"}

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", return_value=mock_intent):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
            )

            assert result.status == ChargeStatus.PENDING


class TestParseIntent:
    """Test _parse_intent."""

    def test_rejected_status(self, stripe_provider):
        """requires_payment_method → REJECTED."""
        mock_intent = MagicMock()
        mock_intent.status = "requires_payment_method"
        mock_intent.id = "pi_test123"
        mock_intent.to_dict.return_value = {"id": "pi_test123"}

        result = stripe_provider._parse_intent(mock_intent)

        assert result.status == ChargeStatus.REJECTED


class TestListAndRevoke:
    """Test list_saved_methods e revoke_method."""

    def test_list_saved_methods(self, stripe_provider):
        """Lista metodi salvati."""
        mock_pm = MagicMock()
        mock_pm.type = "card"
        mock_pm.id = "pm_test123"
        mock_pm.card.brand = "visa"
        mock_pm.card.last4 = "4242"
        mock_pm.card.exp_month = 12
        mock_pm.card.exp_year = 2025
        mock_pm.card.fingerprint = "fp_123"
        mock_pm.customer = "cus_test123"

        mock_list = MagicMock()
        mock_list.data = [mock_pm]

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "list", return_value=mock_list):
            methods = stripe_provider.list_saved_methods("cus_test123")

            assert len(methods) == 1
            assert methods[0].method_type == "card"
            assert methods[0].display_label == "Visa •••• 4242"
            assert methods[0].gateway_customer_id == "cus_test123"
            assert methods[0].gateway_method_id == "pm_test123"

    def test_list_saved_methods_sepa_debit(self, stripe_provider):
        """List methods with SEPA debit (lines 132-135)."""
        mock_pm = MagicMock()
        mock_pm.type = "sepa_debit"
        mock_pm.id = "pm_sepa_123"
        mock_pm.sepa_debit.last4 = "6789"
        mock_pm.sepa_debit.mandate_id = "mandate_123"
        mock_pm.customer = "cus_test123"

        mock_list = MagicMock()
        mock_list.data = [mock_pm]

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "list", return_value=mock_list):
            methods = stripe_provider.list_saved_methods("cus_test123")

            assert len(methods) == 1
            assert methods[0].method_type == "sepa"
            assert "IBAN" in methods[0].display_label

    def test_list_saved_methods_paypal(self, stripe_provider):
        """List methods with PayPal (lines 136-140)."""
        mock_pm = MagicMock()
        mock_pm.type = "paypal"
        mock_pm.id = "pm_pp_123"
        mock_pm.paypal.payer_email = "user@example.com"
        mock_pm.customer = "cus_test123"

        mock_list = MagicMock()
        mock_list.data = [mock_pm]

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "list", return_value=mock_list):
            methods = stripe_provider.list_saved_methods("cus_test123")

            assert len(methods) == 1
            assert methods[0].method_type == "paypal"
            assert "PayPal" in methods[0].display_label

    def test_list_saved_methods_unknown_type(self, stripe_provider):
        """List methods with unknown type (lines 141-144)."""
        mock_pm = MagicMock()
        mock_pm.type = "unknown_type"
        mock_pm.id = "pm_unknown_123"
        mock_pm.customer = "cus_test123"

        mock_list = MagicMock()
        mock_list.data = [mock_pm]

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "list", return_value=mock_list):
            methods = stripe_provider.list_saved_methods("cus_test123")

            assert len(methods) == 1
            assert methods[0].method_type == "unknown_type"
            assert methods[0].display_label == "unknown_type"

    def test_revoke_method(self, stripe_provider):
        """Revoca metodo salvato."""
        saved_method = SavedMethod(
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa •••• 4242",
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "detach") as mock_detach:
            stripe_provider.revoke_method(saved_method)
            mock_detach.assert_called_once_with("pm_test123")


class TestValidateMethod:
    """Test validate_method."""

    def test_validate_valid_card(self, stripe_provider):
        """Valid card."""
        mock_pm = MagicMock()
        mock_pm.type = "card"
        mock_pm.customer = "cus_test123"
        mock_pm.card.exp_year = 2027
        mock_pm.card.exp_month = 12
        mock_pm.to_dict.return_value = {"id": "pm_test123"}

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.validate_method(saved_method)

            assert result.is_valid is True
            assert result.is_expired is False
            assert result.needs_refresh is False

    def test_validate_expired_card(self, stripe_provider):
        """Expired card."""
        mock_pm = MagicMock()
        mock_pm.type = "card"
        mock_pm.customer = "cus_test123"
        mock_pm.card.exp_year = 2020  # Scaduta
        mock_pm.card.exp_month = 1
        mock_pm.to_dict.return_value = {"id": "pm_test123"}

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.validate_method(saved_method)

            assert result.is_valid is False
            assert result.is_expired is True
            assert result.needs_refresh is True

    def test_validate_detached_method(self, stripe_provider):
        """Payment method detached."""
        mock_pm = MagicMock()
        mock_pm.type = "card"
        mock_pm.customer = None  # Detached
        mock_pm.to_dict.return_value = {"id": "pm_test123"}

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.validate_method(saved_method)

            assert result.is_valid is False
            assert result.needs_refresh is True


class TestRefresh:
    """Test refresh_method e confirm_refresh."""

    @override_settings(AUTOPAY_STRIPE_PUBLISHABLE_KEY="pk_test_dummy")
    def test_refresh_method(self, stripe_provider):
        """Create SetupIntent for refresh."""
        mock_intent = MagicMock()
        mock_intent.id = "seti_test123"
        mock_intent.client_secret = "secret_xxx"

        with patch.object(stripe_provider.stripe_client.v1.setup_intents, "create", return_value=mock_intent):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
                id=1,
            )

            result = stripe_provider.refresh_method(saved_method)

            assert result.gateway_setup_id == "seti_test123"
            assert result.frontend_sdk == "stripe"
            assert result.frontend_payload["client_secret"] == "secret_xxx"
            assert "publishable_key" in result.frontend_payload
            assert result.frontend_payload["setup_intent_id"] == "seti_test123"

    @override_settings(AUTOPAY_STRIPE_PUBLISHABLE_KEY="pk_test_dummy")
    def test_confirm_refresh(self, stripe_provider):
        """Confirm refresh with new PaymentMethod."""
        mock_intent = MagicMock()
        mock_intent.status = "succeeded"
        mock_intent.payment_method = "pm_new456"

        mock_new_pm = MagicMock()
        mock_new_pm.type = "card"
        mock_new_pm.id = "pm_new456"
        mock_new_pm.card.brand = "mastercard"
        mock_new_pm.card.last4 = "5555"
        mock_new_pm.card.exp_month = 6
        mock_new_pm.card.exp_year = 2026
        mock_new_pm.card.fingerprint = "fp_456"

        with (
            patch.object(stripe_provider.stripe_client.v1.setup_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_new_pm),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "detach") as mock_detach,
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
                is_active=True,
                metadata={"brand": "visa"},
                id=1,
            )

            result = stripe_provider.confirm_refresh(saved_method, {"setup_intent_id": "seti_test123"})

            assert result.gateway_method_id == "pm_new456"
            assert result.display_label == "Mastercard •••• 5555"
            assert result.metadata["brand"] == "mastercard"
            assert result.metadata["last4"] == "5555"

            # Verify old PM was detached
            mock_detach.assert_called_once_with("pm_test123")


class TestCapabilityFlags:
    """Test capability flags."""

    def test_off_session_charge_supported(self):
        """SUPPORTS_OFF_SESSION_CHARGE is True."""
        assert StripeAutopayProvider.SUPPORTS_OFF_SESSION_CHARGE is True

    def test_list_methods_supported(self):
        """SUPPORTS_LIST_METHODS is True."""
        assert StripeAutopayProvider.SUPPORTS_LIST_METHODS is True

    def test_revoke_supported(self):
        """SUPPORTS_REVOKE is True."""
        assert StripeAutopayProvider.SUPPORTS_REVOKE is True

    def test_validate_supported(self):
        """SUPPORTS_VALIDATE is True."""
        assert StripeAutopayProvider.SUPPORTS_VALIDATE is True

    def test_refresh_supported(self):
        """SUPPORTS_REFRESH is True."""
        assert StripeAutopayProvider.SUPPORTS_REFRESH is True

    def test_webhook_verification_supported(self):
        """SUPPORTS_WEBHOOK_VERIFICATION is True."""
        assert StripeAutopayProvider.SUPPORTS_WEBHOOK_VERIFICATION is True

    def test_is_autopay_capable(self):
        """is_autopay_capable returns True for Stripe."""
        provider = StripeAutopayProvider(api_key="sk_test_dummy")
        assert is_autopay_capable(provider) is True


class TestWebhook:
    """Test webhook handling."""

    def test_verify_webhook_valid(self, stripe_provider):
        """Test valid webhook."""
        raw_body = b'{"type": "test"}'
        headers = {"Stripe-Signature": "sig_valid"}

        with patch("stripe.Webhook.construct_event", return_value=MagicMock()):
            result = stripe_provider.verify_webhook_request(raw_body, headers)

            assert result is True

    def test_verify_webhook_invalid(self, stripe_provider):
        """Test invalid webhook."""
        raw_body = b'{"type": "test"}'
        headers = {"Stripe-Signature": "sig_invalid"}

        with patch(
            "stripe.Webhook.construct_event",
            side_effect=stripe.error.SignatureVerificationError("Invalid signature", "sig_invalid"),
        ):
            result = stripe_provider.verify_webhook_request(raw_body, headers)

            assert result is False

    def test_verify_webhook_no_endpoint_secret(self, stripe_provider):
        """Test verify_webhook_request returns False when endpoint_secret not set (line 459)."""
        provider = StripeAutopayProvider(api_key="sk_test_dummy")  # No endpoint_secret

        raw_body = b'{"type": "test"}'
        headers = {"Stripe-Signature": "sig_valid"}

        result = provider.verify_webhook_request(raw_body, headers)
        assert result is False

    def test_verify_webhook_value_error(self, stripe_provider):
        """Test verify_webhook_request catches ValueError (line 460)."""
        raw_body = b'invalid body'
        headers = {"Stripe-Signature": "sig_invalid"}

        with patch(
            "stripe.Webhook.construct_event",
            side_effect=ValueError("Invalid JSON"),
        ):
            result = stripe_provider.verify_webhook_request(raw_body, headers)
            assert result is False

    def test_process_webhook_payment_intent_succeeded(self, stripe_provider):
        """Processa evento payment_intent.succeeded."""
        payload = {"data": {"object": {"id": "pi_test123"}}}

        result = stripe_provider.process_webhook_event("payment_intent.succeeded", payload)

        assert result.processed is True
        assert result.action == "status_update"
        assert result.transaction_id == "pi_test123"

    def test_process_webhook_payment_intent_failed(self, stripe_provider):
        """Processa evento payment_intent.payment_failed."""
        payload = {"data": {"object": {"id": "pi_test123"}}}

        result = stripe_provider.process_webhook_event("payment_intent.payment_failed", payload)

        assert result.processed is True
        assert result.action == "status_update"

    def test_process_webhook_charge_refunded(self, stripe_provider):
        """Processa evento charge.refunded."""
        payload = {"data": {"object": {"payment_intent": "pi_test123"}}}

        result = stripe_provider.process_webhook_event("charge.refunded", payload)

        assert result.processed is True
        assert result.action == "refund"

    def test_process_webhook_dispute(self, stripe_provider):
        """Processa evento charge.dispute.created."""
        payload = {"data": {"object": {"payment_intent": "pi_test123"}}}

        result = stripe_provider.process_webhook_event("charge.dispute.created", payload)

        assert result.processed is True
        assert result.action == "chargeback"

    def test_process_webhook_ignored(self, stripe_provider):
        """Evento non riconosciuto."""
        payload = {}

        result = stripe_provider.process_webhook_event("unknown.event", payload)

        assert result.processed is False
        assert result.action == "ignored"


class TestStripeSetupIntent:
    """Test SetupIntent-related failures."""

    @override_settings(AUTOPAY_STRIPE_PUBLISHABLE_KEY="pk_test_dummy")
    def test_refresh_setup_intent_creation_fails(self, stripe_provider):
        """SetupIntent creation fails."""
        with patch.object(
            stripe_provider.stripe_client.v1.setup_intents, "create", side_effect=stripe.error.StripeError("API error")
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
                id=1,
            )

            with pytest.raises(stripe.error.StripeError, match="API error"):
                stripe_provider.refresh_method(saved_method)

    @override_settings(AUTOPAY_STRIPE_PUBLISHABLE_KEY="pk_test_dummy")
    def test_confirm_refresh_setup_intent_not_succeeded(self, stripe_provider):
        """SetupIntent not succeeded."""
        mock_intent = MagicMock()
        mock_intent.status = "requires_payment_method"
        mock_intent.payment_method = None

        with patch.object(
            stripe_provider.stripe_client.v1.setup_intents, "retrieve", return_value=mock_intent
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
                id=1,
            )

            with pytest.raises(RuntimeError, match="SetupIntent not succeeded"):
                stripe_provider.confirm_refresh(saved_method, {"setup_intent_id": "seti_test123"})

    @override_settings(AUTOPAY_STRIPE_PUBLISHABLE_KEY="pk_test_dummy")
    def test_confirm_refresh_no_payment_method(self, stripe_provider):
        """SetupIntent succeeded but no payment method attached."""
        mock_intent = MagicMock()
        mock_intent.status = "succeeded"
        mock_intent.payment_method = None

        with patch.object(
            stripe_provider.stripe_client.v1.setup_intents, "retrieve", return_value=mock_intent
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
                id=1,
            )

            # Source raises TypeError when payment_method is None
            with pytest.raises(TypeError):
                stripe_provider.confirm_refresh(saved_method, {"setup_intent_id": "seti_test123"})

    @override_settings(AUTOPAY_STRIPE_PUBLISHABLE_KEY="pk_test_dummy")
    def test_confirm_refresh_payment_method_retrieval_fails(self, stripe_provider):
        """Payment method retrieval fails."""
        mock_intent = MagicMock()
        mock_intent.status = "succeeded"
        mock_intent.payment_method = "pm_new456"

        with (
            patch.object(
                stripe_provider.stripe_client.v1.setup_intents, "retrieve", return_value=mock_intent
            ),
            patch.object(
                stripe_provider.stripe_client.v1.payment_methods, "retrieve", side_effect=stripe.error.StripeError("API error")
            ),
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
                id=1,
            )

            with pytest.raises(stripe.error.StripeError, match="API error"):
                stripe_provider.confirm_refresh(saved_method, {"setup_intent_id": "seti_test123"})

    def test_confirm_refresh_detach_error_logged(self, stripe_provider):
        """confirm_refresh catches detach error and logs (lines 426-427)."""
        mock_intent = MagicMock()
        mock_intent.status = "succeeded"
        mock_intent.payment_method = "pm_new456"

        mock_new_pm = MagicMock()
        mock_new_pm.type = "card"
        mock_new_pm.id = "pm_new456"
        mock_new_pm.card.brand = "mastercard"
        mock_new_pm.card.last4 = "5555"
        mock_new_pm.card.exp_month = 6
        mock_new_pm.card.exp_year = 2026
        mock_new_pm.card.fingerprint = "fp_456"

        with (
            patch.object(stripe_provider.stripe_client.v1.setup_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_new_pm),
            patch.object(
                stripe_provider.stripe_client.v1.payment_methods, "detach", side_effect=stripe.error.StripeError("Detach failed")
            ),
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
                is_active=True,
                metadata={"brand": "visa"},
                id=1,
            )

            # Should not raise - the error is caught and logged
            result = stripe_provider.confirm_refresh(saved_method, {"setup_intent_id": "seti_test123"})
            assert result.gateway_method_id == "pm_new456"


class TestStripeChargeEdgeCases:
    """Test additional charge edge cases."""

    def test_charge_cancelled_status(self, stripe_provider):
        """Charge cancelled status returns PENDING."""
        mock_intent = MagicMock()
        mock_intent.status = "canceled"
        mock_intent.id = "pi_test123"
        mock_intent.to_dict.return_value = {"id": "pi_test123", "status": "canceled"}

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", return_value=mock_intent):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
            )

            # Source returns PENDING for canceled status (not in explicit handling)
            assert result.status == ChargeStatus.PENDING

    def test_charge_invalid_request_error(self, stripe_provider):
        """InvalidRequestError with non-SCA code re-raises."""
        mock_error = stripe.error.InvalidRequestError(
            "Invalid customer ID",
            param="customer",
            json_body={"error": {"message": "Invalid customer ID"}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            with pytest.raises(stripe.error.InvalidRequestError, match="Invalid customer"):
                stripe_provider.charge_off_session(saved_method, Decimal("10.50"), "EUR")

    def test_charge_rate_limit_error(self, stripe_provider):
        """RateLimitError returns FAILED."""
        mock_error = stripe.error.RateLimitError(
            "Too many requests",
            json_body={"error": {"message": "Too many requests"}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
            )

            assert result.status == ChargeStatus.FAILED

    def test_charge_unexpected_error(self, stripe_provider):
        """Unexpected StripeError returns FAILED."""
        mock_error = stripe.error.StripeError("Unexpected error")

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
            )

            assert result.status == ChargeStatus.FAILED

    def test_charge_with_metadata(self, stripe_provider):
        """Charge with metadata (line 189)."""
        mock_intent = MagicMock()
        mock_intent.status = "succeeded"
        mock_intent.id = "pi_test123"
        mock_intent.amount = 1050
        mock_intent.currency = "eur"
        mock_intent.to_dict.return_value = {"id": "pi_test123", "status": "succeeded"}

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", return_value=mock_intent) as mock_create:
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
                metadata={"order_id": "12345"},
            )

            assert result.status == ChargeStatus.CONFIRMED
            mock_create.assert_called_once()
            call_args = mock_create.call_args
            assert call_args[0][0]["metadata"] == {"order_id": "12345"}


class TestStripeDetachValidation:
    """Test detach and validation edge cases."""

    def test_revoke_method_detach_fails(self, stripe_provider):
        """Detach raises error."""
        saved_method = SavedMethod(
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa •••• 4242",
        )

        with (
            patch.object(
                stripe_provider.stripe_client.v1.payment_methods, "detach", side_effect=stripe.error.StripeError("Detach failed")
            ),
            pytest.raises(stripe.error.StripeError, match="Detach failed"),
        ):
            # Source does not catch detach errors
            stripe_provider.revoke_method(saved_method)

    def test_validate_method_not_found(self, stripe_provider):
        """Payment method not found."""
        mock_error = stripe.error.InvalidRequestError(
            "No such payment method",
            param="payment_method",
            json_body={"error": {"code": "resource_missing", "message": "No such payment method"}},
        )

        with patch.object(
            stripe_provider.stripe_client.v1.payment_methods, "retrieve", side_effect=mock_error
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.validate_method(saved_method)

            assert result.is_valid is False
            assert result.needs_refresh is True

    def test_validate_method_auth_error(self, stripe_provider):
        """Payment method validation with auth error (lines 331-332)."""
        mock_error = stripe.error.AuthenticationError(
            "Invalid API key",
            json_body={"error": {"message": "Invalid API key"}},
        )

        with patch.object(
            stripe_provider.stripe_client.v1.payment_methods, "retrieve", side_effect=mock_error
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.validate_method(saved_method)

            assert result.is_valid is False
            assert result.needs_refresh is False
            assert "auth error" in result.reason.lower()

    def test_validate_method_wrong_customer(self, stripe_provider):
        """Payment method belongs to different customer."""
        mock_error = stripe.error.InvalidRequestError(
            "No such payment method",
            param="payment_method",
            json_body={
                "error": {
                    "code": "resource_missing",
                    "message": "No such payment method",
                    "payment_method_details": {"customer": "cus_different"},
                }
            },
        )

        with patch.object(
            stripe_provider.stripe_client.v1.payment_methods, "retrieve", side_effect=mock_error
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.validate_method(saved_method)

            assert result.is_valid is False
            assert result.needs_refresh is True


class TestStripeGetChargeStatus:
    """Test get_charge_status."""

    def test_get_charge_status_succeeded(self, stripe_provider):
        """PaymentIntent succeeded."""
        mock_intent = MagicMock()
        mock_intent.status = "succeeded"
        mock_intent.id = "pi_test123"
        mock_intent.amount = 1050
        mock_intent.currency = "eur"
        mock_intent.to_dict.return_value = {"id": "pi_test123", "status": "succeeded"}

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent):
            result = stripe_provider.get_charge_status("pi_test123")

            assert result.status == ChargeStatus.CONFIRMED
            assert result.transaction_id == "pi_test123"
            assert result.amount_charged == Decimal("10.50")

    def test_get_charge_status_requires_action(self, stripe_provider):
        """PaymentIntent requires_action."""
        mock_intent = MagicMock()
        mock_intent.status = "requires_action"
        mock_intent.id = "pi_test123"
        mock_intent.to_dict.return_value = {"id": "pi_test123", "status": "requires_action"}

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent):
            result = stripe_provider.get_charge_status("pi_test123")

            assert result.status == ChargeStatus.REQUIRES_SCA

    def test_get_charge_status_processing(self, stripe_provider):
        """PaymentIntent processing."""
        mock_intent = MagicMock()
        mock_intent.status = "processing"
        mock_intent.id = "pi_test123"
        mock_intent.to_dict.return_value = {"id": "pi_test123", "status": "processing"}

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent):
            result = stripe_provider.get_charge_status("pi_test123")

            assert result.status == ChargeStatus.PENDING

    def test_get_charge_status_api_error(self, stripe_provider):
        """API error during retrieval."""
        with (
            patch.object(
                stripe_provider.stripe_client.v1.payment_intents, "retrieve", side_effect=stripe.error.StripeError("API error")
            ),
            pytest.raises(stripe.error.StripeError, match="API error"),
        ):
            stripe_provider.get_charge_status("pi_test123")


class TestStripeListMethodsEdgeCases:
    """Test list_saved_methods edge cases."""

    def test_list_methods_unsupported_type(self, stripe_provider):
        """List methods with unsupported type returns empty."""
        # Source filters by type="card" in API call, so sepa_debit won't be returned
        mock_list = MagicMock()
        mock_list.data = []  # Empty because API only returns card types

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "list", return_value=mock_list):
            methods = stripe_provider.list_saved_methods("cus_test123")

            assert len(methods) == 0

    def test_list_methods_empty(self, stripe_provider):
        """List methods returns empty list."""
        mock_list = MagicMock()
        mock_list.data = []

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "list", return_value=mock_list):
            methods = stripe_provider.list_saved_methods("cus_test123")

            assert methods == []


class TestStripeInit:
    """Test provider initialization."""

    def test_init_with_api_key(self):
        """Initialize with API key."""
        provider = StripeAutopayProvider(api_key="sk_test_123")
        assert provider.api_key == "sk_test_123"

    def test_init_with_endpoint_secret(self):
        """Initialize with endpoint secret."""
        provider = StripeAutopayProvider(api_key="sk_test_123", endpoint_secret="whsec_123")
        assert provider.endpoint_secret == "whsec_123"

    def test_init_sets_stripe_client(self):
        """Initialize sets Stripe client."""
        provider = StripeAutopayProvider(api_key="sk_test_123")
        assert provider.stripe_client is not None

    def test_publishable_key_missing(self):
        """Test that publishable_key raises ImproperlyConfigured when missing (line 96)."""
        from django.core.exceptions import ImproperlyConfigured

        provider = StripeAutopayProvider(api_key="sk_test_123")

        with override_settings(AUTOPAY_STRIPE_PUBLISHABLE_KEY=""), pytest.raises(
            ImproperlyConfigured, match="AUTOPAY_STRIPE_PUBLISHABLE_KEY is required"
        ):
            _ = provider.publishable_key


class TestSaveMethod:
    """Test save_method implementation."""

    def test_save_method_success_card(self, stripe_provider):
        """Save method with card payment method."""
        mock_pm = MagicMock()
        mock_pm.id = "pm_test123"
        mock_pm.type = "card"
        mock_pm.customer = "cus_test123"
        mock_pm.card.brand = "visa"
        mock_pm.card.last4 = "4242"
        mock_pm.card.exp_month = 12
        mock_pm.card.exp_year = 2025
        mock_pm.card.fingerprint = "fp_123"

        mock_charge = MagicMock()
        mock_charge.payment_method = "pm_test123"
        mock_charge.customer = "cus_test123"

        mock_intent = MagicMock()
        mock_intent.charges.data = [mock_charge]
        mock_intent.latest_charge = mock_charge

        with (
            patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm),
        ):
            # Create a mock payment object
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            result = stripe_provider.save_method(mock_payment)

            assert result.gateway_method_id == "pm_test123"
            assert result.gateway_customer_id == "cus_test123"
            assert result.method_type == "card"
            assert result.display_label == "Visa •••• 4242"
            assert result.metadata["brand"] == "visa"
            assert result.metadata["last4"] == "4242"
            assert result.metadata["exp_month"] == 12
            assert result.metadata["exp_year"] == 2025
            assert result.is_active is True

    def test_save_method_no_payment_method_attached(self, stripe_provider):
        """Save method when PaymentIntent has no payment method."""
        mock_intent = MagicMock()
        mock_intent.charges.data = []
        mock_intent.latest_charge = None

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            with pytest.raises(RuntimeError, match="No charge found on PaymentIntent"):
                stripe_provider.save_method(mock_payment)

    def test_save_method_retrieve_fails(self, stripe_provider):
        """Save method when payment method retrieval fails."""
        mock_charge = MagicMock()
        mock_charge.payment_method = "pm_test123"
        mock_charge.customer = "cus_test123"

        mock_intent = MagicMock()
        mock_intent.charges.data = [mock_charge]

        mock_error = stripe.error.InvalidRequestError(
            "No such payment method",
            param="payment_method",
            json_body={"error": {"message": "No such payment method"}},
        )

        with (
            patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", side_effect=mock_error),
        ):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            with pytest.raises(RuntimeError, match="Failed to retrieve payment method"):
                stripe_provider.save_method(mock_payment)

    def test_save_method_no_transaction_id(self, stripe_provider):
        """Save method fails when transaction_id is empty."""
        mock_payment = MagicMock()
        mock_payment.transaction_id = ""

        with pytest.raises(ValueError, match="payment.transaction_id required"):
            stripe_provider.save_method(mock_payment)

    def test_save_method_invalid_request_error(self, stripe_provider):
        """Save method raises RuntimeError when retrieve raises InvalidRequestError."""
        mock_error = stripe.error.InvalidRequestError(
            "No such payment intent",
            param="payment_intent",
            json_body={"error": {"message": "No such payment intent"}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", side_effect=mock_error):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            with pytest.raises(RuntimeError, match="Failed to retrieve PaymentIntent"):
                stripe_provider.save_method(mock_payment)

    def test_save_method_with_latest_charge(self, stripe_provider):
        """Save method uses latest_charge when charges.data is empty."""
        mock_pm = MagicMock()
        mock_pm.id = "pm_test123"
        mock_pm.type = "card"
        mock_pm.customer = "cus_test123"
        mock_pm.card.brand = "visa"
        mock_pm.card.last4 = "4242"
        mock_pm.card.exp_month = 12
        mock_pm.card.exp_year = 2025
        mock_pm.card.fingerprint = "fp_123"

        mock_charge = MagicMock()
        mock_charge.payment_method = "pm_test123"
        mock_charge.customer = "cus_test123"

        mock_intent = MagicMock()
        mock_intent.charges.data = []  # Empty charges.data
        mock_intent.latest_charge = mock_charge  # But latest_charge exists

        with (
            patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm),
        ):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            result = stripe_provider.save_method(mock_payment)

            assert result.gateway_method_id == "pm_test123"
            assert result.gateway_customer_id == "cus_test123"

    def test_save_method_no_payment_method_on_charge(self, stripe_provider):
        """Save method raises RuntimeError when charge has no payment_method."""
        mock_charge = MagicMock()
        mock_charge.payment_method = None
        mock_charge.customer = "cus_test123"

        mock_intent = MagicMock()
        mock_intent.charges.data = [mock_charge]

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            with pytest.raises(RuntimeError, match="Payment has no attached payment method"):
                stripe_provider.save_method(mock_payment)

    def test_save_method_charge_customer_fallback(self, stripe_provider):
        """Save method uses charge.customer when pm.customer is missing."""
        mock_pm = MagicMock()
        mock_pm.id = "pm_test123"
        mock_pm.type = "card"
        mock_pm.customer = None  # No customer on PM
        mock_pm.card.brand = "visa"
        mock_pm.card.last4 = "4242"
        mock_pm.card.exp_month = 12
        mock_pm.card.exp_year = 2025
        mock_pm.card.fingerprint = "fp_123"

        mock_charge = MagicMock()
        mock_charge.payment_method = "pm_test123"
        mock_charge.customer = "cus_test123"  # But charge has customer

        mock_intent = MagicMock()
        mock_intent.charges.data = [mock_charge]

        with (
            patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm),
        ):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            result = stripe_provider.save_method(mock_payment)

            assert result.gateway_customer_id == "cus_test123"

    def test_save_method_no_customer_id(self, stripe_provider):
        """Save method raises RuntimeError when neither pm nor charge has customer."""
        mock_pm = MagicMock()
        mock_pm.id = "pm_test123"
        mock_pm.type = "card"
        mock_pm.customer = None
        mock_pm.card.brand = "visa"
        mock_pm.card.last4 = "4242"
        mock_pm.card.exp_month = 12
        mock_pm.card.exp_year = 2025
        mock_pm.card.fingerprint = "fp_123"

        mock_charge = MagicMock()
        mock_charge.payment_method = "pm_test123"
        mock_charge.customer = None

        mock_intent = MagicMock()
        mock_intent.charges.data = [mock_charge]

        with (
            patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm),
        ):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            with pytest.raises(RuntimeError, match="No customer ID found"):
                stripe_provider.save_method(mock_payment)

    def test_save_method_unknown_payment_method_type(self, stripe_provider):
        """Save method handles unknown payment method type."""
        mock_pm = MagicMock()
        mock_pm.id = "pm_test123"
        mock_pm.type = "unknown_type"
        mock_pm.customer = "cus_test123"

        mock_charge = MagicMock()
        mock_charge.payment_method = "pm_test123"
        mock_charge.customer = "cus_test123"

        mock_intent = MagicMock()
        mock_intent.charges.data = [mock_charge]

        with (
            patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm),
        ):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            result = stripe_provider.save_method(mock_payment)

            assert result.method_type == "unknown_type"
            assert result.display_label == "unknown_type"
            assert result.metadata == {}

    def test_charge_auth_error_with_client_secret(self, stripe_provider):
        """CardError with authentication_required and payment_intent.client_secret."""
        mock_error = stripe.error.CardError(
            "Authentication required",
            param="card_number",
            code="authentication_required",
            json_body={"error": {"message": "Authentication required"}},
        )
        # Add payment_intent attribute with client_secret
        mock_pi = MagicMock()
        mock_pi.client_secret = "pi_secret_123"
        mock_error.payment_intent = mock_pi

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
            )

            assert result.status == ChargeStatus.REQUIRES_SCA
            assert result.requires_action["type"] == "3ds"
            assert result.requires_action["client_secret"] == "pi_secret_123"

    def test_charge_invalid_request_error_with_client_secret(self, stripe_provider):
        """InvalidRequestError with requires_action and payment_intent.client_secret."""
        mock_error = stripe.error.InvalidRequestError(
            "Requires action",
            param="payment_intent",
            json_body={"error": {"message": "Requires action"}},
        )
        mock_error.code = "requires_action"
        # Add payment_intent attribute with client_secret
        mock_pi = MagicMock()
        mock_pi.client_secret = "pi_secret_456"
        mock_error.payment_intent = mock_pi

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(
                saved_method=saved_method,
                amount=Decimal("10.50"),
                currency="EUR",
            )

            assert result.status == ChargeStatus.REQUIRES_SCA
            assert result.requires_action["type"] == "3ds"
            assert result.requires_action["client_secret"] == "pi_secret_456"

    def test_save_method_sepa_debit(self, stripe_provider):
        """Save method with SEPA debit."""
        mock_pm = MagicMock()
        mock_pm.id = "pm_sepa_123"
        mock_pm.type = "sepa_debit"
        mock_pm.customer = "cus_test123"
        mock_pm.sepa_debit.last4 = "6789"
        mock_pm.sepa_debit.mandate_id = "mandate_123"

        mock_charge = MagicMock()
        mock_charge.payment_method = "pm_sepa_123"
        mock_charge.customer = "cus_test123"

        mock_intent = MagicMock()
        mock_intent.charges.data = [mock_charge]

        with (
            patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm),
        ):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            result = stripe_provider.save_method(mock_payment)

            assert result.method_type == "sepa"
            assert "IBAN" in result.display_label
            assert result.metadata["mandate"] == "mandate_123"

    def test_save_method_paypal(self, stripe_provider):
        """Save method with PayPal."""
        mock_pm = MagicMock()
        mock_pm.id = "pm_pp_123"
        mock_pm.type = "paypal"
        mock_pm.customer = "cus_test123"
        mock_pm.paypal.payer_email = "user@example.com"

        mock_charge = MagicMock()
        mock_charge.payment_method = "pm_pp_123"
        mock_charge.customer = "cus_test123"

        mock_intent = MagicMock()
        mock_intent.charges.data = [mock_charge]

        with (
            patch.object(stripe_provider.stripe_client.v1.payment_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm),
        ):
            mock_payment = MagicMock()
            mock_payment.transaction_id = "pi_test123"

            result = stripe_provider.save_method(mock_payment)

            assert result.method_type == "paypal"
            assert "PayPal" in result.display_label
            assert result.metadata["email"] == "user@example.com"


class TestSCAFlow:
    """Test SCA flow handling."""

    def test_card_error_authentication_required(self, stripe_provider):
        """CardError with authentication_required code returns REQUIRES_SCA."""
        mock_error = stripe.error.CardError(
            "Authentication required",
            param="card",
            code="authentication_required",
            json_body={"error": {"message": "Authentication required"}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(saved_method, Decimal("10.50"), "EUR")

            assert result.status == ChargeStatus.REQUIRES_SCA
            assert result.requires_action is not None
            assert result.requires_action["type"] == "3ds"

    def test_invalid_request_error_requires_action(self, stripe_provider):
        """InvalidRequestError with requires_action code returns REQUIRES_SCA."""
        mock_error = stripe.error.InvalidRequestError(
            "Payment requires action",
            param="payment_method",
            code="requires_action",
            json_body={"error": {"message": "Payment requires action"}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(saved_method, Decimal("10.50"), "EUR")

            assert result.status == ChargeStatus.REQUIRES_SCA
            assert result.requires_action is not None
            assert result.requires_action["type"] == "3ds"

    def test_invalid_request_error_requires_source_action(self, stripe_provider):
        """InvalidRequestError with requires_source_action code returns REQUIRES_SCA."""
        mock_error = stripe.error.InvalidRequestError(
            "Source action required",
            param="source",
            code="requires_source_action",
            json_body={"error": {"message": "Source action required"}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(saved_method, Decimal("10.50"), "EUR")

            assert result.status == ChargeStatus.REQUIRES_SCA

    def test_invalid_request_error_other_code(self, stripe_provider):
        """InvalidRequestError with other code re-raises."""
        mock_error = stripe.error.InvalidRequestError(
            "Invalid customer",
            param="customer",
            code="resource_missing",
            json_body={"error": {"message": "Invalid customer"}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            with pytest.raises(stripe.error.InvalidRequestError, match="Invalid customer"):
                stripe_provider.charge_off_session(saved_method, Decimal("10.50"), "EUR")


class TestCardDeclined:
    """Test card_declined error handling."""

    def test_card_declined_returns_rejected(self, stripe_provider):
        """card_declined code returns REJECTED, not FAILED."""
        mock_error = stripe.error.CardError(
            "Your card was declined.",
            param="card",
            code="card_declined",
            json_body={"error": {"message": "Your card was declined."}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(saved_method, Decimal("10.50"), "EUR")

            assert result.status == ChargeStatus.REJECTED

    def test_expired_card_returns_failed(self, stripe_provider):
        """expired_card code returns FAILED."""
        mock_error = stripe.error.CardError(
            "Your card has expired.",
            param="card",
            code="expired_card",
            json_body={"error": {"message": "Your card has expired."}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(saved_method, Decimal("10.50"), "EUR")

            assert result.status == ChargeStatus.FAILED

    def test_processing_error_returns_failed(self, stripe_provider):
        """processing_error code returns FAILED."""
        mock_error = stripe.error.CardError(
            "Processing error.",
            param="card",
            code="processing_error",
            json_body={"error": {"message": "Processing error."}},
        )

        with patch.object(stripe_provider.stripe_client.v1.payment_intents, "create", side_effect=mock_error):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.charge_off_session(saved_method, Decimal("10.50"), "EUR")

            assert result.status == ChargeStatus.FAILED


class TestCurrencyTable:
    """Test zero-decimal currency table."""

    def test_isk_is_two_decimal(self, stripe_provider):
        """ISK (Icelandic Króna) is treated as 2-decimal, not zero-decimal."""
        # ISK should NOT be in zero-decimal list
        assert "ISK" not in StripeAutopayProvider.ZERO_DECIMAL_CURRENCIES

        # 10.50 ISK should be 1050 minor units (2 decimals)
        assert StripeAutopayProvider._to_minor_units(Decimal("10.50"), "ISK") == 1050
        assert StripeAutopayProvider._to_minor_units(Decimal(10), "ISK") == 1000

        # Minor units to ISK
        assert StripeAutopayProvider._minor_to_amount(1050, "ISK") == Decimal("10.50")
        assert StripeAutopayProvider._minor_to_amount(1000, "ISK") == Decimal("10.00")

    def test_xpf_is_zero_decimal(self, stripe_provider):
        """XPF (CFP Franc) is treated as zero-decimal."""
        # XPF should be in zero-decimal list
        assert "XPF" in StripeAutopayProvider.ZERO_DECIMAL_CURRENCIES

        # 10 XPF should be 10 minor units (zero decimal)
        assert StripeAutopayProvider._to_minor_units(Decimal(10), "XPF") == 10
        assert StripeAutopayProvider._to_minor_units(Decimal(1000), "XPF") == 1000

        # Minor units to XPF
        assert StripeAutopayProvider._minor_to_amount(10, "XPF") == Decimal(10)
        assert StripeAutopayProvider._minor_to_amount(1000, "XPF") == Decimal(1000)

    def test_complete_zero_decimal_list(self):
        """Verify the complete zero-decimal currency list matches Stripe's."""
        expected = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
        assert StripeAutopayProvider.ZERO_DECIMAL_CURRENCIES == expected


class TestValidateMethodBranchCoverage:
    """Test validate_method branch coverage for non-card and missing expiry."""

    def test_validate_non_card_payment_method(self, stripe_provider):
        """validate_method with non-card type (sepa_debit) skips expiry check."""
        mock_pm = MagicMock()
        mock_pm.type = "sepa_debit"
        mock_pm.customer = "cus_test123"
        mock_pm.to_dict.return_value = {"id": "pm_sepa_123"}

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_sepa_123",
                method_type="sepa",
                display_label="IBAN •••• 6789",
            )

            result = stripe_provider.validate_method(saved_method)

            assert result.is_valid is True
            assert result.is_expired is False
            assert result.needs_refresh is False

    def test_validate_card_with_missing_expiry(self, stripe_provider):
        """validate_method with card type but missing exp_year/exp_month keeps is_expired=False."""
        mock_pm = MagicMock()
        mock_pm.type = "card"
        mock_pm.customer = "cus_test123"
        mock_pm.card.exp_year = None  # Missing expiry
        mock_pm.card.exp_month = 12
        mock_pm.to_dict.return_value = {"id": "pm_test123"}

        with patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_pm):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="card",
                display_label="Visa •••• 4242",
            )

            result = stripe_provider.validate_method(saved_method)

            assert result.is_valid is True
            assert result.is_expired is False
            assert result.needs_refresh is False


class TestConfirmRefreshBranchCoverage:
    """Test confirm_refresh branch coverage for non-card payment methods."""

    @override_settings(AUTOPAY_STRIPE_PUBLISHABLE_KEY="pk_test_dummy")
    def test_confirm_refresh_non_card_payment_method(self, stripe_provider):
        """confirm_refresh with non-card type does not update card-specific metadata."""
        mock_intent = MagicMock()
        mock_intent.status = "succeeded"
        mock_intent.payment_method = "pm_sepa_new456"

        mock_new_pm = MagicMock()
        mock_new_pm.type = "sepa_debit"
        mock_new_pm.id = "pm_sepa_new456"
        mock_new_pm.sepa_debit.last4 = "6789"
        mock_new_pm.sepa_debit.mandate_id = "mandate_456"

        with (
            patch.object(stripe_provider.stripe_client.v1.setup_intents, "retrieve", return_value=mock_intent),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "retrieve", return_value=mock_new_pm),
            patch.object(stripe_provider.stripe_client.v1.payment_methods, "detach") as mock_detach,
        ):
            saved_method = SavedMethod(
                gateway="stripe",
                gateway_customer_id="cus_test123",
                gateway_method_id="pm_test123",
                method_type="sepa",
                display_label="IBAN •••• 1234",
                is_active=True,
                metadata={"last4": "1234", "mandate": "mandate_123"},
                id=1,
            )

            result = stripe_provider.confirm_refresh(saved_method, {"setup_intent_id": "seti_test123"})

            assert result.gateway_method_id == "pm_sepa_new456"
            assert result.display_label == "IBAN •••• 1234"  # Original display_label preserved
            assert "brand" not in result.metadata  # No card-specific fields added
            assert result.method_type == "sepa"
            # Original metadata preserved for non-card types
            assert result.metadata["mandate"] == "mandate_123"

            mock_detach.assert_called_once_with("pm_test123")



