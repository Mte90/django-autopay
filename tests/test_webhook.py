"""Webhook tests for django-autopay.

Covers:
- Deduplication (including Mollie composite key case)
- Signature verification failure → error result + no persist
- Unknown variant → typed error
- Replay idempotency
- View e2e with test client
"""
import json
import logging
import uuid

import django.contrib.auth.models
from django.test import Client, override_settings
from django.urls import reverse

from autopay.core import base, models
from autopay.core.facade import Payments
from autopay.core.models import ProcessedWebhookEvent

# Import dummy provider to ensure it's registered before tests run


class TestWebhookDeduplication:
    """Test deduplicazione webhook."""

    def test_same_event_id_deduped(self, db):
        """Stesso event_id → seconda chiamata non re-dispatcha."""
        event_id = "evt_" + str(uuid.uuid4())
        # Prima chiamata
        result1 = Payments.process_webhook("dummy", json.dumps({"id": event_id, "type": "charge.succeeded"}).encode(), {}, "application/json")
        assert result1.processed is True

        # Seconda chiamata con stesso event_id
        result2 = Payments.process_webhook("dummy", json.dumps({"id": event_id, "type": "charge.succeeded"}).encode(), {}, "application/json")
        assert result2.action == "noop"  # Already processed, no re-dispatch

    def test_mollie_composite_key_different_status(self, db, monkeypatch):
        """Mollie: same payment_id with different status → NOT deduped."""
        from unittest.mock import MagicMock

        from autopay.core.base import AutopayProvider
        from autopay.core.result import ChargeStatus, WebhookEventResult

        call_count = 0

        def fake_process(event_type, payload):
            nonlocal call_count
            call_count += 1
            status = ChargeStatus.PENDING if call_count == 1 else ChargeStatus.CONFIRMED
            return WebhookEventResult(
                processed=True,
                action="status_update",
                transaction_id=payload.get("id"),
                new_status=status,
            )

        provider = MagicMock(spec=AutopayProvider)
        provider.SUPPORTS_WEBHOOK_VERIFICATION = False
        provider.SUPPORTS_OFF_SESSION_CHARGE = True
        provider.process_webhook_event = fake_process
        monkeypatch.setattr("autopay.core.facade.provider_factory", lambda v: provider)

        # Mollie webhooks only contain {"id": "..."} — no status field
        result1 = Payments.process_webhook(
            "mollie", json.dumps({"id": "tr_123"}).encode(), {}, "application/json"
        )
        assert result1.processed is True
        assert result1.action == "status_update"

        # Same payment_id but provider returns a different status → different composite key
        result2 = Payments.process_webhook(
            "mollie", json.dumps({"id": "tr_123"}).encode(), {}, "application/json"
        )
        assert result2.processed is True
        assert result2.action == "status_update"

    def test_process_webhook_passes_client_ip_to_verification(self, db, monkeypatch):
        """client_ip is injected as REMOTE_ADDR for IP-allowlist verification."""
        from unittest.mock import MagicMock

        from autopay.core.base import AutopayProvider
        from autopay.core.result import WebhookEventResult

        seen_headers = {}

        def fake_verify(raw, headers):
            seen_headers.update(headers)
            return True

        provider = MagicMock(spec=AutopayProvider)
        provider.SUPPORTS_WEBHOOK_VERIFICATION = True
        provider.verify_webhook_request = fake_verify
        provider.process_webhook_event = lambda event_type, payload: WebhookEventResult(
            processed=True, action="noop", transaction_id=payload.get("id")
        )
        monkeypatch.setattr("autopay.core.facade.provider_factory", lambda v: provider)

        Payments.process_webhook(
            "mollie",
            json.dumps({"id": "tr_ip"}).encode(),
            {},
            "application/json",
            client_ip="198.51.100.7",
        )

        assert seen_headers.get("REMOTE_ADDR") == "198.51.100.7"

    def test_mollie_same_status_deduped(self, db, monkeypatch):
        """Mollie: same payment_id + same status → deduped."""
        from unittest.mock import MagicMock

        from autopay.core.base import AutopayProvider
        from autopay.core.result import ChargeStatus, WebhookEventResult

        def fake_process(event_type, payload):
            return WebhookEventResult(
                processed=True,
                action="status_update",
                transaction_id=payload.get("id"),
                new_status=ChargeStatus.CONFIRMED,
            )

        provider = MagicMock(spec=AutopayProvider)
        provider.SUPPORTS_WEBHOOK_VERIFICATION = False
        provider.SUPPORTS_OFF_SESSION_CHARGE = True
        provider.process_webhook_event = fake_process
        monkeypatch.setattr("autopay.core.facade.provider_factory", lambda v: provider)

        Payments.process_webhook("mollie", json.dumps({"id": "tr_456"}).encode(), {}, "application/json")

        result2 = Payments.process_webhook("mollie", json.dumps({"id": "tr_456"}).encode(), {}, "application/json")
        assert result2.action == "noop"


