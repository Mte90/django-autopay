"""URL configuration for django-autopay demo."""
from django.urls import include, path

urlpatterns = [
    path("", include("payments_app.urls")),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("autopay.core.urls")),
]
