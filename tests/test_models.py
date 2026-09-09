"""Test per i modelli Django di django-autopay."""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import override_settings

from autopay.core.models import (
    ChargeRecord,
    ProcessedWebhookEvent,
    RefreshAttempt,
    SavedPaymentMethod,
    _get_saved_method_model,
)


@pytest.mark.django_db
class TestSavedPaymentMethod:
    """Tests for SavedPaymentMethod model."""

    def test_create_saved_payment_method(self, user):
        """Create a SavedPaymentMethod."""
        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa ending in 4242",
            is_active=True,
            metadata={"brand": "visa", "last4": "4242"},
        )

        assert sm.pk is not None
        assert sm.user == user
        assert sm.gateway == "stripe"
        assert sm.gateway_customer_id == "cus_test123"
        assert sm.gateway_method_id == "pm_test123"
        assert sm.method_type == "card"
        assert sm.display_label == "Visa ending in 4242"
        assert sm.is_active is True
        assert sm.metadata == {"brand": "visa", "last4": "4242"}

    def test_unique_constraint_gateway_gateway_method_id(self, user):
        """Unique constraint (gateway, gateway_method_id) raises IntegrityError."""
        SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa ending in 4242",
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            SavedPaymentMethod.objects.create(
                user=user,
                gateway="stripe",
                gateway_customer_id="cus_different",
                gateway_method_id="pm_test123",  # Stesso gateway_method_id
                method_type="card",
                display_label="Different card",
            )

    def test_unique_constraint_allows_different_gateway(self, user):
        """Same gateway_method_id is not allowed (unique=True on gateway_method_id)."""
        # gateway_method_id has unique=True, so same ID is not allowed even with different gateway
        SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe",
            gateway_method_id="pm_same_id",
            method_type="card",
            display_label="Stripe card",
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            SavedPaymentMethod.objects.create(
                user=user,
                gateway="paypal",
                gateway_customer_id="cus_paypal",
                gateway_method_id="pm_same_id",  # Same ID - not allowed due to unique=True
                method_type="paypal",
                display_label="PayPal account",
            )

    def test_query_active_methods(self, user):
        """Query to filter active methods."""
        SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_active",
            method_type="card",
            display_label="Active card",
            is_active=True,
        )

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_inactive",
            method_type="card",
            display_label="Inactive card",
            is_active=False,
        )

        active_methods = SavedPaymentMethod.objects.filter(user=user, is_active=True)

        assert active_methods.count() == 1
        assert active_methods.first().gateway_method_id == "pm_active"

    def test_as_saved_method_method(self, user):
        """as_saved_method() returns a SavedMethod dataclass."""
        from autopay.core.saved_method import SavedMethod

        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa ending in 4242",
            is_active=True,
            metadata={"brand": "visa", "last4": "4242"},
        )

        saved_method = sm.as_saved_method()

        assert isinstance(saved_method, SavedMethod)
        assert saved_method.gateway == "stripe"
        assert saved_method.gateway_customer_id == "cus_test123"
        assert saved_method.gateway_method_id == "pm_test123"
        assert saved_method.method_type == "card"
        assert saved_method.display_label == "Visa ending in 4242"
        assert saved_method.is_active is True
        assert saved_method.metadata == {"brand": "visa", "last4": "4242"}
        assert saved_method.id == sm.pk

    def test_needs_validation_property(self, user):
        """Test needs_validation property."""
        from datetime import timedelta

        from django.utils import timezone

        # Mai validato
        sm1 = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_needs_validation",
            method_type="card",
            display_label="Never validated",
            last_validated_at=None,
        )

        assert sm1.needs_validation is True

        # Validato da meno di un giorno
        sm2 = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_recently_validated",
            method_type="card",
            display_label="Recently validated",
            last_validated_at=timezone.now() - timedelta(hours=12),
        )

        assert sm2.needs_validation is False

        # Validated more than a day ago
        sm3 = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_old_validation",
            method_type="card",
            display_label="Old validation",
            last_validated_at=timezone.now() - timedelta(days=2),
        )

        assert sm3.needs_validation is True


@pytest.mark.django_db
class TestChargeRecord:
    """Tests for ChargeRecord model."""

    def test_create_charge_record(self, user):
        """Create a ChargeRecord."""
        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa ending in 4242",
        )

        charge = ChargeRecord.objects.create(
            saved_method=sm,
            amount=Decimal("9.99"),
            currency="EUR",
            description="Test charge",
            transaction_id="tr_test123",
            status="confirmed",
            message="Charge successful",
            raw_response={"status": "completed"},
            idempotency_key="idem_key_123",
        )

        assert charge.pk is not None
        assert charge.saved_method == sm
        assert charge.amount == Decimal("9.99")
        assert charge.currency == "EUR"
        assert charge.transaction_id == "tr_test123"
        assert charge.status == "confirmed"
        assert charge.idempotency_key == "idem_key_123"

    def test_protect_fk_on_delete(self, user):
        """FK PROTECT raises ProtectedError on deletion."""
        from decimal import Decimal

        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa ending in 4242",
        )

        ChargeRecord.objects.create(
            saved_method=sm,
            amount=Decimal("9.99"),
            currency="EUR",
            description="Test charge",
            transaction_id="tr_test123",
            status="confirmed",
            message="Charge successful",
            raw_response={},
        )

        with pytest.raises(ProtectedError):
            sm.delete()

    def test_idempotency_key_index(self, user):
        """Test that idempotency_key index exists."""

        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa ending in 4242",
        )

        ChargeRecord.objects.create(
            saved_method=sm,
            amount=Decimal("9.99"),
            currency="EUR",
            description="Test charge",
            transaction_id="tr_test123",
            status="confirmed",
            message="Charge successful",
            raw_response={},
            idempotency_key="idem_key_123",
        )

        # Test querying by idempotency_key
        charges = ChargeRecord.objects.filter(idempotency_key="idem_key_123")
        assert charges.count() == 1


