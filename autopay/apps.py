"""Django app configuration for autopay."""
from django.apps import AppConfig


class AutopayConfig(AppConfig):
    """Configuration for the autopay app."""
    
    name = "autopay"
    label = "autopay"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "Django Autopay"