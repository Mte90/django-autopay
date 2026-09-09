"""URL configuration for django-autopay core module."""
from django.urls import path

from .views import autopay_webhook_view

urlpatterns = [
    path(
        "payments/webhook/<slug:variant>/",
        autopay_webhook_view,
        name="autopay_webhook",
    ),
]