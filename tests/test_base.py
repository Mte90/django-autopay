"""Tests for autopay/core/base.py - capability flags and errors."""

import pytest

from autopay.core.base import (
    AutopayCapabilityError,
    AutopayProvider,
    is_autopay_capable,
)
from autopay.core.saved_method import SavedMethod


class TestAutopayCapabilityError:
    """Tests for AutopayCapabilityError."""

    def test_autopay_capability_error_is_runtime_error(self):
        """Verify AutopayCapabilityError is a RuntimeError."""
        assert issubclass(AutopayCapabilityError, RuntimeError)

    def test_autopay_capability_error_message(self):
        """Verify AutopayCapabilityError has the correct message."""
        error = AutopayCapabilityError("Test error message")
        assert str(error) == "Test error message"


class TestIsAutopayCapable:
    """Tests for is_autopay_capable."""

    def test_is_autopay_capable_with_capable_provider(self):
        """Verify is_autopay_capable returns True for capable provider."""

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True

        provider = CapableProvider()
        assert is_autopay_capable(provider) is True

    def test_is_autopay_capable_with_non_capable_provider(self):
        """Verify is_autopay_capable returns False for non-capable provider."""

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False

        provider = NonCapableProvider()
        assert is_autopay_capable(provider) is False

    def test_is_autopay_capable_with_not_autopay_provider(self):
        """Verify is_autopay_capable returns False for non-provider objects."""
        assert is_autopay_capable("not a provider") is False
        assert is_autopay_capable(123) is False
        assert is_autopay_capable(None) is False
        assert is_autopay_capable({"not": "provider"}) is False


class TestAutopayProviderCapabilityFlags:
    """Tests for AutopayProvider capability flags."""

    def test_default_flags_are_false(self):
        """Verify default flags are False."""
        provider = AutopayProvider()
        assert provider.SUPPORTS_OFF_SESSION_CHARGE is False
        assert provider.SUPPORTS_LIST_METHODS is False
        assert provider.SUPPORTS_REVOKE is False
        assert provider.SUPPORTS_REFRESH is False
        assert provider.SUPPORTS_VALIDATE is False
        assert provider.SUPPORTS_REFUND is False
        assert provider.SUPPORTS_WEBHOOK_VERIFICATION is False

    def test_can_override_flags(self):
        """Verify flags can be overridden."""

        class CustomProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_LIST_METHODS = True
            SUPPORTS_REVOKE = True
            SUPPORTS_REFRESH = True
            SUPPORTS_VALIDATE = True
            SUPPORTS_REFUND = True
            SUPPORTS_WEBHOOK_VERIFICATION = True

        provider = CustomProvider()
        assert provider.SUPPORTS_OFF_SESSION_CHARGE is True
        assert provider.SUPPORTS_LIST_METHODS is True
        assert provider.SUPPORTS_REVOKE is True
        assert provider.SUPPORTS_REFRESH is True
        assert provider.SUPPORTS_VALIDATE is True
        assert provider.SUPPORTS_REFUND is True
        assert provider.SUPPORTS_WEBHOOK_VERIFICATION is True


