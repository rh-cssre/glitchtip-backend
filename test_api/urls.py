from django.urls import path
from .views import SeedDataAPIView

urlpatterns = [
    path("seed/", SeedDataAPIView.as_view(), name="seed_data"),
]
