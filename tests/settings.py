"""Minimal Django settings for the django-autopay test suite."""
SECRET_KEY = "test-only-not-secret"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "autopay",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# django-payments required settings
PAYMENT_HOST = "example.com"
PAYMENT_VARIANTS = {
    "paypal": ("autopay.providers.paypal.PaypalAutopayProvider", {"client_id": "test", "secret": "test"}),
    "stripe": ("autopay.providers.stripe.StripeAutopayProvider", {"api_key": "sk_test_dummy"}),
    "mollie": ("autopay.providers.mollie.MollieAutopayProvider", {"api_key": "test_dummy"}),
    "dummy": ("autopay.dummy.DummyAutopayProvider", {}),
}

ROOT_URLCONF = "autopay.core.urls"
