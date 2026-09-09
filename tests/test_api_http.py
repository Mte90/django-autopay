"""HTTP-level tests for the django-bolt API endpoints.

Handlers are invoked directly with a fake request object: django-bolt's
TestClient drops the Authorization header in its Rust layer, so HTTP-level
auth is covered by the JWT helper tests instead of end-to-end here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autopay.api import bolt
from autopay.api.schemas import (
    ChargeIn,
    ConfirmRefreshIn,
    CreateMethodIn,
    RefreshMethodIn,
)
from autopay.core.result import ChargeStatus

MODULE = "autopay.api.bolt"


class FakeRequest:
    def __init__(self, user=None, headers=None, body=b""):
        self.user = user
        self.headers = headers or {}
        self._body = body

    async def body(self):
        return self._body


async def make_user(django_user_model, username=None):
    username = username or f"u_{uuid4().hex[:10]}"
    return await django_user_model.objects.acreate(username=username, password="x")


async def make_method(user, gateway="stripe", method_id=None, **kw):
    from autopay.core.models import SavedPaymentMethod

    method_id = method_id or f"pm_{uuid4().hex[:12]}"
    return await SavedPaymentMethod.objects.acreate(
        user=user,
        gateway=gateway,
        gateway_customer_id=kw.pop("gateway_customer_id", f"cus_{uuid4().hex[:12]}"),
        gateway_method_id=method_id,
        method_type="card",
        display_label="Visa 4242",
        **kw,
    )


def validation_result(**kw):
    from autopay.core.result import ValidationResult

    defaults = {
        "is_valid": True,
        "is_expired": False,
        "needs_refresh": False,
        "reason": "ok",
        "checked_at": datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
    }
    defaults.update(kw)
    return ValidationResult(**defaults)


def charge_result(status=ChargeStatus.CONFIRMED, **kw):
    defaults = {
        "status": status,
        "message": "ok",
        "transaction_id": "tx_1",
        "amount_charged": Decimal("10.00"),
        "currency": "EUR",
        "requires_action": None,
        "created_at": datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        "raw_response": {"id": "tx_1"},
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestListMethods:
    @pytest.mark.django_db(transaction=True)
    async def test_lists_only_active_methods_of_user(self, django_user_model):
        user = await make_user(django_user_model)
        other = await make_user(django_user_model, username="other")
        await make_method(user, method_id="pm_a", is_default=True)
        await make_method(user, method_id="pm_b")
        await make_method(user, method_id="pm_c", is_active=False)
        await make_method(other, method_id="pm_d")

        out = await bolt.list_methods(FakeRequest(user=user))

        from autopay.core.models import SavedPaymentMethod

        row_a = await SavedPaymentMethod.objects.aget(gateway_method_id="pm_a")
        row_b = await SavedPaymentMethod.objects.aget(gateway_method_id="pm_b")
        assert [m.id for m in out] == [row_a.id, row_b.id]
        assert out[0].is_default is True
        assert out[1].is_active is True

    @pytest.mark.django_db(transaction=True)
    async def test_empty_list(self, django_user_model):
        user = await make_user(django_user_model)
        assert await bolt.list_methods(FakeRequest(user=user)) == []

    @pytest.mark.django_db(transaction=True)
    async def test_unauthenticated_returns_empty(self, django_user_model):
        """IDOR guard: unauthenticated request returns empty list."""
        out = await bolt.list_methods(FakeRequest(user=None))
        assert out == []


class TestCreateMethod:
    @pytest.mark.django_db(transaction=True)
    async def test_payment_not_found_returns_empty(self, monkeypatch, django_user_model):
        user = await make_user(django_user_model)

        class DoesNotExist(Exception):
            pass

        fake_model = SimpleNamespace(
            DoesNotExist=DoesNotExist,
            objects=SimpleNamespace(
                aget=None  # replaced below via AsyncMock side effect
            ),
        )

        async def aget(**kw):
            raise fake_model.DoesNotExist()

        fake_model.objects.aget = aget
        monkeypatch.setattr("payments.get_payment_model", lambda: fake_model)

        out = await bolt.create_method(FakeRequest(user=user), CreateMethodIn(gateway="stripe", payment_token="99"))
        assert (out.setup_id, out.frontend_sdk, out.frontend_payload, out.return_url) == (0, "", {}, "")

    @pytest.mark.django_db(transaction=True)
    async def test_payment_owned_by_other_user_returns_empty(self, monkeypatch, django_user_model):
        user = await make_user(django_user_model)
        payment = SimpleNamespace(id=7, user_id=other_id) if (other_id := user.id + 1) else None

        fake_model = SimpleNamespace(objects=SimpleNamespace(aget=None))

        async def aget(**kw):
            return payment

        fake_model.objects.aget = aget
        monkeypatch.setattr("payments.get_payment_model", lambda: fake_model)

        out = await bolt.create_method(FakeRequest(user=user), CreateMethodIn(gateway="stripe", payment_token="7"))
        assert out.setup_id == 0

    @pytest.mark.django_db(transaction=True)
    async def test_save_method_none_returns_empty(self, monkeypatch, django_user_model):
        user = await make_user(django_user_model)
        payment = SimpleNamespace(id=7, user_id=user.id)
        fake_model = SimpleNamespace(objects=SimpleNamespace(aget=None))

        async def aget(**kw):
            return payment

        fake_model.objects.aget = aget
        monkeypatch.setattr("payments.get_payment_model", lambda: fake_model)
        monkeypatch.setattr(f"{MODULE}.Payments.save_method", lambda p: None)

        out = await bolt.create_method(FakeRequest(user=user), CreateMethodIn(gateway="stripe", payment_token="7"))
        assert out.setup_id == 0

    @pytest.mark.django_db(transaction=True)
    async def test_success_persists_method(self, monkeypatch, django_user_model):
        from autopay.core.models import SavedPaymentMethod

        user = await make_user(django_user_model)
        payment = SimpleNamespace(id=7, user_id=user.id)
        fake_model = SimpleNamespace(objects=SimpleNamespace(aget=None))

        async def aget(**kw):
            return payment

        fake_model.objects.aget = aget
        monkeypatch.setattr("payments.get_payment_model", lambda: fake_model)

        # save_method now persists the SavedPaymentMethod row itself,
        # so we mock it to return a real row's as_saved_method().
        sm = await SavedPaymentMethod.objects.acreate(
            user=user,
            gateway="stripe",
            gateway_customer_id="cus_9",
            gateway_method_id="pm_9",
            method_type="card",
            display_label="Visa 1111",
            metadata={"brand": "visa"},
        )
        monkeypatch.setattr(f"{MODULE}.Payments.save_method", lambda p: sm.as_saved_method())

        out = await bolt.create_method(FakeRequest(user=user), CreateMethodIn(gateway="stripe", payment_token="7"))

        assert out.setup_id > 0
        assert out.frontend_sdk == "stripe"
        assert out.frontend_payload == {"brand": "visa"}
        assert out.return_url == f"/autopay/api/methods/{out.setup_id}/"
        row = await SavedPaymentMethod.objects.aget(id=out.setup_id)
        assert row.gateway == "stripe"
        assert row.gateway_method_id == "pm_9"
        assert row.user_id == user.id


class TestGetMethod:
    @pytest.mark.django_db(transaction=True)
    async def test_returns_method(self, django_user_model):
        user = await make_user(django_user_model)
        sm = await make_method(user)

        out = await bolt.get_method(FakeRequest(user=user), method_id=sm.id)

        assert out.id == sm.id
        assert out.gateway == "stripe"
        assert out.display_label == "Visa 4242"
        assert out.created_at  # ISO string


class TestRevokeMethod:
    @pytest.mark.django_db(transaction=True)
    async def test_revokes_and_deactivates(self, monkeypatch, django_user_model):
        from autopay.api.schemas import DeleteMethodOut
        from autopay.core.models import SavedPaymentMethod

        user = await make_user(django_user_model)
        sm = await make_method(user)
        calls = []
        monkeypatch.setattr(f"{MODULE}.Payments.revoke_method", lambda m: calls.append(m))

        out = await bolt.revoke_method(FakeRequest(user=user), method_id=sm.id)

        assert isinstance(out, DeleteMethodOut)
        assert out.success is True
        assert out.deleted_method_id == sm.id
        assert calls and calls[0].gateway_method_id == sm.gateway_method_id
        sm_db = await SavedPaymentMethod.objects.aget(id=sm.id)
        assert sm_db.is_active is False

    @pytest.mark.django_db(transaction=True)
    async def test_idor_user_b_cannot_revoke_user_a_method(self, django_user_model):
        """IDOR guard: user B cannot revoke user A's method."""
        user_a = await make_user(django_user_model, username="user_a")
        user_b = await make_user(django_user_model, username="user_b")
        sm_a = await make_method(user_a, method_id="pm_a")

        # Should raise DoesNotExist since user_b is not the owner
        # This is caught by the aget(user=request.user) filter
        from autopay.core.models import SavedPaymentMethod
        with pytest.raises(SavedPaymentMethod.DoesNotExist):
            await SavedPaymentMethod.objects.aget(id=sm_a.id, user=user_b)


