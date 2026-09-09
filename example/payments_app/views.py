"""Views for django-autopay demo."""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from autopay.core.facade import Payments
from autopay.core.models import SavedPaymentMethod


def home(request: HttpRequest) -> HttpResponse:
    """Home page with demo flow links."""
    return render(request, "payments_app/home.html")


@login_required
def list_methods(request: HttpRequest) -> HttpResponse:
    """List saved payment methods for the current user."""
    methods = Payments.list_saved_methods(request.user)
    return render(request, "payments_app/list_methods.html", {"methods": methods})


@login_required
def save_method(request: HttpRequest) -> HttpResponse:
    """Simulate saving a payment method via the dummy provider."""
    if request.method == "POST":
        # The dummy gateway has no checkout flow; create the row that
        # Payments.save_method(payment) would produce after a real payment.
        SavedPaymentMethod.objects.get_or_create(
            user=request.user,
            gateway="dummy",
            gateway_method_id=f"pm_{request.user.id}_001",
            defaults={
                "gateway_customer_id": f"cust_{request.user.id}",
                "method_type": "card",
                "display_label": "Demo Card ending in 1234",
                "metadata": {"brand": "visa", "last4": "1234"},
            },
        )
        messages.success(request, "Payment method saved successfully.")
        return redirect("list_methods")

    return render(request, "payments_app/save_method.html")


@login_required
def charge_saved_method(request: HttpRequest) -> HttpResponse:
    """Charge a saved payment method off-session."""
    if request.method == "POST":
        # Get the first active method for demo
        methods = Payments.list_saved_methods(request.user)
        if not methods:
            messages.error(request, "No saved payment methods found. Please save a method first.")
            return redirect("list_methods")

        method = methods[0]

        # Perform the charge
        result = Payments.charge_off_session(
            saved_method=method,
            amount=Decimal("10.00"),
            currency="EUR",
            description="Demo charge",
        )

        return render(
            request,
            "payments_app/charge_result.html",
            {"result": result, "amount": Decimal("10.00"), "currency": "EUR"},
        )

    methods = Payments.list_saved_methods(request.user)
    return render(
        request,
        "payments_app/charge_form.html",
        {"methods": methods, "amount": Decimal("10.00"), "currency": "EUR"},
    )