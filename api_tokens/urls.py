from django.urls import path, include
from rest_framework import routers
from .views import APITokenViewSet

router = routers.SimpleRouter()
router.register(r"api-tokens", APITokenViewSet, basename="api-tokens")


urlpatterns = [
    path("", include(router.urls)),
]
