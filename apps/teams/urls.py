from django.urls import include, path
from rest_framework_nested import routers

from apps.organizations_ext.views import OrganizationMemberViewSet
from apps.projects.views import TeamProjectViewSet
from glitchtip.routers import BulkSimpleRouter

from .views import TeamViewSet

router = BulkSimpleRouter()
router.register(r"teams", TeamViewSet)

teams_router = routers.NestedSimpleRouter(router, r"teams", lookup="team")
teams_router.register(r"projects", TeamProjectViewSet, basename="team-projects")
teams_router.register(r"members", OrganizationMemberViewSet, basename="team-members")

urlpatterns = [path("", include(router.urls)), path("", include(teams_router.urls))]
