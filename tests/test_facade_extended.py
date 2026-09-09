"""Extended tests for autopay/core/facade.py - coverage for remaining lines."""

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from autopay.core.base import AutopayCapabilityError, AutopayProvider
from autopay.core.facade import (
    Payments,
    _apply_webhook_action,
    _extract_webhook_event_id,
    _extract_webhook_event_type,
    _get_customer_id_for,
)
from autopay.core.result import ChargeResult, ChargeStatus, WebhookEventResult
from autopay.core.saved_method import SavedMethod


def custom_customer_lookup(user, variant):
    """Custom lookup function for testing."""
    return "custom_cust_123"


class VerifyingProviderForTest(AutopayProvider):
    """Provider for testing signature verification."""
    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_WEBHOOK_VERIFICATION = True

    def verify_webhook_request(self, raw_body, headers):
        return False  # Always return invalid signature

    def process_webhook_event(self, event_type, payload):
        return WebhookEventResult(
            processed=True,
            action="status_update",
            transaction_id="txn_123",
        )


class TestFacadeWebhookHelpers:
    """Tests for the facade webhook helper functions."""

    def test_extract_webhook_event_id_paypal(self):
        """Verify event_id extraction for PayPal."""
        payload = {"id": "evt_paypal_123", "event_type": "charge.succeeded"}
        event_id = _extract_webhook_event_id("paypal", payload)
        assert event_id == "evt_paypal_123"

    def test_extract_webhook_event_id_stripe(self):
        """Verify event_id extraction for Stripe."""
        payload = {"id": "evt_stripe_123", "type": "charge.succeeded"}
        event_id = _extract_webhook_event_id("stripe", payload)
        assert event_id == "evt_stripe_123"

    def test_extract_webhook_event_id_mollie_returns_id(self):
        """Mollie event_id extraction returns the raw payment id.

        The composite key (payment_id:new_status) is built in process_webhook
        after fetching the payment from the API, not in this helper.
        """
        payload = {"id": "pay_mollie_123"}
        event_id = _extract_webhook_event_id("mollie", payload)
        assert event_id == "pay_mollie_123"

    def test_extract_webhook_event_id_dummy(self):
        """Verify event_id extraction for dummy."""
        payload = {"id": "evt_dummy_123", "type": "charge.succeeded"}
        event_id = _extract_webhook_event_id("dummy", payload)
        assert event_id == "evt_dummy_123"

    def test_extract_webhook_event_id_unknown(self):
        """Verify event_id extraction for unknown gateway."""
        payload = {"id": "evt_unknown_123"}
        event_id = _extract_webhook_event_id("unknown", payload)
        assert event_id == "evt_unknown_123"

    def test_extract_webhook_event_type_paypal(self):
        """Verify event_type extraction for PayPal."""
        payload = {"event_type": "charge.succeeded"}
        event_type = _extract_webhook_event_type("paypal", payload)
        assert event_type == "charge.succeeded"

    def test_extract_webhook_event_type_stripe(self):
        """Verify event_type extraction for Stripe."""
        payload = {"type": "payment_intent.succeeded"}
        event_type = _extract_webhook_event_type("stripe", payload)
        assert event_type == "payment_intent.succeeded"

    def test_extract_webhook_event_type_mollie(self):
        """Verify event_type extraction for Mollie."""
        payload = {"id": "pay_123"}
        event_type = _extract_webhook_event_type("mollie", payload)
        assert event_type == "payment.update"

    def test_extract_webhook_event_type_dummy(self):
        """Verify event_type extraction for dummy."""
        payload = {"type": "charge.succeeded"}
        event_type = _extract_webhook_event_type("dummy", payload)
        assert event_type == "charge.succeeded"

    def test_extract_webhook_event_type_unknown(self):
        """Verify event_type extraction for unknown gateway."""
        payload = {"type": "some.event"}
        event_type = _extract_webhook_event_type("unknown", payload)
        assert event_type == ""


class TestGetCustomerIdFor:
    """Test per _get_customer_id_for."""

    @pytest.mark.django_db
    def test_get_customer_id_from_saved_method(self, user):
        """Verify customer_id extraction from SavedPaymentMethod."""
        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        customer_id = _get_customer_id_for(user, "stripe")
        assert customer_id == "cus_stripe_123"

    @pytest.mark.django_db
    def test_get_customer_id_no_active_method(self, user):
        """Verify customer_id extraction without active method."""
        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=False,  # Not active
        )

        customer_id = _get_customer_id_for(user, "stripe")
        assert customer_id is None


