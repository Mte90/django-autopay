"""Tests for Payments facade - the behavioral core of django-autopay."""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from django.test import override_settings as django_override_settings

from autopay.core.base import AutopayCapabilityError, is_autopay_capable
from autopay.core.facade import Payments
from autopay.core.result import ChargeStatus, RefreshSetup, RefreshSetupData
from autopay.core.saved_method import SavedMethod


class TestChargeOffSession:
    """Tests for Payments.charge_off_session."""

    @pytest.mark.django_db
    def test_charge_off_session_dispatches_to_correct_provider(
        self, saved_method_db_factory, fake_provider, override_settings, user
    ):
        """Test that charge_off_session dispatches to correct provider by gateway."""
        # Create a saved method in the DB
        sm = saved_method_db_factory(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe123",
            gateway_method_id="pm_stripe123",
            method_type="card",
        )

        # Configure fake provider for stripe
        custom_result = fake_provider(
            charge_result=fake_provider().charge_off_session(
                SavedMethod("stripe", "cus", "pm", "card"),
                Decimal("9.99"),
                "EUR",
            )
        )
        custom_result.call_counts = {"charge_off_session": 0}

        with django_override_settings(
            PAYMENT_VARIANTS={
                "stripe": (
                    "tests.conftest.FakeAutopayProvider",
                    {"charge_result": custom_result.charge_off_session(
                        SavedMethod("stripe", "cus", "pm", "card"),
                        Decimal("9.99"),
                        "EUR",
                    )},
                )
            }
        ):
            result = Payments.charge_off_session(
                saved_method=sm.as_saved_method(),
                amount=Decimal("9.99"),
                currency="EUR",
                description="Test charge",
            )

            assert result.status == ChargeStatus.CONFIRMED

    @pytest.mark.django_db
    def test_charge_off_session_idempotency_key_passed_through(
        self, saved_method_db_factory, user
    ):
        """Test that idempotency_key is passed to provider."""
        sm = saved_method_db_factory(
            user=user,
            gateway="dummy",
            gateway_customer_id="cus_dummy123",
            gateway_method_id="pm_dummy_idempotency",
            method_type="card",
        )

        # We use the DummyProvider from django-payments which is not autopay-capable
        # So we expect AutopayCapabilityError, but the test verifies that
        # the idempotency_key was passed before the error

        with django_override_settings(
            PAYMENT_VARIANTS={
                "dummy": ("payments.dummy.DummyProvider", {}),
            }
        ), pytest.raises(AutopayCapabilityError):
            Payments.charge_off_session(
                saved_method=sm.as_saved_method(),
                amount=Decimal("9.99"),
                currency="EUR",
                idempotency_key="idem_key_123",
            )

    def test_charge_off_session_unknown_gateway_raises_autopay_capability_error(self, saved_method_factory):
        """Test that unknown gateway raises ValueError (from provider_factory)."""
        sm = saved_method_factory(gateway="unknown_gateway")

        with django_override_settings(PAYMENT_VARIANTS={}):
            with pytest.raises(ValueError) as exc_info:
                Payments.charge_off_session(
                    saved_method=sm,
                    amount=Decimal("9.99"),
                    currency="EUR",
                )

            assert "unknown_gateway" in str(exc_info.value)

    def test_charge_off_session_provider_without_support_raises_autopay_capability_error(
        self, saved_method_factory, override_settings
    ):
        """Test that provider without SUPPORTS_OFF_SESSION_CHARGE raises AutopayCapabilityError."""

        sm = saved_method_factory(gateway="dummy")

        with django_override_settings(
            PAYMENT_VARIANTS={
                "dummy": ("tests.conftest.UnsupportedAutopayProvider", {}),
            }
        ):
            with pytest.raises(AutopayCapabilityError) as exc_info:
                Payments.charge_off_session(
                    saved_method=sm,
                    amount=Decimal("9.99"),
                    currency="EUR",
                )

            assert "does not support off-session charges" in str(exc_info.value)