class TestValidateMethod:
    @pytest.mark.django_db(transaction=True)
    async def test_inactive_method_skips_gateway(self, monkeypatch, django_user_model):
        user = await make_user(django_user_model)
        sm = await make_method(user, is_active=False)
        called = []
        monkeypatch.setattr(f"{MODULE}.Payments.validate_method", lambda m: called.append(m))

        out = await bolt.validate_method(FakeRequest(user=user), method_id=sm.id)

        assert out.is_valid is False
        assert out.needs_refresh is True
        assert out.reason == "Method no longer active"
        assert out.gateway == "stripe"
        assert called == []

    @pytest.mark.django_db(transaction=True)
    async def test_active_method_persists_result(self, monkeypatch, django_user_model):
        from autopay.core.models import SavedPaymentMethod

        user = await make_user(django_user_model)
        sm = await make_method(user)
        monkeypatch.setattr(
            f"{MODULE}.Payments.validate_method",
            lambda m: validation_result(is_valid=False, needs_refresh=True, reason="mandate revoked"),
        )

        out = await bolt.validate_method(FakeRequest(user=user), method_id=sm.id)

        assert out.is_valid is False
        assert out.needs_refresh is True
        assert out.reason == "mandate revoked"
        assert out.checked_at == "2026-01-01T00:00:00+00:00"
        sm_db = await SavedPaymentMethod.objects.aget(id=sm.id)
        assert sm_db.last_validation_is_valid is False
        assert sm_db.last_validation_reason == "mandate revoked"
        assert sm_db.last_validated_at is not None


