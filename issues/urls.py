from django.urls import path, include
from rest_framework_nested import routers
from glitchtip.routers import BulkSimpleRouter
from user_reports.views import UserReportViewSet
from .views import IssueViewSet, EventViewSet

router = BulkSimpleRouter()
router.register(r"issues", IssueViewSet)

issues_router = routers.NestedSimpleRouter(router, r"issues", lookup="issue")
issues_router.register(r"events", EventViewSet, basename="issue-events")
issues_router.register(
    r"user-reports", UserReportViewSet, basename="issue-user-reports"
)

# The API URLs are now determined automatically by the router.
urlpatterns = [
    path("", include(router.urls)),
    path("", include(issues_router.urls)),
]
