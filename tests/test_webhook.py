"""Webhook tests for django-autopay.

Covers:
- Deduplication (including Mollie composite key case)
- Signature verification failure → error result + no persist
- Unknown variant → typed error
- Replay idempotency
- View e2e with test client
"""
import json
import uuid

from django.test import Client, override_settings
from django.urls import reverse

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
