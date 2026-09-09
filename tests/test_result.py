"""Tests for core dataclasses: ChargeStatus, ChargeResult, RefreshSetupData, RefreshSetup, ValidationResult."""

from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autopay.core.result import (
    ChargeResult,
    ChargeStatus,
    RefreshSetup,
    RefreshSetupData,
    ValidationResult,
    WebhookEventResult,
)


class TestChargeStatus:
    """Tests for ChargeStatus enum."""

    def test_all_statuses_exist(self):
        """Verify all 5 statuses exist."""
        assert ChargeStatus.CONFIRMED == "confirmed"
        assert ChargeStatus.PENDING == "pending"
        assert ChargeStatus.FAILED == "failed"
        assert ChargeStatus.REQUIRES_SCA == "requires_sca"
        assert ChargeStatus.REJECTED == "rejected"

    def test_statuses_are_string_enum(self):
        """Test that ChargeStatus is a string enum."""
        assert isinstance(ChargeStatus.CONFIRMED.value, str)
        assert ChargeStatus.CONFIRMED.value == "confirmed"


class TestChargeResult:
    """Tests for ChargeResult dataclass."""

    def test_charge_result_fields(self):
        """Test that ChargeResult has all required fields."""

        result = ChargeResult(
            status=ChargeStatus.CONFIRMED,
            message="Charge successful",
            transaction_id="tr_123",
            raw_response={"status": "completed"},
            amount_charged=Decimal("9.99"),
            currency="EUR",
            requires_action=None,
            created_at=datetime.now(timezone.utc),
        )

        assert result.status == ChargeStatus.CONFIRMED
        assert result.message == "Charge successful"
        assert result.transaction_id == "tr_123"
        assert result.raw_response == {"status": "completed"}
        assert result.amount_charged == Decimal("9.99")
        assert result.currency == "EUR"
        assert result.requires_action is None
        assert isinstance(result.created_at, datetime)

    def test_charge_result_required_fields(self):
        """Test that required fields are required."""
        with pytest.raises(TypeError):
            ChargeResult()  # type: ignore

    def test_charge_result_with_sca(self):
        """Test that REQUIRES_SCA has requires_action."""

        result = ChargeResult(
            status=ChargeStatus.REQUIRES_SCA,
            message="3DS authentication required",
            transaction_id="pi_123",
            raw_response={"next_action": {"type": "use_stripe_sdk"}},
            amount_charged=None,
            currency="EUR",
            requires_action={"client_secret": "pi_123_secret", "url": "https://stripe.com/3ds"},
            created_at=datetime.now(timezone.utc),
        )

        assert result.status == ChargeStatus.REQUIRES_SCA
        assert result.requires_action is not None
        assert "client_secret" in result.requires_action

    def test_charge_result_field_count(self):
        """Test exact field count."""
        field_names = [f.name for f in fields(ChargeResult)]
        assert len(field_names) == 8
        assert "status" in field_names
        assert "message" in field_names
        assert "transaction_id" in field_names
        assert "raw_response" in field_names
        assert "amount_charged" in field_names
        assert "currency" in field_names
        assert "requires_action" in field_names
        assert "created_at" in field_names


class TestRefreshSetupData:
    """Tests for RefreshSetupData dataclass (gateway-only, no DB)."""

    def test_refresh_setup_data_fields(self):
        """Test that RefreshSetupData has all required fields."""
        from datetime import timedelta

        data = RefreshSetupData(
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        assert data.gateway_setup_id == "setup_intent_123"
        assert data.frontend_sdk == "stripe"
        assert data.frontend_payload == {"client_secret": "seti_123"}
        assert isinstance(data.expires_at, datetime)

    def test_refresh_setup_data_distinction_from_refresh_setup(self):
        """Test that RefreshSetupData does not have setup_id (gateway-only data)."""
        from datetime import timedelta

        data = RefreshSetupData(
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        # RefreshSetupData does not have setup_id - that is added by the facade
        assert not hasattr(data, "setup_id")
        assert not hasattr(data, "return_url")


class TestRefreshSetup:
    """Tests for RefreshSetup dataclass (facade output with setup_id)."""

    def test_refresh_setup_fields(self):
        """Test that RefreshSetup has all required fields."""
        from datetime import timedelta

        setup = RefreshSetup(
            setup_id=42,
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            return_url="https://example.com/autopay/api/methods/1/refresh/confirm/?attempt=42",
        )

        assert setup.setup_id == 42
        assert setup.frontend_sdk == "stripe"
        assert setup.frontend_payload == {"client_secret": "seti_123"}
        assert isinstance(setup.expires_at, datetime)
        assert setup.return_url == "https://example.com/autopay/api/methods/1/refresh/confirm/?attempt=42"

    def test_refresh_setup_setup_id_semantics(self):
        """Test that setup_id=0 when models are not present."""
        from datetime import timedelta

        setup = RefreshSetup(
            setup_id=0,
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            return_url="https://example.com/autopay/api/methods/1/refresh/confirm/?attempt=0",
        )

        assert setup.setup_id == 0  # Models not installed


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_validation_result_fields(self):
        """Test that ValidationResult has all required fields."""

        result = ValidationResult(
            is_valid=True,
            is_expired=False,
            needs_refresh=False,
            reason="OK",
            checked_at=datetime.now(timezone.utc),
            raw_response={"status": "valid"},
        )

        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False
        assert result.reason == "OK"
        assert isinstance(result.checked_at, datetime)
        assert result.raw_response == {"status": "valid"}

    def test_validation_result_defaults(self):
        """Test optional field defaults."""

        result = ValidationResult(is_valid=True)

        assert result.is_valid is True
        assert result.is_expired is False
        assert result.needs_refresh is False
        assert result.reason == ""
        assert result.checked_at is None
        assert result.raw_response is None

    def test_validation_result_expired_card(self):
        """Test ValidationResult for expired card."""

        result = ValidationResult(
            is_valid=False,
            is_expired=True,
            needs_refresh=True,
            reason="Card expired 11/2024",
            checked_at=datetime.now(timezone.utc),
            raw_response={"error": "card_expired"},
        )

        assert result.is_valid is False
        assert result.is_expired is True
        assert result.needs_refresh is True


class TestWebhookEventResult:
    """Tests for WebhookEventResult dataclass."""

    def test_webhook_event_result_fields(self):
        """Test that WebhookEventResult has all required fields."""
        result = WebhookEventResult(
            processed=True,
            action="status_update",
            transaction_id="tr_123",
            new_status=ChargeStatus.CONFIRMED,
            raw={"event": "payment.completed"},
            error=None,
        )

        assert result.processed is True
        assert result.action == "status_update"
        assert result.transaction_id == "tr_123"
        assert result.new_status == ChargeStatus.CONFIRMED
        assert result.raw == {"event": "payment.completed"}
        assert result.error is None

    def test_webhook_event_result_error_action(self):
        """Test WebhookEventResult for error."""
        result = WebhookEventResult(
            processed=False,
            action="error",
            transaction_id=None,
            new_status=None,
            raw={"event": "invalid"},
            error="Unknown event type",
        )

        assert result.processed is False
        assert result.action == "error"
        assert result.error == "Unknown event type"