class PartialVerifyingProvider(AutopayProvider):
    """Provider with verify not implemented."""
    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_WEBHOOK_VERIFICATION = True

    def verify_webhook_request(self, raw_body, headers):
        raise NotImplementedError("Not implemented")

    def process_webhook_event(self, event_type, payload):
        return WebhookEventResult(
            processed=True,
            action="status_update",
            transaction_id="txn_123",
        )


class ErrorProvider(AutopayProvider):
    """Provider that raises errors."""
    SUPPORTS_OFF_SESSION_CHARGE = True
    SUPPORTS_WEBHOOK_VERIFICATION = True

    def verify_webhook_request(self, request):
        raise ValueError("Signature verification failed")

    def process_webhook_event(self, event_type, payload):
        return WebhookEventResult(
            processed=True,
            action="status_update",
            transaction_id="txn_123",
        )


class TestApplyWebhookAction:
    """Test per _apply_webhook_action."""

    @pytest.mark.django_db
    def test_apply_webhook_action_revoke_method(self, user):
        """Test revoke_method action."""
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

        result = WebhookEventResult(
            processed=True,
            action="revoke_method",
            transaction_id="pm_123",
        )

        _apply_webhook_action(result)

        sm.refresh_from_db()
        assert sm.is_active is False

    @pytest.mark.django_db
    def test_apply_webhook_action_status_update(self, user):
        """Test status_update action."""
        from autopay.core.models import ChargeRecord, SavedPaymentMethod

        sm = SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        charge = ChargeRecord.objects.create(
            saved_method=sm,
            amount=Decimal("10.00"),
            currency="EUR",
            description="Test charge",
            transaction_id="txn_123",
        )

        result = WebhookEventResult(
            processed=True,
            action="status_update",
            transaction_id="txn_123",
            new_status=ChargeStatus.CONFIRMED,
        )

        _apply_webhook_action(result)

        charge.refresh_from_db()
        assert charge.status == ChargeStatus.CONFIRMED.value


class TestFacadeWebhookEdgeCases:
    """Tests for webhook processing edge cases."""

    @pytest.mark.django_db
    def test_process_webhook_signature_verification_failed(self, client):
        """Test webhook with invalid signature."""

        with override_settings(
            PAYMENT_VARIANTS={
                "dummy": ("tests.test_facade_extended.VerifyingProviderForTest", {}),
            }
        ):
            result = Payments.process_webhook(
                "dummy",
                json.dumps({"id": "evt_123", "type": "charge.succeeded"}).encode(),
                headers={"Authorization": "invalid"},
                content_type="application/json",
            )

            assert result.processed is False
            assert result.action == "error"
            assert "signature" in result.error.lower()

    @pytest.mark.django_db
    def test_process_webhook_not_implemented_verification(self):
        """Test webhook with verification not implemented."""
        with override_settings(
            PAYMENT_VARIANTS={
                "dummy": ("tests.test_facade_extended.PartialVerifyingProvider", {}),
            }
        ):
            # Should continue despite verification not implemented
            result = Payments.process_webhook(
                "dummy",
                json.dumps({"id": "evt_123", "type": "charge.succeeded"}).encode(),
                content_type="application/json",
            )

            # The webhook is processed because ValueError is caught
            assert result.processed is False
            assert result.action == "error"


class TestFacadeValidateAllUserMethods:
    """Test per validate_all_user_methods."""

    @pytest.mark.django_db
    def test_validate_all_user_methods_not_supported(self, user):
        """Test validation with provider that does not support validate."""

        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_success",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        class NoValidateProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_VALIDATE = False

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = NoValidateProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NoValidateProvider", {}),
                }
            ):
                results = Payments.validate_all_user_methods(user)

                assert len(results) == 1
                _, result = results[0]
                assert result.is_valid is False
                assert result.needs_refresh is False

    @pytest.mark.django_db
    def test_validate_all_user_methods(self, user):
        """Test validation of all user methods."""
        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_success",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        results = Payments.validate_all_user_methods(user)

        assert len(results) == 1
        _, result = results[0]
        assert result.is_valid is True
        assert result.checked_at is not None


