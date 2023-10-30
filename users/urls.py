from django.urls import include, path
from rest_framework_nested import routers

from .views import EmailAddressViewSet, UserViewSet

router = routers.SimpleRouter()
router.register(r"users", UserViewSet)

users_router = routers.NestedSimpleRouter(router, r"users", lookup="user")
users_router.register(r"emails", EmailAddressViewSet, basename="user-emails")

urlpatterns = [
    path("", include(router.urls)),
    path("", include(users_router.urls)),
]
