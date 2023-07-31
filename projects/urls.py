from django.urls import include, path
from rest_framework_nested import routers

from alerts.views import ProjectAlertViewSet
from environments.views import EnvironmentProjectViewSet
from issues.views import EventViewSet, IssueViewSet
from releases.views import ReleaseFileViewSet, ReleaseViewSet

from .views import ProjectKeyViewSet, ProjectTeamViewSet, ProjectViewSet

router = routers.SimpleRouter()
router.register(r"projects", ProjectViewSet)

projects_router = routers.NestedSimpleRouter(router, r"projects", lookup="project")
projects_router.register(r"keys", ProjectKeyViewSet, basename="project-keys")
projects_router.register(r"issues", IssueViewSet, basename="project-issues")
projects_router.register(r"events", EventViewSet, basename="project-events")
projects_router.register(r"alerts", ProjectAlertViewSet, basename="project-alerts")
projects_router.register(r"teams", ProjectTeamViewSet, basename="project-teams")
projects_router.register(
    r"environments", EnvironmentProjectViewSet, basename="project-environments"
)
projects_router.register(r"releases", ReleaseViewSet, basename="project-releases")

releases_router = routers.NestedSimpleRouter(
    projects_router, r"releases", lookup="release"
)
releases_router.register(r"files", ReleaseFileViewSet, basename="files")

urlpatterns = [
    path("", include(router.urls)),
    path("", include(projects_router.urls)),
    path("", include(releases_router.urls)),
]
