"""Reusable fixtures for django-autopay test suite."""

from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings

from autopay.core.base import AutopayProvider
from autopay.core.result import (
    ChargeResult,
    ChargeStatus,
    RefreshSetupData,
    ValidationResult,
)
from autopay.core.saved_method import SavedMethod


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    """Clear PROVIDER_CACHE of django-payments between tests.

    django-payments caches provider instances per variant on first
    resolution: PAYMENT_VARIANTS overrides in tests do not invalidate the
    cache, and stale instances pollute subsequent tests (e.g. the DummyProvider
    non-capable in test_facade masked the dummy provider in webhooks).
    """
    from payments.core import PROVIDER_CACHE

    PROVIDER_CACHE.clear()
    yield
    PROVIDER_CACHE.clear()


class FakeAutopayProvider(AutopayProvider):
    """Fake provider for tests with canned responses and counters."""

    SUPPORTS_LIST_METHODS = True
    SUPPORTS_REVOKE = True
    SUPPORTS_WEBHOOK_VERIFICATION = False

    def __init__(
        self,
        charge_result: ChargeResult | None = None,
        validate_result: ValidationResult | None = None,
        refresh_data: RefreshSetupData | None = None,
        supports_off_session: bool = True,
        supports_validate: bool = True,
        supports_refresh: bool = True,
        raise_on_charge: Exception | None = None,
        failing_confirm: bool = False,
        non_capable: bool = False,
        call_counts: dict[str, int] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the fake provider with preconfigured responses."""
        super().__init__(**kwargs)
        self._charge_result = charge_result
        self._validate_result = validate_result
        self._refresh_data = refresh_data
        self._supports_off_session = supports_off_session
        self._supports_validate = supports_validate
        self._supports_refresh = supports_refresh
        self._raise_on_charge = raise_on_charge
        self._failing_confirm = failing_confirm
        self._non_capable = non_capable
        self.call_counts = call_counts or {
            "charge_off_session": 0,
            "validate_method": 0,
            "refresh_method": 0,
            "confirm_refresh": 0,
            "revoke_method": 0,
            "list_saved_methods": 0,
        }

    @property
    def SUPPORTS_OFF_SESSION_CHARGE(self) -> bool:
        return self._supports_off_session

    @property
    def SUPPORTS_VALIDATE(self) -> bool:
        return self._supports_validate

    @property
    def SUPPORTS_REFRESH(self) -> bool:
        return self._supports_refresh and not self._non_capable

    def charge_off_session(
        self,
        saved_method: SavedMethod,
        amount: Decimal,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> ChargeResult:
        self.call_counts["charge_off_session"] += 1
        if self._raise_on_charge:
            raise self._raise_on_charge
        if self._charge_result:
            return self._charge_result
        # Default: charge confirmed
        from datetime import datetime, timezone

        return ChargeResult(
            status=ChargeStatus.CONFIRMED,
            message="Charge successful",
            transaction_id=f"tr_{saved_method.gateway_method_id}_001",
            raw_response={"status": "completed", "amount": str(amount)},
            amount_charged=amount,
            currency=currency,
            requires_action=None,
            created_at=datetime.now(timezone.utc),
        )

    def validate_method(self, saved_method: SavedMethod) -> ValidationResult:
        self.call_counts["validate_method"] += 1
        if self._validate_result:
            return self._validate_result
        from datetime import datetime, timezone

        return ValidationResult(
            is_valid=True,
            is_expired=False,
            needs_refresh=False,
            reason="OK",
            checked_at=datetime.now(timezone.utc),
            raw_response={"status": "valid"},
        )

    def refresh_method(self, saved_method: SavedMethod) -> RefreshSetupData:
        self.call_counts["refresh_method"] += 1
        if self._refresh_data:
            return self._refresh_data
        from datetime import datetime, timedelta, timezone

        return RefreshSetupData(
            gateway_setup_id=f"setup_{saved_method.gateway_method_id}_refresh",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "seti_test_secret"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def confirm_refresh(self, saved_method: SavedMethod, refresh_payload: dict) -> SavedMethod:
        self.call_counts["confirm_refresh"] += 1
        if self._failing_confirm:
            raise RuntimeError("Provider error")
        return SavedMethod(
            gateway=saved_method.gateway,
            gateway_customer_id=saved_method.gateway_customer_id,
            gateway_method_id=refresh_payload.get("new_method_id", f"new_{saved_method.gateway_method_id}"),
            method_type=saved_method.method_type,
            display_label=saved_method.display_label,
            is_active=True,
            metadata=saved_method.metadata or {},
            id=saved_method.id,
        )

    def revoke_method(self, saved_method: SavedMethod) -> None:
        self.call_counts["revoke_method"] += 1

    def list_saved_methods(self, customer_id: str) -> list[SavedMethod]:
        self.call_counts["list_saved_methods"] += 1
        return []


@pytest.fixture
def fake_provider():
    """Returns a factory to create FakeAutopayProvider with different config."""

    def make_provider(**kwargs):
        return FakeAutopayProvider(**kwargs)

    return make_provider


@pytest.fixture
def saved_method_factory():
    """Factory per creare SavedMethod dataclass."""

    def make_saved_method(
        gateway: str = "stripe",
        gateway_customer_id: str = "cus_test123",
        gateway_method_id: str = "pm_test123",
        method_type: str = "card",
        display_label: str = "Visa ending in 4242",
        is_active: bool = True,
        metadata: dict | None = None,
        id: int | None = None,
    ) -> SavedMethod:
        return SavedMethod(
            gateway=gateway,
            gateway_customer_id=gateway_customer_id,
            gateway_method_id=gateway_method_id,
            method_type=method_type,
            display_label=display_label,
            is_active=is_active,
            metadata=metadata or {"brand": "visa", "last4": "4242"},
            id=id,
        )

    return make_saved_method


@pytest.fixture
def saved_method_db_factory(django_db_setup, django_db_blocker):
    """Factory to create SavedPaymentMethod rows in the DB."""

    def make_saved_method_db(
        user,
        gateway: str = "stripe",
        gateway_customer_id: str = "cus_test123",
        gateway_method_id: str = "pm_test123",
        method_type: str = "card",
        display_label: str = "Visa ending in 4242",
        is_active: bool = True,
        metadata: dict | None = None,
    ):
        from autopay.core.models import SavedPaymentMethod

        return SavedPaymentMethod.objects.create(
            user=user,
            gateway=gateway,
            gateway_customer_id=gateway_customer_id,
            gateway_method_id=gateway_method_id,
            method_type=method_type,
            display_label=display_label,
            is_active=is_active,
            metadata=metadata or {"brand": "visa", "last4": "4242"},
        )

    return make_saved_method_db


@pytest.fixture
def override_settings():
    """Context manager per override temporaneo di Django settings."""
    from contextlib import contextmanager

    @contextmanager
    def _override(**kwargs):
        old = {}
        for key, value in kwargs.items():
            old[key] = getattr(settings, key, None)
            setattr(settings, key, value)
        try:
            yield
        finally:
            for key, value in old.items():
                if value is None:
                    delattr(settings, key)
                else:
                    setattr(settings, key, value)

    return _override


class NonAutopayProvider:
    """Provider non-autopay per test di is_autopay_capable."""

    def __init__(self, **kwargs):
        pass


class UnsupportedAutopayProvider(AutopayProvider):
    """AutopayProvider con SUPPORTS_OFF_SESSION_CHARGE=False."""

    SUPPORTS_OFF_SESSION_CHARGE = False
    SUPPORTS_LIST_METHODS = False
    SUPPORTS_REVOKE = False
    SUPPORTS_REFRESH = False
    SUPPORTS_VALIDATE = False
    SUPPORTS_REFUND = False


@pytest.fixture
def user(django_user_model):
    """Crea un user per i test."""
    return django_user_model.objects.create_user(
        username="testuser",
        email="test@example.com",
        password="testpass123",
    )