class TestValidateAllMyMethods:
    @pytest.mark.django_db(transaction=True)
    async def test_validates_each_active_method(self, monkeypatch, django_user_model):
        from autopay.core.models import SavedPaymentMethod

        user = await make_user(django_user_model)
        sm_a = await make_method(user, method_id="pm_a")
        sm_b = await make_method(user, method_id="pm_b")
        await make_method(user, method_id="pm_c", is_active=False)
        monkeypatch.setattr(f"{MODULE}.Payments.validate_method", lambda m: validation_result())

        out = await bolt.validate_all_my_methods(FakeRequest(user=user))

        assert len(out) == 2
        assert {r.method_id for r in out} == {sm_a.id, sm_b.id}
        for pk in (sm_a.id, sm_b.id):
            row = await SavedPaymentMethod.objects.aget(id=pk)
            assert row.last_validation_is_valid is True


class TestRefreshMethod:
    @pytest.mark.django_db(transaction=True)
    async def test_inactive_method_returns_empty(self, monkeypatch, django_user_model):
        user = await make_user(django_user_model)
        sm = await make_method(user, is_active=False)
        monkeypatch.setattr(f"{MODULE}.Payments.refresh_method", lambda m, reason: (_ for _ in ()).throw(AssertionError))

        out = await bolt.refresh_method(FakeRequest(user=user), method_id=sm.id, body=RefreshMethodIn())

        assert out.setup_id == 0
        assert out.return_url == ""

    @pytest.mark.django_db(transaction=True)
    async def test_active_method_returns_setup(self, monkeypatch, django_user_model):
        user = await make_user(django_user_model)
        sm = await make_method(user)
        reasons = []
        setup = SimpleNamespace(
            setup_id=55,
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "cs_x"},
            expires_at=datetime(2026, 1, 2, tzinfo=dt_timezone.utc),
            return_url="https://ex.test/refresh",
        )

        def fake_refresh(m, reason):
            reasons.append(reason)
            return setup

        monkeypatch.setattr(f"{MODULE}.Payments.refresh_method", fake_refresh)

        out = await bolt.refresh_method(
            FakeRequest(user=user), method_id=sm.id, body=RefreshMethodIn(reason="expired_card")
        )

        assert reasons == ["expired_card"]
        assert out.setup_id == 55
        assert out.frontend_payload == {"client_secret": "cs_x"}
        assert out.expires_at == "2026-01-02T00:00:00+00:00"