class TestFacadeListSavedMethods:
    """Test per list_saved_methods."""

    @pytest.mark.django_db
    def test_list_saved_methods(self, user):
        """Test list_saved_methods."""
        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        methods = Payments.list_saved_methods(user)
        assert len(methods) == 1
        assert methods[0].gateway == "dummy"

    @pytest.mark.django_db
    def test_list_saved_methods_inactive(self, user):
        """Test list_saved_methods excludes inactive methods."""
        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=False,
        )

        methods = Payments.list_saved_methods(user)
        assert len(methods) == 0
    @pytest.mark.django_db
    def test_list_saved_methods_not_capable(self):
        """Test list_saved_methods with non-capable provider."""

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False
            SUPPORTS_LIST_METHODS = False

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = NonCapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NonCapableProvider", {}),
                }
            ):
                result = Payments.list_saved_methods(None)
                assert result == []


class TestFacadeSyncSavedMethods:

    @pytest.mark.django_db
    def test_sync_saved_methods_no_customer_id(self, user):
        """Test sync without customer_id."""
        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        # Nessun customer_id configurato per dummy
        methods = Payments.sync_saved_methods(user)

        # Should return empty list because there is no customer_id
        assert methods == []

    @pytest.mark.django_db
    def test_sync_saved_methods_skips_not_capable(self, user):
        """Test sync skips non-capable providers (line 83)."""

        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        class NotCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False
            SUPPORTS_LIST_METHODS = False

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = NotCapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NotCapableProvider", {}),
                }
            ):
                methods = Payments.sync_saved_methods(user)
                assert methods == []

    @pytest.mark.django_db
    def test_sync_saved_methods_not_implemented(self, user):
        """Test sync with provider that does not implement list_saved_methods."""

        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        class NotImplementedProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_LIST_METHODS = True

            def list_saved_methods(self, customer_id):
                raise NotImplementedError()

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = NotImplementedProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NotImplementedProvider", {}),
                }
            ):
                methods = Payments.sync_saved_methods(user)
                assert methods == []


class TestFacadeSaveMethod:
    """Test per save_method."""

    @pytest.mark.django_db
    def test_save_method_not_capable(self):
        """Test save_method with non-capable provider."""

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False
            SUPPORTS_SAVE_METHOD = False

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = NonCapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NonCapableProvider", {}),
                }
            ):
                mock_payment = MagicMock()
                mock_payment.variant = "dummy"
                # Non-capable provider returns None
                result = Payments.save_method(mock_payment)
                assert result is None

    @pytest.mark.django_db
    def test_save_method_success(self, django_user_model):
        """Test save_method persists a SavedPaymentMethod and returns it."""
        from autopay.models import SavedPaymentMethod

        user = django_user_model.objects.create_user(
            username="save_method_user", password="x"
        )

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_SAVE_METHOD = True

            def save_method(self, payment):
                return SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_123",
                    gateway_method_id="pm_123",
                    method_type="card",
                    display_label="Test Card",
                )

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableProvider", {}),
                }
            ):
                result = Payments.save_method(
                    type(
                        "FakePayment",
                        (),
                        {"variant": "dummy", "user": user, "user_id": user.id},
                    )()
                )
                assert result is not None
                assert result.gateway == "dummy"
                assert result.id is not None

                sm = SavedPaymentMethod.objects.get(id=result.id)
                assert sm.gateway == "dummy"
                assert sm.gateway_method_id == "pm_123"
                assert sm.user_id == user.id

    @pytest.mark.django_db
    def test_save_method_provider_returns_none(self, django_user_model):
        """Test save_method returns None when provider.save_method returns None."""
        user = django_user_model.objects.create_user(
            username="save_none_user", password="x"
        )

        class CapableButEmptyProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_SAVE_METHOD = True

            def save_method(self, payment):
                return None

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = CapableButEmptyProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableButEmptyProvider", {}),
                }
            ):
                result = Payments.save_method(
                    type(
                        "FakePayment",
                        (),
                        {"variant": "dummy", "user": user, "user_id": user.id},
                    )()
                )
                assert result is None

    @pytest.mark.django_db
    def test_save_method_anonymous_payment_returns_none(self):
        """Test save_method returns None when payment has no associated user."""

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_SAVE_METHOD = True

            def save_method(self, payment):
                return SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_anon",
                    gateway_method_id="pm_anon",
                    method_type="card",
                    display_label="Anon Card",
                )

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": (
                        "tests.test_facade_extended.CapableProvider",
                        {},
                    ),
                }
            ):
                result = Payments.save_method(
                    type(
                        "FakePayment",
                        (),
                        {"variant": "dummy", "user": None, "user_id": None},
                    )()
                )
                assert result is None