class TestAutopayProviderDefaultMethods:
    """Tests for AutopayProvider default methods."""

    def test_save_method_raises_not_implemented(self):
        """Verify save_method raises NotImplementedError."""
        provider = AutopayProvider()
        with pytest.raises(NotImplementedError):
            provider.save_method(None)

    def test_charge_off_session_raises_not_implemented(self):
        """Verify charge_off_session raises NotImplementedError."""
        provider = AutopayProvider()
        saved_method = SavedMethod(
            gateway="test",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )
        with pytest.raises(NotImplementedError):
            provider.charge_off_session(saved_method, 10.0, "EUR")

    def test_refresh_method_raises_not_implemented_when_not_supported(self):
        """Verify refresh_method raises NotImplementedError when not supported."""
        provider = AutopayProvider()
        saved_method = SavedMethod(
            gateway="test",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )
        with pytest.raises(NotImplementedError):
            provider.refresh_method(saved_method)

    def test_refresh_method_raises_not_implemented_when_declared_but_not_implemented(self):
        """Verify refresh_method raises NotImplementedError if declared but not implemented."""

        class PartialProvider(AutopayProvider):
            SUPPORTS_REFRESH = True

        provider = PartialProvider()
        saved_method = SavedMethod(
            gateway="test",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )
        with pytest.raises(NotImplementedError):
            provider.refresh_method(saved_method)

    def test_confirm_refresh_raises_not_implemented_when_not_supported(self):
        """Verify confirm_refresh raises NotImplementedError when not supported."""
        provider = AutopayProvider()
        saved_method = SavedMethod(
            gateway="test",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )
        with pytest.raises(NotImplementedError):
            provider.confirm_refresh(saved_method, {})

    def test_confirm_refresh_raises_not_implemented_when_declared_but_not_implemented(self):
        """Verify confirm_refresh raises NotImplementedError if declared but not implemented."""

        class PartialProvider(AutopayProvider):
            SUPPORTS_REFRESH = True

        provider = PartialProvider()
        saved_method = SavedMethod(
            gateway="test",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )
        with pytest.raises(NotImplementedError):
            provider.confirm_refresh(saved_method, {})

    def test_validate_method_raises_not_implemented_when_not_supported(self):
        """Verify validate_method raises NotImplementedError when not supported."""
        provider = AutopayProvider()
        saved_method = SavedMethod(
            gateway="test",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )
        with pytest.raises(NotImplementedError):
            provider.validate_method(saved_method)

    def test_list_saved_methods_raises_not_implemented_when_not_supported(self):
        """Verify list_saved_methods raises NotImplementedError when not supported."""
        provider = AutopayProvider()
        with pytest.raises(NotImplementedError):
            provider.list_saved_methods("customer_123")

    def test_revoke_method_raises_not_implemented_when_not_supported(self):
        """Verify revoke_method raises NotImplementedError when not supported."""
        provider = AutopayProvider()
        saved_method = SavedMethod(
            gateway="test",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
        )
        with pytest.raises(NotImplementedError):
            provider.revoke_method(saved_method)

    def test_get_charge_status_raises_not_implemented(self):
        """Verify get_charge_status raises NotImplementedError."""
        provider = AutopayProvider()
        with pytest.raises(NotImplementedError):
            provider.get_charge_status("txn_123")

    def test_refund_raises_not_implemented_when_not_supported(self):
        """Verify refund raises NotImplementedError when not supported."""
        provider = AutopayProvider()
        with pytest.raises(NotImplementedError):
            provider.refund("txn_123")

    def test_process_webhook_event_returns_none_when_not_supported(self):
        """Verify process_webhook_event returns None when not supported."""
        provider = AutopayProvider()
        result = provider.process_webhook_event("charge.succeeded", {})
        assert result is None

    def test_verify_webhook_request_returns_false_when_not_supported(self):
        """Verify verify_webhook_request returns False when not supported."""
        provider = AutopayProvider()
        result = provider.verify_webhook_request(None, {})
        assert result is False

class TestAutopayProviderUnimplementedMethods:
    """Tests for unimplemented methods in AutopayProvider."""

    def test_validate_method_raises_when_unimplemented(self):
        """Test that validate_method raises when unimplemented."""
        provider = AutopayProvider()
        provider.SUPPORTS_VALIDATE = True  # Enable the flag
        with pytest.raises(NotImplementedError):
            provider.validate_method(None)

    def test_list_saved_methods_raises_when_unimplemented(self):
        """Test that list_saved_methods raises when unimplemented."""
        provider = AutopayProvider()
        provider.SUPPORTS_LIST_METHODS = True  # Enable the flag
        with pytest.raises(NotImplementedError):
            provider.list_saved_methods("cust_123")

    def test_revoke_method_raises_when_unimplemented(self):
        """Test that revoke_method raises when unimplemented."""
        provider = AutopayProvider()
        provider.SUPPORTS_REVOKE = True  # Enable the flag
        with pytest.raises(NotImplementedError):
            provider.revoke_method(None)

    def test_get_charge_status_raises_when_unimplemented(self):
        """Test that get_charge_status raises when unimplemented."""
        provider = AutopayProvider()
        with pytest.raises(NotImplementedError):
            provider.get_charge_status("txn_123")

    def test_refund_raises_when_unimplemented(self):
        """Test that refund raises when unimplemented."""
        provider = AutopayProvider()
        provider.SUPPORTS_REFUND = True  # Enable the flag
        with pytest.raises(NotImplementedError):
            provider.refund("txn_123")

    def test_process_webhook_event_raises_when_unimplemented(self):
        """Test that process_webhook_event raises when unimplemented."""
        provider = AutopayProvider()
        provider.SUPPORTS_WEBHOOK_VERIFICATION = True  # Enable the flag
        with pytest.raises(NotImplementedError):
            provider.process_webhook_event("charge.succeeded", {})

    def test_verify_webhook_request_raises_when_unimplemented(self):
        """Test that verify_webhook_request raises when unimplemented."""
        provider = AutopayProvider()
        provider.SUPPORTS_WEBHOOK_VERIFICATION = True  # Enable the flag
        with pytest.raises(NotImplementedError):
            provider.verify_webhook_request(None, {})
