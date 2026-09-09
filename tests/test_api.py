"""Test for django-bolt API endpoints.

Uses django-bolt's TestClient to test REST endpoints.
Per plan §12.5.
"""
from __future__ import annotations

import json

import pytest

# Skip all tests if django_bolt is not available (Python < 3.12)
pytest.importorskip("django_bolt")

from decimal import Decimal

from django.test import override_settings

from autopay.api.bolt import api
from autopay.api.schemas import (
    ChargeIn,
    ChargeOut,
    ConfirmRefreshIn,
    ConfirmRefreshOut,
    RefreshMethodIn,
    RefreshMethodOut,
    SavedMethodOut,
    ValidationResultOut,
    WebhookReceiverOut,
)
from autopay.core.facade import Payments
from autopay.core.models import ProcessedWebhookEvent, SavedPaymentMethod

pytestmark = [pytest.mark.django_db]


@pytest.fixture
def api_client():
    """TestClient for BoltAPI endpoints."""
    from django_bolt.testing import TestClient
    return TestClient(api)


@pytest.fixture
def user(django_user_model):
    """User for tests."""
    return django_user_model.objects.create_user(username="testuser", password="testpass")


@pytest.fixture
def saved_method(user):
    """SavedPaymentMethod for tests."""
    return SavedPaymentMethod.objects.create(
        user=user,
        gateway="paypal",
        gateway_customer_id="cus_test123",
        gateway_method_id="pm_test123",
        method_type="card",
        display_label="Visa •••• 4242",
        is_active=True,
        metadata={"brand": "visa", "last4": "4242"},
    )


class TestSchemas:
    """Test per msgspec Struct schemas."""

    def test_saved_method_out_construction(self, saved_method):
        """Costruzione SavedMethodOut."""
        out = SavedMethodOut(
            id=saved_method.id,
            gateway=saved_method.gateway,
            method_type=saved_method.method_type,
            display_label=saved_method.display_label,
            is_default=saved_method.is_default,
            is_active=saved_method.is_active,
            metadata=saved_method.metadata,
            created_at=saved_method.created_at.isoformat(),
        )
        assert out.id == saved_method.id
        assert out.gateway == "paypal"

    def test_charge_in_construction(self):
        """Costruzione ChargeIn."""
        charge = ChargeIn(
            saved_method_id=1,
            amount=Decimal("10.50"),
            currency="EUR",
            description="Test charge",
        )
        assert charge.amount == Decimal("10.50")
        assert charge.currency == "EUR"

    def test_refresh_method_in_default_reason(self):
        """RefreshMethodIn con default reason."""
        refresh = RefreshMethodIn()
        assert refresh.reason == "manual"

    def test_confirm_refresh_in_gateway_fields(self):
        """ConfirmRefreshIn con campi gateway-specific."""
        paypal_payload = ConfirmRefreshIn(
            setup_token_id="token123",
            payment_token_id="payment456",
        )
        assert paypal_payload.setup_token_id == "token123"

        stripe_payload = ConfirmRefreshIn(setup_intent_id="si_789")
        assert stripe_payload.setup_intent_id == "si_789"

        mollie_payload = ConfirmRefreshIn(first_payment_id="tr_mollie")
        assert mollie_payload.first_payment_id == "tr_mollie"


class TestValidationResultOut:
    """Test per ValidationResultOut schema."""

    def test_validation_result_structure(self):
        """Struttura ValidationResultOut."""
        result = ValidationResultOut(
            is_valid=True,
            is_expired=False,
            needs_refresh=False,
            reason="OK",
            checked_at="2024-01-01T00:00:00+00:00",
            method_id=1,
            gateway="paypal",
        )
        assert result.is_valid is True
        assert result.gateway == "paypal"


class TestChargeOut:
    """Test per ChargeOut schema."""

    def test_charge_out_confirmed(self):
        """ChargeOut per charge confermato."""
        out = ChargeOut(
            status="confirmed",
            message="Charge successful",
            transaction_id="pi_123",
            amount_charged="10.50",
            currency="EUR",
            requires_action=None,
            created_at="2024-01-01T00:00:00+00:00",
        )
        assert out.status == "confirmed"
        assert out.amount_charged == "10.50"

    def test_charge_out_pending(self):
        """ChargeOut per charge pending."""
        out = ChargeOut(
            status="pending",
            message="Processing",
            transaction_id="pi_456",
            amount_charged=None,
            currency="EUR",
            requires_action=None,
            created_at="2024-01-01T00:00:00+00:00",
        )
        assert out.status == "pending"
        assert out.amount_charged is None


class TestConfirmRefreshOut:
    """Test per ConfirmRefreshOut schema."""

    def test_confirm_refresh_success(self, saved_method):
        """Confirm refresh success."""
        method = SavedMethodOut(
            id=saved_method.id,
            gateway=saved_method.gateway,
            method_type=saved_method.method_type,
            display_label=saved_method.display_label,
            is_default=saved_method.is_default,
            is_active=saved_method.is_active,
            metadata=saved_method.metadata,
            created_at=saved_method.created_at.isoformat(),
        )
        out = ConfirmRefreshOut(success=True, method=method, error=None)
        assert out.success is True
        assert out.error is None

    def test_confirm_refresh_failure(self):
        """Confirm refresh failed."""
        out = ConfirmRefreshOut(success=False, method=None, error="Invalid token")
        assert out.success is False
        assert out.error == "Invalid token"


class TestRefreshMethodOut:
    """Test per RefreshMethodOut schema."""

    def test_refresh_method_out_structure(self):
        """Struttura RefreshMethodOut."""
        out = RefreshMethodOut(
            setup_id=1,
            frontend_sdk="paypal",
            frontend_payload={"setup_token": "token123"},
            expires_at="2024-01-02T00:00:00+00:00",
            return_url="https://example.com/return",
        )
        assert out.setup_id == 1
        assert out.frontend_sdk == "paypal"
        assert out.expires_at == "2024-01-02T00:00:00+00:00"


class TestWebhookReceiverOut:
    """Test per WebhookReceiverOut schema."""

    def test_webhook_processed(self):
        """Webhook processato."""
        out = WebhookReceiverOut(processed=True, event_id="evt_123")
        assert out.processed is True
        assert out.event_id == "evt_123"

    def test_webhook_no_event_id(self):
        """Webhook without event_id (Mollie)."""
        out = WebhookReceiverOut(processed=True, event_id=None)
        assert out.processed is True
        assert out.event_id is None


class TestFacadeWebhookIntegration:
    """Integrazione facade process_webhook: persistenza + dedup + event_id."""

    def test_process_webhook_via_facade(self):
        """process_webhook elabora, persiste e deduplica l'evento."""
        payload = {"id": "evt_facade_1", "type": "charge.succeeded", "amount": "10.00"}

        with override_settings(
            PAYMENT_VARIANTS={"dummy": ("autopay.dummy.DummyAutopayProvider", {})}
        ):
            result = Payments.process_webhook("dummy", json.dumps(payload).encode(), {}, "application/json")

            assert result.processed is True
            assert result.event_id == "evt_facade_1"
            assert ProcessedWebhookEvent.objects.filter(
                gateway="dummy", event_id="evt_facade_1"
            ).count() == 1

            # Replay dello stesso evento → noop, nessuna nuova riga
            replay = Payments.process_webhook("dummy", json.dumps(payload).encode(), {}, "application/json")
            assert replay.processed is True
            assert replay.action == "noop"
            assert ProcessedWebhookEvent.objects.count() == 1