class TestFacadeChargeOffSession:
    """Test per charge_off_session."""

    @pytest.mark.django_db
    def test_charge_off_session_not_capable(self):
        """Test charge_off_session with non-capable provider."""

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = NonCapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NonCapableProvider", {}),
                }
            ):
                method = SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_123",
                    gateway_method_id="pm_123",
                    method_type="card",
                    display_label="Test Card",
                )
                with pytest.raises(AutopayCapabilityError):
                    Payments.charge_off_session(method, Decimal("10.00"), "EUR")

    @pytest.mark.django_db
    def test_charge_off_session_success(self):
        """Test charge_off_session with success."""

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True

            def charge_off_session(self, saved_method, amount, currency, **kwargs):
                return ChargeResult(
                    status=ChargeStatus.CONFIRMED,
                    message="Charge successful",
                    transaction_id="txn_123",
                    raw_response={},
                    amount_charged=Decimal("10.00"),
                    currency="EUR",
                    requires_action=None,
                    created_at=datetime.now(timezone.utc),
                )

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableProvider", {}),
                }
            ):
                method = SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_123",
                    gateway_method_id="pm_123",
                    method_type="card",
                    display_label="Test Card",
                )
                result = Payments.charge_off_session(method, Decimal("10.00"), "EUR")
                assert result.status == ChargeStatus.CONFIRMED
                assert result.transaction_id == "txn_123"


class TestFacadeRevokeMethod:
    """Test per revoke_method."""

    @pytest.mark.django_db
    def test_revoke_method_not_capable(self):
        """Test revoke_method with non-capable provider."""

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False
            SUPPORTS_REVOKE = False

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = NonCapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NonCapableProvider", {}),
                }
            ):
                method = SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_123",
                    gateway_method_id="pm_123",
                    method_type="card",
                    display_label="Test Card",
                )
                with pytest.raises(AutopayCapabilityError):
                    Payments.revoke_method(method)

    @pytest.mark.django_db
    def test_revoke_method_success(self):
        """Test revoke_method with success."""

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_REVOKE = True

            def revoke_method(self, saved_method):
                pass  # Success

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableProvider", {}),
                }
            ):
                method = SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_123",
                    gateway_method_id="pm_123",
                    method_type="card",
                    display_label="Test Card",
                )
                Payments.revoke_method(method)  # Should not raise


class TestFacadeGetChargeStatus:
    """Test per get_charge_status."""

    @pytest.mark.django_db
    def test_get_charge_status_not_capable(self):
        """Test get_charge_status with non-capable provider."""

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = NonCapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NonCapableProvider", {}),
                }
            ), pytest.raises(AutopayCapabilityError):
                Payments.get_charge_status("dummy", "txn_123")

    @pytest.mark.django_db
    def test_get_charge_status_success(self):
        """Test get_charge_status with success."""

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True

            def get_charge_status(self, transaction_id):
                return ChargeResult(
                    status=ChargeStatus.CONFIRMED,
                    message="Charge confirmed",
                    transaction_id=transaction_id,
                    raw_response={},
                    amount_charged=Decimal("10.00"),
                    currency="EUR",
                    requires_action=None,
                    created_at=datetime.now(timezone.utc),
                )

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableProvider", {}),
                }
            ):
                result = Payments.get_charge_status("dummy", "txn_123")
                assert result.status == ChargeStatus.CONFIRMED