class TestWebhookSignatureVerification:
    """Test webhook signature verification."""

    @override_settings(PAYMENT_VARIANTS={"dummy": ("autopay.dummy.DummyAutopayProvider", {})})
    def test_dummy_no_verification(self, db):
        """Dummy provider does not verify signatures (SUPPORTS_WEBHOOK_VERIFICATION=False)."""
        result = Payments.process_webhook("dummy", json.dumps({"id": "evt_" + str(uuid.uuid4())}).encode(), {"X-Signature": "invalid"}, "application/json")
        assert result.processed is True  # Accepted without verification

    def test_unknown_variant_error(self, db):
        """Unknown variant → error result."""
        result = Payments.process_webhook("unknown_gateway", json.dumps({"id": "evt_" + str(uuid.uuid4())}).encode(), {}, "application/json")
        assert result.processed is False
        assert "not found or not capable" in result.error or result.action == "error"


class TestWebhookIdempotency:
    """Test webhook idempotency."""

    def test_replay_returns_success(self, db):
        """Replay webhook → success without side effects."""
        event_id = "evt_" + str(uuid.uuid4())
        # Prima elaborazione
        result1 = Payments.process_webhook("dummy", json.dumps({"id": event_id, "type": "charge.succeeded"}).encode(), {}, "application/json")
        assert result1.processed is True

        # Count webhook events
        count1 = ProcessedWebhookEvent.objects.filter(gateway="dummy", event_id=event_id).count()
        assert count1 == 1

        # Replay
        result2 = Payments.process_webhook("dummy", json.dumps({"id": event_id, "type": "charge.succeeded"}).encode(), {}, "application/json")
        assert result2.action == "noop"

        # Count webhook events (should not increase)
        count2 = ProcessedWebhookEvent.objects.filter(gateway="dummy", event_id=event_id).count()
        assert count2 == 1


