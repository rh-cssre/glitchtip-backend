from django.urls import include, path
from rest_framework_nested import routers

from apps.performance.views import (
    SpanViewSet,
    TransactionGroupViewSet,
    TransactionViewSet,
)
from glitchtip.routers import BulkSimpleRouter

from .views import OrganizationViewSet

router = BulkSimpleRouter()
router.register(r"organizations", OrganizationViewSet)

organizations_router = routers.NestedSimpleRouter(
    router, r"organizations", lookup="organization"
)
organizations_router.register(
    r"transactions", TransactionViewSet, basename="organization-transactions"
)
organizations_router.register(
    r"transaction-groups",
    TransactionGroupViewSet,
    basename="organization-transaction-groups",
)
organizations_router.register(
    r"spans",
    SpanViewSet,
    basename="organization-spans",
)

urlpatterns = [
    path("", include(router.urls)),
    path("", include(organizations_router.urls)),
]
