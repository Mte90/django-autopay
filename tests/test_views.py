"""Tests for autopay/core/views.py."""

import json

import pytest
from django.test import Client, TestCase, override_settings

from autopay.core.base import AutopayProvider
from autopay.core.result import ChargeStatus, WebhookEventResult


class MockProvider(AutopayProvider):
    """Mock provider for tests."""
    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_WEBHOOK_VERIFICATION = False

    def process_webhook_event(self, event_type, payload):
        return WebhookEventResult(
            processed=True,
            action="status_update",
            transaction_id="txn_123",
            new_status=ChargeStatus.CONFIRMED,
        )


class NonCapableProvider(AutopayProvider):
    """Non-capable mock provider."""
    SUPPORTS_OFF_SESSION_CHARGE = False


class ErrorProvider(AutopayProvider):
    """Error mock provider."""
    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_WEBHOOK_VERIFICATION = False

    def process_webhook_event(self, event_type, payload):
        return WebhookEventResult(
            processed=False,
            action="error",
            transaction_id=None,
            error="Processing failed",
        )


class NoopProvider(AutopayProvider):
    """No-op mock provider."""
    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_WEBHOOK_VERIFICATION = False

    def process_webhook_event(self, event_type, payload):
        return WebhookEventResult(
            processed=True,
            action="noop",
            transaction_id=None,
        )


@override_settings(ROOT_URLCONF="tests.urls")
class TestCoreViews(TestCase):
    """Tests for core views."""

    def setUp(self):
        self.client = Client()

    @pytest.mark.django_db
    def test_autopay_webhook_view_post_success(self):
        """Test webhook view with success."""
        class MockProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_WEBHOOK_VERIFICATION = False

            def process_webhook_event(self, event_type, payload):
                return WebhookEventResult(
                    processed=True,
                    action="status_update",
                    transaction_id="txn_123",
                    new_status=ChargeStatus.CONFIRMED,
                )

        with override_settings(
            PAYMENT_VARIANTS={
                "dummy": ("tests.test_views.MockProvider", {}),
            }
        ):
            response = self.client.post(
                "/autopay/webhook/dummy/",
                data=json.dumps({"id": "evt_123", "type": "charge.succeeded"}),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"

    @pytest.mark.django_db
    def test_autopay_webhook_view_invalid_json(self):
        """Test webhook view with invalid JSON."""
        response = self.client.post(
            "/autopay/webhook/dummy/",
            data="not json",
            content_type="application/json",
        )

        assert response.status_code == 400

    @pytest.mark.django_db
    def test_autopay_webhook_view_unknown_variant(self):
        """Test webhook view with unknown variant."""
        with override_settings(PAYMENT_VARIANTS={}):
            response = self.client.post(
                "/autopay/webhook/unknown/",
                data=json.dumps({"id": "evt_123"}),
                content_type="application/json",
            )

            assert response.status_code == 400

    @pytest.mark.django_db
    def test_autopay_webhook_view_non_capable_provider(self):
        """Test webhook view with non-capable provider."""

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False

        with override_settings(
            PAYMENT_VARIANTS={
                "dummy": ("tests.test_views.NonCapableProvider", {}),
            }
        ):
            response = self.client.post(
                "/autopay/webhook/dummy/",
                data=json.dumps({"id": "evt_123"}),
                content_type="application/json",
            )

            assert response.status_code == 400

    @pytest.mark.django_db
    def test_autopay_webhook_view_webhook_error(self):
        """Test webhook view with processing error."""

        class ErrorProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_WEBHOOK_VERIFICATION = False

            def process_webhook_event(self, event_type, payload):
                return WebhookEventResult(
                    processed=False,
                    action="error",
                    transaction_id=None,
                    error="Processing failed",
                )

        with override_settings(
            PAYMENT_VARIANTS={
                "dummy": ("tests.test_views.ErrorProvider", {}),
            }
        ):
            response = self.client.post(
                "/autopay/webhook/dummy/",
                data=json.dumps({"id": "evt_123", "type": "charge.succeeded"}),
                content_type="application/json",
            )

            assert response.status_code == 400
            data = response.json()
            assert data["status"] == "error"

    @pytest.mark.django_db
    def test_autopay_webhook_view_noop_action(self):
        """Test webhook view with noop action."""

        class NoopProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_WEBHOOK_VERIFICATION = False

            def process_webhook_event(self, event_type, payload):
                return WebhookEventResult(
                    processed=True,
                    action="noop",
                    transaction_id=None,
                )

        with override_settings(
            PAYMENT_VARIANTS={
                "dummy": ("tests.test_views.NoopProvider", {}),
            }
        ):
            response = self.client.post(
                "/autopay/webhook/dummy/",
                data=json.dumps({"id": "evt_123"}),
                content_type="application/json",
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["action"] == "noop"

    @pytest.mark.django_db
    def test_autopay_webhook_view_get_method_not_allowed(self):
        """Test that GET returns 405."""
        response = self.client.get("/autopay/webhook/dummy/")
        assert response.status_code == 405