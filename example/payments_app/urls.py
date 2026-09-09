"""URL configuration for payments app."""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("methods/", views.list_methods, name="list_methods"),
    path("methods/save/", views.save_method, name="save_method"),
    path("charge/", views.charge_saved_method, name="charge_saved_method"),
]