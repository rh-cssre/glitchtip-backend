from django.urls import include, path
from rest_framework_nested import routers

from glitchtip.routers import BulkSimpleRouter
from organizations_ext.views import OrganizationMemberViewSet
from projects.views import NestedProjectViewSet

from .views import TeamViewSet

router = BulkSimpleRouter()
router.register(r"teams", TeamViewSet)

teams_router = routers.NestedSimpleRouter(router, r"teams", lookup="team")
teams_router.register(r"projects", NestedProjectViewSet, basename="team-projects")
teams_router.register(r"members", OrganizationMemberViewSet, basename="team-members")

urlpatterns = [path("", include(router.urls)), path("", include(teams_router.urls))]