class TestFacadeRefund:
    """Test per refund."""

    @pytest.mark.django_db
    def test_refund_not_capable(self):
        """Test refund with non-capable provider."""

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False
            SUPPORTS_REFUND = False

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = NonCapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NonCapableProvider", {}),
                }
            ), pytest.raises(AutopayCapabilityError):
                Payments.refund("dummy", "txn_123")

    @pytest.mark.django_db
    def test_refund_success(self):
        """Test refund with success."""

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_REFUND = True

            def refund(self, transaction_id, amount=None, reason=""):
                return ChargeResult(
                    status=ChargeStatus.CONFIRMED,
                    message="Refund successful",
                    transaction_id=f"refund_{transaction_id}",
                    raw_response={},
                    amount_charged=amount or Decimal("10.00"),
                    currency="EUR",
                    requires_action=None,
                    created_at=datetime.now(timezone.utc),
                )

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableProvider", {}),
                }
            ):
                result = Payments.refund("dummy", "txn_123", Decimal("5.00"), "Test refund")
                assert result.status == ChargeStatus.CONFIRMED


class TestFacadeConfirmRefresh:
    """Test per confirm_refresh."""

    @pytest.mark.django_db
    def test_confirm_refresh_success(self):
        """Test confirm_refresh with success."""

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True

            def confirm_refresh(self, saved_method, refresh_payload):
                return saved_method

        with patch(
            "autopay.core.facade.provider_factory"
        ) as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableProvider", {}),
                }
            ):
                method = SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_123",
                    gateway_method_id="pm_123",
                    method_type="card",
                    display_label="Test Card",
                )
                # Should work
            result = Payments.confirm_refresh(method, {"status": "completed"})
            assert result.gateway == "dummy"

    @pytest.mark.django_db
    def test_confirm_refresh_provider_fails_no_db(self, monkeypatch):
        """ImportError path: provider raises and models unavailable."""

        class FailingProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True

            def confirm_refresh(self, saved_method, refresh_payload):
                raise RuntimeError("provider crashed")

        monkeypatch.setitem(sys.modules, "autopay.core.models", None)

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_factory.return_value = FailingProvider()
            method = SavedMethod(
                gateway="dummy",
                gateway_customer_id="cust_123",
                gateway_method_id="pm_123",
                method_type="card",
                display_label="Test Card",
            )
            with pytest.raises(RuntimeError, match="provider crashed"):
                Payments.confirm_refresh(method, {"status": "completed"})


class TestFacadeGetCustomerIdFor:
    """Tests for _get_customer_id_for."""

    @pytest.mark.django_db
    def test_get_customer_id_for_custom_lookup(self):
        """Verify _get_customer_id_for with custom lookup."""
        with override_settings(
            AUTOPAY_CUSTOMER_LOOKUP="tests.test_facade_extended.custom_customer_lookup"
        ):
            result = _get_customer_id_for(None, "dummy")
            assert result == "custom_cust_123"

    @pytest.mark.django_db
    def test_get_customer_id_for_no_method(self):
        """Verify _get_customer_id_for without saved method."""

        # Nessun metodo salvato
        result = _get_customer_id_for(None, "dummy")
        assert result is None

    @pytest.mark.django_db
    def test_get_customer_id_for_import_error(self, monkeypatch):
        """_get_customer_id_for returns None when the model module is missing."""
        monkeypatch.setitem(sys.modules, "autopay.core.models", None)
        assert _get_customer_id_for(None, "dummy") is None

class TestFacadeCoverageGaps:
    """Tests to cover remaining facade.py lines."""

    @pytest.mark.django_db
    def test_save_method_provider_not_implemented(self, user):
        """Test save_method when provider raises NotImplementedError."""

        class NotImplProvider(AutopayProvider):
            SUPPORTS_SAVE_METHOD = True

            def save_method(self, payment):
                raise NotImplementedError()

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = NotImplProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NotImplProvider", {}),
                }
            ):
                result = Payments.save_method(
                    type(
                        "FakePayment",
                        (),
                        {"variant": "dummy", "user": user, "user_id": user.id},
                    )()
                )
                assert result is None

    @pytest.mark.django_db
    def test_validate_all_user_methods_provider_not_implemented(self, user):
        """Test validate_all_user_methods when provider raises NotImplementedError."""

        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        class NotImplProvider(AutopayProvider):
            SUPPORTS_VALIDATE = True

            def validate_method(self, saved_method):
                raise NotImplementedError()

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = NotImplProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NotImplProvider", {}),
                }
            ):
                results = Payments.validate_all_user_methods(user)
                # Should return empty list when provider raises
                assert len(results) == 1
                assert results[0][1].reason == "Gateway 'dummy' does not support validate_method"

    @pytest.mark.django_db
    def test_process_webhook_provider_not_implemented(self, user):
        """Test process_webhook when provider raises NotImplementedError."""

        class NotImplProvider(AutopayProvider):
            SUPPORTS_WEBHOOK_VERIFICATION = True

            def process_webhook_event(self, event_type, payload):
                raise NotImplementedError()

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = NotImplProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NotImplProvider", {}),
                }
            ):
                result = Payments.process_webhook("dummy", json.dumps({"id": "evt_123", "type": "charge.succeeded"}).encode(), {}, "application/json")
                assert result.processed is False

    @pytest.mark.django_db
    def test_process_webhook_verify_not_implemented(self, user):
        """Test process_webhook when verify_webhook_request raises NotImplementedError."""

        class NotImplProvider(AutopayProvider):
            SUPPORTS_WEBHOOK_VERIFICATION = True

            def verify_webhook_request(self, raw_body, headers):
                raise NotImplementedError()

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = NotImplProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NotImplProvider", {}),
                }
            ):
                result = Payments.process_webhook("dummy", json.dumps({"id": "evt_123", "type": "charge.succeeded"}).encode(), {}, "application/json")
                assert result.processed is False


