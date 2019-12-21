from django.urls import path, include
from rest_framework_nested import routers
from issues.views import IssueViewSet, EventViewSet
from .views import ProjectViewSet, ProjectKeyViewSet

router = routers.SimpleRouter()
router.register(r"projects", ProjectViewSet)

projects_router = routers.NestedSimpleRouter(router, r"projects", lookup="project")
projects_router.register(r"keys", ProjectKeyViewSet, basename="project-keys")
projects_router.register(r"issues", IssueViewSet, basename="project-issues")
projects_router.register(r"events", EventViewSet, basename="project-events")

urlpatterns = [path("", include(router.urls)), path("", include(projects_router.urls))]