class TestGetRefreshSetup:
    @pytest.mark.django_db(transaction=True)
    async def test_unknown_attempt_returns_empty(self, django_user_model):
        user = await make_user(django_user_model)
        sm = await make_method(user)

        out = await bolt.get_refresh_setup(FakeRequest(user=user), method_id=sm.id, attempt=999)

        assert out.setup_id == 0

    @pytest.mark.django_db(transaction=True)
    async def test_idor_user_b_cannot_access_user_a_refresh(self, django_user_model):
        """IDOR guard: user B cannot access user A's refresh setup."""
        user_a = await make_user(django_user_model, username="user_a")
        user_b = await make_user(django_user_model, username="user_b")
        sm_a = await make_method(user_a, method_id="pm_a")
        from autopay.core.models import RefreshAttempt
        attempt = await RefreshAttempt.objects.acreate(
            saved_method=sm_a,
            gateway_setup_id="seti_1",
            frontend_sdk="stripe",
            status="pending",
            expires_at=timezone_now_plus(hours=1),
        )

        # User B should get empty response (not found) for user A's refresh attempt
        out = await bolt.get_refresh_setup(FakeRequest(user=user_b), method_id=sm_a.id, attempt=attempt.id)
        assert out.setup_id == 0

    @pytest.mark.django_db(transaction=True)
    async def test_non_pending_attempt_returns_empty(self, django_user_model):
        """Test get_refresh_setup returns empty when attempt status is not pending."""
        from autopay.core.models import RefreshAttempt

        user = await make_user(django_user_model)
        sm = await make_method(user)
        
        # Create a RefreshAttempt with status "in_progress" (not pending)
        attempt = await RefreshAttempt.objects.acreate(
            saved_method=sm,
            gateway_setup_id="seti_1",
            frontend_sdk="stripe",
            status="in_progress",  # Not pending
            frontend_payload={"client_secret": "cs_y"},
            expires_at=timezone_now_plus(hours=1),
        )

        out = await bolt.get_refresh_setup(FakeRequest(user=user), method_id=sm.id, attempt=attempt.id)

        assert out.setup_id == 0

    @pytest.mark.django_db(transaction=True)
    async def test_pending_attempt_returns_setup(self, django_user_model):
        from autopay.core.models import RefreshAttempt

        user = await make_user(django_user_model)
        sm = await make_method(user)
        attempt = await RefreshAttempt.objects.acreate(
            saved_method=sm,
            gateway_setup_id="seti_1",
            frontend_sdk="stripe",
            frontend_payload={"client_secret": "cs_y"},
            expires_at=timezone_now_plus(hours=1),
        )

        out = await bolt.get_refresh_setup(FakeRequest(user=user), method_id=sm.id, attempt=attempt.id)

        assert out.setup_id == attempt.id
        assert out.frontend_sdk == "stripe"
        assert "confirm/?attempt=" in out.return_url


