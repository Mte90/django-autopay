"""Test URL configuration for coverage tests."""
from django.urls import path

from autopay.core.views import autopay_webhook_view

urlpatterns = [
    path("autopay/webhook/<slug:variant>/", autopay_webhook_view, name="autopay_webhook"),
]