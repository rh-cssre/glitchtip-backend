from django.urls import include, path
from rest_framework_nested import routers

from apps.environments.views import EnvironmentViewSet
from apps.performance.views import (
    SpanViewSet,
    TransactionGroupViewSet,
    TransactionViewSet,
)
from apps.projects.views import OrganizationProjectsViewSet
from apps.releases.views import ReleaseFileViewSet, ReleaseViewSet
from apps.teams.views import NestedTeamViewSet
from apps.uptime.views import (
    MonitorCheckViewSet,
    MonitorViewSet,
    StatusPageViewSet,
)
from glitchtip.routers import BulkSimpleRouter
from issues.views import IssueViewSet

from .views import (
    AcceptInviteView,
    OrganizationMemberViewSet,
    OrganizationUserViewSet,
    OrganizationViewSet,
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
organizations_router.register(
    r"monitors", MonitorViewSet, basename="organization-monitors"
)
organizations_router.register(
    r"status-pages", StatusPageViewSet, basename="organization-status-pages"
)

organizations_monitors_router = routers.NestedSimpleRouter(
    organizations_router, r"monitors", lookup="monitor"
)
organizations_monitors_router.register(
    r"checks", MonitorCheckViewSet, basename="organization-monitor-checks"
)

organizations_router.register(
    r"releases", ReleaseViewSet, basename="organization-releases"
)
organizations_releases_router = routers.NestedSimpleRouter(
    organizations_router, r"releases", lookup="release"
)
organizations_releases_router.register(
    r"files", ReleaseFileViewSet, basename="organization-release-files"
)

urlpatterns = [
    path("", include(router.urls)),
    path("", include(organizations_router.urls)),
    path("", include(organizations_monitors_router.urls)),
    path("", include(organizations_releases_router.urls)),
    path(
        "accept/<int:org_user_id>/<token>/",
        AcceptInviteView.as_view(),
        name="accept-invite",
    ),
]
