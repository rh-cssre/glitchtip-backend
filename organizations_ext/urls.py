from django.urls import path, include
from rest_framework_nested import routers
from issues.views import IssueViewSet
from teams.views import NestedTeamViewSet
from users.views import UserViewSet
from glitchtip.routers import BulkSimpleRouter
from .views import OrganizationViewSet

router = BulkSimpleRouter()
router.register(r"organizations", OrganizationViewSet)

organizations_router = routers.NestedSimpleRouter(
    router, r"organizations", lookup="organization"
)
organizations_router.register(r"issues", IssueViewSet, basename="organization-issues")
organizations_router.register(
    r"teams", NestedTeamViewSet, basename="organization-teams"
)
organizations_router.register(r"members", UserViewSet, basename="organization-members")

urlpatterns = [
    path("", include(router.urls)),
    path("", include(organizations_router.urls)),
]