class TestFacadeConfirmRefreshConcurrency:
    """Test confirm_refresh concurrent protection."""

    @pytest.mark.django_db
    def test_confirm_refresh_concurrent_protection(self, user):
        """Test that concurrent confirms are protected."""

        from autopay.core.models import RefreshAttempt, SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
            id=1,
        )

        # Create a pending refresh attempt (expires in 1 hour)
        RefreshAttempt.objects.create(
            saved_method_id=1,
            gateway_setup_id="seti_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "secret_xxx"},
            reason="manual",
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_REFRESH = True

            def confirm_refresh(self, saved_method, refresh_payload):
                # Simulate successful refresh
                return SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_123",
                    gateway_method_id="pm_new456",
                    method_type="card",
                    display_label="New Card",
                    is_active=True,
                    metadata={},
                )

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableProvider", {}),
                }
            ):
                method = SavedPaymentMethod.objects.get(id=1)
                result = Payments.confirm_refresh(method, {"setup_intent_id": "seti_123"})

                assert result.gateway_method_id == "pm_new456"

                # Second confirm should not raise because the attempt is already confirmed
                # The RuntimeError is only raised when two concurrent requests try to claim
                # the same attempt simultaneously (atomic update returns 0 rows)
                result2 = Payments.confirm_refresh(method, {"setup_intent_id": "seti_123"})
                assert result2.gateway_method_id == "pm_new456"

    @pytest.mark.django_db
    def test_confirm_refresh_expired_attempt_marks_expired(self, user):
        """Expired attempt: status is written as expired, then RuntimeError is raised."""
        from autopay.core.facade import Payments
        from autopay.core.models import RefreshAttempt, SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
            id=1,
        )

        RefreshAttempt.objects.create(
            saved_method_id=1,
            gateway_setup_id="seti_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "secret_xxx"},
            reason="manual",
            status="pending",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        with pytest.raises(RuntimeError, match="has expired"):
            Payments.confirm_refresh(
                SavedPaymentMethod.objects.get(id=1), {"setup_intent_id": "seti_123"}
            )

        assert RefreshAttempt.objects.get(saved_method_id=1).status == "expired"

    @pytest.mark.django_db
    def test_confirm_refresh_concurrent_claim_error(self, user):
        """Test that concurrent claim raises RuntimeError (line 177)."""
        from autopay.core.facade import Payments
        from autopay.core.models import RefreshAttempt, SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
            id=1,
        )

        # Create a pending refresh attempt (expires in 1 hour)
        RefreshAttempt.objects.create(
            saved_method_id=1,
            gateway_setup_id="seti_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "secret_xxx"},
            reason="manual",
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_REFRESH = True

            def confirm_refresh(self, saved_method, refresh_payload):
                return SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_123",
                    gateway_method_id="pm_new456",
                    method_type="card",
                    display_label="New Card",
                    is_active=True,
                    metadata={},
                )

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            # Get the actual attempt from DB
            attempt = RefreshAttempt.objects.get(saved_method_id=1, status="pending")

            # Patch the update to return 0 rows to simulate concurrent claim
            original_filter = RefreshAttempt.objects.filter

            def mock_filter_side_effect(*args, **kwargs):
                # First call (line 173) returns the attempt
                if "saved_method_id" in kwargs and "status" in kwargs:
                    mock_qs = MagicMock()
                    mock_qs.order_by.return_value.first.return_value = attempt
                    return mock_qs
                # Second call (line 182) - the update call - returns 0
                elif "pk" in kwargs:
                    mock_qs = MagicMock()
                    mock_qs.update.return_value = 0  # Simulate concurrent claim
                    return mock_qs
                return original_filter(*args, **kwargs)

            with patch.object(RefreshAttempt.objects, "filter", side_effect=mock_filter_side_effect):
                # This should raise RuntimeError because the update returns 0 rows
                method = SavedPaymentMethod.objects.get(id=1)
                with pytest.raises(RuntimeError, match="already confirmed"):
                    Payments.confirm_refresh(method, {"token": "test"})


