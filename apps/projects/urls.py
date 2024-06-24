from django.urls import include, path
from rest_framework_nested import routers

from apps.releases.views import ReleaseViewSet

from .views import ProjectKeyViewSet, ProjectViewSet

router = routers.SimpleRouter()
router.register(r"projects", ProjectViewSet)

projects_router = routers.NestedSimpleRouter(router, r"projects", lookup="project")
projects_router.register(r"keys", ProjectKeyViewSet, basename="project-keys")
projects_router.register(r"releases", ReleaseViewSet, basename="project-releases")

releases_router = routers.NestedSimpleRouter(
    projects_router, r"releases", lookup="release"
)

urlpatterns = [
    path("", include(router.urls)),
    path("", include(projects_router.urls)),
    path("", include(releases_router.urls)),
]