class TestWebhookView:
    """Test webhook Django view."""

    def test_webhook_view_success(self, db):
        """Webhook view → 200 for valid event."""
        client = Client()
        url = reverse("autopay_webhook", kwargs={"variant": "dummy"})

        response = client.post(
            url,
            data=json.dumps({"id": "evt_" + str(uuid.uuid4()), "type": "charge.succeeded"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_webhook_view_invalid_json(self, db):
        """Webhook view → 400 for invalid JSON."""
        client = Client()
        url = reverse("autopay_webhook", kwargs={"variant": "dummy"})

        response = client.post(
            url,
            data="not json",
            content_type="application/json",
        )

        assert response.status_code == 400

    def test_webhook_view_unknown_variant(self, db):
        """Webhook view → 400 for unknown variant."""
        client = Client()
        url = reverse("autopay_webhook", kwargs={"variant": "unknown"})

        response = client.post(
            url,
            data=json.dumps({"id": "evt_" + str(uuid.uuid4())}),
            content_type="application/json",
        )

        assert response.status_code == 400

    def test_webhook_view_e2e(self, db):
        """E2E: webhook → persistence → replay."""
        client = Client()
        url = reverse("autopay_webhook", kwargs={"variant": "dummy"})
        event_id = "evt_" + str(uuid.uuid4())

        # First call
        response1 = client.post(
            url,
            data=json.dumps({"id": event_id, "type": "charge.succeeded"}),
            content_type="application/json",
        )
        assert response1.status_code == 200

        # Verify persistence
        assert ProcessedWebhookEvent.objects.filter(
            gateway="dummy", event_id=event_id
        ).exists()

        # Replay
        response2 = client.post(
            url,
            data=json.dumps({"id": event_id, "type": "charge.succeeded"}),
            content_type="application/json",
        )
        assert response2.status_code == 200
        assert response2.json()["action"] == "noop"


class TestWebhookEventProcessing:
    """Test webhook event processing."""

    def test_charge_succeeded_event(self, db):
        """charge.succeeded event → processed=True."""
        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_charge_" + str(uuid.uuid4()), "type": "charge.succeeded", "transaction_id": "txn_123"}).encode(),
            {},
            "application/json",
        )
        assert result.processed is True
        assert result.action == "status_update"

    def test_charge_failed_event(self, db):
        """charge.failed event → processed=True."""
        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_fail_" + str(uuid.uuid4()), "type": "charge.failed", "transaction_id": "txn_456"}).encode(),
            {},
            "application/json",
        )
        assert result.processed is True
        assert result.action == "status_update"

    def test_method_revoked_event(self, db):
        """method.revoked event → action=revoke_method."""
        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_revoke_" + str(uuid.uuid4()), "type": "method.revoked", "method_id": "method_123"}).encode(),
            {},
            "application/json",
        )
        assert result.processed is True
        assert result.action == "revoke_method"

    def test_unknown_event_type(self, db):
        """Unknown event → processed=False."""
        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_unknown_" + str(uuid.uuid4()), "type": "unknown.event"}).encode(),
            {},
            "application/json",
        )
        assert result.processed is False
        assert result.action == "ignored"

class TestWebhookBranchCoverage:
    """Tests for uncovered webhook facade branches."""

    def test_webhook_verification_succeeds(self, db, monkeypatch):
        """Provider with SUPPORTS_WEBHOOK_VERIFICATION=True and valid signature (branch 372->392)."""
        from unittest.mock import MagicMock

        from autopay.core.base import AutopayProvider
        from autopay.core.result import WebhookEventResult

        provider = MagicMock(spec=AutopayProvider)
        provider.SUPPORTS_WEBHOOK_VERIFICATION = True
        provider.SUPPORTS_OFF_SESSION_CHARGE = True
        provider.verify_webhook_request = lambda raw, headers: True
        provider.process_webhook_event = lambda event_type, payload: WebhookEventResult(
            processed=True,
            action="status_update",
            transaction_id=payload.get("id"),
        )
        monkeypatch.setattr("autopay.core.facade.provider_factory", lambda v: provider)

        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_verify_" + str(uuid.uuid4()), "type": "charge.succeeded"}).encode(),
            {"X-Signature": "valid"},
            "application/json",
        )
        assert result.processed is True
        assert result.action == "status_update"

    def test_process_webhook_event_returns_none(self, db, monkeypatch):
        """Provider returns None from process_webhook_event (branch 419->422)."""
        from unittest.mock import MagicMock

        from autopay.core.base import AutopayProvider

        provider = MagicMock(spec=AutopayProvider)
        provider.SUPPORTS_WEBHOOK_VERIFICATION = False
        provider.SUPPORTS_OFF_SESSION_CHARGE = True
        provider.process_webhook_event = lambda event_type, payload: None
        monkeypatch.setattr("autopay.core.facade.provider_factory", lambda v: provider)

        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_none_" + str(uuid.uuid4()), "type": "charge.succeeded"}).encode(),
            {},
            "application/json",
        )
        assert result.processed is False
        assert result.action == "ignored"