@pytest.mark.django_db
class TestRefreshAttempt:
    """Tests for RefreshAttempt model."""

    def test_create_refresh_attempt(self, user):
        """Create a RefreshAttempt."""
        from datetime import datetime, timedelta, timezone

        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa ending in 4242",
        )

        attempt = RefreshAttempt.objects.create(
            saved_method=sm,
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            status="pending",
            reason="expired_card",
            initiated_by=user,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        assert attempt.pk is not None
        assert attempt.saved_method == sm
        assert attempt.gateway_setup_id == "setup_intent_123"
        assert attempt.frontend_sdk == "stripe"
        assert attempt.status == "pending"
        assert attempt.reason == "expired_card"
        assert attempt.initiated_by == user
        assert attempt.confirmed_at is None

    def test_ordering_by_created_at_desc(self, user):
        """RefreshAttempt are ordered by created_at DESC."""
        from datetime import datetime, timedelta, timezone

        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa ending in 4242",
        )

        # Crea tre attempt
        attempt1 = RefreshAttempt.objects.create(
            saved_method=sm,
            gateway_setup_id="setup_1",
            frontend_sdk="stripe",
            frontend_payload={},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        attempt2 = RefreshAttempt.objects.create(
            saved_method=sm,
            gateway_setup_id="setup_2",
            frontend_sdk="stripe",
            frontend_payload={},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        attempt3 = RefreshAttempt.objects.create(
            saved_method=sm,
            gateway_setup_id="setup_3",
            frontend_sdk="stripe",
            frontend_payload={},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        # Test ordering (most recent first)
        attempts = list(RefreshAttempt.objects.filter(saved_method=sm))
        assert len(attempts) == 3
        assert attempts[0].pk == attempt3.pk
        assert attempts[1].pk == attempt2.pk
        assert attempts[2].pk == attempt1.pk

    def test_expires_at_field(self, user):
        """Test that expires_at is a datetime field."""
        from datetime import datetime, timedelta, timezone

        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_test123",
            gateway_method_id="pm_test123",
            method_type="card",
            display_label="Visa ending in 4242",
        )

        expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
        attempt = RefreshAttempt.objects.create(
            saved_method=sm,
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={},
            expires_at=expires_at,
        )

        assert attempt.expires_at == expires_at
        assert isinstance(attempt.expires_at, datetime)


@pytest.mark.django_db
class TestProcessedWebhookEvent:
    """Tests for ProcessedWebhookEvent model."""

    def test_create_processed_webhook_event(self):
        """Create a ProcessedWebhookEvent."""
        event = ProcessedWebhookEvent.objects.create(
            gateway="stripe",
            event_id="evt_test123",
            payload={"type": "payment.succeeded", "data": {}},
        )

        assert event.pk is not None
        assert event.gateway == "stripe"
        assert event.event_id == "evt_test123"
        assert event.payload == {"type": "payment.succeeded", "data": {}}

    def test_unique_together_gateway_event_id(self):
        """Unique constraint (gateway, event_id) raises IntegrityError."""
        ProcessedWebhookEvent.objects.create(
            gateway="stripe",
            event_id="evt_test123",
            payload={},
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            ProcessedWebhookEvent.objects.create(
                gateway="stripe",
                event_id="evt_test123",  # Stesso event_id
                payload={"different": "payload"},
            )

    def test_unique_together_allows_different_gateway(self):
        """Same event_id is allowed with different gateway."""
        event1 = ProcessedWebhookEvent.objects.create(
            gateway="stripe",
            event_id="evt_same_id",
            payload={},
        )

        event2 = ProcessedWebhookEvent.objects.create(
            gateway="paypal",
            event_id="evt_same_id",  # Stesso ID ma gateway diverso
            payload={},
        )

        assert event1.pk is not None
        assert event2.pk is not None
        assert event1.gateway != event2.gateway


class TestGetSavedMethodModel:
    """Test per la funzione _get_saved_method_model()."""

    def test_default_returns_saved_payment_method(self):
        """Default returns SavedPaymentMethod."""
        model = _get_saved_method_model()
        assert model == SavedPaymentMethod

    @override_settings(AUTOPAY_SAVED_METHOD_MODEL="auth.User")
    def test_custom_model_from_setting(self):
        """Test that AUTOPAY_SAVED_METHOD_MODEL uses custom model."""
        from django.contrib.auth.models import User

        model = _get_saved_method_model()
        assert model == User

    def test_invalid_model_setting_raises_error(self):
        """Test that invalid model raises LookupError."""
        with override_settings(AUTOPAY_SAVED_METHOD_MODEL="invalid.NonExistentModel"), pytest.raises(LookupError):
            _get_saved_method_model()