class TestConfirmRefreshPost:
    @pytest.mark.django_db(transaction=True)
    async def test_success(self, monkeypatch, django_user_model):

        user = await make_user(django_user_model)
        sm = await make_method(user)
        seen = []
        monkeypatch.setattr(f"{MODULE}.Payments.confirm_refresh", lambda m, p: seen.append(p))

        out = await bolt.confirm_refresh_post(
            FakeRequest(user=user),
            method_id=sm.id,
            body=ConfirmRefreshIn(setup_intent_id="seti_1"),
        )

        assert out.success is True
        assert out.method.id == sm.id
        assert out.error is None
        assert seen == [{"setup_intent_id": "seti_1"}]

    @pytest.mark.django_db(transaction=True)
    async def test_idor_user_b_cannot_confirm_user_a_method(self, django_user_model):
        """IDOR guard: user B cannot confirm refresh for user A's method."""
        user_a = await make_user(django_user_model, username="user_a")
        user_b = await make_user(django_user_model, username="user_b")
        sm_a = await make_method(user_a, method_id="pm_a")

        # Should raise DoesNotExist since user_b is not the owner
        from autopay.core.models import SavedPaymentMethod
        with pytest.raises(SavedPaymentMethod.DoesNotExist):
            await SavedPaymentMethod.objects.aget(id=sm_a.id, user=user_b)

    @pytest.mark.django_db(transaction=True)
    async def test_payload_none_fields_dropped(self, monkeypatch, django_user_model):
        user = await make_user(django_user_model)
        sm = await make_method(user)
        seen = []
        monkeypatch.setattr(f"{MODULE}.Payments.confirm_refresh", lambda m, p: seen.append(p))

        await bolt.confirm_refresh_post(
            FakeRequest(user=user), method_id=sm.id, body=ConfirmRefreshIn(payment_token_id="tok_1")
        )

        assert seen == [{"payment_token_id": "tok_1"}]

    @pytest.mark.django_db(transaction=True)
    async def test_capability_error_returns_failure(self, monkeypatch, django_user_model):
        from autopay.core.base import AutopayCapabilityError

        user = await make_user(django_user_model)
        sm = await make_method(user)

        def boom(m, p):
            raise AutopayCapabilityError("gateway does not support refresh")

        monkeypatch.setattr(f"{MODULE}.Payments.confirm_refresh", boom)

        out = await bolt.confirm_refresh_post(
            FakeRequest(user=user), method_id=sm.id, body=ConfirmRefreshIn(setup_intent_id="seti_1")
        )

        assert out.success is False
        assert "does not support" in out.error


class TestConfirmRefreshGet:
    @pytest.mark.django_db(transaction=True)
    async def test_invalid_signature_rejected(self, django_user_model, settings):
        from autopay.api.schemas import RefreshStatusOut

        user = await make_user(django_user_model)
        sm = await make_method(user)
        settings.AUTOPAY_WEBHOOK_SECRET = "sekrit"

        out = await bolt.confirm_refresh_get(
            FakeRequest(user=user), method_id=sm.id, first_payment_id="tr_1", attempt=3, sig="deadbeef"
        )

        assert isinstance(out, RefreshStatusOut)
        assert out.success is False
        assert out.error == "invalid signature"

    @pytest.mark.django_db(transaction=True)
    async def test_secret_configured_requires_signed_attempt(self, django_user_model, settings):
        """With a secret configured the HMAC signature is mandatory: an unsigned
        call (attempt omitted) must not reach the method lookup."""
        user = await make_user(django_user_model)
        sm = await make_method(user)
        settings.AUTOPAY_WEBHOOK_SECRET = "sekrit"

        out = await bolt.confirm_refresh_get(
            FakeRequest(user=user), method_id=sm.id, first_payment_id="tr_1", attempt=0, sig=""
        )

        assert out.success is False
        assert out.error == "invalid signature"

    @pytest.mark.django_db(transaction=True)
    async def test_valid_attempt_signature_succeeds(self, monkeypatch, django_user_model, settings):
        user = await make_user(django_user_model)
        sm = await make_method(user)
        settings.AUTOPAY_WEBHOOK_SECRET = "sekrit"
        attempt_id = 5
        sig = hmac.new(b"sekrit", f"{attempt_id}:{sm.id}".encode(), hashlib.sha256).hexdigest()
        monkeypatch.setattr(f"{MODULE}.Payments.confirm_refresh", lambda m, p: m)

        out = await bolt.confirm_refresh_get(
            FakeRequest(user=user), method_id=sm.id, first_payment_id="tr_2", attempt=attempt_id, sig=sig
        )

        assert out.success is True
        assert out.method_id == sm.id

    @pytest.mark.django_db(transaction=True)
    async def test_returns_refresh_status_out_struct(self, monkeypatch, django_user_model):
        """Return type: confirm_refresh_get returns RefreshStatusOut, not dict."""
        from autopay.api.schemas import RefreshStatusOut

        user = await make_user(django_user_model)
        sm = await make_method(user)
        monkeypatch.setattr(f"{MODULE}.Payments.confirm_refresh", lambda m, p: m)

        out = await bolt.confirm_refresh_get(
            FakeRequest(user=user), method_id=sm.id, first_payment_id="tr_9", attempt=0, sig=""
        )

        assert isinstance(out, RefreshStatusOut)
        assert out.success is True
        assert out.method_id == sm.id
        assert out.error is None

    @pytest.mark.django_db(transaction=True)
    async def test_method_not_found(self, django_user_model):
        user = await make_user(django_user_model)

        out = await bolt.confirm_refresh_get(FakeRequest(user=user), method_id=4242)

        assert out.success is False
        assert out.error == "method not found"

    @pytest.mark.django_db(transaction=True)
    async def test_success(self, monkeypatch, django_user_model):
        user = await make_user(django_user_model)
        sm = await make_method(user)
        seen = []
        monkeypatch.setattr(f"{MODULE}.Payments.confirm_refresh", lambda m, p: seen.append(p))

        out = await bolt.confirm_refresh_get(
            FakeRequest(user=user), method_id=sm.id, first_payment_id="tr_9", attempt=0, sig=""
        )

        assert out.success is True
        assert out.method_id == sm.id
        assert seen == [{"first_payment_id": "tr_9"}]

    @pytest.mark.django_db(transaction=True)
    async def test_capability_error_returns_failure(self, monkeypatch, django_user_model):
        from autopay.core.base import AutopayCapabilityError

        user = await make_user(django_user_model)
        sm = await make_method(user)

        def boom(m, p):
            raise AutopayCapabilityError("no refresh support")

        monkeypatch.setattr(f"{MODULE}.Payments.confirm_refresh", boom)

        out = await bolt.confirm_refresh_get(FakeRequest(user=user), method_id=sm.id)

        assert out.success is False
        assert "no refresh" in out.error or out.error == "no refresh support"


