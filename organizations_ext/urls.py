from django.urls import path, include
from rest_framework_nested import routers
from issues.views import IssueViewSet
from teams.views import NestedTeamViewSet
from environments.views import EnvironmentViewSet
from releases.views import ReleaseViewSet
from performance.views import TransactionViewSet
from glitchtip.routers import BulkSimpleRouter
from .views import (
    OrganizationViewSet,
    OrganizationUserViewSet,
    OrganizationMemberViewSet,
    OrganizationProjectsViewSet,
    AcceptInviteView,
)

router = BulkSimpleRouter()
router.register(r"organizations", OrganizationViewSet)

organizations_router = routers.NestedSimpleRouter(
    router, r"organizations", lookup="organization"
)
organizations_router.register(r"issues", IssueViewSet, basename="organization-issues")
organizations_router.register(
    r"teams", NestedTeamViewSet, basename="organization-teams"
)
organizations_router.register(
    r"members", OrganizationMemberViewSet, basename="organization-members"
)
organizations_router.register(
    r"users", OrganizationUserViewSet, basename="organization-users"
)
organizations_router.register(
    r"projects", OrganizationProjectsViewSet, basename="organization-projects"
)
organizations_router.register(
    r"environments", EnvironmentViewSet, basename="organization-environments"
)
organizations_router.register(
    r"releases", ReleaseViewSet, basename="organization-releases"
)
organizations_router.register(
    r"transactions", TransactionViewSet, basename="organization-transactions"
)

urlpatterns = [
    path("", include(router.urls)),
    path("", include(organizations_router.urls)),
    path(
        "accept/<int:org_user_id>/<token>/",
        AcceptInviteView.as_view(),
        name="accept-invite",
    ),
]
