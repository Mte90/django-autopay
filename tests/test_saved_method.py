"""Tests for SavedMethod dataclass and SavedMethodProtocol."""

from dataclasses import fields

from autopay.core.saved_method import SavedMethod


class TestSavedMethod:
    """Tests for SavedMethod dataclass."""

    def test_saved_method_required_fields(self):
        """Test that required fields are required."""
        sm = SavedMethod(
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
        )

        assert sm.gateway == "stripe"
        assert sm.gateway_customer_id == "cus_test123"
        assert sm.gateway_method_id == "pm_test123"
        assert sm.method_type == "card"

    def test_saved_method_default_values(self):
        """Test optional field defaults."""
        sm = SavedMethod(
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
        )

        assert sm.display_label == ""
        assert sm.is_active is True
        assert sm.metadata is None
        assert sm.id is None

    def test_saved_method_full_construction(self):
        """Test full construction with all fields."""
        sm = SavedMethod(
            gateway="paypal",
            gateway_customer_id="cus_paypal123",
            gateway_method_id="token_paypal123",
            method_type="paypal",
            display_label="PayPal account",
            is_active=True,
            metadata={"email": "user@example.com"},
            id=42,
        )

        assert sm.gateway == "paypal"
        assert sm.gateway_customer_id == "cus_paypal123"
        assert sm.gateway_method_id == "token_paypal123"
        assert sm.method_type == "paypal"
        assert sm.display_label == "PayPal account"
        assert sm.is_active is True
        assert sm.metadata == {"email": "user@example.com"}
        assert sm.id == 42

    def test_saved_method_field_count(self):
        """Test exact field count."""
        field_names = [f.name for f in fields(SavedMethod)]
        assert len(field_names) == 8
        assert "gateway" in field_names
        assert "gateway_customer_id" in field_names
        assert "gateway_method_id" in field_names
        assert "method_type" in field_names
        assert "display_label" in field_names
        assert "is_active" in field_names
        assert "metadata" in field_names
        assert "id" in field_names

    def test_saved_method_with_metadata(self):
        """Test that metadata can contain gateway-specific data."""
        sm = SavedMethod(
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            metadata={
                "brand": "visa",
                "last4": "4242",
                "exp_month": 12,
                "exp_year": 2025,
                "fingerprint": "abc123",
            },
        )

        assert sm.metadata["brand"] == "visa"
        assert sm.metadata["last4"] == "4242"
        assert sm.metadata["exp_month"] == 12
        assert sm.metadata["exp_year"] == 2025

    def test_saved_method_is_active_false(self):
        """Test that is_active can be False."""
        sm = SavedMethod(
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            is_active=False,
        )

        assert sm.is_active is False


class TestSavedMethodProtocol:
    """Tests for SavedMethodProtocol interface."""

    def test_protocol_attributes(self):
        """Test that SavedMethod has protocol attributes."""
        sm = SavedMethod(
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            is_active=True,
            metadata={},
        )

        # Verify all protocol attributes are present
        assert hasattr(sm, "gateway")
        assert hasattr(sm, "gateway_customer_id")
        assert hasattr(sm, "gateway_method_id")
        assert hasattr(sm, "method_type")
        assert hasattr(sm, "is_active")
        assert hasattr(sm, "metadata")

    def test_protocol_types(self):
        """Test protocol attribute types."""
        sm = SavedMethod(
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            is_active=True,
            metadata={"key": "value"},
        )

        assert isinstance(sm.gateway, str)
        assert isinstance(sm.gateway_customer_id, str)
        assert isinstance(sm.gateway_method_id, str)
        assert isinstance(sm.method_type, str)
        assert isinstance(sm.is_active, bool)
        assert isinstance(sm.metadata, dict)