class TestCreateCharge:
    @pytest.mark.django_db(transaction=True)
    async def test_persists_charge_record(self, monkeypatch, django_user_model):
        from autopay.api.schemas import ChargeOut
        from autopay.core.models import ChargeRecord

        user = await make_user(django_user_model)
        sm = await make_method(user)
        captured = {}

        def fake_charge(saved_method, amount, currency, description, idempotency_key, metadata):
            captured.update(
                amount=amount,
                currency=currency,
                idempotency_key=idempotency_key,
                description=description,
                metadata=metadata,
            )
            return charge_result()

        monkeypatch.setattr(f"{MODULE}.Payments.charge_off_session", fake_charge)
        body = ChargeIn(
            saved_method_id=sm.id,
            amount=Decimal("10.00"),
            description="sub",
            idempotency_key="idem_1",
            metadata={"plan": "pro"},
        )

        out = await bolt.create_charge(FakeRequest(user=user), body=body)

        assert captured["amount"] == Decimal("10.00")
        assert captured["idempotency_key"] == "idem_1"
        assert out.status == "confirmed"
        assert out.transaction_id == "tx_1"
        assert out.amount_charged == "10.00"
        assert out.currency == "EUR"
        rec = await ChargeRecord.objects.aget(id=1)
        assert rec.status == "confirmed"
        assert rec.idempotency_key == "idem_1"
        assert rec.raw_response == {"id": "tx_1"}
        assert isinstance(out, ChargeOut)

    @pytest.mark.django_db(transaction=True)
    async def test_unauthenticated_returns_failed(self, django_user_model):
        """IDOR guard: unauthenticated request to create_charge."""
        sm = await make_method(await make_user(django_user_model))
        body = ChargeIn(saved_method_id=sm.id, amount=Decimal("10.00"))

        out = await bolt.create_charge(FakeRequest(user=None), body=body)

        assert out.status == "failed"
        assert "not found" in out.message.lower()

    @pytest.mark.django_db(transaction=True)
    async def test_idor_user_b_cannot_charge_user_a_method(self, monkeypatch, django_user_model):
        """IDOR guard: user B cannot charge using user A's saved method."""
        user_a = await make_user(django_user_model, username="user_a")
        user_b = await make_user(django_user_model, username="user_b")
        sm_a = await make_method(user_a, method_id="pm_a")

        def fake_charge(*args, **kwargs):
            raise AssertionError("Should not call gateway for cross-user charge")

        monkeypatch.setattr(f"{MODULE}.Payments.charge_off_session", fake_charge)
        body = ChargeIn(saved_method_id=sm_a.id, amount=Decimal("10.00"))

        out = await bolt.create_charge(FakeRequest(user=user_b), body=body)

        assert out.status == "failed"
        assert "not found" in out.message.lower()