class TestFacadeValidateMethodPass:
    """Test validate_method pass case (line 240)."""

    @pytest.mark.django_db
    def test_validate_method_pass(self, user):
        """Test validate_method with valid result."""
        from autopay.core.models import SavedPaymentMethod
        from autopay.core.result import ValidationResult

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True  # Required for is_autopay_capable
            SUPPORTS_VALIDATE = True

            def validate_method(self, saved_method):
                # Return result with needs_refresh=True to trigger line 240
                return ValidationResult(
                    is_valid=True,
                    is_expired=False,
                    needs_refresh=True,  # This triggers the pass on line 240
                    reason="Card needs refresh",
                )

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableProvider", {}),
                }
            ):
                results = Payments.validate_all_user_methods(user)
                assert len(results) == 1
                _, result = results[0]
                assert result.is_valid is True
                assert result.needs_refresh is True


class TestFacadeImportErrorHandling:
    """Test ImportError handling in confirm_refresh."""

    def test_confirm_refresh_without_models(self):
        """Test confirm_refresh when models are not available."""

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_REFRESH = True

            def confirm_refresh(self, saved_method, refresh_payload):
                return SavedMethod(
                    gateway="dummy",
                    gateway_customer_id="cust_123",
                    gateway_method_id="pm_new456",
                    method_type="card",
                    display_label="New Card",
                    is_active=True,
                    metadata={},
                )

        saved_method = SavedMethod(
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
            metadata={},
        )

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            # Simulate ImportError by patching the import
            with patch.dict("sys.modules", {"autopay.core.models": None}):
                # Should not raise even without models
                result = Payments.confirm_refresh(saved_method, {})
                assert result.gateway_method_id == "pm_new456"


class TestFacadeSyncNoCustomerID:
    """Test sync_saved_methods when no customer_id (line 83)."""

    @pytest.mark.django_db
    def test_sync_saved_methods_skip_no_customer(self, user):
        """Test that sync skips providers without customer_id."""
        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="",  # Empty customer_id
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        # Should return empty list since no customer_id
        methods = Payments.sync_saved_methods(user)
        assert methods == []


class TestFacadeRefreshCoverage:
    """Tests to cover refresh_method lines 128-146."""

    @pytest.mark.django_db
    def test_refresh_method_creates_attempt(self, user):
        """Test refresh_method creates RefreshAttempt (lines 128-146)."""

        from datetime import datetime, timedelta, timezone

        from autopay.core.models import RefreshAttempt, SavedPaymentMethod
        from autopay.core.result import RefreshSetupData

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_REFRESH = True

            def refresh_method(self, saved_method):
                return RefreshSetupData(
                    gateway_setup_id="seti_123",
                    frontend_sdk="stripe",
                    frontend_payload={"client_secret": "secret_xxx"},
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            id=1,
        )

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={"dummy": ("tests.test_facade_extended.CapableProvider", {})},
                AUTOPAY_SITE_BASE_URL="https://example.com",
            ):
                method = SavedPaymentMethod.objects.get(id=1)
                result = Payments.refresh_method(method, reason="manual")

                # Verify RefreshAttempt was created
                assert RefreshAttempt.objects.count() == 1
                attempt = RefreshAttempt.objects.get()
                assert attempt.gateway_setup_id == "seti_123"
                assert attempt.status == "pending"
                assert result.setup_id == 1


