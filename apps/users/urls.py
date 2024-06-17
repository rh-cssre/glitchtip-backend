from django.urls import include, path
from rest_framework_nested import routers

from .views import UserViewSet

router = routers.SimpleRouter()
router.register(r"users", UserViewSet)

urlpatterns = [
    path("", include(router.urls)),
]
