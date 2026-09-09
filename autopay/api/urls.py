"""URL configuration for autopay API.

Include-ready per plan §12.6.
"""
from django.urls import path

from .bolt import api

app_name = "autopay_api"
urlpatterns = [
    path("", api.urls),  # BoltAPI espone i suoi endpoint automaticamente
]