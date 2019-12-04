from django.urls import path
from rest_framework.routers import SimpleRouter
from .views import IssueViewSet, EventStoreAPIView, MakeSampleErrorView

router = SimpleRouter()
router.register(r"issues", IssueViewSet)

# The API URLs are now determined automatically by the router.
urlpatterns = [
    path("<int:id>/store/", EventStoreAPIView.as_view()),
    path("make-sample-error/", MakeSampleErrorView.as_view()),
]