class TestWebhookFacadeIntegration:
    """Integration tests for new webhook functionality (signal emission + status updates)."""

    def test_refund_action_marks_charge_refunded(
        self, db, monkeypatch
    ):
        """Provider returns action='refund' → ChargeRecord.status updated to 'refunded'."""
        from unittest.mock import MagicMock
        from autopay.core.result import ChargeStatus, WebhookEventResult
        from autopay.core.models import SavedPaymentMethod

        # Create a ChargeRecord
        user = django.contrib.auth.models.User.objects.create_user(
            username="testuser1", password="testpass"
        )
        saved_method = SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_1",
            gateway_method_id="method_1",
            method_type="card",
            display_label="Test Card",
        )
        charge = models.ChargeRecord.objects.create(
            saved_method=saved_method,
            amount=1000,  # 10.00 EUR
            currency="EUR",
            description="Test charge",
            transaction_id="txn_1",
            status=ChargeStatus.CONFIRMED,
            message="Initial confirmation",
            raw_response={},
            idempotency_key="idem_1",
        )
        old_status = charge.status

        # Mock provider to return refund action
        provider = MagicMock(spec=base.AutopayProvider)
        provider.SUPPORTS_WEBHOOK_VERIFICATION = False
        provider.SUPPORTS_OFF_SESSION_CHARGE = True
        provider.process_webhook_event = lambda event_type, payload: WebhookEventResult(
            processed=True,
            action="refund",
            transaction_id="txn_1",
        )
        monkeypatch.setattr("autopay.core.facade.provider_factory", lambda v: provider)

        # Process webhook
        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_refund_1", "type": "charge.refunded"}).encode(),
            {},
            "application/json",
        )

        # Verify ChargeRecord was updated
        charge.refresh_from_db()
        assert charge.status == ChargeStatus.REFUNDED
        assert charge.status != old_status

    def test_capture_completed_updates_charge_status(
        self, db, monkeypatch
    ):
        """Provider returns action='status_update' with new_status → ChargeRecord updated."""
        from unittest.mock import MagicMock
        from autopay.core.result import ChargeStatus, WebhookEventResult
        from autopay.core.models import SavedPaymentMethod

        # Create a ChargeRecord with PENDING status
        user = django.contrib.auth.models.User.objects.create_user(
            username="testuser2", password="testpass"
        )
        saved_method = SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_2",
            gateway_method_id="method_2",
            method_type="card",
            display_label="Test Card 2",
        )
        charge = models.ChargeRecord.objects.create(
            saved_method=saved_method,
            amount=2000,  # 20.00 EUR
            currency="EUR",
            description="Test charge 2",
            transaction_id="txn_2",
            status=ChargeStatus.PENDING,
            message="Pending status",
            raw_response={},
            idempotency_key="idem_2",
        )
        old_status = charge.status

        # Mock provider to return status_update with new status
        provider = MagicMock(spec=base.AutopayProvider)
        provider.SUPPORTS_WEBHOOK_VERIFICATION = False
        provider.SUPPORTS_OFF_SESSION_CHARGE = True
        provider.process_webhook_event = lambda event_type, payload: WebhookEventResult(
            processed=True,
            action="status_update",
            transaction_id="txn_2",
            new_status=ChargeStatus.CONFIRMED,
        )
        monkeypatch.setattr("autopay.core.facade.provider_factory", lambda v: provider)

        # Process webhook
        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_status_2", "type": "charge.succeeded"}).encode(),
            {},
            "application/json",
        )

        # Verify ChargeRecord was updated to CONFIRMED
        charge.refresh_from_db()
        assert charge.status == ChargeStatus.CONFIRMED
        assert charge.status != old_status

    def test_webhook_processed_signal_emitted(
        self, db, monkeypatch
    ):
        """webhook_processed signal is emitted after successful processing."""
        from unittest.mock import MagicMock
        from django.dispatch import receiver
        from autopay.core.result import ChargeStatus, WebhookEventResult
        from autopay.core.models import SavedPaymentMethod

        # Received call kwargs
        captured = []

        def capture_receiver(sender, event_type, payload, result, **extra):
            captured.append(
                {
                    "sender": sender,
                    "event_type": event_type,
                    "payload": payload,
                    "result": result,
                }
            )

        logger = logging.getLogger("autopay.fixture")
        logger.setLevel(logging.DEBUG)

        # Create a ChargeRecord to trigger update action
        user = django.contrib.auth.models.User.objects.create_user(
            username="testuser3", password="testpass"
        )
        saved_method = SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_3",
            gateway_method_id="method_3",
            method_type="card",
            display_label="Test Card 3",
        )
        charge = models.ChargeRecord.objects.create(
            saved_method=saved_method,
            amount=1500,  # 15.00 EUR
            currency="EUR",
            description="Test charge 3",
            transaction_id="txn_3",
            status=ChargeStatus.PENDING,
            message="Webhook test",
            raw_response={},
            idempotency_key="idem_3",
        )

        # Mock provider
        provider = MagicMock(spec=base.AutopayProvider)
        provider.SUPPORTS_WEBHOOK_VERIFICATION = False
        provider.SUPPORTS_OFF_SESSION_CHARGE = True
        provider.process_webhook_event = lambda event_type, payload: WebhookEventResult(
            processed=True,
            action="status_update",
            transaction_id="txn_3",
        )
        monkeypatch.setattr("autopay.core.facade.provider_factory", lambda v: provider)

        # Connect signal receiver
        from autopay.core.signals import webhook_processed
        webhook_processed.connect(capture_receiver)

        # Process webhook
        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_signal_3", "type": "charge.succeeded", "transaction_id": "txn_3"}).encode(),
            {},
            "application/json",
        )

        # Verify signal was emitted with correct params
        assert len(captured) == 1
        assert captured[0]["sender"] == "dummy"
        assert captured[0]["event_type"] == "charge.succeeded"
        assert captured[0]["payload"]["id"] == "evt_signal_3"
        assert captured[0]["result"].processed is True
        assert captured[0]["result"].action == "status_update"
        assert captured[0]["result"].transaction_id == "txn_3"

        # Disconnect receiver
        webhook_processed.disconnect(capture_receiver)

    def test_webhook_processed_signal_not_emitted_on_unprocessed(
        self, db, monkeypatch
    ):
        """Signal is NOT emitted when webhook fails to process."""
        from unittest.mock import MagicMock
        from autopay.core.result import WebhookEventResult

        # Track if signal was emitted
        captured = []

        def capture_receiver(sender, event_type, payload, result, **extra):
            captured.append({"sender": sender, "event_type": event_type})

        # Mock provider to return unprocessed result
        provider = MagicMock(spec=base.AutopayProvider)
        provider.SUPPORTS_WEBHOOK_VERIFICATION = False
        provider.SUPPORTS_OFF_SESSION_CHARGE = True
        provider.process_webhook_event = lambda event_type, payload: WebhookEventResult(
            processed=False,
            action="ignored",
            transaction_id=None,
        )
        monkeypatch.setattr("autopay.core.facade.provider_factory", lambda v: provider)

        # Connect signal receiver
        from autopay.core.signals import webhook_processed
        webhook_processed.connect(capture_receiver)

        # Process unknown event → should not emit signal
        result = Payments.process_webhook(
            "dummy",
            json.dumps({"id": "evt_fail_sig", "type": "unknown.event"}).encode(),
            {},
            "application/json",
        )

        # Verify signal was NOT emitted
        assert len(captured) == 0
        assert result.processed is False

        # Disconnect receiver
        webhook_processed.disconnect(capture_receiver)