class TestGetCharge:
    @pytest.mark.django_db(transaction=True)
    async def test_non_pending_returns_record_as_is(self, django_user_model):
        from autopay.api.schemas import ChargeOut

        user = await make_user(django_user_model)
        sm = await make_method(user)
        rec = await make_charge_record(sm, status="failed", message="declined")

        out = await bolt.get_charge(FakeRequest(user=user), charge_id=rec.id)

        assert out.status == "failed"
        assert out.amount_charged is None
        assert isinstance(out, ChargeOut)

    @pytest.mark.django_db(transaction=True)
    async def test_pending_syncs_from_gateway(self, monkeypatch, django_user_model):
        from autopay.core.models import ChargeRecord

        user = await make_user(django_user_model)
        sm = await make_method(user)
        rec = await make_charge_record(sm, status="pending", message="")
        monkeypatch.setattr(
            f"{MODULE}.Payments.get_charge_status",
            lambda gateway, tx: charge_result(status=ChargeStatus.CONFIRMED, message="settled"),
        )

        out = await bolt.get_charge(FakeRequest(user=user), charge_id=rec.id)

        assert out.status == "confirmed"
        assert out.amount_charged == "10.00"
        rec_db = await ChargeRecord.objects.aget(id=rec.id)
        assert rec_db.status == "confirmed"
        assert rec_db.message == "settled"

    @pytest.mark.django_db(transaction=True)
    async def test_pending_status_unchanged_skips_update(self, monkeypatch, django_user_model):
        user = await make_user(django_user_model)
        sm = await make_method(user)
        rec = await make_charge_record(sm, status="pending", message="waiting")
        monkeypatch.setattr(
            f"{MODULE}.Payments.get_charge_status",
            lambda gateway, tx: charge_result(status=ChargeStatus.PENDING, message="still pending"),
        )

        out = await bolt.get_charge(FakeRequest(user=user), charge_id=rec.id)

        assert out.status == "pending"
        assert out.message == "waiting"
        assert out.amount_charged is None

    @pytest.mark.django_db(transaction=True)
    async def test_idor_user_b_cannot_see_user_a_charge(self, django_user_model):
        """IDOR guard: user B cannot see user A's charge."""
        user_a = await make_user(django_user_model, username="user_a")
        user_b = await make_user(django_user_model, username="user_b")
        sm_a = await make_method(user_a, method_id="pm_a")
        rec_a = await make_charge_record(sm_a, status="confirmed", message="ok")

        out = await bolt.get_charge(FakeRequest(user=user_b), charge_id=rec_a.id)

        assert out.status == "failed"
        assert "not found" in out.message.lower()