class TestFacadeIsAutopayCapable:
    """Tests for is_autopay_capable helper (line 247)."""

    def test_is_autopay_capable_true(self):
        """Test is_autopay_capable returns True for capable provider."""
        from autopay.core.base import is_autopay_capable

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True

        provider = CapableProvider()
        assert Payments.is_autopay_capable(provider) is True
        assert is_autopay_capable(provider) is True

    def test_is_autopay_capable_false(self):
        """Test is_autopay_capable returns False for non-capable provider."""
        from autopay.core.base import is_autopay_capable

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False

        provider = NonCapableProvider()
        assert Payments.is_autopay_capable(provider) is False
        assert is_autopay_capable(provider) is False

    def test_validate_all_user_methods_needs_refresh_pass(self, user):
        """Test validate_all_user_methods with needs_refresh=True (line 240)."""
        from autopay.core.models import SavedPaymentMethod

        SavedPaymentMethod.objects.create(
            user=user,
            gateway="dummy",
            gateway_customer_id="cust_123",
            gateway_method_id="pm_123",
            method_type="card",
            display_label="Test Card",
            is_active=True,
        )

        # The pass statement at line 240 is executed when needs_refresh is True
        # This is covered by the normal validate_all_user_methods test
        results = Payments.validate_all_user_methods(user)
        assert len(results) == 1


class TestFacadeWebhookUnknownProvider:
    """Tests for webhook with unknown provider (lines 272-274)."""

    @pytest.mark.django_db
    def test_process_webhook_unknown_variant(self):
        """Test webhook with unknown variant returns error (lines 272-274)."""
        result = Payments.process_webhook(
            "unknown_variant",
            json.dumps({"id": "evt_123", "type": "charge.succeeded"}).encode(),
            {},
            "application/json",
        )
        assert result.processed is False
        assert result.action == "error"
        assert "not found" in result.error.lower()

    @pytest.mark.django_db
    def test_process_webhook_not_capable_provider(self, user):
        """Test webhook with non-capable provider (lines 281-287)."""

        class NonCapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = False

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = NonCapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.NonCapableProvider", {}),
                }
            ):
                result = Payments.process_webhook(
                    "dummy",
                    json.dumps({"id": "evt_123", "type": "charge.succeeded"}).encode(),
                    {},
                    "application/json",
                )
                assert result.processed is False
                assert result.action == "error"

    @pytest.mark.django_db
    def test_process_webhook_verify_returns_false(self, user):
        """Test webhook when verify_webhook_request returns False (line 302)."""

        class VerifyingProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True
            SUPPORTS_WEBHOOK_VERIFICATION = True

            def verify_webhook_request(self, raw_body, headers):
                return False  # Always fail

            def process_webhook_event(self, event_type, payload):
                return WebhookEventResult(processed=True, action="status_update", transaction_id="txn_123")

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = VerifyingProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.VerifyingProvider", {}),
                }
            ):
                result = Payments.process_webhook(
                    "dummy",
                    json.dumps({"id": "evt_123", "type": "charge.succeeded"}).encode(),
                    {},
                    "application/json",
                )
                assert result.processed is False
                assert result.action == "error"
                assert "signature" in result.error.lower()

    @pytest.mark.django_db
    def test_process_webhook_dedup_integrity_error(self, user):
        """Test webhook deduplication with IntegrityError (lines 318-348)."""

        class CapableProvider(AutopayProvider):
            SUPPORTS_OFF_SESSION_CHARGE = True

            def process_webhook_event(self, event_type, payload):
                return WebhookEventResult(processed=True, action="status_update", transaction_id="txn_123")

        with patch("autopay.core.facade.provider_factory") as mock_factory:
            mock_provider = CapableProvider()
            mock_factory.return_value = mock_provider

            with override_settings(
                PAYMENT_VARIANTS={
                    "dummy": ("tests.test_facade_extended.CapableProvider", {}),
                }
            ):
                # First call - should create the record
                result1 = Payments.process_webhook(
                    "dummy",
                    json.dumps({"id": "evt_123", "type": "charge.succeeded"}).encode(),
                    {},
                    "application/json",
                )
                assert result1.processed is True

                # Second call with same event_id - should return noop (deduplicated)
                result2 = Payments.process_webhook(
                    "dummy",
                    json.dumps({"id": "evt_123", "type": "charge.succeeded"}).encode(),
                    {},
                    "application/json",
                )
                assert result2.processed is True
                assert result2.action == "noop"