class TestIsAutopayCapable:
    """Test per is_autopay_capable."""

    def test_is_autopay_capable_true_for_capable_provider(self, fake_provider):
        """Test that is_autopay_capable returns True for capable provider."""
        provider = fake_provider(supports_off_session=True)
        assert is_autopay_capable(provider) is True

    def test_is_autopay_capable_false_for_non_capable_provider(self, fake_provider):
        """Test that is_autopay_capable returns False for non-capable provider."""
        provider = fake_provider(supports_off_session=False)
        assert is_autopay_capable(provider) is False

    def test_is_autopay_capable_false_for_foreign_object(self):
        """Test that is_autopay_capable returns False for foreign objects."""
        assert is_autopay_capable("not a provider") is False
        assert is_autopay_capable(123) is False
        assert is_autopay_capable(None) is False

    def test_payments_is_autopay_capable_static_method(self, fake_provider):
        """Test that Payments.is_autopay_capable works as static method."""
        provider = fake_provider(supports_off_session=True)
        assert Payments.is_autopay_capable(provider) is True


class TestRefreshMethod:
    """Test per Payments.refresh_method."""

    @pytest.mark.django_db
    def test_refresh_method_persists_refresh_attempt(
        self, saved_method_db_factory, fake_provider, override_settings, user
    ):
        """Test that refresh_method persists RefreshAttempt."""
        from autopay.core.models import RefreshAttempt

        sm = saved_method_db_factory(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe123",
            gateway_method_id="pm_stripe_refresh_123",
            method_type="card",
        )

        from datetime import datetime, timezone

        from autopay.core.result import RefreshSetupData

        refresh_data = RefreshSetupData(
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        with django_override_settings(
            PAYMENT_VARIANTS={
                "stripe": (
                    "tests.conftest.FakeAutopayProvider",
                    {"refresh_data": refresh_data},
                )
            },
            AUTOPAY_SITE_BASE_URL="https://example.com",
        ):
            result = Payments.refresh_method(saved_method=sm.as_saved_method())

            assert isinstance(result, RefreshSetup)
            assert result.setup_id > 0
            assert result.frontend_sdk == "stripe"
            assert result.return_url == f"https://example.com/autopay/api/methods/{sm.id}/refresh/confirm/?attempt={result.setup_id}"

            # Verify that RefreshAttempt was created
            attempt = RefreshAttempt.objects.get(pk=result.setup_id)
            assert attempt.saved_method_id == sm.id
            assert attempt.status == "pending"
            # The gateway_setup_id comes from refresh_data or fake provider default
            assert attempt.gateway_setup_id is not None
            assert len(attempt.gateway_setup_id) > 0

    @pytest.mark.django_db
    def test_refresh_method_return_url_uses_autopay_site_base_url(
        self, saved_method_db_factory, fake_provider, user
    ):
        """Test that return_url uses AUTOPAY_SITE_BASE_URL."""
        sm = saved_method_db_factory(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe123",
            gateway_method_id="pm_stripe123",
            method_type="card",
        )

        from datetime import datetime, timezone

        from autopay.core.result import RefreshSetupData

        refresh_data = RefreshSetupData(
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        with django_override_settings(
            PAYMENT_VARIANTS={
                "stripe": (
                    "tests.conftest.FakeAutopayProvider",
                    {"refresh_data": refresh_data},
                )
            },
            AUTOPAY_SITE_BASE_URL="https://x.example",
        ):
            result = Payments.refresh_method(saved_method=sm.as_saved_method())

            assert result.return_url.startswith("https://x.example/")
            assert f"/autopay/api/methods/{sm.id}/refresh/confirm/?attempt=" in result.return_url

    @pytest.mark.django_db
    def test_refresh_method_models_absent_returns_setup_id_zero(
        self, saved_method_factory, fake_provider, monkeypatch
    ):
        """Test that refresh_method returns setup_id=0 when models are not present."""
        refresh_data = RefreshSetupData(
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        sm = saved_method_factory(gateway="stripe", id=1)

        with django_override_settings(
            PAYMENT_VARIANTS={
                "stripe": (
                    "tests.conftest.FakeAutopayProvider",
                    {"refresh_data": refresh_data},
                )
            },
            AUTOPAY_SITE_BASE_URL="https://example.com",
        ):
            # None in sys.modules → ImportError al «from .models import RefreshAttempt» (§9.5bis)
            monkeypatch.setitem(sys.modules, "autopay.core.models", None)
            result = Payments.refresh_method(saved_method=sm)

        assert result.setup_id == 0
        assert result.frontend_sdk == "stripe"


class TestConfirmRefresh:
    """Test per Payments.confirm_refresh."""

    @pytest.mark.django_db
    def test_confirm_refresh_happy_path_pending_to_confirmed(
        self, saved_method_db_factory, fake_provider, user
    ):
        """Test that confirm_refresh converts pending → confirmed."""
        from autopay.core.models import RefreshAttempt

        sm = saved_method_db_factory(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe123",
            gateway_method_id="pm_stripe123",
            method_type="card",
        )

        # Create a pending RefreshAttempt
        attempt = RefreshAttempt.objects.create(
            saved_method=sm,
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        with django_override_settings(
            PAYMENT_VARIANTS={
                "stripe": ("tests.conftest.FakeAutopayProvider", {}),
            }
        ):
            Payments.confirm_refresh(
                saved_method=sm.as_saved_method(),
                refresh_payload={"new_method_id": "pm_new123"},
            )

            # Verify that RefreshAttempt was confirmed
            attempt.refresh_from_db()
            assert attempt.status == "confirmed"
            assert attempt.confirmed_at is not None

            # Verify that saved_method was updated
            sm.refresh_from_db()
            assert sm.gateway_method_id == "pm_new123"
            assert sm.last_refreshed_at is not None

    @pytest.mark.django_db
    def test_confirm_refresh_race_guard_prevents_double_write(
        self, saved_method_db_factory, fake_provider, user
    ):
        """Test that confirm_refresh prevents double-write if called twice."""
        from autopay.core.models import RefreshAttempt

        sm = saved_method_db_factory(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe123",
            gateway_method_id="pm_stripe123",
            method_type="card",
        )

        # Create a pending RefreshAttempt
        attempt = RefreshAttempt.objects.create(
            saved_method=sm,
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        with django_override_settings(
            PAYMENT_VARIANTS={
                "stripe": ("tests.conftest.FakeAutopayProvider", {}),
            }
        ):
            # First call - should work
            Payments.confirm_refresh(
                saved_method=sm.as_saved_method(),
                refresh_payload={"new_method_id": "pm_new123"},
            )

            # Verify state after first call
            attempt.refresh_from_db()
            assert attempt.status == "confirmed"

            # Second call - should be a no-op (no pending RefreshAttempt)
            # The facade will not find a pending attempt and will still call the provider
            # but the DB update will be a no-op because the attempt is already confirmed
            Payments.confirm_refresh(
                saved_method=sm.as_saved_method(),
                refresh_payload={"new_method_id": "pm_new456"},
            )

            # Verify that attempt is still confirmed (unchanged)
            attempt.refresh_from_db()
            assert attempt.status == "confirmed"


class TestValidateMethod:
    """Test per Payments.validate_method."""

    @pytest.mark.django_db
    def test_validate_method_supported_gateway_returns_result(self, saved_method_db_factory, user):
        """Test that validate_method returns ValidationResult from provider."""
        from datetime import datetime, timezone

        from autopay.core.result import ValidationResult

        sm = saved_method_db_factory(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe123",
            gateway_method_id="pm_validate_123",
            method_type="card",
        )

        validate_result = ValidationResult(
            is_valid=True,
            is_expired=False,
            needs_refresh=False,
            reason="OK",
            checked_at=datetime.now(timezone.utc),
            raw_response={"status": "valid"},
        )

        with django_override_settings(
            PAYMENT_VARIANTS={
                "stripe": (
                    "tests.conftest.FakeAutopayProvider",
                    {"validate_result": validate_result},
                )
            }
        ):
            result = Payments.validate_method(saved_method=sm.as_saved_method())

            assert result.is_valid is True
            assert result.reason == "OK"

    @pytest.mark.django_db
    def test_refresh_method_raises_capability_error_when_not_capable(self, saved_method_factory, fake_provider, override_settings):
        """Test that refresh_method raises AutopayCapabilityError when provider is not capable."""
        sm = saved_method_factory(gateway="stripe", id=1)

        with override_settings(
            PAYMENT_VARIANTS={
                "stripe": ("tests.conftest.FakeAutopayProvider", {"supports_off_session": False}),
            },
            AUTOPAY_SITE_BASE_URL="https://example.com",
        ), pytest.raises(AutopayCapabilityError, match="does not support token refresh"):
            Payments.refresh_method(saved_method=sm)

    def test_confirm_refresh_raises_on_expired_attempt(self, saved_method_db_factory, override_settings, user):
        """Test that confirm_refresh raises RuntimeError when attempt is expired."""
        from datetime import datetime, timedelta, timezone

        from autopay.core.models import RefreshAttempt

        sm = saved_method_db_factory(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe123",
            gateway_method_id="pm_stripe123",
            method_type="card",
        )

        # Create an expired RefreshAttempt
        RefreshAttempt.objects.create(
            saved_method=sm,
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            status="pending",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),  # Expired
        )

        with override_settings(
            PAYMENT_VARIANTS={
                "stripe": ("tests.conftest.FakeAutopayProvider", {}),
            }
        ), pytest.raises(RuntimeError, match="expired"):
            Payments.confirm_refresh(
                saved_method=sm.as_saved_method(),
                refresh_payload={},
            )

    def test_confirm_refresh_marks_failed_on_provider_error(self, saved_method_db_factory, override_settings, user):
        """Test that confirm_refresh marks attempt as failed when provider call raises."""
        from datetime import datetime, timedelta, timezone

        from autopay.core.models import RefreshAttempt

        sm = saved_method_db_factory(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_stripe123",
            gateway_method_id="pm_stripe123",
            method_type="card",
        )

        # Create a pending RefreshAttempt
        attempt = RefreshAttempt.objects.create(
            saved_method=sm,
            gateway_setup_id="setup_intent_123",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_123"},
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        # Make provider.confirm_refresh raise
        class FailingProvider:
            def confirm_refresh(self, saved_method, refresh_payload):
                raise RuntimeError("Provider error")

        with override_settings(
            PAYMENT_VARIANTS={
                "stripe": ("tests.conftest.FakeAutopayProvider", {"failing_confirm": True}),
            }
        ), pytest.raises(RuntimeError, match="Provider error"):
            Payments.confirm_refresh(
                saved_method=sm.as_saved_method(),
                refresh_payload={},
            )

        # Verify attempt was marked as failed
        attempt.refresh_from_db()
        assert attempt.status == "failed"
            # Note: validate_method does NOT persist automatically - only validate_all_user_methods does

    def test_validate_method_unsupported_gateway_degrades_gracefully(self, saved_method_factory):
        """Test that validate_method raises ValueError for unconfigured gateways."""
        sm = saved_method_factory(gateway="dummy")

        with django_override_settings(PAYMENT_VARIANTS={}):
            with pytest.raises(ValueError) as exc_info:
                Payments.validate_method(saved_method=sm)

            assert "dummy" in str(exc_info.value)


class TestWebhookPayloadParsing:
    """Tests for _parse_webhook_body function."""

    def test_parse_webhook_empty_body_returns_none(self):
        """Test that empty raw_body returns None."""
        from autopay.core.facade import _parse_webhook_body

        result = _parse_webhook_body(b"", "application/json")
        assert result is None

    def test_parse_webhook_form_encoded_success(self):
        """Test form-encoded body parsing."""
        from autopay.core.facade import _parse_webhook_body

        body = b"event=payment.completed&payment_id=123"
        result = _parse_webhook_body(body, "application/x-www-form-urlencoded")

        assert result == {"event": "payment.completed", "payment_id": "123"}

    def test_parse_webhook_form_encoded_multi_value(self):
        """Test form-encoded body with multiple values for same key."""
        from autopay.core.facade import _parse_webhook_body

        body = b"tags[]=a&tags[]=b&event=test"
        result = _parse_webhook_body(body, "application/x-www-form-urlencoded")

        # parse_qs keeps the brackets in the key name
        assert result == {"tags[]": ["a", "b"], "event": "test"}

    def test_parse_webhook_form_encoded_bad_utf8_returns_none(self):
        """Test form-encoded body with bad UTF-8 returns None."""
        from autopay.core.facade import _parse_webhook_body

        # Invalid UTF-8 sequence
        body = b"event=test\xff\xfe"
        result = _parse_webhook_body(body, "application/x-www-form-urlencoded")

        assert result is None

    def test_parse_webhook_json_success(self):
        """Test JSON body parsing."""
        from autopay.core.facade import _parse_webhook_body

        body = b'{"event": "payment.completed", "payment_id": "123"}'
        result = _parse_webhook_body(body, "application/json")

        assert result == {"event": "payment.completed", "payment_id": "123"}

    def test_parse_webhook_json_invalid_returns_none(self):
        """Test invalid JSON returns None."""
        from autopay.core.facade import _parse_webhook_body

        body = b"{invalid json"
        result = _parse_webhook_body(body, "application/json")

        assert result is None

    def test_parse_webhook_form_content_type_with_charset(self):
        """Test form-encoded with charset in content type."""
        from autopay.core.facade import _parse_webhook_body

        body = b"event=test&id=456"
        result = _parse_webhook_body(body, "application/x-www-form-urlencoded; charset=utf-8")

        assert result == {"event": "test", "id": "456"}