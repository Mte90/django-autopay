"""Tests for autopay/dummy/provider.py - dummy provider coverage."""

from decimal import Decimal

import pytest

from autopay.core.facade import Payments
from autopay.core.result import ChargeStatus
from autopay.core.saved_method import SavedMethod
from autopay.dummy.provider import DummyAutopayProvider


class TestDummyAutopayProvider:
    """Tests for DummyAutopayProvider."""

    def test_capability_flags_are_true(self):
        """Test that capability flags are True."""
        provider = DummyAutopayProvider()
        assert provider.SUPPORTS_OFF_SESSION_CHARGE is True
        assert provider.SUPPORTS_LIST_METHODS is True
        assert provider.SUPPORTS_REVOKE is True
        assert provider.SUPPORTS_VALIDATE is True
        assert provider.SUPPORTS_REFRESH is True
        assert provider.SUPPORTS_WEBHOOK_VERIFICATION is False

    def test_charge_off_session_success(self):
        """Test successful off-session charge."""
        provider = DummyAutopayProvider()
        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_success",
            method_type="card",
            display_label="Test Card",
        )

        result = provider.charge_off_session(saved_method, Decimal("10.00"), "EUR")

        assert result.status == ChargeStatus.CONFIRMED
        assert result.transaction_id.startswith("dummy_charge_")
        assert result.amount_charged == Decimal("10.00")
        assert result.currency == "EUR"

    def test_charge_off_session_failure(self):
        """Test failed off-session charge."""
        provider = DummyAutopayProvider()
        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_fail",
            method_type="card",
            display_label="Failed Card",
        )

        result = provider.charge_off_session(saved_method, Decimal("10.00"), "EUR")

        assert result.status == ChargeStatus.FAILED
        assert "simulated_failure" in result.raw_response.get("error", "")

    def test_charge_off_session_decline(self):
        """Test declined off-session charge."""
        provider = DummyAutopayProvider()
        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_decline",
            method_type="card",
            display_label="Declined Card",
        )

        result = provider.charge_off_session(saved_method, Decimal("10.00"), "EUR")

        assert result.status == ChargeStatus.REJECTED
        assert "card_declined" in result.raw_response.get("error", "")

    def test_list_saved_methods(self):
        """Test list_saved_methods."""
        provider = DummyAutopayProvider()
        # Add a method first
        provider._methods["pm_123"] = {
            "customer_id": "cust_123",
            "type": "card",
            "label": "Test Card",
        }

        methods = provider.list_saved_methods("cust_123")

        assert len(methods) == 1
        assert methods[0].gateway_method_id == "pm_123"
        assert methods[0].gateway_customer_id == "cust_123"

    def test_list_saved_methods_empty(self):
        """Test list_saved_methods with empty list."""
        provider = DummyAutopayProvider()

        methods = provider.list_saved_methods("cust_123")

        assert methods == []

    def test_revoke_method(self):
        """Test revoke_method."""
        provider = DummyAutopayProvider()
        # Add a method first
        provider._methods["pm_123"] = {
            "customer_id": "cust_123",
            "type": "card",
        }

        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )
        provider.revoke_method(saved_method)

        assert "pm_123" not in provider._methods

    def test_validate_method(self):
        """Test validate_method."""
        provider = DummyAutopayProvider()
        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )

        result = provider.validate_method(saved_method)

        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False

    def test_refresh_method(self):
        """Test refresh_method."""
        provider = DummyAutopayProvider()
        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )

        result = provider.refresh_method(saved_method, reason="scheduled")

        assert result.gateway_setup_id.startswith("dummy_refresh_")
        assert result.frontend_sdk == "dummy"

    def test_confirm_refresh(self):
        """Test confirm_refresh."""
        provider = DummyAutopayProvider()
        # Add a method first
        provider._methods["pm_123"] = {
            "customer_id": "cust_123",
            "type": "card",
        }

        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )

        result = provider.confirm_refresh(saved_method, {"metadata": {"refreshed": True}})

        assert result.gateway_method_id == "pm_123"
        assert provider._methods["pm_123"]["metadata"] == {"refreshed": True}

    def test_process_webhook_event_charge_succeeded(self):
        """Test process_webhook_event for charge.succeeded."""
        provider = DummyAutopayProvider()

        result = provider.process_webhook_event(
            "charge.succeeded", {"transaction_id": "txn_123"}
        )

        assert result.processed is True
        assert result.action == "status_update"
        assert result.new_status == ChargeStatus.CONFIRMED

    def test_process_webhook_event_charge_failed(self):
        """Test process_webhook_event for charge.failed."""
        provider = DummyAutopayProvider()

        result = provider.process_webhook_event(
            "charge.failed", {"transaction_id": "txn_123"}
        )

        assert result.processed is True
        assert result.action == "status_update"
        assert result.new_status == ChargeStatus.FAILED

    def test_process_webhook_event_method_revoked(self):
        """Test process_webhook_event for method.revoked."""
        provider = DummyAutopayProvider()

        result = provider.process_webhook_event(
            "method.revoked", {"method_id": "pm_123"}
        )

        assert result.processed is True
        assert result.action == "revoke_method"
        assert result.transaction_id == "pm_123"

    def test_process_webhook_event_unknown_event(self):
        """Test process_webhook_event for unknown event."""
        provider = DummyAutopayProvider()

        result = provider.process_webhook_event(
            "unknown.event", {"id": "evt_123"}
        )

        assert result.processed is False
        assert result.action == "ignored"

    def test_process_webhook_event_default(self):
        """Test process_webhook_event for default event."""
        provider = DummyAutopayProvider()

        result = provider.process_webhook_event("some.other.event", {"id": "evt_123"})

        assert result.processed is True
        assert result.action == "noop"
        assert result.transaction_id == "evt_123"


