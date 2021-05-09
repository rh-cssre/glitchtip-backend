from django.urls import path, include
from rest_framework_nested import routers
from issues.views import IssueViewSet, EventViewSet
from alerts.views import ProjectAlertViewSet
from releases.views import ReleaseViewSet, ReleaseFileViewSet
from environments.views import EnvironmentProjectViewSet
from .views import ProjectViewSet, ProjectKeyViewSet, ProjectTeamViewSet

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