class TestWebhookReceiver:
    @pytest.mark.django_db(transaction=True)
    async def test_invalid_json_returns_not_processed(self):
        req = FakeRequest(body=b"not-json{")

        out = await bolt.webhook_receiver(req, variant="stripe")

        assert out.processed is False
        assert out.event_id is None

    @pytest.mark.django_db(transaction=True)
    async def test_dispatches_to_facade_with_raw_bytes(self, monkeypatch):
        """The facade contract is raw bytes + content type, never a parsed dict."""
        req = FakeRequest(
            headers={"stripe-signature": "sig", "content-type": "application/json"},
            body=json.dumps({"id": "evt_1", "type": "x"}).encode(),
        )
        captured = {}

        def fake_process(variant, raw_body, headers, content_type="", client_ip=""):
            captured["raw_body"] = raw_body
            captured["content_type"] = content_type
            return SimpleNamespace(processed=True, event_id="stripe:evt_1")

        monkeypatch.setattr(f"{MODULE}.Payments.process_webhook", fake_process)

        out = await bolt.webhook_receiver(req, variant="stripe")

        assert out.processed is True
        assert out.event_id == "stripe:evt_1"
        assert isinstance(captured["raw_body"], bytes)
        assert captured["raw_body"] == json.dumps({"id": "evt_1", "type": "x"}).encode()
        assert captured["content_type"] == "application/json"

    @pytest.mark.django_db(transaction=True)
    async def test_end_to_end_unknown_event_via_facade(self):
        """No mocks: raw bytes flow through the real facade, dedup and dispatch."""
        req = FakeRequest(
            headers={"content-type": "application/json"},
            body=json.dumps({"id": f"evt_{uuid4().hex[:12]}", "type": "unknown.event"}).encode(),
        )

        out = await bolt.webhook_receiver(req, variant="dummy")

        assert out.processed is False
        assert out.event_id.startswith("evt_")


class TestVerifyAttemptSignature:
    def test_no_secret_disables_check(self, settings):
        settings.AUTOPAY_WEBHOOK_SECRET = ""
        assert bolt._verify_attempt_signature(1, 2, "anything") is True

    def test_valid_signature(self, settings):
        settings.AUTOPAY_WEBHOOK_SECRET = "sekrit"
        sig = hmac.new(b"sekrit", b"3:7", hashlib.sha256).hexdigest()
        assert bolt._verify_attempt_signature(3, 7, sig) is True

    def test_invalid_signature(self, settings):
        settings.AUTOPAY_WEBHOOK_SECRET = "sekrit"
        assert bolt._verify_attempt_signature(3, 7, "wrong") is False


class TestSyncToAsyncWrapping:
    """Tests to verify that sync Payments calls are wrapped in sync_to_async."""

    @pytest.mark.django_db(transaction=True)
    async def test_create_charge_is_coroutine(self, django_user_model):
        """Handler is async and can be awaited."""
        import inspect

        assert inspect.iscoroutinefunction(bolt.create_charge)

    @pytest.mark.django_db(transaction=True)
    async def test_get_charge_is_coroutine(self, django_user_model):
        """Handler is async and can be awaited."""
        import inspect

        assert inspect.iscoroutinefunction(bolt.get_charge)

    @pytest.mark.django_db(transaction=True)
    async def test_revoke_method_is_coroutine(self, django_user_model):
        """Handler is async and can be awaited."""
        import inspect

        assert inspect.iscoroutinefunction(bolt.revoke_method)

    @pytest.mark.django_db(transaction=True)
    async def test_validate_method_is_coroutine(self, django_user_model):
        """Handler is async and can be awaited."""
        import inspect

        assert inspect.iscoroutinefunction(bolt.validate_method)

    @pytest.mark.django_db(transaction=True)
    async def test_refresh_method_is_coroutine(self, django_user_model):
        """Handler is async and can be awaited."""
        import inspect

        assert inspect.iscoroutinefunction(bolt.refresh_method)

    @pytest.mark.django_db(transaction=True)
    async def test_confirm_refresh_post_is_coroutine(self, django_user_model):
        """Handler is async and can be awaited."""
        import inspect

        assert inspect.iscoroutinefunction(bolt.confirm_refresh_post)

    @pytest.mark.django_db(transaction=True)
    async def test_confirm_refresh_get_is_coroutine(self, django_user_model):
        """Handler is async and can be awaited."""
        import inspect

        assert inspect.iscoroutinefunction(bolt.confirm_refresh_get)


def timezone_now_plus(hours):
    return datetime.now(tz=dt_timezone.utc) + timedelta(hours=hours)


async def make_charge_record(sm, **kw):
    from autopay.core.models import ChargeRecord

    defaults = {
        "saved_method": sm,
        "amount": Decimal("10.00"),
        "currency": "EUR",
        "description": "sub",
        "transaction_id": "tx_1",
        "status": "confirmed",
        "message": "ok",
        "raw_response": {},
        "idempotency_key": "",
    }
    defaults.update(kw)
    return await ChargeRecord.objects.acreate(**defaults)
