from django.urls import path, include, re_path
from rest_framework.routers import DefaultRouter
from .views import IssueViewSet, EventStoreAPIView

# Create a router and register our viewsets with it.
router = DefaultRouter()
router.register(r"issues", IssueViewSet)

# The API URLs are now determined automatically by the router.
urlpatterns = [
    path("", include(router.urls)),
    path("<int:id>/store/", EventStoreAPIView.as_view()),
]
