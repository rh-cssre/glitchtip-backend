from django.urls import include, path
from rest_framework_nested import routers

from .views import ProjectViewSet

router = routers.SimpleRouter()
router.register(r"projects", ProjectViewSet)

projects_router = routers.NestedSimpleRouter(router, r"projects", lookup="project")

urlpatterns = [
    path("", include(router.urls)),
    path("", include(projects_router.urls)),
]
