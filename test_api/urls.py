from django.urls import path

from .views import seed_data

urlpatterns = [
    path("seed/", seed_data, name="seed_data"),
]
