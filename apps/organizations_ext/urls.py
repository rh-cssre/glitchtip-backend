from django.urls import include, path
from rest_framework_nested import routers

from glitchtip.routers import BulkSimpleRouter

from .views import OrganizationViewSet

router = BulkSimpleRouter()
router.register(r"organizations", OrganizationViewSet)

organizations_router = routers.NestedSimpleRouter(
    router, r"organizations", lookup="organization"
)

urlpatterns = [
    path("", include(router.urls)),
    path("", include(organizations_router.urls)),
]