class TestDummyProviderIntegration:
    """Integration tests for DummyAutopayProvider with facade."""

    @pytest.mark.django_db
    def test_dummy_provider_via_facade_charge_success(self, user):
        """Test charge via facade with dummy provider."""
        from autopay.core.models import SavedPaymentMethod

        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_success",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        result = Payments.charge_off_session(
            saved_method=sm.as_saved_method(),
            amount=Decimal("25.00"),
            currency="USD",
            description="Test charge",
        )

        assert result.status == ChargeStatus.CONFIRMED

    @pytest.mark.django_db
    def test_dummy_provider_via_facade_validate(self, user):
        """Test validate via facade with dummy provider."""
        from autopay.core.models import SavedPaymentMethod

        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        result = Payments.validate_method(sm.as_saved_method())

        assert result.is_valid is True

    def test_list_saved_methods_filters_by_customer_id(self):
        """Test that list_saved_methods skips methods for different customer_id."""
        provider = DummyAutopayProvider()
        # Store a method for customer A
        provider._methods["pm_456"] = {
            "customer_id": "cust_A",
            "type": "card",
            "label": "Customer A Card",
        }

        # List methods for customer B - should return empty
        methods = provider.list_saved_methods("cust_B")

        assert methods == []

    def test_revoke_method_nonexistent_is_noop(self):
        """Test that revoking a non-existent method does not raise."""
        provider = DummyAutopayProvider()
        # Do NOT add the method to _methods
        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_nonexistent",
            method_type="card",
            display_label="Nonexistent Card",
        )

        # Should not raise
        provider.revoke_method(saved_method)

        # _methods should still be empty
        assert provider._methods == {}

    def test_confirm_refresh_nonexistent_returns_unchanged(self):
        """Test that confirming refresh for non-existent method returns saved_method unchanged."""
        provider = DummyAutopayProvider()
        # Do NOT add the method to _methods
        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_nonexistent",
            method_type="card",
            display_label="Nonexistent Card",
            metadata={"original": "value"},
        )

        result = provider.confirm_refresh(saved_method, {"metadata": {"refreshed": True}})

        # Should return the saved_method unchanged
        assert result is saved_method
        assert result.gateway_method_id == "pm_nonexistent"
        # _methods should still be empty
        assert provider._methods == {}